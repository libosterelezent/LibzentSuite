"""State persistence across restarts.

Persists to a single JSON file (default state/state.json, gitignored):
  * plugin enable/disable toggles per (account, plugin)
  * per-account rate-limit overrides set at runtime

The container applies plugin toggles when accounts are added; ControlAPI
records limit changes. Writes are atomic (tmp file + os.replace).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

LOG = logging.getLogger("mudae.state")

EMPTY_STATE: Dict[str, Any] = {"plugin_states": {}, "limit_overrides": {}}


class StateStore:
    """Tiny JSON-backed persistence for runtime UI changes."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: Dict[str, Any] = {"plugin_states": {}, "limit_overrides": {}}
        self.load()

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        """Read the state file; missing or corrupt files start fresh.

        A corrupt file is renamed to <path>.bak (never silently deleted).
        """
        if not self._path.is_file():
            return
        try:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("state root must be an object")
            self._data = {
                "plugin_states": dict(parsed.get("plugin_states") or {}),
                "limit_overrides": dict(parsed.get("limit_overrides") or {}),
            }
        except (json.JSONDecodeError, ValueError) as exc:
            backup = self._path.with_suffix(self._path.suffix + ".bak")
            LOG.warning("state file corrupt (%s); backed up to %s", exc, backup)
            try:
                os.replace(self._path, backup)
            except OSError:
                pass
            self._data = {"plugin_states": {}, "limit_overrides": {}}

    def snapshot(self) -> Dict[str, Any]:
        """Deep-ish copy of the whole state (for display/tests)."""
        return {
            "plugin_states": {a: dict(p) for a, p in self._data["plugin_states"].items()},
            "limit_overrides": {a: dict(r) for a, r in self._data["limit_overrides"].items()},
        }

    def _save(self) -> None:
        """Atomic write: serialize to tmp then os.replace over the target."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    # ------------------------------------------------------------ plugin state
    def plugin_enabled(self, account: str, plugin: str, default: bool = True) -> bool:
        """Persisted toggle for (account, plugin); default when never set."""
        stored = self._data["plugin_states"].get(account, {}).get(plugin)
        return default if stored is None else bool(stored)

    def set_plugin_enabled(self, account: str, plugin: str, enabled: bool) -> None:
        """Record and persist a plugin toggle."""
        self._data["plugin_states"].setdefault(account, {})[plugin] = bool(enabled)
        self._save()

    # ------------------------------------------------------------ limit state
    def limit_overrides(self) -> Dict[str, Dict[str, str]]:
        """account -> {action: rule string} for restoring on boot."""
        return {
            account: dict(rules)
            for account, rules in self._data["limit_overrides"].items()
        }

    def set_limit_override(self, account: str, action: str, rule: str) -> None:
        """Record and persist a per-account rule override."""
        self._data["limit_overrides"].setdefault(account, {})[action] = str(rule)
        self._save()

    def clear_limit_override(self, account: str, action: str) -> None:
        """Drop one override (falls back to the global default rule)."""
        rules = self._data["limit_overrides"].get(account)
        if rules and action in rules:
            del rules[action]
            if not rules:
                del self._data["limit_overrides"][account]
            self._save()
