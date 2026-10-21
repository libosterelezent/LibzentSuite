"""AccessManager — the DB-backed authorization/prefix/admin store.

This is the admin panel's data layer, replacing the env-based
OWNER_USER_IDS model (see the migration note in credentials.py).

Storage: single kv row per concern under the ``access:`` prefix —

  access:admins          -> [discord user id, ...]
  access:mudae_bot_id    -> int
  access:account:<name>  -> {"is_admin": bool,
                             "prefix_mode": "mention"|"symbol"|"both",
                             "prefix_symbol": str,
                             "allowed_user_ids": [int, ...]}

Fail-closed semantics:
  * empty per-account allowed_user_ids -> NOBODY may invoke that
    account's commands (except admin ids / admin-flagged accounts)
  * an unknown/absent account row -> same as empty
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .errors import CredentialsError

LOG = logging.getLogger("libzent.access")

#: kv keys (single source of truth for the row names)
KEY_ADMINS = "access:admins"
KEY_MUDAE_ID = "access:mudae_bot_id"
def account_key(name: str) -> str:
    return f"access:account:{name}"

#: prefix modes accepted by the admin panel + adminprefix command
PREFIX_MODES = ("mention", "symbol", "both")

#: the per-account row's defaults (also the create shape)
DEFAULT_ACCOUNT_ROW: Dict[str, Any] = {
    "is_admin": False,
    "prefix_mode": "mention",     # operator default: mentionable
    "prefix_symbol": "",
    "allowed_user_ids": [],
}


class AccessManager:
    """CRUD + gate lookups over the access kv rows.

    Duck-typed ``kv`` needs ``await get(key, default)`` and
    ``await set(key, value)`` (the KvRepository satisfies both). All
    mutations write the FULL row (read-modify-write) — the admin panel is
    a single-operator surface, and the framework never races itself here.
    """

    def __init__(self, kv: Any, logger: Optional[logging.Logger] = None,
                 *, credentials: Any = None) -> None:
        self._kv = kv
        self._log = logger or LOG
        self._credentials = credentials   # legacy env view (migration + fallbacks)
        self._migrated = False
        #: per-account prefix cache for the sync factory read (warmed by
        #: the bootstrap + every prefix mutation)
        self._prefix_cache: Dict[str, Dict[str, Any]] = {}
        #: serializes every read-modify-write mutation: concurrent
        #: add/remove pairs used to race across the kv awaits and lose
        #: updates (a revoked admin could reappear authorized)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- helpers
    async def _row(self, name: str) -> Dict[str, Any]:
        raw = await self._kv.get(account_key(name))
        if not isinstance(raw, dict):
            return dict(DEFAULT_ACCOUNT_ROW)
        # merge over defaults so older rows gain new keys. ``user_id``
        # is preserved explicitly: it lives outside DEFAULT_ACCOUNT_ROW,
        # and losing it silently revoked the admin-account grant on
        # every subsequent flag/perms/prefix write (fail-closed, but
        # wrong).
        row = dict(DEFAULT_ACCOUNT_ROW)
        row.update({k: v for k, v in raw.items() if k in row})
        row["user_id"] = raw.get("user_id")
        return row

    async def _write(self, name: str, row: Dict[str, Any]) -> None:
        await self._kv.set(account_key(name), row)

    # ------------------------------------------------------------ admins
    async def admin_ids(self) -> List[int]:
        raw = await self._kv.get(KEY_ADMINS)
        if not isinstance(raw, list):
            return []
        return [int(x) for x in raw if str(x).strip().lstrip("-").isdigit()]

    async def _write_admin_ids(self, ids: List[int]) -> List[int]:
        """Unlocked write core (callers hold ``self._lock``)."""
        clean = sorted({int(x) for x in ids})
        await self._kv.set(KEY_ADMINS, clean)
        self._log.info("admin id list set (%d id(s))", len(clean))
        return clean

    async def set_admin_ids(self, ids: List[int]) -> List[int]:
        async with self._lock:
            return await self._write_admin_ids(ids)

    async def add_admin(self, user_id: int) -> List[int]:
        async with self._lock:
            ids = await self.admin_ids()
            if int(user_id) not in ids:
                ids.append(int(user_id))
                ids = await self._write_admin_ids(ids)
            return ids

    async def remove_admin(self, user_id: int) -> List[int]:
        async with self._lock:
            ids = await self.admin_ids()
            ids = [x for x in ids if x != int(user_id)]
            return await self._write_admin_ids(ids)

    # ------------------------------------------------- one-time migration
    async def migrate_from_credentials(self) -> int:
        """Seed the admin list from OWNER_USER_IDS when the DB list is
        EMPTY (first boot on the new model). After this the env value is
        IGNORED for authorization — the security hole it represented
        (every account controllable by every env-listed id) is closed."""
        if self._migrated:
            return 0
        self._migrated = True
        if await self.admin_ids():
            return 0
        env_ids: List[int] = []
        if self._credentials is not None:
            try:
                env_ids = sorted(self._credentials.owner_user_ids)
            except Exception:
                env_ids = []
        if env_ids:
            await self.set_admin_ids(env_ids)
            self._log.warning(
                "ADMIN MIGRATION: seeded %d admin id(s) from OWNER_USER_IDS "
                "(one-time; the env var is no longer read for "
                "authorization)", len(env_ids))
        else:
            self._log.warning(
                "ADMIN MIGRATION: no stored admins and no OWNER_USER_IDS — "
                "NOBODY can invoke commands until an admin is added via "
                "the dashboard (fail-closed)")
        return len(env_ids)

    # --------------------------------------------------- per-account rows
    async def account_view(self, name: str) -> Dict[str, Any]:
        """The account's access row (copies; safe to mutate)."""
        return await self._row(name)

    async def set_admin_flag(self, name: str, is_admin: bool) -> Dict[str, Any]:
        async with self._lock:
            row = await self._row(name)
            row["is_admin"] = bool(is_admin)
            await self._write(name, row)
        self._log.info("account %s admin flag -> %s", name, is_admin)
        return row

    # -------------------------------------------------- command permissions
    async def allowed_user_ids(self, name: str) -> List[int]:
        return list((await self._row(name))["allowed_user_ids"])

    async def _write_allowed(self, name: str, ids: List[int]) -> List[int]:
        """Unlocked read-modify-write core (callers hold ``self._lock``)."""
        row = await self._row(name)
        clean = sorted({int(x) for x in ids})
        row["allowed_user_ids"] = clean
        await self._write(name, row)
        return clean

    async def set_allowed_user_ids(self, name: str,
                                    ids: List[int]) -> List[int]:
        async with self._lock:
            return await self._write_allowed(name, ids)

    async def add_allowed(self, name: str, user_id: int) -> List[int]:
        async with self._lock:
            ids = await self.allowed_user_ids(name)
            if int(user_id) not in ids:
                ids.append(int(user_id))
                ids = await self._write_allowed(name, ids)
            return ids

    async def remove_allowed(self, name: str, user_id: int) -> List[int]:
        async with self._lock:
            ids = [x for x in await self.allowed_user_ids(name)
                   if x != int(user_id)]
            return await self._write_allowed(name, ids)

    # ---------------------------------------------------------- prefixes
    async def prefix_config(self, name: str) -> Dict[str, Any]:
        row = await self._row(name)
        return {"prefix_mode": row["prefix_mode"],
                "prefix_symbol": row["prefix_symbol"]}

    def prefix_config_sync(self, name: str) -> Optional[Dict[str, Any]]:
        """Last-known per-account prefix config (no I/O — the factory
        reads this at client-build time; None until first async load).

        The bootstrap WARMS the cache for every known account at boot and
        after every admin-panel prefix mutation, so the sync read is only
        stale between a DB write and the next warm — bounded by the
        operator's own edit cadence, and the authoritative async path
        (:meth:`prefix_config`) serves every later rebuild."""
        return self._prefix_cache.get(name)

    async def warm_prefix_cache(self, names=None) -> None:
        """Load prefix rows for the given accounts (all when None)."""
        if names is None:
            rows = await self._kv.prefix("access:account:") or {}
            self._prefix_cache = {
                key.rsplit(":", 1)[-1]: {
                    "prefix_mode": row.get("prefix_mode", "mention"),
                    "prefix_symbol": row.get("prefix_symbol", ""),
                }
                for key, row in rows.items() if isinstance(row, dict)
            }
        else:
            for name in names:
                self._prefix_cache[name] = await self.prefix_config(name)

    async def set_prefix_config(self, name: str, mode: str,
                                symbol: str = "") -> Dict[str, Any]:
        mode = str(mode).strip().lower()
        if mode not in PREFIX_MODES:
            raise CredentialsError(
                f"prefix mode must be one of {list(PREFIX_MODES)}, got {mode!r}")
        symbol = str(symbol or "").strip()
        if mode in ("symbol", "both"):
            if not symbol:
                raise CredentialsError(
                    f"prefix mode {mode!r} requires a symbol (1-4 chars, "
                    f"no whitespace)")
            if len(symbol) > 4 or any(ch.isspace() for ch in symbol):
                raise CredentialsError(
                    f"prefix symbol {symbol!r} must be 1-4 chars with no "
                    f"whitespace")
        async with self._lock:
            row = await self._row(name)
            row["prefix_mode"] = mode
            row["prefix_symbol"] = symbol
            await self._write(name, row)
        self._prefix_cache[name] = {"prefix_mode": mode,
                                    "prefix_symbol": symbol}
        self._log.info("account %s prefix -> mode=%s symbol=%r",
                       name, mode, symbol)
        return {"prefix_mode": mode, "prefix_symbol": symbol}

    # ---------------------------------------------------------- mudae id
    async def mudae_bot_id(self) -> Optional[int]:
        """DB value first; the env MUDAE_BOT_ID is the fallback."""
        raw = await self._kv.get(KEY_MUDAE_ID)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        if self._credentials is not None:
            try:
                return self._credentials.mudae_bot_id
            except Exception:
                return None
        return None

    async def set_mudae_bot_id(self, user_id: Optional[int]) -> None:
        if user_id is None:
            await self._kv.delete(KEY_MUDAE_ID)
            self._log.info("mudae bot id cleared (env fallback applies)")
            return
        await self._kv.set(KEY_MUDAE_ID, int(user_id))
        self._log.info("mudae bot id set to %d", int(user_id))

    # ---------------------------------------------------------- overview
    async def all_account_rows(self) -> Dict[str, Dict[str, Any]]:
        """Every access row keyed by account name (adminlist verb + the
        dashboard's admin panel overview)."""
        rows = await self._kv.prefix("access:account:") or {}
        out = {}
        for key, raw in rows.items():
            if not isinstance(raw, dict):
                continue
            row = dict(DEFAULT_ACCOUNT_ROW)
            row.update({k: v for k, v in raw.items() if k in row})
            row["user_id"] = raw.get("user_id")
            out[key.rsplit(":", 1)[-1]] = row
        return out

    async def forget_account(self, name: str) -> None:
        """Removal cascade: delete the account's access row (admin flag,
        perms, prefix) so the Admin page doesn't show deleted accounts."""
        # under the lock: an unlocked delete racing an in-flight locked
        # mutation (parked on its kv await) let the mutation re-write
        # the row AFTER the delete — resurrecting perms for a removed
        # account
        async with self._lock:
            await self._kv.delete(account_key(name))
        self._prefix_cache.pop(name, None)
        self._log.info("access row cleared for removed account %s", name)

    # ---------------------------------------------------------- gating
    async def is_admin_id(self, user_id: Optional[int]) -> bool:
        """Admin = id in the admin list OR the user id of an
        admin-flagged ACCOUNT (fetched lazily — the account map is
        provided by the wiring as ``account_user_ids``)."""
        if user_id is None:
            return False
        if int(user_id) in await self.admin_ids():
            return True
        return int(user_id) in await self._admin_account_ids()

    async def _admin_account_ids(self) -> List[int]:
        """User ids of admin-flagged accounts, resolved from the raw kv
        rows (each row may carry ``user_id`` recorded at flag time by
        the manager; absent -> the flag can't be attributed)."""
        rows = await self._kv.prefix("access:account:") or {}
        out = []
        for row in rows.values():
            if isinstance(row, dict) and row.get("is_admin") \
                    and row.get("user_id"):
                try:
                    out.append(int(row["user_id"]))
                except (TypeError, ValueError):
                    continue
        return out

    async def set_account_user_id(self, name: str,
                                  user_id: Optional[int]) -> None:
        """Record the account's Discord user id in its access row (the
        account manager calls this on connect) so admin flags gate."""
        async with self._lock:
            row = await self._row(name)
            row["user_id"] = int(user_id) if user_id else None
            await self._write(name, row)

    async def can_invoke(self, account: str,
                         user_id: Optional[int]) -> bool:
        """THE command gate: per-account allowed list, admin bypass,
        fail-closed on empty."""
        if user_id is None:
            return False
        if await self.is_admin_id(user_id):
            return True
        return int(user_id) in await self.allowed_user_ids(account)


__all__ = [
    "AccessManager", "PREFIX_MODES", "DEFAULT_ACCOUNT_ROW",
    "KEY_ADMINS", "KEY_MUDAE_ID", "account_key",
]
