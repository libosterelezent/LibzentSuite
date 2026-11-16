"""AccountsRepository — the persistent account registry (spec §1).

One row per account in ``accounts``. Dependent per-account tables
(``account_settings``, ``plugin_settings``, ``plugin_states``,
``account_plugins``) are keyed by account NAME — the public key
everywhere — and removed explicitly by
:meth:`AccountsRepository.delete` (the schema declares no foreign keys, so
the cascade is manual, exactly like the audited old implementation).

Token VALUES never enter the database; only the ``.env`` key referencing
them (``token_ref``) is persisted.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from core.errors import DatabaseError
from database.models import AccountRecord

from .base import BaseRepository, loads_tolerant

if TYPE_CHECKING:
    import builtins

__all__ = ["AccountsRepository"]

_INSERT = """
    INSERT INTO accounts (name, token_ref, enabled, workspace, settings_json, created_at)
    VALUES (?, ?, ?, ?, '{}', ?)
"""


#: per-account tables (keyed by account NAME) removed by ``delete``
#: (``account_plugins`` included: an orphaned row would let a re-created
#: same-named account silently inherit the dead account's plugin grants)
_DEPENDENT_TABLES = (
    "account_settings",
    "plugin_settings",
    "plugin_states",
    "api_cache",
    "credentials",
    "cooldowns",
    "account_plugins",
    "account_plugin_profiles",
)
#: kv rows keyed by PREFIX (PHASE-4 S3-G5 defense-in-depth): plugin state
#: lives in kv under these prefixes; the service-level cascade is primary,
#: the repo-level sweep is the backstop for unwired/limited services.
_KV_PREFIX_SWEEPS = ("pstate:account:{name}:", "pstate:account_plugin:{name}/")


class AccountsRepository(BaseRepository):
    """CRUD for accounts, keyed by NAME."""

    # ------------------------------------------------------------- mapping
    async def _record(self, row: sqlite3.Row) -> AccountRecord:
        """Map one accounts row to an AccountRecord.

        Corrupt ``settings_json`` degrades to ``{}`` instead of crashing.
        """
        return AccountRecord(
            id=int(row["id"]),
            name=row["name"],
            token_ref=row["token_ref"],
            enabled=bool(row["enabled"]),
            workspace=(
                row["workspace"] if "workspace" in row.keys() else None  # noqa: SIM118
            ),
            settings=loads_tolerant(
                row["settings_json"], {}, f"accounts[{row['name']!r}].settings_json"
            ),
            created_at=float(row["created_at"]),
        )

    # ------------------------------------------------------------------ add
    async def add(
        self,
        name: str,
        *,
        token_ref: str | None = None,
        enabled: bool = True,
        workspace: str | None = None,
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
                    _INSERT, (name, token_ref, int(bool(enabled)), workspace, time.time())
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(f"account {name!r} already exists") from exc
        record = await self.get(name)
        if record is None:  # vanished between insert and read (never silent)
            raise DatabaseError(f"account {name!r} could not be read back")
        return record

    # ----------------------------------------------------------------- read
    async def get(self, name: str) -> AccountRecord | None:
        """One account by name (None when unknown)."""
        row = await self._fetch_one("SELECT * FROM accounts WHERE name = ?", (name,))
        if row is None:
            return None
        return await self._record(row)

    async def list(self) -> builtins.list[AccountRecord]:
        """All accounts ordered by name."""
        rows = await self._fetch_all("SELECT * FROM accounts ORDER BY name")
        return [await self._record(row) for row in rows]

    # ---------------------------------------------------------------- write
    async def set_enabled(self, name: str, enabled: bool) -> None:
        """Flip the persisted enabled flag (False = skipped by start_all).

        Raises DatabaseError when the account does not exist — silently
        updating zero rows would hide caller bugs.
        """
        async with (
            self._tx(f"set enabled={bool(enabled)} for account {name!r}") as conn,
            conn.execute(
                "UPDATE accounts SET enabled = ? WHERE name = ?", (int(bool(enabled)), name)
            ) as cur,
        ):
            if cur.rowcount == 0:
                raise DatabaseError(f"unknown account: {name!r}")

    async def set_settings(self, name: str, settings: dict[str, Any]) -> None:
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
        async with (
            self._tx(f"store settings for account {name!r}") as conn,
            conn.execute(
                "UPDATE accounts SET settings_json = ? WHERE name = ?", (payload, name)
            ) as cur,
        ):
            if cur.rowcount == 0:
                raise DatabaseError(f"unknown account: {name!r}")

    async def delete(self, name: str) -> bool:
        """Delete an account AND every dependent row; True when it existed.

        Cascades (all in one transaction): account_settings, plugin_settings,
        plugin_states, account_plugins, api_cache,
        credentials (the encrypted token must never outlive its account)
        and cooldowns. Other accounts' rows are untouched.
        """
        async with self._tx(f"delete account {name!r}") as conn:
            async with conn.execute("DELETE FROM accounts WHERE name = ?", (name,)) as cur:
                existed = cur.rowcount > 0
            if existed:
                # Bandit B608: table names come from the module-level
                # _DEPENDENT_TABLES constant; the account value is bound.
                for table in _DEPENDENT_TABLES:
                    await conn.execute(
                        f"DELETE FROM {table} WHERE account = ?",  # nosec B608
                        (name,),
                    )
                # PHASE-4 (S3-G5): kv plugin-state prefix sweep (backstop —
                # the PluginStateService cascade is the primary path).
                # LIKE wildcards in the account NAME ('\', '%', '_') are
                # escaped exactly like kv.py's delete_prefix(): unescaped,
                # '_' is any-single-char and deleting 'alfa_bot' also
                # swept 'alfaXbot' rows — silent cross-account data loss.
                for prefix in _KV_PREFIX_SWEEPS:
                    pattern = (
                        prefix.format(name=name)
                        .replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                        + "%"
                    )
                    await conn.execute("DELETE FROM kv WHERE key LIKE ? ESCAPE '\\'", (pattern,))
                # access rows (admin flag / perms / prefix / user_id) live
                # under 'access:account:<name>' — an EXACT-match delete,
                # never a prefix sweep: 'access:account:bob%' would also
                # wipe bob2/bobby's authorization rows (review finding #5)
                await conn.execute("DELETE FROM kv WHERE key = ?", (f"access:account:{name}",))
        return existed
