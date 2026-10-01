"""PluginRepository — plugin manifests, per-account states and settings.

Three tables, one facade:

* ``plugins``          discovered-plugin metadata (manifest for the UI);
* ``plugin_states``    per-account enabled/disabled flag (absent = default);
* ``plugin_settings``  per-account setting overrides (JSON values).

Metadata is returned as plain dicts shaped like PluginMetadata.to_dict():
``{"name", "version", "description", "settings"}`` — the UI/settings layers
consume exactly that shape.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.errors import DatabaseError

from database.models import PluginMetadata

from .base import BaseRepository, loads_tolerant

__all__ = ["PluginRepository"]


class PluginRepository(BaseRepository):
    """Manifest + state + settings persistence for plugins, keyed by NAME."""

    # ------------------------------------------------------------ metadata
    def upsert_metadata(
        self,
        name: str,
        version: str,
        description: str,
        settings_manifest: Any,
    ) -> None:
        """Insert or refresh a discovered plugin's manifest row.

        Called on every discovery pass, so it must be an idempotent upsert.
        The settings manifest (list of Setting.as_manifest() dicts) is stored
        as JSON. Raises DatabaseError for a non-serializable manifest.
        """
        if not isinstance(name, str) or not name:
            raise DatabaseError("plugin name must be a non-empty string")
        try:
            manifest = json.dumps(list(settings_manifest or []))
        except (TypeError, ValueError) as exc:
            raise DatabaseError(
                f"settings manifest for plugin {name!r} is not JSON-serializable: {exc}"
            ) from exc
        with self._tx(f"upsert metadata for plugin {name!r}") as conn:
            conn.execute(
                """INSERT INTO plugins (name, version, description, settings_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     version = excluded.version,
                     description = excluded.description,
                     settings_json = excluded.settings_json""",
                (name, str(version), str(description or ""), manifest),
            )

    def metadata(self) -> List[Dict[str, Any]]:
        """All plugin manifests as PluginMetadata-shaped dicts (name order).

        A corrupt manifest degrades to an empty settings list, not a crash.
        """
        out: List[Dict[str, Any]] = []
        for row in self._read("SELECT * FROM plugins ORDER BY name"):
            settings = loads_tolerant(
                row["settings_json"], [], f"plugins[{row['name']!r}].settings_json"
            )
            out.append(
                PluginMetadata(
                    name=row["name"],
                    version=row["version"],
                    description=row["description"],
                    settings_manifest=list(settings or []),
                ).to_dict()
            )
        return out

    def metadata_for(self, name: str) -> Optional[Dict[str, Any]]:
        """One plugin's manifest dict (None when the plugin is unknown)."""
        row = self._read_one("SELECT * FROM plugins WHERE name = ?", (name,))
        if row is None:
            return None
        settings = loads_tolerant(row["settings_json"], [], f"plugins[{name!r}].settings_json")
        return PluginMetadata(
            name=row["name"],
            version=row["version"],
            description=row["description"],
            settings_manifest=list(settings or []),
        ).to_dict()

    # --------------------------------------------------------------- state
    def state(self, account: str, plugin: str) -> Optional[bool]:
        """Persisted enabled flag for (account, plugin).

        None means "no row" — the caller applies its own default (True for
        fresh plugin instances), which is why absent is distinct from False.
        """
        row = self._read_one(
            "SELECT enabled FROM plugin_states WHERE account = ? AND plugin = ?",
            (account, plugin),
        )
        return None if row is None else bool(row["enabled"])

    def set_state(self, account: str, plugin: str, enabled: bool) -> None:
        """Persist the enabled flag (upsert); survives restarts."""
        with self._tx(f"set plugin state {account!r}/{plugin!r}") as conn:
            conn.execute(
                """INSERT INTO plugin_states (account, plugin, enabled) VALUES (?, ?, ?)
                   ON CONFLICT(account, plugin) DO UPDATE SET enabled = excluded.enabled""",
                (account, plugin, int(bool(enabled))),
            )

    # ------------------------------------------------------------ settings
    def setting(self, account: str, plugin: str, key: str) -> Any:
        """One stored setting value (None when absent)."""
        row = self._read_one(
            "SELECT value_json FROM plugin_settings WHERE account = ? AND plugin = ? AND key = ?",
            (account, plugin, key),
        )
        if row is None:
            return None
        return loads_tolerant(
            row["value_json"], None, f"plugin_settings[{account!r}/{plugin!r}].{key}"
        )

    def settings(self, account: str, plugin: str) -> Dict[str, Any]:
        """All stored overrides for one (account, plugin) ({} when none)."""
        out: Dict[str, Any] = {}
        for row in self._read(
            "SELECT key, value_json FROM plugin_settings WHERE account = ? AND plugin = ? ORDER BY key",
            (account, plugin),
        ):
            value = loads_tolerant(
                row["value_json"], None, f"plugin_settings[{account!r}/{plugin!r}].{row['key']}"
            )
            if value is not None:
                out[row["key"]] = value
        return out

    def set_setting(self, account: str, plugin: str, key: str, value: Any) -> None:
        """Store one per-account setting override (upsert, JSON value).

        Raises DatabaseError for an empty/non-string key or a value that
        cannot be serialized.
        """
        if not isinstance(key, str) or not key:
            raise DatabaseError("setting key must be a non-empty string")
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise DatabaseError(
                f"setting {plugin!r}.{key} for {account!r} is not JSON-serializable: {exc}"
            ) from exc
        with self._tx(f"store plugin setting {account!r}/{plugin!r}.{key}") as conn:
            conn.execute(
                """INSERT INTO plugin_settings (account, plugin, key, value_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account, plugin, key)
                   DO UPDATE SET value_json = excluded.value_json""",
                (account, plugin, key, payload),
            )

    def all_account_settings(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """The whole override layer: account -> plugin -> key -> value.

        Consumed by SettingsManager to preload every override in one query.
        Corrupt rows are skipped with a warning (never a crash).
        """
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in self._read(
            "SELECT account, plugin, key, value_json FROM plugin_settings ORDER BY account, plugin, key"
        ):
            value = loads_tolerant(
                row["value_json"], None,
                f"plugin_settings[{row['account']!r}/{row['plugin']!r}].{row['key']}",
            )
            if value is not None:
                out.setdefault(row["account"], {}).setdefault(row["plugin"], {})[row["key"]] = value
        return out
