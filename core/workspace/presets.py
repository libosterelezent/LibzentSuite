"""Preset lifecycle — pure functions (W1; WORKSPACE_DESIGN §6).

Presets are reusable TEMPLATES, copied onto an account's own
configuration (decision 9): apply writes exactly the keys the preset
defines (the locked D.3 semantics — never a full-configuration
replace), under an atomic revision check (decision 16) so a stale
preview can never overwrite intervening account edits.

Everything here is a pure dict/record transform: no I/O, no schema
imports — validation ducks against Setting-like objects (the
ControlAPI layer owns schema validation in production; these
functions are the testable core of that contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

from .models import InvalidDefinition, PresetScope, Provenance

__all__ = [
    "PresetRecord",
    "StalePreview",
    "apply_diff",
    "compute_apply_diff",
    "detach",
    "preview_revision_ok",
    "validate_preset_settings",
]


class StalePreview(Exception):
    """Decision 16: the account's configuration changed after the
    diff was previewed — the apply must refresh and re-confirm.
    Revisions are opaque fingerprints (the service hashes the sorted
    settings content); any intervening write changes them."""

    def __init__(self, preview_revision: str, current_revision: str) -> None:
        super().__init__(
            f"stale preview: configuration moved from {preview_revision} "
            f"to {current_revision} — re-preview and re-confirm"
        )
        self.preview_revision = preview_revision
        self.current_revision = current_revision


@dataclass(frozen=True)
class PresetRecord:
    """A preset template identity (decision 15 — immutable versions).

    ``version`` is the IMMUTABLE version number; editing creates a
    new version under the same ``preset_id``. ``schema_version`` is
    the destination plugin's settings-schema version the settings
    were validated against.
    """

    preset_id: str
    scope: str  # PresetScope value
    plugin: str
    name: str
    version: int
    schema_version: int
    settings: dict[str, Any]
    owner_id: str | None = None  # workspace or account name; None only for global
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.preset_id, str) or not self.preset_id.strip():
            raise InvalidDefinition("preset_id must be a non-empty string")
        if self.scope not in PresetScope.ALL:
            raise InvalidDefinition(
                f"preset scope must be one of {sorted(PresetScope.ALL)}, got {self.scope!r}"
            )
        for field_name in ("plugin", "name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidDefinition(f"preset {field_name} must be a non-empty string")
        if self.version < 1 or self.schema_version < 1:
            raise InvalidDefinition(
                f"version/schema_version must be >= 1, got version={self.version} "
                f"schema_version={self.schema_version}"
            )
        if not isinstance(self.settings, dict):
            raise InvalidDefinition(
                f"preset settings must be a dict, got {type(self.settings).__name__}"
            )
        # scope-owner consistency: global owns nothing, every other
        # scope names its owner (§6.1)
        if self.scope == PresetScope.GLOBAL and self.owner_id is not None:
            raise InvalidDefinition("a global preset carries no owner_id")
        if self.scope != PresetScope.GLOBAL and not (self.owner_id or "").strip():
            raise InvalidDefinition(f"a {self.scope} preset requires owner_id")

    @property
    def uniqueness_key(self) -> tuple[str, str | None, str, str]:
        """§6.1: name uniqueness is scoped to the owner, never global."""
        return (self.scope, self.owner_id, self.plugin, self.name.strip().lower())


class SettingSpec(Protocol):
    """The duck-typed schema surface ``validate_preset_settings``
    consumes — satisfied by ``core.settings_manager.Setting`` without
    importing it (this module stays PURE: no framework imports)."""

    @property
    def name(self) -> str: ...

    def validate(self, value: Any) -> Any: ...


def validate_preset_settings(
    settings: dict[str, Any], schema: Iterable[SettingSpec] | None
) -> dict[str, Any]:
    """Validate preset values against a plugin's Setting schema.

    ``schema`` is any sequence of Setting-like objects exposing
    ``name`` and ``validate(value)`` (the core settings contract).
    Every preset key must exist in the schema (a template may not
    invent settings the plugin never declared); every value must
    pass its validator. Returns the validated dict.
    """
    if not isinstance(settings, dict):
        raise InvalidDefinition(f"preset settings must be a dict, got {type(settings).__name__}")
    by_name = {getattr(spec, "name", ""): spec for spec in (schema or ())}
    validated: dict[str, Any] = {}
    for key, value in settings.items():
        spec = by_name.get(key)
        if spec is None:
            raise InvalidDefinition(
                f"preset key {key!r} is not in the plugin's settings schema "
                "(templates may not invent settings)"
            )
        try:
            validated[key] = spec.validate(value)
        except Exception as exc:
            raise InvalidDefinition(f"preset key {key!r} failed schema validation: {exc}") from exc
    return validated


def compute_apply_diff(
    preset_settings: dict[str, Any], current_settings: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """The §6.3 apply diff: old→new for the PRESET'S KEYS ONLY.

    Keys the preset does not mention are untouched by apply (locked
    D.3 semantics) and therefore absent from the diff.
    """
    return {key: (current_settings.get(key), value) for key, value in preset_settings.items()}


def preview_revision_ok(preview_revision: str, current_revision: str) -> None:
    """Decision 16's guard: raise :class:`StalePreview` when the
    account's configuration revision moved after the preview."""
    if current_revision != preview_revision:
        raise StalePreview(preview_revision, current_revision)


def apply_diff(
    preset_settings: dict[str, Any],
    current_settings: dict[str, Any],
    *,
    preview_revision: str,
    current_revision: str,
    preset: PresetRecord,
    applied_at: float,
) -> tuple[dict[str, Any], Provenance]:
    """Apply a validated preset onto an account's settings — PURE.

    Returns ``(new_settings, provenance)``. Raises
    :class:`StalePreview` when the revision moved (the caller
    re-previews); callers MUST persist the result and the provenance
    in ONE transaction for the atomicity decision 16 requires.
    """
    preview_revision_ok(preview_revision, current_revision)
    new_settings = dict(current_settings)
    # the locked semantics: add/overwrite the preset's keys only
    new_settings.update(preset_settings)
    provenance = Provenance(
        source_preset_id=preset.preset_id,
        source_version=preset.version,
        applied_at=applied_at,
    )
    return new_settings, provenance


def detach(provenance: Provenance | None) -> Provenance | None:
    """§6.3 detach: the account KEEPS its values; the provenance link
    drops. Modeled as returning None (no source) — the pure mirror
    of the persisted detach."""
    del provenance
    return None
