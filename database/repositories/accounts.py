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


class AccountsRepository(BaseRepository):
    """CRUD for accounts and their profile assignments, keyed by NAME."""

    # ------------------------------------------------------------- mapping
    def _record(
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
    def add(
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
        with self._tx(f"add account {name!r}") as conn:
            try:
                conn.execute(
                    _INSERT, (name, token_ref, int(bool(enabled)), sync_group, time.time())
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(f"account {name!r} already exists") from exc
        record = self.get(name)
        assert record is not None  # just inserted in the same transaction
        return record

    # ----------------------------------------------------------------- read
    def get(self, name: str) -> Optional[AccountRecord]:
        """One account by name (None when unknown), with its profile list."""
        row = self._read_one("SELECT * FROM accounts WHERE name = ?", (name,))
        if row is None:
            return None
        profiles = [
            r["profile"]
            for r in self._read(
                "SELECT profile FROM account_profiles WHERE account = ? ORDER BY profile",
                (name,),
            )
        ]
        return self._record(row, profiles)

    def list(self) -> List[AccountRecord]:
        """All accounts ordered by name, each with its sorted profiles."""
        rows = self._read("SELECT * FROM accounts ORDER BY name")
        by_account: Dict[str, List[str]] = {}
        for row in self._read("SELECT account, profile FROM account_profiles ORDER BY profile"):
            by_account.setdefault(row["account"], []).append(row["profile"])
        return [self._record(row, by_account.get(row["name"], [])) for row in rows]

    # ---------------------------------------------------------------- write
    def set_enabled(self, name: str, enabled: bool) -> None:
        """Flip the persisted enabled flag (False = skipped by start_all).

        Raises DatabaseError when the account does not exist — silently
        updating zero rows would hide caller bugs.
        """
        with self._tx(f"set enabled={bool(enabled)} for account {name!r}") as conn:
            cur = conn.execute(
                "UPDATE accounts SET enabled = ? WHERE name = ?", (int(bool(enabled)), name)
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"unknown account: {name!r}")

    def set_settings(self, name: str, settings: Dict[str, Any]) -> None:
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
        with self._tx(f"store settings for account {name!r}") as conn:
            cur = conn.execute(
                "UPDATE accounts SET settings_json = ? WHERE name = ?", (payload, name)
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"unknown account: {name!r}")

    def delete(self, name: str) -> bool:
        """Delete an account AND every dependent row; True when it existed.

        Cascades (all in one transaction): account_settings, plugin_settings,
        plugin_states, account_profiles. The account's profiles themselves
        and other accounts' rows are untouched.
        """
        with self._tx(f"delete account {name!r}") as conn:
            cur = conn.execute("DELETE FROM accounts WHERE name = ?", (name,))
            existed = cur.rowcount > 0
            if existed:
                for table in ("account_settings", "plugin_settings", "plugin_states",
                              "account_profiles", "api_cache"):
                    conn.execute(f"DELETE FROM {table} WHERE account = ?", (name,))
        return existed

    # -------------------------------------------------------------- profiles
    def assign_profile(self, name: str, profile: str) -> None:
        """Attach a profile to an account (idempotent — re-assign is a no-op).

        Raises DatabaseError when the account itself does not exist; whether
        the PROFILE exists is the ProfileManager's validation (it owns that
        table), not this repository's.
        """
        with self._tx(f"assign profile {profile!r} to account {name!r}") as conn:
            if not conn.execute("SELECT 1 FROM accounts WHERE name = ?", (name,)).fetchone():
                raise DatabaseError(f"unknown account: {name!r}")
            conn.execute(
                "INSERT OR IGNORE INTO account_profiles (account, profile) VALUES (?, ?)",
                (name, profile),
            )

    def unassign_profile(self, name: str, profile: str) -> None:
        """Detach a profile from an account (idempotent; never raises)."""
        with self._tx(f"unassign profile {profile!r} from account {name!r}") as conn:
            conn.execute(
                "DELETE FROM account_profiles WHERE account = ? AND profile = ?",
                (name, profile),
            )

    def profiles_of(self, name: str) -> List[str]:
        """Sorted names of the profiles assigned to this account ([] when none)."""
        return [
            row["profile"]
            for row in self._read(
                "SELECT profile FROM account_profiles WHERE account = ? ORDER BY profile",
                (name,),
            )
        ]
