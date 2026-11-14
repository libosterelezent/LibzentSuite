"""AccountChannelsRepository — the account's channel model (W2;
WORKSPACE_DESIGN §5): registry (identity + validity), assignments
(the account's own selection), plugin bindings (the plugin's WHERE
for that account).

The registry records IDENTITY, never proof of usability: only an
``available`` validity resolves (§5.2 — the pre-execution contract).
Cache refreshes may update metadata/validity through :meth:`observe`;
they must never touch assignments or bindings (architecture rule 1).
Removing a registry entry is refused while dependents reference it
(the documented-conflict rule, never a silent cascade).
"""

from __future__ import annotations

import time
from typing import Any

from database.models import (
    AccountChannelRecord,
    PluginChannelBindingRecord,
)

from .base import BaseRepository

__all__ = ["AccountChannelsRepository"]


def _channel(row: Any) -> AccountChannelRecord:
    return AccountChannelRecord(
        account=row["account"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        name=row["name"],
        validity=row["validity"],
        last_seen_at=row["last_seen_at"],
        validation_reason=row["validation_reason"],
    )


def _binding(row: Any) -> PluginChannelBindingRecord:
    return PluginChannelBindingRecord(
        account=row["account"],
        plugin=row["plugin"],
        channel_id=row["channel_id"],
        guild_id=row["guild_id"],
        bound_at=float(row["bound_at"]),
    )


class AccountChannelsRepository(BaseRepository):
    """Registry + assignments + plugin bindings, account-keyed."""

    # ------------------------------------------------------------ registry
    async def upsert_channel(
        self,
        account: str,
        guild_id: str,
        channel_id: str,
        *,
        name: str = "",
        validity: str = "unknown",
        validation_reason: str = "",
    ) -> None:
        """Insert or refresh a registry row (identity + validity)."""
        now = time.time()
        async with self._tx(f"upsert channel {account}/{channel_id}") as conn:
            await conn.execute(
                "INSERT INTO account_channels (account, guild_id, channel_id,"
                " name, validity, last_seen_at, validation_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(account, guild_id, channel_id) DO UPDATE SET"
                " name = excluded.name, validity = excluded.validity,"
                " last_seen_at = excluded.last_seen_at,"
                " validation_reason = excluded.validation_reason",
                (account, guild_id, channel_id, name, validity, now, validation_reason),
            )

    async def observe(
        self,
        account: str,
        guild_id: str,
        channel_id: str,
        *,
        name: str | None = None,
        validity: str = "available",
        validation_reason: str = "",
    ) -> None:
        """A cache/runtime observation: metadata + validity only —
        the §5.2 rule that cache refreshes never touch selections."""
        row = await self.channel(account, guild_id, channel_id)
        now = time.time()
        async with self._tx(f"observe channel {account}/{channel_id}") as conn:
            if row is None:
                await conn.execute(
                    "INSERT INTO account_channels (account, guild_id,"
                    " channel_id, name, validity, last_seen_at,"
                    " validation_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (account, guild_id, channel_id, name or "", validity, now, validation_reason),
                )
            else:
                await conn.execute(
                    "UPDATE account_channels SET"
                    " name = COALESCE(?, name), validity = ?,"
                    " last_seen_at = ?, validation_reason = ?"
                    " WHERE account = ? AND guild_id = ? AND channel_id = ?",
                    (name, validity, now, validation_reason, account, guild_id, channel_id),
                )

    async def channel(
        self, account: str, guild_id: str, channel_id: str
    ) -> AccountChannelRecord | None:
        row = await self._fetch_one(
            "SELECT * FROM account_channels WHERE account = ? AND guild_id = ? AND channel_id = ?",
            (account, guild_id, channel_id),
        )
        return _channel(row) if row is not None else None

    async def channels(self, account: str) -> list[AccountChannelRecord]:
        rows = await self._fetch_all(
            "SELECT * FROM account_channels WHERE account = ? ORDER BY guild_id, channel_id",
            (account,),
        )
        return [_channel(r) for r in rows]

    async def delete_channel(self, account: str, guild_id: str, channel_id: str) -> str | None:
        """Refuse while dependents reference the entry (§5.1); returns
        the human conflict, None when deleted."""
        conflict = await self._dependent_conflict(account, channel_id)
        if conflict is not None:
            return conflict
        async with self._tx(f"delete channel {account}/{channel_id}") as conn:
            await conn.execute(
                "DELETE FROM account_channels WHERE account = ?"
                " AND guild_id = ? AND channel_id = ?",
                (account, guild_id, channel_id),
            )
        return None

    async def _dependent_conflict(self, account: str, channel_id: str) -> str | None:
        """The human reason a registry entry cannot be removed, or
        None when nothing references it (§5.1 — documented conflict,
        never a silent cascade)."""
        assignment = await self._fetch_one(
            "SELECT 1 FROM account_channel_assignments WHERE account = ? AND channel_id = ?",
            (account, channel_id),
        )
        if assignment is not None:
            return f"channel {channel_id} is referenced by an account assignment"
        bindings = await self._fetch_all(
            "SELECT plugin FROM account_plugin_channels WHERE account = ? AND channel_id = ?",
            (account, channel_id),
        )
        if bindings:
            names = ", ".join(sorted(str(r["plugin"]) for r in bindings))
            return f"channel {channel_id} is referenced by plugin bindings ({names})"
        return None

    # -------------------------------------------------------- assignments
    async def assign_channel(self, account: str, guild_id: str, channel_id: str) -> None:
        """Add ONE channel to the account's selection (the add/remove
        one vs replace-set distinction is the §14 mutation contract)."""
        async with self._tx(f"assign channel {account}/{channel_id}") as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO account_channel_assignments"
                " (account, channel_id, guild_id, added_at)"
                " VALUES (?, ?, ?, ?)",
                (account, channel_id, guild_id, time.time()),
            )

    async def unassign_channel(self, account: str, channel_id: str) -> bool:
        async with self._tx(f"unassign channel {account}/{channel_id}") as conn:
            cur = await conn.execute(
                "DELETE FROM account_channel_assignments WHERE account = ? AND channel_id = ?",
                (account, channel_id),
            )
            return bool(cur.rowcount)

    async def assignments(self, account: str) -> list[AccountChannelRecord]:
        rows = await self._fetch_all(
            "SELECT a.account, a.guild_id, a.channel_id,"
            " COALESCE(c.name, '') AS name,"
            " COALESCE(c.validity, 'unknown') AS validity,"
            " c.last_seen_at, COALESCE(c.validation_reason, '') AS validation_reason"
            " FROM account_channel_assignments a"
            " LEFT JOIN account_channels c"
            " ON c.account = a.account AND c.channel_id = a.channel_id"
            " AND c.guild_id = a.guild_id"
            " WHERE a.account = ? ORDER BY a.channel_id",
            (account,),
        )
        return [_channel(r) for r in rows]

    # ----------------------------------------------------------- bindings
    async def bind_plugin_channel(
        self, account: str, plugin: str, channel_id: str, *, guild_id: str = ""
    ) -> None:
        """Add ONE binding (initialize-by-copy from assignments lands
        through repeated calls; rule 2: bindings diverge freely)."""
        async with self._tx(f"bind {plugin}/{account}/{channel_id}") as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO account_plugin_channels"
                " (account, plugin, channel_id, guild_id, bound_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (account, plugin, channel_id, guild_id, time.time()),
            )

    async def unbind_plugin_channel(self, account: str, plugin: str, channel_id: str) -> bool:
        async with self._tx(f"unbind {plugin}/{account}/{channel_id}") as conn:
            cur = await conn.execute(
                "DELETE FROM account_plugin_channels"
                " WHERE account = ? AND plugin = ? AND channel_id = ?",
                (account, plugin, channel_id),
            )
            return bool(cur.rowcount)

    async def plugin_bindings(self, account: str, plugin: str) -> list[PluginChannelBindingRecord]:
        rows = await self._fetch_all(
            "SELECT * FROM account_plugin_channels WHERE account = ?"
            " AND plugin = ? ORDER BY channel_id",
            (account, plugin),
        )
        return [_binding(r) for r in rows]

    async def drop_account(self, account: str) -> None:
        """Account-removal boundary: every channel row keyed under the
        account dies (registry + assignments + bindings)."""
        async with self._tx(f"drop channel rows for {account!r}") as conn:
            await conn.execute("DELETE FROM account_channels WHERE account = ?", (account,))
            await conn.execute(
                "DELETE FROM account_channel_assignments WHERE account = ?", (account,)
            )
            await conn.execute("DELETE FROM account_plugin_channels WHERE account = ?", (account,))
