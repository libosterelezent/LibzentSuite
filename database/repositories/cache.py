"""Per-account persistent cache of Discord entities (spec add-on: "plugins
do discord api calls and then cache it in database for that profile only").

Rows are scoped to ONE account — account A's resolved DM channel is never
reused for account B (different Discord relationship). Kinds:

    "dm"      key=str(user_id)        -> channel_id (int)
    "user"    key=str(user_id)        -> {"id": int, "name": str}
    "channel" key=str(channel_id)     -> {"id": int, "name": str, "guild_id": int|None}

Why persist at all: re-opening a DM after every restart costs a fetch_user +
create_dm round trip per account per boot — exactly the redundant API
traffic that looks robotic. With the table, each entity costs ONE call for
the lifetime of the account. Rows are invalidated when Discord says a target
is gone (TargetGone) — a stale row then heals on the next resolution.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from .base import BaseRepository


class CacheRepository(BaseRepository):
    """Read/write the account-scoped API entity cache."""

    async def get(self, account: str, kind: str, key: str) -> Optional[Any]:
        """Cached value for (account, kind, key); None on a miss/corrupt row."""
        row = await self._fetch_one(
            "SELECT value_json FROM api_cache WHERE account=? AND kind=? AND key=?",
            (account, kind, str(key)),
            what="api cache read",
        )
        if row is None:
            return None
        from .base import loads_tolerant

        return loads_tolerant(row["value_json"], None, f"api_cache:{account}/{kind}/{key}")

    async def set(self, account: str, kind: str, key: str, value: Any) -> None:
        """Store one cached value (JSON-serialized; never raises on types)."""
        async with self._tx(f"api cache write {account}/{kind}/{key}") as conn:
            await conn.execute(
                """INSERT INTO api_cache (account, kind, key, value_json, cached_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(account, kind, key) DO UPDATE SET
                     value_json=excluded.value_json, cached_at=excluded.cached_at""",
                (account, kind, str(key), json.dumps(value), time.time()),
            )

    async def delete(self, account: str, kind: str, key: str) -> bool:
        """Drop one cached value; True when a row existed."""
        async with self._tx(f"api cache delete {account}/{kind}/{key}") as conn:
            cur = await conn.execute(
                "DELETE FROM api_cache WHERE account=? AND kind=? AND key=?",
                (account, kind, str(key)),
            )
            return cur.rowcount > 0

    async def drop_account(self, account: str, kind: Optional[str] = None) -> int:
        """Invalidate every cached entity for one account (optionally one
        kind); returns how many rows were dropped. Used when an account is
        removed (full) or a target went stale (kind-scoped maintenance)."""
        async with self._tx(f"api cache drop {account}") as conn:
            if kind is None:
                cur = await conn.execute(
                    "DELETE FROM api_cache WHERE account=?", (account,)
                )
            else:
                cur = await conn.execute(
                    "DELETE FROM api_cache WHERE account=? AND kind=?",
                    (account, kind),
                )
            return cur.rowcount

    async def counts(self) -> Dict[str, int]:
        """Row counts per account (diagnostics for the Advanced page)."""
        rows = await self._fetch_all(
            "SELECT account, COUNT(*) AS n FROM api_cache GROUP BY account",
            what="api cache counts",
        )
        return {row["account"]: row["n"] for row in rows}


__all__ = ["CacheRepository"]
