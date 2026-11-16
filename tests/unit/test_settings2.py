"""Round-2 hostile audit coverage for core.settings_manager (agent 12/15).

Three-layer merge PROOF with deliberately contradicting layers,
SettingChanged exactly-once per set (including repo-failure and
concurrent-set sabotage), explicit-None vs missing-key semantics, and
schema coercion edge shapes.
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from typing import Any

from core.errors import SettingsValidationError
from core.event_bus import EventBus, SettingChanged
from core.settings_manager import Setting, SettingsManager

SCHEMA = (
    Setting("gain", "int", 1),
    Setting("label", "str", "default"),
    Setting("ratio", "float", 0.5, min_value=0.0, max_value=2.0),
    Setting("enabled", "bool", True),
)


class FakePluginRepo:
    def __init__(self, layers: dict[str, dict[str, dict[str, Any]]] | None = None) -> None:
        self.layers = layers or {}
        self.writes: list[tuple[str, str, str, Any]] = []

    async def all_account_settings(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {a: {p: dict(v) for p, v in pl.items()} for a, pl in self.layers.items()}

    async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> None:
        self.writes.append((account, plugin, key, value))


class DeletablePluginRepo(FakePluginRepo):
    """Repo shape once F9 lands PluginRepository.delete_setting."""

    def __init__(self, layers: dict[str, dict[str, dict[str, Any]]] | None = None) -> None:
        super().__init__(layers)
        self.deletes: list[tuple[str, str, str]] = []

    async def delete_setting(self, account: str, plugin: str, key: str) -> None:
        self.deletes.append((account, plugin, key))
        self.layers.get(account, {}).get(plugin, {}).pop(key, None)


class BoomPluginRepo:
    async def all_account_settings(self) -> dict[str, Any]:
        return {}

    async def set_setting(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")


class FakeAccountRepo:
    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows = rows or {}
        self.writes: list[tuple[str, str, Any]] = []

    async def list_accounts(self) -> list[str]:
        return sorted(self.rows)

    async def get(self, account: str) -> dict[str, Any]:
        return dict(self.rows.get(account, {}))

    async def set(self, account: str, key: str, value: Any) -> None:
        self.writes.append((account, key, value))


class Events:
    """SettingChanged collector: sync handler + drain() for nowait tasks."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.seen: list[SettingChanged] = []
        self._cursor = 0
        bus.subscribe(SettingChanged, self.seen.append)

    async def settle(self) -> list[SettingChanged]:
        await self.bus.drain(timeout=2.0)
        fresh = self.seen[self._cursor :]
        self._cursor = len(self.seen)
        return list(fresh)


