"""AccountSettingsRepository — per-account framework settings (key -> value).

This is the key/value layer consumed by
``core.settings_manager.SettingsManager`` for framework account settings
(reconnect policy etc.). Rows live in ``account_settings`` keyed by account
NAME + setting key; values are JSON so types round-trip.

Extra beyond the pinned method set (deliberate, documented): ``list_accounts``
is probed by SettingsManager via ``hasattr`` to preload the framework layer
after a restart; without it, stored reconnect settings would be invisible
until first write. Removing it would silently drop functionality the spine
already codes against.
"""

from __future__ import annotations

import json
from typing import Any

from libzentsuite.core.errors import DatabaseError

from .base import CORRUPT, BaseRepository, loads_tolerant

__all__ = ["AccountSettingsRepository"]


class AccountSettingsRepository(BaseRepository):
    """Framework account settings: get/set/delete keyed by (account, key)."""

    async def get(self, name: str) -> dict[str, Any]:
        """All stored settings for one account ({} when none/corrupt).

        Corrupt value rows are skipped with a warning — one bad row must not
        hide the healthy ones.
        """
        out: dict[str, Any] = {}
        for row in await self._fetch_all(
            "SELECT key, value_json FROM account_settings WHERE account = ? ORDER BY key",
            (name,),
        ):
            value = loads_tolerant(
                row["value_json"], CORRUPT, f"account_settings[{name!r}].{row['key']}"
            )
            if value is not CORRUPT:
                out[row["key"]] = value
        return out

    async def set(self, name: str, key: str, value: Any) -> None:
        """Store one setting (upsert). Values must be JSON-serializable.

        Raises DatabaseError for an empty/non-string key or a value that
        cannot be serialized. Existence of the account is NOT validated here
        (SettingsManager owns that invariant; orphan rows are harmless).
        """
        if not isinstance(key, str) or not key:
            raise DatabaseError("setting key must be a non-empty string")
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise DatabaseError(f"setting {name!r}.{key} is not JSON-serializable: {exc}") from exc
        async with self._tx(f"store account setting {name!r}.{key}") as conn:
            await conn.execute(
                """INSERT INTO account_settings (account, key, value_json) VALUES (?, ?, ?)
                   ON CONFLICT(account, key) DO UPDATE SET value_json = excluded.value_json""",
                (name, key, payload),
            )

    async def delete(self, name: str, key: str) -> bool:
        """Remove one setting; True when a row was actually deleted."""
        async with (
            self._tx(f"delete account setting {name!r}.{key}") as conn,
            conn.execute(
                "DELETE FROM account_settings WHERE account = ? AND key = ?", (name, key)
            ) as cur,
        ):
            return cur.rowcount > 0

    # -------------------------------------------------- spine compat helper
    async def list_accounts(self) -> list[str]:
        """Sorted names of accounts that have at least one stored setting.

        Consumed by SettingsManager._ensure_loaded (probed with hasattr) to
        rebuild the framework-settings layer after a restart.
        """
        return [
            row["account"]
            for row in await self._fetch_all(
                "SELECT DISTINCT account FROM account_settings ORDER BY account"
            )
        ]
