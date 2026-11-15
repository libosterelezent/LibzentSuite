"""Persistent cooldown repository (spec §21/§22).

Cooldowns persist ABSOLUTE epoch expiry (``expires_at``) so a restart
restores the REMAINING time instead of restarting the window. The core
CooldownService owns persistence/lookup/expiry/cleanup; plugins decide
what a cooldown MEANS — nothing here is plugin-specific.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..connection import DatabaseError
from .base import BaseRepository, loads_tolerant


class CooldownRepository(BaseRepository):
    """CRUD + expiry-aware queries over the ``cooldowns`` table."""

    async def upsert(
        self,
        *,
        account: str,
        plugin: str,
        cooldown_key: str,
        started_at: float,
        expires_at: float,
        source: str = "",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or replace one cooldown (same identity = same row).

        Raises DatabaseError (never a raw TypeError) for non-serializable
        metadata — checked BEFORE the transaction opens.
        """
        now = time.time()
        try:
            meta_json = json.dumps(metadata or {})
        except (TypeError, ValueError) as exc:
            raise DatabaseError(
                f"cooldown metadata for {account}/{plugin}/{cooldown_key} is "
                f"not JSON-serializable: {exc}"
            ) from exc
        async with self._tx(f"upsert cooldown {account}/{plugin}/{cooldown_key}") as conn:
            await conn.execute(
                """INSERT INTO cooldowns (account, plugin, cooldown_key,
                     started_at, expires_at, source, reason, metadata_json,
                     created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account, plugin, cooldown_key) DO UPDATE SET
                     started_at=excluded.started_at,
                     expires_at=excluded.expires_at,
                     source=excluded.source, reason=excluded.reason,
                     metadata_json=excluded.metadata_json,
                     updated_at=excluded.updated_at""",
                (
                    account,
                    plugin,
                    cooldown_key,
                    float(started_at),
                    float(expires_at),
                    source,
                    reason,
                    meta_json,
                    now,
                    now,
                ),
            )

    async def active_for(
        self, account: str, plugin: str | None = None, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Non-expired cooldowns for an account (optionally one plugin)."""
        now = time.time() if now is None else now
        sql = "SELECT * FROM cooldowns WHERE account=? AND expires_at>? ORDER BY expires_at"
        params: list[str | float] = [account, now]
        if plugin is not None:
            sql = sql.replace("ORDER BY", "AND plugin=? ORDER BY")
            params.append(plugin)
        rows = await self._fetch_all(sql, params)
        return [self._row_dict(row) for row in rows]

    async def active_all(self, now: float | None = None) -> list[dict[str, Any]]:
        """Every non-expired cooldown of EVERY account in ONE query
        (boot restore: replaces a DISTINCT-accounts query + N per-account
        queries)."""
        now = time.time() if now is None else now
        rows = await self._fetch_all(
            "SELECT * FROM cooldowns WHERE expires_at>? ORDER BY expires_at", (now,)
        )
        return [self._row_dict(row) for row in rows]

    async def get(
        self, account: str, plugin: str, cooldown_key: str, now: float | None = None
    ) -> dict[str, Any] | None:
        """One cooldown (None when absent or already expired)."""
        now = time.time() if now is None else now
        row = await self._fetch_one(
            """SELECT * FROM cooldowns WHERE account=? AND plugin=?
               AND cooldown_key=? AND expires_at>?""",
            (account, plugin, cooldown_key, now),
        )
        return None if row is None else self._row_dict(row)

    async def clear(self, account: str, plugin: str, cooldown_key: str) -> bool:
        """Remove one cooldown; True when it existed."""
        async with (
            self._tx(f"clear cooldown {account}/{plugin}/{cooldown_key}") as conn,
            conn.execute(
                "DELETE FROM cooldowns WHERE account=? AND plugin=? AND cooldown_key=?",
                (account, plugin, cooldown_key),
            ) as cur,
        ):
            return cur.rowcount > 0

    async def clear_expired_for(
        self, account: str, plugin: str, cooldown_key: str, now: float
    ) -> bool:
        """Delete one cooldown ONLY when still expired at ``now``.

        A competing process may have re-upserted a longer expiry since the
        caller's memory copy went stale — an unconditional delete would
        destroy that extension.
        """
        async with (
            self._tx("lazy clear cooldown") as conn,
            conn.execute(
                "DELETE FROM cooldowns WHERE account=? AND plugin=? "
                "AND cooldown_key=? AND expires_at<=?",
                (account, plugin, cooldown_key, now),
            ) as cur,
        ):
            return cur.rowcount > 0

    async def clear_expired(self, now: float | None = None) -> int:
        """Delete every expired row; returns how many (boot cleanup)."""
        now = time.time() if now is None else now
        async with (
            self._tx("clear expired cooldowns") as conn,
            conn.execute("DELETE FROM cooldowns WHERE expires_at<=?", (now,)) as cur,
        ):
            return cur.rowcount

    async def delete_account(self, account: str) -> int:
        """Drop every cooldown owned by an account (deletion cascade)."""
        async with (
            self._tx(f"delete cooldowns for {account!r}") as conn,
            conn.execute("DELETE FROM cooldowns WHERE account=?", (account,)) as cur,
        ):
            return cur.rowcount

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        """Row -> dict; corrupt ``metadata_json`` degrades to ``{}`` instead
        of crashing the whole listing."""
        out = dict(row)
        out.pop("id", None)
        meta = loads_tolerant(
            out.pop("metadata_json", None),
            {},
            f"cooldowns[{out.get('account')}/{out.get('plugin')}/"
            f"{out.get('cooldown_key')}].metadata_json",
        )
        out["metadata"] = meta if isinstance(meta, dict) else {}
        return out


__all__ = ["CooldownRepository"]
