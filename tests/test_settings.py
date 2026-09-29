"""Settings manifests: layering, coercion, validation errors."""
from __future__ import annotations

import unittest

from core.settings import SettingDef, SettingValidationError, SettingsStore


class TestSettingDef(unittest.TestCase):
    def test_invalid_type_rejected(self) -> None:
        with self.assertRaises(SettingValidationError):
            SettingDef("x", "yaml")

    def test_invalid_default_rejected(self) -> None:
        with self.assertRaises(SettingValidationError):
            SettingDef("x", "int", 99, min_value=1, max_value=10)

    def test_coercion(self) -> None:
        self.assertEqual(SettingDef("a", "int", 1).validate("42"), 42)
        self.assertEqual(SettingDef("b", "float", 1.0).validate("3.5"), 3.5)
        self.assertIs(SettingDef("c", "bool", True).validate("true"), True)
        self.assertIs(SettingDef("c", "bool", True).validate("0"), False)
        self.assertEqual(SettingDef("d", "str", "x").validate(5), "5")

    def test_bounds(self) -> None:
        bounded = SettingDef("v", "int", 5, min_value=1, max_value=10)
        with self.assertRaises(SettingValidationError) as ctx:
            bounded.validate(11)
        self.assertIn("max", str(ctx.exception))
        with self.assertRaises(SettingValidationError):
            bounded.validate(0)

    def test_bad_coercion_message_contains_key(self) -> None:
        with self.assertRaises(SettingValidationError) as ctx:
            SettingDef("num", "int", 1).validate("not-a-number")
        self.assertIn("num", str(ctx.exception))


class TestSettingsStore(unittest.TestCase):
    DEFS = (
        SettingDef("trigger", "str", "slcopy"),
        SettingDef("delay", "float", 10.0, min_value=0.0),
        SettingDef("chunks", "int", 2000, min_value=50, max_value=2000),
    )

    def test_defaults_only(self) -> None:
        store = SettingsStore()
        resolved = store.resolve("soulcopy", self.DEFS, account="alpha")
        self.assertEqual(resolved, {"trigger": "slcopy", "delay": 10.0, "chunks": 2000})

    def test_global_override(self) -> None:
        store = SettingsStore(global_settings={"soulcopy": {"delay": "2.5"}})
        resolved = store.resolve("soulcopy", self.DEFS, account="alpha")
        self.assertEqual(resolved["delay"], 2.5)

    def test_account_override_wins(self) -> None:
        store = SettingsStore(
            global_settings={"soulcopy": {"delay": 2.5}},
            account_overrides={"alpha": {"soulcopy": {"delay": 0.5}}},
        )
        self.assertEqual(store.resolve("soulcopy", self.DEFS, "alpha")["delay"], 0.5)
        self.assertEqual(store.resolve("soulcopy", self.DEFS, "beta")["delay"], 2.5)

    def test_unknown_key_rejected(self) -> None:
        store = SettingsStore(global_settings={"soulcopy": {"nope": 1}})
        with self.assertRaises(SettingValidationError) as ctx:
            store.resolve("soulcopy", self.DEFS, account="alpha")
        self.assertIn("nope", str(ctx.exception))

    def test_invalid_value_rejected(self) -> None:
        store = SettingsStore(global_settings={"soulcopy": {"chunks": 999999}})
        with self.assertRaises(SettingValidationError):
            store.resolve("soulcopy", self.DEFS, account="alpha")


if __name__ == "__main__":
    unittest.main()