class TestThreeLayerMergeProof(unittest.IsolatedAsyncioTestCase):
    async def test_contradicting_layers_account_beats_global_beats_default(self) -> None:
        bus = EventBus()
        repo = FakePluginRepo()
        mgr = SettingsManager(
            bus,
            global_layer={"p": {"gain": 2, "label": "global"}},
            plugin_repo=repo,
        )
        await mgr.load()
        bare = SettingsManager(EventBus(), plugin_repo=FakePluginRepo())
        await bare.load()
        defaults = bare.resolve("p", SCHEMA)  # schema layer alone
        self.assertEqual(defaults["gain"], 1)
        self.assertEqual(defaults["label"], "default")
        await mgr.set_plugin_setting("acct", "p", "gain", 3, SCHEMA)
        await mgr.set_plugin_setting("acct", "p", "ratio", 1.5, SCHEMA)
        effective = mgr.resolve("p", SCHEMA, account="acct")
        self.assertEqual(effective["gain"], 3)  # account layer
        self.assertEqual(effective["label"], "global")  # global layer
        self.assertEqual(effective["ratio"], 1.5)
        self.assertEqual(effective["enabled"], True)  # schema default
        no_account = mgr.resolve("p", SCHEMA)
        self.assertEqual(no_account["gain"], 2)  # global wins
        self.assertEqual(no_account["ratio"], 0.5)

    async def test_merge_is_per_key_not_layer_replacement(self) -> None:
        bus = EventBus()
        repo = FakePluginRepo()
        mgr = SettingsManager(
            bus,
            global_layer={"p": {"gain": 2, "label": "global"}},
            plugin_repo=repo,
        )
        await mgr.load()
        await mgr.set_plugin_setting("acct", "p", "ratio", 1.5, SCHEMA)
        effective = mgr.resolve("p", SCHEMA, account="acct")
        self.assertEqual(effective["gain"], 2)  # global key survives
        self.assertEqual(effective["ratio"], 1.5)  # account key applied

    async def test_unknown_key_in_any_layer_raises_with_layer_named(self) -> None:
        bus = EventBus()
        mgr_bad_global = SettingsManager(bus, global_layer={"p": {"gain": 2, "ghost": 1}})
        with self.assertRaises(SettingsValidationError) as ctx:
            mgr_bad_global.resolve("p", SCHEMA)
        self.assertIn("global layer", str(ctx.exception))
        repo = FakePluginRepo(layers={"acct": {"p": {"ghost": 1}}})
        mgr_bad_account = SettingsManager(bus, plugin_repo=repo)
        await mgr_bad_account.load()
        with self.assertRaises(SettingsValidationError) as ctx:
            mgr_bad_account.resolve("p", SCHEMA, account="acct")
        self.assertIn("account 'acct' layer", str(ctx.exception))

    async def test_bad_value_error_names_the_layer(self) -> None:
        bus = EventBus()
        mgr = SettingsManager(bus, global_layer={"p": {"gain": "not-a-number"}})
        with self.assertRaises(SettingsValidationError) as ctx:
            mgr.resolve("p", SCHEMA)
        self.assertIn("global layer", str(ctx.exception))


class TestSettingChangedExactlyOnce(unittest.IsolatedAsyncioTestCase):
    async def test_one_event_per_successful_set(self) -> None:
        bus = EventBus()
        events = Events(bus)
        mgr = SettingsManager(bus, plugin_repo=FakePluginRepo())
        await mgr.load()
        await mgr.set_plugin_setting("a", "p", "gain", 7, SCHEMA)
        await mgr.set_plugin_setting("a", "p", "gain", 8, SCHEMA)
        settled = await events.settle()
        gains = [e for e in settled if e.key == "gain"]
        self.assertEqual(len(gains), 2)  # exactly-once each
        self.assertEqual([g.new_value for g in gains], [7, 8])
        self.assertEqual(gains[0].old_value, None)
        self.assertEqual(gains[1].old_value, 7)
        self.assertEqual(gains[1].account, "a")

    async def test_validation_failure_emits_nothing(self) -> None:
        bus = EventBus()
        events = Events(bus)
        mgr = SettingsManager(bus, plugin_repo=FakePluginRepo())
        await mgr.load()
        with self.assertRaises(SettingsValidationError):
            await mgr.set_plugin_setting("a", "p", "gain", 99.5, SCHEMA)
        with self.assertRaises(SettingsValidationError):
            await mgr.set_plugin_setting("a", "p", "ghost", 1, SCHEMA)
        self.assertEqual(await events.settle(), [])
        self.assertEqual(mgr.resolve("p", SCHEMA, account="a")["gain"], 1)

    async def test_repo_failure_no_event_memory_unchanged(self) -> None:
        bus = EventBus()
        events = Events(bus)
        mgr = SettingsManager(bus, plugin_repo=BoomPluginRepo())
        await mgr.load()
        with self.assertRaises(RuntimeError):
            await mgr.set_plugin_setting("a", "p", "gain", 5, SCHEMA)
        self.assertEqual(await events.settle(), [])  # nothing emitted
        self.assertEqual(mgr.resolve("p", SCHEMA, account="a")["gain"], 1)

    async def test_concurrent_sets_same_key_each_exactly_once(self) -> None:
        bus = EventBus()
        events = Events(bus)
        repo = FakePluginRepo()
        mgr = SettingsManager(bus, plugin_repo=repo)
        await mgr.load()
        await asyncio.gather(
            mgr.set_plugin_setting("a", "p", "gain", 10, SCHEMA),
            mgr.set_plugin_setting("a", "p", "gain", 20, SCHEMA),
        )
        settled = await events.settle()
        gains = [e for e in settled if e.key == "gain"]
        self.assertEqual(len(gains), 2)  # no lost/dup events
        self.assertEqual({g.new_value for g in gains}, {10, 20})
        self.assertEqual(len(repo.writes), 2)  # both persisted
        final = mgr.resolve("p", SCHEMA, account="a")["gain"]
        self.assertIn(final, (10, 20))  # one coherent winner

    async def test_account_setting_event_exactly_once(self) -> None:
        bus = EventBus()
        events = Events(bus)
        mgr = SettingsManager(bus, account_settings_repo=FakeAccountRepo())
        await mgr.load()
        await mgr.set_account_setting("a", "reconnect_initial_delay", 2.0)
        settled = await events.settle()
        matched = [e for e in settled if e.key == "reconnect_initial_delay"]
        self.assertEqual(len(matched), 1)
        with self.assertRaises(SettingsValidationError):
            await mgr.set_account_setting("a", "bogus", 1)
        self.assertEqual(len(await events.settle()), 0)


