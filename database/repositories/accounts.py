"""AccountsRepository — the persistent account registry (spec §1).

One row per account in ``accounts`` plus membership rows in
``account_profiles``. Dependent per-account tables (``account_settings``,
``plugin_settings``, ``plugin_states``, ``account_profiles``) are keyed by
account NAME — the public key everywhere — and removed explicitly by
:meth:`AccountsRepository.delete` (the schema declares no foreign keys, so
the cascade is manual, exactly like the audited old implementation).

Token VALUES never enter the database; only the ``.env`` key referencing
them (``token_ref``) is persisted.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from core.errors import DatabaseError

from database.models import AccountRecord

from .base import BaseRepository, loads_tolerant

__all__ = ["AccountsRepository"]

_INSERT = """
    INSERT INTO accounts (name, token_ref, enabled, sync_group, settings_json, created_at)
    VALUES (?, ?, ?, ?, '{}', ?)
"""


#: per-account tables (keyed by account NAME) removed by ``delete``
_DEPENDENT_TABLES = ("account_settings", "plugin_settings", "plugin_states",
                     "account_profiles", "api_cache", "credentials",
                     "cooldowns")
#: kv rows keyed by PREFIX (PHASE-4 S3-G5 defense-in-depth): plugin state
#: lives in kv under these prefixes; the service-level cascade is primary,
#: the repo-level sweep is the backstop for unwired/limited services.
_KV_PREFIX_SWEEPS = ("pstate:account:{name}:", "pstate:account_plugin:{name}/")


class AccountsRepository(BaseRepository):
    """CRUD for accounts and their profile assignments, keyed by NAME."""

    # ------------------------------------------------------------- mapping
    async def _record(
        self, row: sqlite3.Row, profiles: Optional[List[str]] = None
    ) -> AccountRecord:
        """Map one accounts row (+ assigned profiles) to an AccountRecord.

        Corrupt ``settings_json`` degrades to ``{}`` instead of crashing.
        """
        return AccountRecord(
            id=int(row["id"]),
            name=row["name"],
            token_ref=row["token_ref"],
            enabled=bool(row["enabled"]),
            sync_group=row["sync_group"],
            settings=loads_tolerant(
                row["settings_json"], {}, f"accounts[{row['name']!r}].settings_json"
            ),
            profiles=list(profiles or []),
            created_at=float(row["created_at"]),
        )

    # ------------------------------------------------------------------ add
    async def add(
        self,
        name: str,
        *,
        token_ref: Optional[str] = None,
        enabled: bool = True,
        sync_group: Optional[str] = None,
    ) -> AccountRecord:
        """Insert a new account and return its persisted record.

        Raises:
            DatabaseError: empty/whitespace name, or the name already exists
                (the underlying UNIQUE violation is caught and re-raised with
                a human message).
        """
        if not isinstance(name, str) or not name.strip():
            raise DatabaseError("account name must be a non-empty string")
        name = name.strip()
        async with self._tx(f"add account {name!r}") as conn:
            try:
                await conn.execute(
                    _INSERT, (name, token_ref, int(bool(enabled)), sync_group, time.time())
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(f"account {name!r} already exists") from exc
        record = await self.get(name)
        if record is None:  # vanished between insert and read (never silent)
            raise DatabaseError(f"account {name!r} could not be read back")
        return record

    # ----------------------------------------------------------------- read
    async def get(self, name: str) -> Optional[AccountRecord]:
        """One account by name (None when unknown), with its profile list."""
        row = await self._fetch_one("SELECT * FROM accounts WHERE name = ?", (name,))
        if row is None:
            return None
        profiles = [
            r["profile"]
            for r in await self._fetch_all(
                "SELECT profile FROM account_profiles WHERE account = ? ORDER BY profile",
                (name,),
            )
        ]
        return await self._record(row, profiles)

    async def list(self) -> List[AccountRecord]:
        """All accounts ordered by name, each with its sorted profiles."""
        rows = await self._fetch_all("SELECT * FROM accounts ORDER BY name")
        by_account: Dict[str, List[str]] = {}
        for row in await self._fetch_all("SELECT account, profile FROM account_profiles ORDER BY profile"):
            by_account.setdefault(row["account"], []).append(row["profile"])
        out = []
        for row in rows:
            out.append(await self._record(row, by_account.get(row["name"], [])))
        return out

    # ---------------------------------------------------------------- write
    async def set_enabled(self, name: str, enabled: bool) -> None:
        """Flip the persisted enabled flag (False = skipped by start_all).

        Raises DatabaseError when the account does not exist — silently
        updating zero rows would hide caller bugs.
        """
        async with self._tx(f"set enabled={bool(enabled)} for account {name!r}") as conn:
            cur = await conn.execute(
                "UPDATE accounts SET enabled = ? WHERE name = ?", (int(bool(enabled)), name)
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"unknown account: {name!r}")

    async def set_settings(self, name: str, settings: Dict[str, Any]) -> None:
        """Replace the account's whole framework-settings JSON blob.

        Raises DatabaseError for an unknown account, a non-dict payload, or
        values that cannot be JSON-serialized.
        """
        if not isinstance(settings, dict):
            raise DatabaseError(f"account settings for {name!r} must be a dict")
        try:
            payload = json.dumps(settings)
        except (TypeError, ValueError) as exc:
            raise DatabaseError(
                f"account settings for {name!r} are not JSON-serializable: {exc}"
            ) from exc
        async with self._tx(f"store settings for account {name!r}") as conn:
            cur = await conn.execute(
                "UPDATE accounts SET settings_json = ? WHERE name = ?", (payload, name)
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"unknown account: {name!r}")

    async def delete(self, name: str) -> bool:
        """Delete an account AND every dependent row; True when it existed.

        Cascades (all in one transaction): account_settings, plugin_settings,
        plugin_states, account_profiles, api_cache, credentials (the
        encrypted token must never outlive its account) and cooldowns. The
        account's profiles themselves and other accounts' rows are untouched.
        """
        async with self._tx(f"delete account {name!r}") as conn:
            cur = await conn.execute("DELETE FROM accounts WHERE name = ?", (name,))
            existed = cur.rowcount > 0
            if existed:
                for table in _DEPENDENT_TABLES:
                    await conn.execute(f"DELETE FROM {table} WHERE account = ?", (name,))
                # PHASE-4 (S3-G5): kv plugin-state prefix sweep (backstop —
                # the PluginStateService cascade is the primary path)
                for prefix in _KV_PREFIX_SWEEPS:
                    await conn.execute(
                        "DELETE FROM kv WHERE key LIKE ?",
                        (prefix.format(name=name) + "%",))
        return existed

    # -------------------------------------------------------------- profiles
    async def assign_profile(self, name: str, profile: str) -> None:
        """Attach a profile to an account (idempotent — re-assign is a no-op).

        Raises DatabaseError when the account itself does not exist; whether
        the PROFILE exists is the ProfileManager's validation (it owns that
        table), not this repository's.
        """
        async with self._tx(f"assign profile {profile!r} to account {name!r}") as conn:
            # the cursor MUST be closed (async with): an unreset SELECT keeps
            # a read snapshot open on this connection, and the next BEGIN
            # IMMEDIATE from here fails SQLITE_BUSY immediately once another
            # connection commits in between (busy_timeout cannot retry a
            # stale snapshot).
            async with conn.execute(
                "SELECT 1 FROM accounts WHERE name = ?", (name,)
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise DatabaseError(f"unknown account: {name!r}")
            await conn.execute(
                "INSERT OR IGNORE INTO account_profiles (account, profile) VALUES (?, ?)",
                (name, profile),
            )

    async def unassign_profile(self, name: str, profile: str) -> None:
        """Detach a profile from an account (idempotent; never raises)."""
        async with self._tx(f"unassign profile {profile!r} from account {name!r}") as conn:
            await conn.execute(
                "DELETE FROM account_profiles WHERE account = ? AND profile = ?",
                (name, profile),
            )

    async def profiles_of(self, name: str) -> List[str]:
        """Sorted names of the profiles assigned to this account ([] when none)."""
        return [
            row["profile"]
            for row in await self._fetch_all(
                "SELECT profile FROM account_profiles WHERE account = ? ORDER BY profile",
                (name,),
            )
        ]
