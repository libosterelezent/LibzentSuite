"""PluginStateService — generic scoped persistence for plugins (spec §20).

A tiny approved key/value API over the ``kv`` table so plugins never
touch SQL or connections (spec §31). Scopes:

    global           — every account, every plugin instance
    account          — one account (any plugin using this service)
    plugin           — one plugin across accounts
    account_plugin   — one (account, plugin) pair

Storage keys are namespaced (``pstate:<scope>:...``) inside kv, values
JSON-encoded. All methods async (aiosqlite underneath). Plugins obtain
this service through PluginServices (approved plugin API), not directly.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .errors import PluginError


class PluginStateService:
    """Scoped get/set/delete/exists for plugin-persistent state."""

    SCOPE_SEPARATOR = ":"

    def __init__(self, kv_repository: Optional[Any] = None) -> None:
        self._kv = kv_repository

    # -------------------------------------------------------------- scoping
    def _key(self, scope: str, owner: Optional[str], key: str) -> str:
        """Namespace a user key inside its scope (collision-free)."""
        if not key or any(c in key for c in ("\x00",)):
            raise PluginError(f"state key {key!r} must be a non-empty string")
        parts = ["pstate", scope]
        if owner:
            if any(c in owner for c in (self.SCOPE_SEPARATOR, "\x00")):
                # '/' is the pair-scope separator (account/plugin) — safe
                # because NAME_RE and plugin slugs forbid '/' in components
                raise PluginError(f"scope owner {owner!r} contains reserved characters")
            parts.append(owner)
        parts.append(key)
        return self.SCOPE_SEPARATOR.join(parts)

    def _global_key(self, key: str) -> str:
        return self._key("global", None, key)

    def _account_key(self, account: str, key: str) -> str:
        return self._key("account", account, key)

    def _plugin_key(self, plugin: str, key: str) -> str:
        return self._key("plugin", plugin, key)

    def _pair_key(self, account: str, plugin: str, key: str) -> str:
        return self._key("account_plugin", f"{account}/{plugin}", key)

    # ------------------------------------------------------------- global
    async def get_global(self, key: str, default: Any = None) -> Any:
        return await self._get(self._global_key(key), default)

    async def set_global(self, key: str, value: Any) -> None:
        await self._set(self._global_key(key), value)

    # ------------------------------------------------------------- account
    async def get_account(self, account: str, key: str, default: Any = None) -> Any:
        return await self._get(self._account_key(account, key), default)

    async def set_account(self, account: str, key: str, value: Any) -> None:
        await self._set(self._account_key(account, key), value)

    async def delete_account(self, account: str, key: str) -> bool:
        return await self._delete(self._account_key(account, key))

    # -------------------------------------------------------------- plugin
    async def get_plugin(self, plugin: str, key: str, default: Any = None) -> Any:
        return await self._get(self._plugin_key(plugin, key), default)

    async def set_plugin(self, plugin: str, key: str, value: Any) -> None:
        await self._set(self._plugin_key(plugin, key), value)

    # ------------------------------------------------------- account+plugin
    async def get_pair(self, account: str, plugin: str, key: str,
                       default: Any = None) -> Any:
        return await self._get(self._pair_key(account, plugin, key), default)

    async def set_pair(self, account: str, plugin: str, key: str, value: Any) -> None:
        await self._set(self._pair_key(account, plugin, key), value)

    async def delete_pair(self, account: str, plugin: str, key: str) -> bool:
        return await self._delete(self._pair_key(account, plugin, key))

    async def cascade_account(self, account: str) -> int:
        """Delete EVERY kv row scoped to one account (both account scopes).

        Used by account removal so a future same-named account never
        inherits the old one's counters/markers. Returns rows removed
        (0 when no repository is wired).
        """
        if self._kv is None:
            return 0
        if not account or any(c in account for c in (self.SCOPE_SEPARATOR, "\x00")):
            raise PluginError(f"invalid account scope {account!r}")
        delete_prefix = getattr(self._kv, "delete_prefix", None)
        if delete_prefix is None:
            return 0  # repository without prefix deletes: nothing to do
        return int(
            await delete_prefix(f"pstate:account:{account}:")
            + await delete_prefix(f"pstate:account_plugin:{account}/")
        )

    # ------------------------------------------------------------ internals
    async def _get(self, stored_key: str, default: Any) -> Any:
        raw = await self._kv.get(stored_key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default  # corrupt row -> default; next set heals it

    async def _set(self, stored_key: str, value: Any) -> None:
        await self._kv.set(stored_key, json.dumps(value))

    async def _delete(self, stored_key: str) -> bool:
        return await self._kv.delete(stored_key)


__all__ = ["PluginStateService"]
