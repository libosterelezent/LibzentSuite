"""SyncGroupsRepository — first-class sync group rows (spec: sync groups).

Group identity + metadata live in ``sync_groups``; MEMBERSHIP stays on
``accounts.sync_group`` (the group name pointer — an account belongs to at
most one group, so a join table would add nothing the column does not
already say). Membership operations therefore go through the ACCOUNTS
table, in the same transaction style as the other account writes.

This layer is persistence ONLY: it knows nothing about the pacing engine
(``RateLimitManager`` runtime synchronization is SyncGroupManager's job).
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

from core.errors import DatabaseError
from database.models import SyncGroupRecord

from .base import BaseRepository

if TYPE_CHECKING:
    import builtins

__all__ = ["SyncGroupsRepository"]


def _record(row: sqlite3.Row) -> SyncGroupRecord:
    return SyncGroupRecord(
        id=int(row["id"]),
        name=row["name"],
        description=row["description"],
        enabled=bool(row["enabled"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class SyncGroupsRepository(BaseRepository):
    """CRUD for sync groups; membership via the accounts table."""

    # --------------------------------------------------------------- groups
    async def create(
        self, name: str, description: str = "", *, enabled: bool = True
    ) -> SyncGroupRecord:
        """Insert one group; returns its persisted record.

        Raises:
            DatabaseError: empty/non-string name, or the name already exists
                (UNIQUE violation re-raised with a human message).
        """
        if not isinstance(name, str) or not name.strip():
            raise DatabaseError("sync group name must be a non-empty string")
        name = name.strip()
        now = time.time()
        async with self._tx(f"create sync group {name!r}") as conn:
            try:
                await conn.execute(
                    "INSERT INTO sync_groups (name, description, enabled,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (name, description or "", int(bool(enabled)), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(f"sync group {name!r} already exists") from exc
        record = await self.get_by_name(name)
        if record is None:  # vanished between insert and read (never silent)
            raise DatabaseError(f"sync group {name!r} could not be read back")
        return record

    async def get(self, group_id: int) -> SyncGroupRecord | None:
        """One group by id (None when unknown)."""
        row = await self._fetch_one("SELECT * FROM sync_groups WHERE id = ?", (int(group_id),))
        return _record(row) if row is not None else None

    async def get_by_name(self, name: str) -> SyncGroupRecord | None:
        """One group by name (None when unknown)."""
        row = await self._fetch_one("SELECT * FROM sync_groups WHERE name = ?", (name,))
        return _record(row) if row is not None else None

    async def list(self) -> builtins.list[SyncGroupRecord]:
        """All groups ordered by name."""
        rows = await self._fetch_all("SELECT * FROM sync_groups ORDER BY name")
        return [_record(row) for row in rows]

    async def update(
        self,
        group_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> SyncGroupRecord | None:
        """Update ONLY the given fields (None = keep); bumps updated_at.

        A RENAME rewrites every member's ``accounts.sync_group`` pointer in
        the SAME transaction, so persistence can never hold a group whose
        members still point at the old name. Returns the updated record
        (None when the id is unknown).

        Raises:
            DatabaseError: renaming to an existing name (UNIQUE violation).
        """
        group = await self.get(group_id)
        if group is None:
            return None
        new_name = group.name if name is None else name.strip()
        new_description = group.description if description is None else description
        new_enabled = group.enabled if enabled is None else bool(enabled)
        async with self._tx(f"update sync group {group.name!r}") as conn:
            try:
                await conn.execute(
                    "UPDATE sync_groups SET name = ?, description = ?,"
                    " enabled = ?, updated_at = ? WHERE id = ?",
                    (new_name, new_description, int(new_enabled), time.time(), int(group_id)),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(f"sync group {new_name!r} already exists") from exc
            if new_name != group.name:
                # keep the membership pointers consistent with the rename
                await conn.execute(
                    "UPDATE accounts SET sync_group = ? WHERE sync_group = ?",
                    (new_name, group.name),
                )
        return await self.get(group_id)

    async def delete(self, group_id: int) -> bool:
        """Delete one group row; True when it existed.

        Member ACCOUNTS are never touched here — detaching members is the
        manager's decision (it must refuse or explicitly detach first).
        """
        async with (
            self._tx(f"delete sync group #{group_id}") as conn,
            conn.execute("DELETE FROM sync_groups WHERE id = ?", (int(group_id),)) as cur,
        ):
            existed = cur.rowcount > 0
        return existed

    # ----------------------------------------------------------- membership
    async def members(self, group_name: str) -> builtins.list[str]:
        """Account names whose membership points at this group (sorted)."""
        return [
            row["name"]
            for row in await self._fetch_all(
                "SELECT name FROM accounts WHERE sync_group = ? ORDER BY name",
                (group_name,),
            )
        ]

    async def membership_counts(self) -> dict[str, int]:
        """group name -> member count (one GROUP BY; groups with no members
        are simply absent from the map)."""
        return {
            row["sync_group"]: int(row["n"])
            for row in await self._fetch_all(
                "SELECT sync_group, COUNT(*) AS n FROM accounts"
                " WHERE sync_group IS NOT NULL AND sync_group != ''"
                " GROUP BY sync_group"
            )
        }

    async def account_group_name(self, account: str) -> str | None:
        """The group name this account points at (None when none/unknown)."""
        row = await self._fetch_one("SELECT sync_group FROM accounts WHERE name = ?", (account,))
        if row is None:
            return None
        group = row["sync_group"]
        return group if group else None

    async def set_account_group(self, account: str, group: str | None) -> None:
        """Point an account's membership at a group (None = leave every
        group). One membership per account by construction: the UPDATE
        replaces any previous pointer.

        Raises:
            DatabaseError: unknown account, or an unknown group name (a
                pointer may only reference an existing group row).
        """
        async with self._tx(f"set sync group of account {account!r}") as conn:
            if group is not None:
                async with conn.execute(
                    "SELECT 1 FROM sync_groups WHERE name = ?", (group,)
                ) as cursor:
                    if await cursor.fetchone() is None:
                        raise DatabaseError(f"unknown sync group: {group!r}")
            async with conn.execute(
                "UPDATE accounts SET sync_group = ? WHERE name = ?",
                (group, account),
            ) as cur:
                if cur.rowcount == 0:
                    raise DatabaseError(f"unknown account: {account!r}")

    async def detach_members(self, group_name: str) -> builtins.list[str]:
        """Clear the membership of every member; returns their names."""
        affected = await self.members(group_name)
        async with self._tx(f"detach members of sync group {group_name!r}") as conn:
            await conn.execute(
                "UPDATE accounts SET sync_group = NULL WHERE sync_group = ?",
                (group_name,),
            )
        return affected
