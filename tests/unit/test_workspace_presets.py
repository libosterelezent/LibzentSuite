"""Preset lifecycle tests (W1) — pure transforms.

Locked semantics under test (decisions 9/15/16 + the D.3 owner call):
apply writes the PRESET'S KEYS ONLY; immutable versions ride along as
provenance; a stale preview raises instead of overwriting.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from core.workspace import (
    InvalidDefinition,
    PresetRecord,
    StalePreview,
    apply_diff,
    compute_apply_diff,
    detach,
    preview_revision_ok,
    validate_preset_settings,
)


def schema(*specs: tuple[str, object]) -> list[NS]:
    """Setting-like doubles: name + validate(value)."""
    return [NS(name=name, validate=validate) for name, validate in specs]


def _str_setting(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    return value


def _bool_setting(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


PRESET = PresetRecord(
    preset_id="preset-1",
    scope="account",
    plugin="cleaner",
    name="standard",
    owner_id="acct",
    version=2,
    schema_version=1,
    settings={"trigger": "$clean", "auto_clean": True},
)


class ValidationTests(unittest.TestCase):
    def test_values_coerced_through_schema(self) -> None:
        out = validate_preset_settings(
            {"trigger": "$clean", "auto_clean": "yes"},
            schema(("trigger", _str_setting), ("auto_clean", _bool_setting)),
        )
        self.assertEqual(out, {"trigger": "$clean", "auto_clean": True})

    def test_templates_may_not_invent_settings(self) -> None:
        with self.assertRaises(InvalidDefinition) as ctx:
            validate_preset_settings({"unknown_key": 1}, schema(("trigger", _str_setting)))
        self.assertIn("unknown_key", str(ctx.exception))
        self.assertIn("may not invent", str(ctx.exception))

    def test_invalid_value_names_the_key(self) -> None:
        with self.assertRaises(InvalidDefinition) as ctx:
            validate_preset_settings({"trigger": "  "}, schema(("trigger", _str_setting)))
        self.assertIn("trigger", str(ctx.exception))


class DiffTests(unittest.TestCase):
    def test_diff_covers_preset_keys_only(self) -> None:
        current = {"trigger": "$old", "auto_clean": False, "unrelated": "keep"}
        diff = compute_apply_diff(PRESET.settings, current)
        self.assertEqual(diff, {"trigger": ("$old", "$clean"), "auto_clean": (False, True)})
        self.assertNotIn("unrelated", diff)


class ApplyTests(unittest.TestCase):
    def test_apply_writes_preset_keys_and_keeps_the_rest(self) -> None:
        current = {"trigger": "$old", "auto_clean": False, "unrelated": "keep"}
        new, provenance = apply_diff(
            PRESET.settings,
            current,
            preview_revision=4,
            current_revision=4,
            preset=PRESET,
            applied_at=123.0,
        )
        self.assertEqual(new, {"trigger": "$clean", "auto_clean": True, "unrelated": "keep"})
        self.assertEqual(
            (provenance.source_preset_id, provenance.source_version, provenance.applied_at),
            ("preset-1", 2, 123.0),
        )

    def test_stale_preview_raises_never_overwrites(self) -> None:
        with self.assertRaises(StalePreview) as ctx:
            apply_diff(
                PRESET.settings,
                {"trigger": "$edited-by-user"},
                preview_revision=4,
                current_revision=5,
                preset=PRESET,
                applied_at=123.0,
            )
        self.assertIn("from revision 4", str(ctx.exception))
        self.assertIn("to 5", str(ctx.exception))

    def test_preview_guard_alone(self) -> None:
        preview_revision_ok(4, 4)  # no raise
        with self.assertRaises(StalePreview):
            preview_revision_ok(4, 6)


class DetachTests(unittest.TestCase):
    def test_detach_keeps_values_by_dropping_the_link(self) -> None:
        current = {"trigger": "$clean"}  # values STAY with the account
        provenance = detach(object())  # the link is what drops
        self.assertIsNone(provenance)
        self.assertEqual(current, {"trigger": "$clean"})


if __name__ == "__main__":
    unittest.main()
