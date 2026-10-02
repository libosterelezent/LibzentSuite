"""Shared settings system (spec §3): ONE schema, consumed by CLI + GUI alike.

Design:
  * Plugins (and the framework's account layer) declare settings as a tuple of
    `Setting` objects — type, default, bounds, human description.
  * `SettingsManager` resolves the effective values by merging layers:
        1. schema defaults          (lowest priority)
        2. global layer             (config/global_settings.json)
        3. account layer            (database plugin_settings / account_settings)
  * The SAME schema auto-generates the dashboard settings editors and the CLI
    'set' validation — no per-surface implementations (parity by construction).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .event_bus import EventBus, SettingChanged
from .errors import SettingsValidationError

SETTING_TYPES = ("str", "int", "float", "bool")


@dataclass(frozen=True)
class Setting:
    """One typed setting: name, type, default, bounds, human description."""

    name: str
    type: str = "str"
    default: Any = None
    description: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise SettingsValidationError(self.name, "setting name must be a simple identifier")
        if self.type not in SETTING_TYPES:
            raise SettingsValidationError(
                self.name, f"unknown type {self.type!r} (use {SETTING_TYPES})"
            )
        if (self.min_value is not None or self.max_value is not None) and self.type not in (
            "int", "float"
        ):
            raise SettingsValidationError(
                self.name, "min/max bounds are only supported for numeric settings"
            )
        try:
            object.__setattr__(self, "default", self.validate(self.default))  # frozen
        except SettingsValidationError as exc:
            raise SettingsValidationError(self.name, f"invalid default: {exc.reason}") from exc

    def validate(self, value: Any) -> Any:
        """Coerce + bounds-check a raw value; returns the typed value.

        Raises SettingsValidationError(name, reason) on wrong type/bounds.
        """
        if value is None:
            if self.default is None:
                return None
            value = self.default
        try:
            if self.type == "bool":
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in ("1", "true", "yes", "on"):
                        return True
                    if lowered in ("0", "false", "no", "off"):
                        return False
                    raise ValueError(value)
                if isinstance(value, bool):
                    return value
                raise ValueError(value)
            if self.type == "int":
                coerced = int(value)
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError(value)
            elif self.type == "float":
                coerced = float(value)
                if math.isnan(coerced):
                    raise ValueError(value)
            else:
                coerced = str(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SettingsValidationError(
                self.name, f"expected {self.type}, got {value!r}"
            ) from exc
        if self.type in ("int", "float") and math.isinf(coerced):
            raise SettingsValidationError(self.name, f"{value!r} is not a finite number")
        if self.min_value is not None and coerced < self.min_value:
            raise SettingsValidationError(self.name, f"{coerced} < min {self.min_value}")
        if self.max_value is not None and coerced > self.max_value:
            raise SettingsValidationError(self.name, f"{coerced} > max {self.max_value}")
        return coerced

    def as_manifest(self) -> Dict[str, Any]:
        """JSON-safe manifest entry for auto-generated settings UIs."""
        return {
            "name": self.name,
            "type": self.type,
            "default": self.default,
            "description": self.description,
            "min": self.min_value,
            "max": self.max_value,
        }


#: Framework-level per-account settings (NOT plugin settings): reconnect
#: policy knobs every account owns. Persisted in `account_settings`, surfaced
#: on the account details page + editable from both interfaces.
ACCOUNT_SETTINGS: tuple = (
    Setting("reconnect_enabled", "bool", True, "Reconnect automatically after drops."),
    Setting("reconnect_initial_delay", "float", 1.0,
            "First reconnect wait (seconds).", min_value=0.1, max_value=60.0),
    Setting("reconnect_max_delay", "float", 60.0,
            "Reconnect wait cap (seconds).", min_value=0.5, max_value=600.0),
    Setting("reconnect_multiplier", "float", 2.0,
            "Backoff growth factor.", min_value=1.0, max_value=10.0),
    Setting("typing_probability", "float", 0.0,
            "Chance a guild send first fires a typing indicator (anti-flag: "
            "humans type; 0 disables — zero extra API calls). ~0.85 looks human.",
            min_value=0.0, max_value=1.0),
)


_ACCOUNT_DEFS_CACHE: Optional[tuple] = None


def _account_defs() -> tuple:
    """(name -> Setting, name -> validated default) for ACCOUNT_SETTINGS.

    Built once: ``account_settings()`` runs on every reconnect/typing
    decision and used to re-validate every default on each call.
    """
    global _ACCOUNT_DEFS_CACHE
    if _ACCOUNT_DEFS_CACHE is None:
        defs = {s.name: s for s in ACCOUNT_SETTINGS}
        _ACCOUNT_DEFS_CACHE = (
            defs, {name: s.validate(s.default) for name, s in defs.items()})
    return _ACCOUNT_DEFS_CACHE


class SettingsManager:
    """Layered, validated, persisted settings for plugins AND accounts."""

    def __init__(
        self,
        bus: EventBus,
        logger: logging.Logger | Any | None = None,
        *,
        plugin_repo: Any = None,
        account_settings_repo: Any = None,
        global_layer: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        self._bus = bus
        self._logger = logger or logging.getLogger("slaughtermudae.settings")
        self._plugin_repo = plugin_repo            # database PluginRepository
        self._account_repo = account_settings_repo  # database AccountSettingsRepository
        #: plugin name -> {key: raw value}      (config file layer)
        self._global: Dict[str, Dict[str, Any]] = {
            plugin: dict(layer) for plugin, layer in (global_layer or {}).items()
            if isinstance(layer, Mapping)
        }
        for plugin, layer in (global_layer or {}).items():
            if not isinstance(layer, Mapping):
                self._logger.warning("ignoring malformed global settings for %r", plugin)
        #: account -> plugin -> {key: raw value}  (DB layer, loaded at boot)
        self._account_plugin: Dict[str, Dict[str, Dict[str, Any]]] = {}
        #: account -> {key: raw value}            (framework account settings)
        self._account_framework: Dict[str, Dict[str, Any]] = {}
        self._loaded = plugin_repo is None and account_settings_repo is None

    # ------------------------------------------------------------ loading
    async def load(self) -> None:
        """Pull the persisted account layers into memory (boot path).

        Called ONCE by the bootstrap (and the simulation runner) after
        ``database.connect()``; every read below (:meth:`resolve`,
        :meth:`account_settings`, :meth:`layers_view`) is then pure memory
        and stays SYNC by design — the account manager and the plugin
        manager call them from sync code paths.

        Best-effort per layer: a repository failure is logged and leaves
        that layer empty (defaults still resolve, boot still completes).
        """
        if self._plugin_repo is not None:
            try:
                self._account_plugin = (
                    await self._plugin_repo.all_account_settings() or {}
                )
            except Exception:
                self._logger.exception("could not load plugin settings layer")
                self._account_plugin = {}
        if self._account_repo is not None:
            listing = getattr(self._account_repo, "list_accounts", None)
            if callable(listing):
                try:  # best-effort like the layers above: a locked read
                    names = await listing() or []  # must not fail the boot
                except Exception:
                    self._logger.exception("could not load account settings layer")
                    names = []
            else:
                names = []
            for name in names:
                try:
                    self._account_framework[name] = dict(
                        await self._account_repo.get(name) or {}
                    )
                except Exception:
                    self._account_framework[name] = {}
        self._loaded = True

    # ------------------------------------------------------------ plugins
    def resolve(
        self,
        plugin_name: str,
        schema: Sequence[Setting],
        account: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Effective, validated settings for one plugin instance.

        Merge order: validated defaults <- global layer <- account layer.
        Raises SettingsValidationError (with the layer named) for unknown keys
        or bad values — at boot this fails fast, at runtime it surfaces as a
        clean error line. Pure memory (call :meth:`load` first at boot).
        """
        defs = {s.name: s for s in schema}
        values: Dict[str, Any] = {s.name: s.validate(s.default) for s in schema}
        layers = [("global layer", self._global.get(plugin_name, {}))]
        if account is not None:
            layers.append(
                (f"account '{account}' layer",
                 self._account_plugin.get(account, {}).get(plugin_name, {}))
            )
        for layer_name, layer in layers:
            for key, raw in layer.items():
                if key not in defs:
                    raise SettingsValidationError(
                        f"{plugin_name}.{key}",
                        f"unknown setting for this plugin ({layer_name})",
                    )
                try:
                    values[key] = defs[key].validate(raw)
                except SettingsValidationError as exc:
                    raise SettingsValidationError(
                        f"{plugin_name}.{key} ({layer_name})", exc.reason
                    ) from exc
        return values

    async def set_plugin_setting(
        self, account: str, plugin: str, key: str, value: Any, schema: Sequence[Setting]
    ) -> Any:
        """Validate + persist one account-layer plugin setting; returns typed value.

        Emits SettingChanged on success. Raises SettingsValidationError for
        unknown keys / bad values; persistence errors propagate (caller rolls
        back nothing — memory layer only updates after a successful write).
        """
        defs = {s.name: s for s in schema}
        if key not in defs:
            raise SettingsValidationError(
                f"{plugin}.{key}",
                f"unknown setting (supports: {sorted(defs)})",
            )
        validated = defs[key].validate(value)
        old = self._account_plugin.get(account, {}).get(plugin, {}).get(key)
        if self._plugin_repo is not None:
            await self._plugin_repo.set_setting(account, plugin, key, validated)
        self._account_plugin.setdefault(account, {}).setdefault(plugin, {})[key] = validated
        self._bus.publish_nowait(SettingChanged(
            account=account, scope=f"account:{account}/plugin:{plugin}/key:{key}",
            key=key, old_value=old, new_value=validated,
        ))
        return validated

    # ----------------------------------------------------------- accounts
    def account_settings(self, account: str) -> Dict[str, Any]:
        """Resolved framework account settings (reconnect policy...).

        Pure memory after :meth:`load` (sync by design — called from sync
        reconnect/typing paths in the account manager).
        """
        defs, defaults = _account_defs()  # cached: hot sync path
        stored = self._account_framework.get(account, {})
        values = dict(defaults)
        for key, raw in stored.items():
            if key in defs:
                try:
                    values[key] = defs[key].validate(raw)
                except SettingsValidationError:
                    self._logger.warning(
                        "ignoring invalid account setting %s.%s=%r", account, key, raw
                    )
        return values

    async def set_account_setting(self, account: str, key: str, value: Any) -> Any:
        """Validate + persist one framework account setting."""
        defs = {s.name: s for s in ACCOUNT_SETTINGS}
        if key not in defs:
            raise SettingsValidationError(
                f"account.{key}",
                f"unknown account setting (supports: {sorted(defs)})",
            )
        validated = defs[key].validate(value)
        old = self._account_framework.get(account, {}).get(key)
        if self._account_repo is not None:
            await self._account_repo.set(account, key, validated)
        self._account_framework.setdefault(account, {})[key] = validated
        self._bus.publish_nowait(SettingChanged(
            account=account, scope=f"account:{account}/key:{key}",
            key=key, old_value=old, new_value=validated,
        ))
        return validated

    # -------------------------------------------------------------- views
    def layers_view(self) -> Dict[str, Any]:
        """Both layers as JSON-safe dicts (dashboard Advanced page).

        Pure memory after :meth:`load`.
        """
        return {
            "global": {p: dict(v) for p, v in self._global.items()},
            "accounts": {
                a: {p: dict(v) for p, v in plugins.items()}
                for a, plugins in self._account_plugin.items()
            },
        }


__all__ = ["Setting", "SettingsManager", "ACCOUNT_SETTINGS", "SETTING_TYPES",
           "SettingsValidationError"]
