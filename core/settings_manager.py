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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import SettingsValidationError
from .event_bus import EventBus, SettingChanged

SETTING_TYPES = ("str", "int", "float", "bool")

#: schema-level cap for str-typed settings: an unbounded string was a 1MB
#: trigger persisted + compared per message. A Setting may override via
#: ``max_length`` (None here means "use the schema default").
STR_MAX_LENGTH = 4096


@dataclass(frozen=True)
class Setting:
    """One typed setting: name, type, default, bounds, human description."""

    name: str
    type: str = "str"
    default: Any = None
    description: str = ""
    min_value: float | None = None
    max_value: float | None = None
    max_length: int | None = None  # str settings only; None -> STR_MAX_LENGTH

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise SettingsValidationError(self.name, "setting name must be a simple identifier")
        if self.type not in SETTING_TYPES:
            raise SettingsValidationError(
                self.name, f"unknown type {self.type!r} (use {SETTING_TYPES})"
            )
        if (self.min_value is not None or self.max_value is not None) and self.type not in (
            "int",
            "float",
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
            if self.type in ("int", "float") and isinstance(value, bool):
                # float(True) == 1.0 silently inverted the operator's intent
                # ({"interval": true} must not become a 1s interval)
                raise ValueError(value)
            coerced: int | float | str
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
                limit = self.max_length if self.max_length is not None else STR_MAX_LENGTH
                if len(coerced) > limit:
                    raise SettingsValidationError(
                        self.name,
                        f"string is {len(coerced)} chars, longer than the max {limit}",
                    )
        except (TypeError, ValueError, OverflowError) as exc:
            raise SettingsValidationError(
                self.name, f"expected {self.type}, got {value!r}"
            ) from exc
        # numeric tail: min/max bounds and finiteness are numeric-setting
        # concerns; the isinstance narrowing expresses that contract (a
        # str setting with bounds set is a manifest authoring error, and
        # max_length already governs strings)
        if isinstance(coerced, (int, float)):
            try:
                nonfinite = math.isinf(coerced)
            except OverflowError:
                # a giant int (10**400) overflows float conversion —
                # surface a validation error, not a crash
                nonfinite = True
            if nonfinite:
                raise SettingsValidationError(self.name, f"{value!r} is not a finite number")
            if self.min_value is not None and coerced < self.min_value:
                raise SettingsValidationError(self.name, f"{coerced} < min {self.min_value}")
            if self.max_value is not None and coerced > self.max_value:
                raise SettingsValidationError(self.name, f"{coerced} > max {self.max_value}")
        return coerced

    def as_manifest(self) -> dict[str, Any]:
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
#: policy knobs every account owns. Persisted in `account_settings`;
#: surfaced via ControlAPI.account_setting_get / account_setting_set
#: (surfaces can adopt those facades — see the round-3 report).
ACCOUNT_SETTINGS: tuple[Setting, ...] = (
    Setting("reconnect_enabled", "bool", True, "Reconnect automatically after drops."),
    Setting(
        "reconnect_initial_delay",
        "float",
        1.0,
        "First reconnect wait (seconds).",
        min_value=0.1,
        max_value=60.0,
    ),
    Setting(
        "reconnect_max_delay",
        "float",
        60.0,
        "Reconnect wait cap (seconds).",
        min_value=0.5,
        max_value=600.0,
    ),
    Setting(
        "reconnect_multiplier",
        "float",
        2.0,
        "Backoff growth factor.",
        min_value=1.0,
        max_value=10.0,
    ),
    Setting(
        "typing_probability",
        "float",
        0.0,
        "Chance a guild send first fires a typing indicator (anti-flag: "
        "humans type; 0 disables — zero extra API calls). ~0.85 looks human.",
        min_value=0.0,
        max_value=1.0,
    ),
)


_ACCOUNT_DEFS_CACHE: tuple[dict[str, Setting], dict[str, Any]] | None = None


def _account_defs() -> tuple[dict[str, Setting], dict[str, Any]]:
    """(name -> Setting, name -> validated default) for ACCOUNT_SETTINGS.

    Built once: ``account_settings()`` runs on every reconnect/typing
    decision and used to re-validate every default on each call.
    """
    global _ACCOUNT_DEFS_CACHE
    if _ACCOUNT_DEFS_CACHE is None:
        defs = {s.name: s for s in ACCOUNT_SETTINGS}
        _ACCOUNT_DEFS_CACHE = (defs, {name: s.validate(s.default) for name, s in defs.items()})
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
        # config-file JSON: the plugin layers are UNVALIDATED — the value
        # type is Any so the malformed-shape filter + warning below stay
        # meaningful to the checker (and to the reader)
        global_layer: Mapping[str, Any] | None = None,
    ) -> None:
        self._bus = bus
        self._logger = logger or logging.getLogger("libzent.settings")
        self._plugin_repo = plugin_repo  # database PluginRepository
        self._account_repo = account_settings_repo  # database AccountSettingsRepository
        #: plugin name -> {key: raw value}      (config file layer)
        self._global: dict[str, dict[str, Any]] = {
            plugin: dict(layer)
            for plugin, layer in (global_layer or {}).items()
            if isinstance(layer, Mapping)
        }
        for plugin, layer in (global_layer or {}).items():
            if not isinstance(layer, Mapping):
                self._logger.warning("ignoring malformed global settings for %r", plugin)
        #: account -> plugin -> {key: raw value}  (DB layer, loaded at boot)
        self._account_plugin: dict[str, dict[str, dict[str, Any]]] = {}
        #: account -> {key: raw value}            (framework account settings)
        self._account_framework: dict[str, dict[str, Any]] = {}
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
                self._account_plugin = await self._plugin_repo.all_account_settings() or {}
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
                    self._account_framework[name] = dict(await self._account_repo.get(name) or {})
                except Exception:
                    self._logger.exception(
                        "could not load account settings for %r "
                        "(defaults will resolve for this account)",
                        name,
                    )
                    self._account_framework[name] = {}
        self._loaded = True

    # ------------------------------------------------------------ plugins
    def forget_account(self, account: str) -> None:
        """Drop an account's in-memory mirror layers (removal cascade).

        K14 (S6-C2): without this, removing and re-adding a same-named
        account in one process inherits the predecessor's plugin/framework
        setting overrides from memory while the database is empty — memory
        and DB then diverge until the next restart.
        """
        self._account_plugin.pop(account, None)
        self._account_framework.pop(account, None)

    def resolve(
        self,
        plugin_name: str,
        schema: Sequence[Setting],
        account: str | None = None,
        *,
        profile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Effective, validated settings for one plugin instance.

        Merge order: validated defaults <- global layer <- bound-profile
        layer (the optional ``profile`` mapping — the plugin manager
        threads the account's bound profile settings here) <- account
        layer. Raises SettingsValidationError (with the layer named) for
        unknown keys or bad values — at boot this fails fast, at runtime
        it surfaces as a clean error line. Pure memory (call
        :meth:`load` first at boot).
        """
        defs = {s.name: s for s in schema}
        values: dict[str, Any] = {s.name: s.validate(s.default) for s in schema}
        layers: list[tuple[str, Mapping[str, Any]]] = [
            ("global layer", self._global.get(plugin_name, {}))
        ]
        if profile is not None:
            layers.append(("profile layer", profile))
        if account is not None:
            layers.append(
                (
                    f"account '{account}' layer",
                    self._account_plugin.get(account, {}).get(plugin_name, {}),
                )
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
        self._bus.publish_nowait(
            SettingChanged(
                account=account,
                scope=f"account:{account}/plugin:{plugin}/key:{key}",
                key=key,
                old_value=old,
                new_value=validated,
            )
        )
        return validated

    async def clear_plugin_setting(
        self,
        account: str,
        plugin: str,
        key: str,
        schema: Sequence[Setting] = (),
        *,
        profile: Mapping[str, Any] | None = None,
    ) -> Any:
        """DELETE one account-layer plugin setting override (dashboard 'reset').

        The account layer is a MASK over the profile/global layers/
        defaults: the old reset persisted the schema DEFAULT as an
        override, permanently shadowing a non-default global value with
        no unpin path. Deleting the row unmasks the lower layers
        (defaults <- global <- ``profile`` when the caller threads the
        account's bound-profile settings — the plugin manager does, so
        the recomputed effective value matches attach-time precedence).

        Emits SettingChanged on success (``new_value`` = the newly
        effective value when a schema is provided, else None). Raises
        SettingsValidationError for unknown keys when a schema is given.
        The repository delete is DUCK-TYPED (``delete_setting``): a repo
        without it is warned at and skipped — the memory layer still
        clears, so in-memory resolution unmasks immediately either way.
        Persistence errors propagate (memory only clears after the write).
        """
        defs = {s.name: s for s in schema}
        if defs and key not in defs:
            raise SettingsValidationError(
                f"{plugin}.{key}",
                f"unknown setting (supports: {sorted(defs)})",
            )
        old = self._account_plugin.get(account, {}).get(plugin, {}).get(key)
        delete = getattr(self._plugin_repo, "delete_setting", None)
        if callable(delete):
            await delete(account, plugin, key)
        elif self._plugin_repo is not None:
            self._logger.warning(
                "plugin repository cannot delete settings "
                "(no delete_setting); cleared %r/%s.%s from memory only",
                account,
                plugin,
                key,
            )
        plugins = self._account_plugin.get(account)
        if plugins is not None:
            plugins.get(plugin, {}).pop(key, None)
            if not plugins.get(plugin):
                plugins.pop(plugin, None)
            if not plugins:
                self._account_plugin.pop(account, None)
        effective = (
            self.resolve(plugin, schema, account=account, profile=profile)[key] if defs else None
        )
        self._bus.publish_nowait(
            SettingChanged(
                account=account,
                scope=f"account:{account}/plugin:{plugin}/key:{key}",
                key=key,
                old_value=old,
                new_value=effective,
            )
        )
        return effective

    def account_settings(self, account: str) -> dict[str, Any]:
        """Resolved framework account settings (reconnect policy...).

        Pure memory after :meth:`load` (sync by design — called from sync
        reconnect/typing paths in the account manager).
        """
        defs, defaults = _account_defs()  # cached: hot sync path
        stored = self._account_framework.get(account, {})
        values = dict(defaults)
        for key, raw in stored.items():
            if key not in defs:
                self._logger.warning("ignoring unknown account setting %s.%s=%r", account, key, raw)
                continue
            try:
                values[key] = defs[key].validate(raw)
            except SettingsValidationError:
                self._logger.warning("ignoring invalid account setting %s.%s=%r", account, key, raw)
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
        self._bus.publish_nowait(
            SettingChanged(
                account=account,
                scope=f"account:{account}/key:{key}",
                key=key,
                old_value=old,
                new_value=validated,
            )
        )
        return validated

    # -------------------------------------------------------------- views
    def layers_view(self) -> dict[str, Any]:
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


__all__ = [
    "ACCOUNT_SETTINGS",
    "SETTING_TYPES",
    "STR_MAX_LENGTH",
    "Setting",
    "SettingsManager",
    "SettingsValidationError",
]
