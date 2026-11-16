"""PluginStateService — generic scoped persistence for plugins (spec §20).

A tiny approved key/value API over the ``kv`` table so plugins never
touch SQL or connections (spec §31). Scopes:

    global           — every account, every plugin instance
    account          — one account (any plugin using this service)
    plugin           — one plugin across accounts
    account_plugin   — one (account, plugin) pair

Storage keys are namespaced (``pstate:<scope>:...``) inside kv. Values are
stored RAW: the kv repository JSON-encodes on write and decodes on read
(one layer of encoding — values used to be pre-encoded here, double-encoding
every row; legacy rows are still decoded transparently in ``_get``).
All methods async (aiosqlite underneath). Plugins obtain
this service through PluginServices (approved plugin API), not directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .errors import PluginError

if TYPE_CHECKING:
    from database.repositories.kv import KvRepository


class PluginStateService:
    """Scoped get/set/delete/exists for plugin-persistent state."""

    SCOPE_SEPARATOR = ":"

    def __init__(self, kv_repository: KvRepository | None = None) -> None:
        self._kv = kv_repository

    def _require_kv(self) -> KvRepository:
        """The wired kv repository — get/set paths need one, period.

        Only the bulk-delete paths tolerate an unwired service (they
        return 0 rows); state access without a repository is a wiring
        bug and fails loud instead of AttributeError-ing deeper down.
        """
        if self._kv is None:
            raise PluginError("plugin-state service has no kv repository wired")
        return self._kv

    # -------------------------------------------------------------- scoping
    def _key(self, scope: str, owner: str | None, key: str) -> str:
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

    def _pair_owner(self, account: str, plugin: str) -> str:
        """Validated pair-scope owner (``account/plugin``).

        Each component is checked SEPARATELY: an unvalidated
        ``"a/p" + "/" + plugin`` owner collides with the pair
        ``("a", "p/x")`` in the flat kv key-space (e.g. both produce
        ``pstate:account_plugin:a/p/x:<key>``). Legit components never
        contain the separators (NAME_RE / plugin slugs forbid them).
        """
        for component in (account, plugin):
            if (
                not component
                or not isinstance(component, str)
                or any(c in component for c in (self.SCOPE_SEPARATOR, "/", "\x00"))
            ):
                raise PluginError(
                    f"pair scope component {component!r} must be a non-empty "
                    "string without ':' or '/'"
                )
        return f"{account}/{plugin}"

    def _global_key(self, key: str) -> str:
        return self._key("global", None, key)

    def _account_key(self, account: str, key: str) -> str:
        return self._key("account", account, key)

    def _plugin_key(self, plugin: str, key: str) -> str:
        return self._key("plugin", plugin, key)

    def _pair_key(self, account: str, plugin: str, key: str) -> str:
        return self._key("account_plugin", self._pair_owner(account, plugin), key)

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
    async def get_pair(self, account: str, plugin: str, key: str, default: Any = None) -> Any:
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
        if not account or any(c in account for c in (self.SCOPE_SEPARATOR, "/", "\x00")):
            # '/' is rejected too: cascading "a/b" would delete the
            # pair-scope rows of account "a", plugin "b"
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
        raw = await self._require_kv().get(stored_key)
        if raw is None:
            return default
        if isinstance(raw, dict) and len(raw) == 1 and self._STR_TAG in raw:
            return raw[self._STR_TAG]  # tagged str payload (lossless)
        if not isinstance(raw, str):
            # current rows store the value ONCE-encoded (kv decodes it)
            return raw
        # K10-mirror: LEGACY rows were pre-encoded by this service on top
        # of the kv layer's own encoding — kv.get hands back the inner
        # JSON text, which must be decoded once more. Rows written through
        # raw kv.set with a plain-string payload fall through to the raw
        # string (it IS the stored value), matching the kv repository's
        # own tolerant-read contract.
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw

    #: write-tag for str payloads: kv stores values JSON-encoded, so a raw
    #: string that itself parses as JSON ('"123"', '"{\"a\": 1}"') would be
    #: indistinguishable from a real int/dict on read — wrapping EVERY str
    #: makes the round-trip lossless (a dict with exactly this key unwraps)
    _STR_TAG = "__sm_str__"

    async def _set(self, stored_key: str, value: Any) -> None:
        # RAW value: the kv repository JSON-encodes on write (it used to be
        # pre-encoded here, double-encoding every row — and rows written
        # via raw kv.set then read back as the caller's default, silently)
        payload = {self._STR_TAG: value} if isinstance(value, str) else value
        await self._require_kv().set(stored_key, payload)

    async def _delete(self, stored_key: str) -> bool:
        return await self._require_kv().delete(stored_key)


__all__ = ["PluginStateService"]
