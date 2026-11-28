"""AccountPluginProfilesRepository — the two-level model's execution
layer (migration v12, docs/design/data-management-adoption.md P2).

One row is ONE account-owned profile instance: a plugin-defined,
opaque-to-the-core settings body keyed by the full ownership triple
``(account, plugin, name)``. Identical names under different accounts
or different plugins are different profiles — the UNIQUE constraint
encodes exactly that, and every method's SQL predicate carries the
whole triple (an ownership claim is scoped, never inferred).

This layer is persistence ONLY: it does not validate that the plugin
is discovered and never interprets ``settings`` — the plugin owns its
schema (``schema_version`` lets a plugin evolve its own shape); the
facade validates account/plugin existence. Anything JSON-serializable
stores and reads back verbatim.

Account deletion cascades through ``accounts``' _DEPENDENT_TABLES.
"""

from __future__ import annotations

import json
import time
from typing import Any

from libzentsuite.core.errors import DatabaseError
from libzentsuite.database.models import AccountPluginProfileRecord

from .base import BaseRepository, inserted_rowid, row_to_dict

__all__ = ["AccountPluginProfilesRepository"]


def _record(row: Any) -> AccountPluginProfileRecord:
    """Row -> AccountPluginProfileRecord (settings_json decoded)."""
    data = row_to_dict(row)
    try:
        settings = json.loads(data.get("settings_json") or "{}")
    except ValueError:
        settings = {}  # a damaged row degrades to empty, never crashes reads
    return AccountPluginProfileRecord(
        id=int(data["id"]),
        account=data["account"],
        plugin=data["plugin"],
        name=data["name"],
        description=data.get("description") or "",
        settings=settings if isinstance(settings, dict) else {},
        schema_version=int(data.get("schema_version") or 1),
        created_at=float(data["created_at"]),
        updated_at=float(data["updated_at"]),
    )


class AccountPluginProfilesRepository(BaseRepository):
    """CRUD for account-owned plugin profile instances (full-scope keyed)."""

    async def create(
        self,
        account: str,
        plugin: str,
        name: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
        *,
        schema_version: int = 1,
    ) -> AccountPluginProfileRecord:
        """Insert one profile; the UNIQUE(account, plugin, name) violation
        surfaces as a clear conflict (never a raw IntegrityError)."""
        now = time.time()
        try:
            async with self._tx(f"create profile {account}/{plugin}/{name}") as conn:
                row_id = inserted_rowid(
                    await conn.execute(
                        "INSERT INTO account_plugin_profiles"
                        " (account, plugin, name, description, settings_json,"
                        "  schema_version, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            account,
                            plugin,
                            name,
                            description,
                            json.dumps(settings or {}),
                            schema_version,
                            now,
                            now,
                        ),
                    )
                )
        except DatabaseError as exc:
            if "UNIQUE" in str(exc) or "unique" in str(exc):
                raise DatabaseError(
                    f"profile {name!r} already exists for {plugin!r} on {account!r}"
                ) from exc
            raise
        return AccountPluginProfileRecord(
            id=row_id,
            account=account,
            plugin=plugin,
            name=name,
            description=description,
            settings=dict(settings or {}),
            schema_version=schema_version,
            created_at=now,
            updated_at=now,
        )

    async def get(self, account: str, plugin: str, name: str) -> AccountPluginProfileRecord | None:
        row = await self._fetch_one(
            "SELECT * FROM account_plugin_profiles WHERE account=? AND plugin=? AND name=?",
            (account, plugin, name),
        )
        return _record(row) if row is not None else None

    async def exists(self, account: str, plugin: str, name: str) -> bool:
        row = await self._fetch_one(
            "SELECT 1 FROM account_plugin_profiles WHERE account=? AND plugin=? AND name=? LIMIT 1",
            (account, plugin, name),
        )
        return row is not None

    async def update(
        self,
        account: str,
        plugin: str,
        name: str,
        *,
        description: str | None = None,
        settings: dict[str, Any] | None = None,
        schema_version: int | None = None,
    ) -> AccountPluginProfileRecord | None:
        """Update only the named fields of the FULL-scope-matched profile.

        None (the default) keeps a field untouched; a scope that does not
        exist updates nothing and returns None (never creates).
        """
        current = await self.get(account, plugin, name)
        if current is None:
            return None
        new_description = current.description if description is None else description
        new_settings = current.settings if settings is None else dict(settings)
        new_version = current.schema_version if schema_version is None else schema_version
        now = time.time()
        async with self._tx(f"update profile {account}/{plugin}/{name}") as conn:
            await conn.execute(
                "UPDATE account_plugin_profiles SET description=?, settings_json=?,"
                " schema_version=?, updated_at=?"
                " WHERE account=? AND plugin=? AND name=?",
                (
                    new_description,
                    json.dumps(new_settings),
                    new_version,
                    now,
                    account,
                    plugin,
                    name,
                ),
            )
        return AccountPluginProfileRecord(
            id=current.id,
            account=account,
            plugin=plugin,
            name=name,
            description=new_description,
            settings=new_settings,
            schema_version=new_version,
            created_at=current.created_at,
            updated_at=now,
        )

    async def delete(self, account: str, plugin: str, name: str) -> bool:
        """Remove the exact full-scope profile; True when it existed.

        A pointer at the deleted profile clears in the SAME transaction
        (a dangling active-profile name would fail every resolution).
        """
        async with self._tx(f"delete profile {account}/{plugin}/{name}") as conn:
            cur = await conn.execute(
                "DELETE FROM account_plugin_profiles WHERE account=? AND plugin=? AND name=?",
                (account, plugin, name),
            )
            existed = cur.rowcount > 0
            if existed:
                await conn.execute(
                    "DELETE FROM account_plugin_active_profile"
                    " WHERE account=? AND plugin=? AND profile=?",
                    (account, plugin, name),
                )
            return existed

    async def list_for(self, account: str, plugin: str) -> list[AccountPluginProfileRecord]:
        """Every profile of one (account, plugin) — name order, newest first
        within a name is impossible (UNIQUE), so plain name order is stable."""
        rows = await self._fetch_all(
            "SELECT * FROM account_plugin_profiles WHERE account=? AND plugin=? ORDER BY name",
            (account, plugin),
        )
        return [_record(row) for row in rows]

    # ------------------------------------------------- active-profile pointer
    async def set_active(self, account: str, plugin: str, profile: str) -> None:
        """Point (account, plugin) at one of ITS profiles (upsert).

        Existence is the SERVICE layer's check; this is persistence.
        """
        now = time.time()
        async with self._tx(f"active profile {account}/{plugin} -> {profile}") as conn:
            await conn.execute(
                "INSERT INTO account_plugin_active_profile"
                " (account, plugin, profile, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(account, plugin) DO UPDATE SET"
                " profile=excluded.profile, updated_at=excluded.updated_at",
                (account, plugin, profile, now),
            )

    async def get_active(self, account: str, plugin: str) -> str | None:
        """The active profile name (None when no pointer exists)."""
        row = await self._fetch_one(
            "SELECT profile FROM account_plugin_active_profile WHERE account=? AND plugin=?",
            (account, plugin),
        )
        return str(row["profile"]) if row is not None else None

    async def clear_active(self, account: str, plugin: str) -> bool:
        """Drop the pointer; True when one existed."""
        async with self._tx(f"clear active profile {account}/{plugin}") as conn:
            cur = await conn.execute(
                "DELETE FROM account_plugin_active_profile WHERE account=? AND plugin=?",
                (account, plugin),
            )
            return cur.rowcount > 0
