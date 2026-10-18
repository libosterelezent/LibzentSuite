"""PluginProfilesRepository — per-plugin named setting profiles (v7).

One row in ``plugin_profiles`` is a reusable bundle of setting values
for ONE plugin, keyed ``(plugin, name)``. Accounts bind to a profile
through ``account_plugins.profile`` (the binding column added by the
same migration); this repository owns the profile ROWS plus the
delete-cascade that keeps bindings from dangling.

This layer is persistence ONLY: it does NOT validate that the plugin is
discovered, and it does NOT schema-validate ``settings`` — both are the
ControlAPI's job (the API checks keys/types against the plugin's
Setting schema and the reserved "channels" structure against
core/plugin_scopes). Anything JSON-serializable stores and reads back
verbatim.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from core.errors import DatabaseError

from database.models import PluginProfileRecord

from .base import BaseRepository, loads_tolerant

__all__ = ["PluginProfilesRepository"]


def _record(row: Any) -> PluginProfileRecord:
    """Row -> PluginProfileRecord (settings_json decoded tolerantly)."""
    return PluginProfileRecord(
        id=int(row["id"]),
        plugin=row["plugin"],
        name=row["name"],
        description=row["description"],
        settings=loads_tolerant(
            row["settings_json"], {}, f"plugin_profiles[{row['plugin']!r}/{row['name']!r}]"
        ) or {},
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


class PluginProfilesRepository(BaseRepository):
    """CRUD for plugin setting profiles, keyed by (plugin, name)."""

    async def create(
        self,
        plugin: str,
        name: str,
        description: str = "",
        settings: Optional[Dict[str, Any]] = None,
    ) -> PluginProfileRecord:
        """Insert one profile; returns its persisted record.

        Raises:
            DatabaseError: empty/non-string plugin or name, a
                non-serializable settings payload, or the (plugin, name)
                pair already exists (UNIQUE violation re-raised with a
                human message).
        """
        if not isinstance(plugin, str) or not plugin.strip():
            raise DatabaseError("plugin name must be a non-empty string")
        if not isinstance(name, str) or not name.strip():
            raise DatabaseError("profile name must be a non-empty string")
        name = name.strip()   # store AND read back the same canonical name
        payload = self._encode(settings or {}, plugin, name)
        now = time.time()
        async with self._tx(f"create plugin profile {plugin!r}/{name!r}") as conn:
            try:
                await conn.execute(
                    "INSERT INTO plugin_profiles"
                    " (plugin, name, description, settings_json,"
                    "  created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (plugin, name, description or "", payload, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(
                    f"profile {name!r} already exists for plugin {plugin!r}"
                ) from exc
        record = await self.get(plugin, name)
        if record is None:  # vanished between insert and read (never silent)
            raise DatabaseError(
                f"profile {name!r} of plugin {plugin!r} could not be read back")
        return record

    async def get(
        self, plugin: str, name: str
    ) -> Optional[PluginProfileRecord]:
        """One profile by (plugin, name); None when unknown."""
        row = await self._fetch_one(
            "SELECT * FROM plugin_profiles WHERE plugin = ? AND name = ?",
            (plugin, name),
        )
        return _record(row) if row is not None else None

    async def list(
        self, plugin: Optional[str] = None
    ) -> List[PluginProfileRecord]:
        """All profiles (or one plugin's), ordered by plugin then name."""
        if plugin is None:
            rows = await self._fetch_all(
                "SELECT * FROM plugin_profiles ORDER BY plugin, name")
        else:
            rows = await self._fetch_all(
                "SELECT * FROM plugin_profiles WHERE plugin = ?"
                " ORDER BY name",
                (plugin,),
            )
        return [_record(row) for row in rows]

    async def update(
        self,
        plugin: str,
        name: str,
        *,
        name_new: Optional[str] = None,
        description: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Optional[PluginProfileRecord]:
        """Update ONLY the given fields (None = keep); bumps updated_at.

        A RENAME rewrites every ``account_plugins.profile`` binding that
        pointed at the old name in the SAME transaction (the same
        pointer-rewrite convention as sync-group renames), so persistence
        can never hold a binding naming a profile that no longer exists.
        Returns the updated record (None when (plugin, name) is unknown).

        Raises:
            DatabaseError: renaming to an existing (plugin, name) pair.
        """
        current = await self.get(plugin, name)
        if current is None:
            return None
        if name_new is not None and (
            not isinstance(name_new, str) or not name_new.strip()
        ):
            raise DatabaseError("profile name_new must be a non-empty string")
        new_name = current.name if name_new is None else name_new.strip()
        new_description = current.description if description is None \
            else description
        payload = self._encode(
            current.settings if settings is None else settings, plugin, name)
        async with self._tx(
            f"update plugin profile {plugin!r}/{current.name!r}"
        ) as conn:
            try:
                await conn.execute(
                    "UPDATE plugin_profiles SET name = ?, description = ?,"
                    " settings_json = ?, updated_at = ?"
                    " WHERE plugin = ? AND name = ?",
                    (new_name, new_description, payload, time.time(),
                     plugin, current.name),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(
                    f"profile {new_name!r} already exists for plugin {plugin!r}"
                ) from exc
            if new_name != current.name:
                # keep the account bindings consistent with the rename
                await conn.execute(
                    "UPDATE account_plugins SET profile = ?"
                    " WHERE plugin = ? AND profile = ?",
                    (new_name, plugin, current.name),
                )
        return await self.get(plugin, new_name)

    async def delete(self, plugin: str, name: str) -> bool:
        """Delete one profile row; True when it existed.

        CASCADE (same transaction): every ``account_plugins.profile``
        reference to this profile is NULLed — a deleted profile means
        "plugin defaults" again for its bound accounts, never a dangling
        name the effective-settings merge would chase.
        """
        async with self._tx(
            f"delete plugin profile {plugin!r}/{name!r}"
        ) as conn:
            async with conn.execute(
                "DELETE FROM plugin_profiles WHERE plugin = ? AND name = ?",
                (plugin, name),
            ) as cur:
                existed = cur.rowcount > 0
            if existed:
                await conn.execute(
                    "UPDATE account_plugins SET profile = NULL"
                    " WHERE plugin = ? AND profile = ?",
                    (plugin, name),
                )
        return existed

    # ------------------------------------------------------------ internals
    @staticmethod
    def _encode(settings: Dict[str, Any], plugin: str, name: str) -> str:
        """settings dict -> settings_json (DatabaseError when unserializable)."""
        try:
            return json.dumps(settings or {})
        except (TypeError, ValueError) as exc:
            raise DatabaseError(
                f"settings for profile {plugin!r}/{name!r} are not "
                f"JSON-serializable: {exc}"
            ) from exc