class TestNoneVsMissing(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_none_resets_to_schema_default_not_lower_layer(self) -> None:
        """PINNED semantics: an explicit null in a HIGHER layer coerces to
        the SCHEMA default (validate(None) -> default) — it does NOT fall
        through to the lower layer's value."""
        bus = EventBus()
        repo = FakePluginRepo(layers={"acct": {"p": {"gain": None}}})
        mgr = SettingsManager(bus, global_layer={"p": {"gain": 2}}, plugin_repo=repo)
        await mgr.load()
        self.assertEqual(mgr.resolve("p", SCHEMA, account="acct")["gain"], 1)
        no_layer = mgr.resolve("p", SCHEMA)  # missing key path
        self.assertEqual(no_layer["gain"], 2)  # global wins

    async def test_missing_key_falls_through_but_present_none_does_not(self) -> None:
        bus = EventBus()
        repo = FakePluginRepo(
            layers={
                "only_ratio": {"p": {"ratio": None}},
                "absent": {"other": {}},
            }
        )
        mgr = SettingsManager(bus, global_layer={"p": {"ratio": 1.25}}, plugin_repo=repo)
        await mgr.load()
        self.assertEqual(mgr.resolve("p", SCHEMA, account="only_ratio")["ratio"], 0.5)
        self.assertEqual(  # nothing stored
            mgr.resolve("p", SCHEMA, account="absent")["ratio"], 1.25
        )

    async def test_str_setting_with_none_default_round_trips(self) -> None:
        bus = EventBus()
        schema = (Setting("note", "str", None),)
        mgr = SettingsManager(bus)
        self.assertIsNone(mgr.resolve("p", schema)["note"])


class TestCoercionEdges(unittest.TestCase):
    def test_bool_string_shapes(self) -> None:
        s = Setting("b", "bool", False)
        for raw in ("1", "true", "yes", "on", "TRUE", " Yes "):
            self.assertIs(s.validate(raw), True, raw)
        for raw in ("0", "false", "no", "off", "OFF"):
            self.assertIs(s.validate(raw), False, raw)
        for bad in ("maybe", 2, 1, 0, 0.5, [True]):
            with self.assertRaises(SettingsValidationError, msg=repr(bad)):
                s.validate(bad)
        self.assertIs(s.validate(None), False)  # -> default
        self.assertIs(s.validate(True), True)

    def test_int_edges(self) -> None:
        s = Setting("i", "int", 0)
        self.assertEqual(s.validate("5"), 5)
        self.assertEqual(s.validate("  7  "), 7)
        self.assertEqual(s.validate(5.0), 5)
        self.assertEqual(s.validate(-3), -3)
        for bad in (5.5, True, False, "5.5", "x", None.__class__, ("1",)):
            with self.assertRaises(SettingsValidationError, msg=repr(bad)):
                s.validate(bad)

    def test_float_edges(self) -> None:
        s = Setting("f", "float", 0.0)
        self.assertEqual(s.validate("1e2"), 100.0)
        self.assertEqual(s.validate(" 1.5 "), 1.5)
        self.assertEqual(s.validate(3), 3.0)
        for bad in ("nan", float("nan"), "inf", float("inf"), "-inf", True, "x", [1.0]):
            with self.assertRaises(SettingsValidationError, msg=repr(bad)):
                s.validate(bad)

    def test_bounds_enforced_both_sides(self) -> None:
        s = Setting("r", "float", 1.0, min_value=0.0, max_value=2.0)
        self.assertEqual(s.validate(0.0), 0.0)
        self.assertEqual(s.validate(2.0), 2.0)
        for bad in (-0.01, 2.01, "-1", "3"):
            with self.assertRaises(SettingsValidationError, msg=repr(bad)):
                s.validate(bad)

    def test_str_coerces_non_string(self) -> None:
        s = Setting("t", "str", "d")
        self.assertEqual(s.validate(123), "123")
        self.assertEqual(s.validate(None), "d")

    def test_str_length_capped_at_schema_default(self) -> None:
        from core.settings_manager import STR_MAX_LENGTH

        s = Setting("t", "str", "d")
        self.assertEqual(len(s.validate("x" * STR_MAX_LENGTH)), STR_MAX_LENGTH)
        with self.assertRaises(SettingsValidationError):
            s.validate("x" * (STR_MAX_LENGTH + 1))
        with self.assertRaises(SettingsValidationError):  # default too
            Setting("big", "str", "y" * (STR_MAX_LENGTH + 1))

    def test_str_length_override_per_setting(self) -> None:
        s = Setting("t", "str", "d", max_length=10)
        self.assertEqual(s.validate("x" * 10), "x" * 10)
        with self.assertRaises(SettingsValidationError):
            s.validate("x" * 11)
        unlimited = Setting("t", "str", "d", max_length=10**9)
        self.assertEqual(len(unlimited.validate("x" * 5000)), 5000)

    def test_construction_rejects_bad_shapes(self) -> None:
        with self.assertRaises(SettingsValidationError):
            Setting("bad-name", "int", 1)
        with self.assertRaises(SettingsValidationError):
            Setting("ok", "vector", 1)
        with self.assertRaises(SettingsValidationError):
            Setting("ok", "str", "x", min_value=1)
        with self.assertRaises(SettingsValidationError):
            Setting("ok", "int", 99, min_value=0, max_value=10)
        Setting("ok_name", "int", 1)  # underscore fine


class TestClearPluginSetting(unittest.IsolatedAsyncioTestCase):
    """Round-3: null reset must DELETE the account-layer row (unpin),
    not persist the schema default as a mask over the global layer."""

    async def test_clear_unmasks_global_layer(self) -> None:
        bus = EventBus()
        events = Events(bus)
        repo = DeletablePluginRepo()
        mgr = SettingsManager(bus, global_layer={"p": {"gain": 2}}, plugin_repo=repo)
        await mgr.load()
        await mgr.set_plugin_setting("a", "p", "gain", 3, SCHEMA)
        self.assertEqual(mgr.resolve("p", SCHEMA, account="a")["gain"], 3)
        effective = await mgr.clear_plugin_setting("a", "p", "gain", SCHEMA)
        self.assertEqual(effective, 2)  # global layer unmasked
        self.assertEqual(mgr.resolve("p", SCHEMA, account="a")["gain"], 2)
        self.assertEqual(repo.deletes, [("a", "p", "gain")])
        settled = await events.settle()
        self.assertEqual(len(settled), 2)  # set + clear, once each
        cleared = [e for e in settled if e.new_value == 2]
        self.assertEqual(cleared[0].old_value, 3)

    async def test_clear_without_delete_method_clears_memory_with_warning(self) -> None:
        bus = EventBus()
        mgr = SettingsManager(
            bus,
            logging.getLogger("t-clear"),
            global_layer={"p": {"gain": 2}},
            plugin_repo=FakePluginRepo(),
        )
        await mgr.load()
        await mgr.set_plugin_setting("a", "p", "gain", 3, SCHEMA)
        with self.assertLogs("t-clear", level="WARNING"):
            await mgr.clear_plugin_setting("a", "p", "gain", SCHEMA)
        self.assertEqual(mgr.resolve("p", SCHEMA, account="a")["gain"], 2)

    async def test_clear_unknown_key_raises_only_with_schema(self) -> None:
        bus = EventBus()
        mgr = SettingsManager(bus, plugin_repo=DeletablePluginRepo())
        await mgr.load()
        with self.assertRaises(SettingsValidationError):
            await mgr.clear_plugin_setting("a", "p", "ghost", SCHEMA)
        # schema-less clear (api fallback path) is permissive
        effective = await mgr.clear_plugin_setting("a", "p", "ghost")
        self.assertIsNone(effective)

    async def test_clear_missing_row_is_noop_delete_still_sent(self) -> None:
        bus = EventBus()
        events = Events(bus)
        repo = DeletablePluginRepo()
        mgr = SettingsManager(bus, plugin_repo=repo)
        await mgr.load()
        effective = await mgr.clear_plugin_setting("a", "p", "gain", SCHEMA)
        self.assertEqual(effective, 1)  # schema default
        self.assertEqual(repo.deletes, [("a", "p", "gain")])
        self.assertEqual(len(await events.settle()), 1)

    async def test_layers_view_drops_emptied_overrides(self) -> None:
        bus = EventBus()
        mgr = SettingsManager(bus, plugin_repo=DeletablePluginRepo())
        await mgr.load()
        await mgr.set_plugin_setting("a", "p", "gain", 3, SCHEMA)
        self.assertIn("a", mgr.layers_view()["accounts"])
        await mgr.clear_plugin_setting("a", "p", "gain", SCHEMA)
        self.assertEqual(mgr.layers_view()["accounts"], {})


class TestAccountSettingsLayer(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_and_invalid_stored_keys_ignored_with_warning(self) -> None:
        bus = EventBus()
        repo = FakeAccountRepo(
            rows={
                "a": {
                    "reconnect_max_delay": 999999.0,  # violates max 600
                    "bogus_key": 1,
                }
            }
        )
        mgr = SettingsManager(bus, logging.getLogger("t"), account_settings_repo=repo)
        await mgr.load()
        with self.assertLogs("t", level="WARNING") as caught:
            resolved = mgr.account_settings("a")
        self.assertEqual(resolved["reconnect_max_delay"], 60.0)  # default
        self.assertNotIn("bogus_key", resolved)
        self.assertTrue(any("bogus_key" in line for line in caught.output))

    async def test_valid_overrides_applied(self) -> None:
        bus = EventBus()
        repo = FakeAccountRepo(rows={"a": {"reconnect_initial_delay": 5.0}})
        mgr = SettingsManager(bus, account_settings_repo=repo)
        await mgr.load()
        self.assertEqual(mgr.account_settings("a")["reconnect_initial_delay"], 5.0)
        self.assertEqual(mgr.account_settings("ghost")["reconnect_multiplier"], 2.0)

    async def test_per_account_read_failure_logs_and_defaults(self) -> None:
        class LockedRepo(FakeAccountRepo):
            async def get(self, account: str) -> dict[str, Any]:
                raise RuntimeError("db locked")

        bus = EventBus()
        mgr = SettingsManager(
            bus, logging.getLogger("t-load"), account_settings_repo=LockedRepo(rows={"a": {}})
        )
        with self.assertLogs("t-load", level="ERROR"):
            await mgr.load()
        self.assertEqual(  # defaults still resolve for that account
            mgr.account_settings("a")["reconnect_multiplier"], 2.0
        )

    async def test_forget_account_drops_memory_layers(self) -> None:
        bus = EventBus()
        repo = FakePluginRepo()
        mgr = SettingsManager(bus, plugin_repo=repo)
        await mgr.load()
        await mgr.set_plugin_setting("a", "p", "gain", 9, SCHEMA)
        self.assertEqual(mgr.resolve("p", SCHEMA, account="a")["gain"], 9)
        mgr.forget_account("a")
        self.assertEqual(mgr.resolve("p", SCHEMA, account="a")["gain"], 1)
        mgr.forget_account("ghost")  # no error


if __name__ == "__main__":
    unittest.main()
