"""Plugin settings: typed manifests + layered resolution.

Layers (lowest to highest priority):
  1. SettingDef defaults declared by the plugin
  2. Global overrides      (config/global_settings.json -> {plugin: {key: value}})
  3. Account overrides     (config/accounts/<name>.json -> settings.{plugin.{key}})
Unknown keys and type mismatches raise SettingValidationError at resolve time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence

SETTING_TYPES = ("str", "int", "float", "bool")


class SettingValidationError(ValueError):
    """Raised with a precise path when a value fails validation."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class SettingDef:
    """One plugin setting: key, type, default, bounds, human description."""

    key: str
    type: str = "str"
    default: Any = None
    description: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").isalnum():
            raise SettingValidationError(self.key, "setting key must be a simple identifier")
        if self.type not in SETTING_TYPES:
            raise SettingValidationError(self.key, f"unknown type {self.type!r} (use {SETTING_TYPES})")
        if (self.min_value is not None or self.max_value is not None) and self.type not in ("int", "float"):
            raise SettingValidationError(self.key, "min/max bounds are only supported for numeric settings")
        try:
            self.validate(self.default)
        except SettingValidationError as exc:
            raise SettingValidationError(self.key, f"invalid default: {exc.reason}") from exc

    def validate(self, value: Any) -> Any:
        """Coerce + bounds-check a raw value; return the typed value.

        Expects: JSON-ish input (str/int/float/bool).
        Raises: SettingValidationError on wrong type or out-of-bounds numbers.
        """
        path = self.key
        if value is None:
            if self.default is None:
                return None
            value = self.default
        try:
            if self.type == "bool":
                if isinstance(value, str):
                    if value.strip().lower() in ("1", "true", "yes", "on"):
                        return True
                    if value.strip().lower() in ("0", "false", "no", "off"):
                        return False
                    raise ValueError(value)
                return bool(value)
            if self.type == "int":
                coerced = int(value) if not isinstance(value, bool) else int(value)
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError(value)
            elif self.type == "float":
                coerced = float(value) if not isinstance(value, bool) else float(value)
            else:
                coerced = str(value)
        except (TypeError, ValueError) as exc:
            raise SettingValidationError(path, f"expected {self.type}, got {value!r}") from exc
        if self.min_value is not None and coerced < self.min_value:
            raise SettingValidationError(path, f"{coerced} < min {self.min_value}")
        if self.max_value is not None and coerced > self.max_value:
            raise SettingValidationError(path, f"{coerced} > max {self.max_value}")
        return coerced

    def as_manifest(self) -> Dict[str, Any]:
        """JSON-safe manifest entry for the dashboard/CLI."""
        return {
            "key": self.key,
            "type": self.type,
            "default": self.default,
            "description": self.description,
            "min": self.min_value,
            "max": self.max_value,
        }


class SettingsStore:
    """Resolves effective plugin settings by merging the three layers."""

    def __init__(
        self,
        global_settings: Optional[Mapping[str, Mapping[str, Any]]] = None,
        account_overrides: Optional[Mapping[str, Mapping[str, Mapping[str, Any]]]] = None,
    ) -> None:
        #: plugin name -> {key: raw value}
        self._global: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in (global_settings or {}).items()
        }
        #: account name -> plugin name -> {key: raw value}
        self._accounts: Dict[str, Dict[str, Dict[str, Any]]] = {
            a: {p: dict(v) for p, v in layers.items()}
            for a, layers in (account_overrides or {}).items()
        }

    def resolve(
        self,
        plugin_name: str,
        setting_defs: Sequence[SettingDef],
        account: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute the effective, validated settings dict for one plugin instance.

        Expects: plugin_name, its SettingDef list, optional account name.
        Returns: {key: validated value}.
        Raises: SettingValidationError for unknown keys or bad values.
        """
        defs = {d.key: d for d in setting_defs}
        values: Dict[str, Any] = {d.key: d.default for d in setting_defs}
        layers = [self._global.get(plugin_name, {})]
        if account is not None:
            layers.append(self._accounts.get(account, {}).get(plugin_name, {}))
        for layer in layers:
            for key, raw in layer.items():
                if key not in defs:
                    raise SettingValidationError(
                        f"{plugin_name}.{key}", "unknown setting for this plugin"
                    )
                values[key] = defs[key].validate(raw)
        return values
