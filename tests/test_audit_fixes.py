"""Regression tests for the 15-agent audit findings (fixes marked AUDIT FIX)."""
from __future__ import annotations

import asyncio
import math
import unittest

from core.account import AccountConfig, AccountError, AccountSession, AccountStatus, ReconnectPolicy
from core.audit import AuditRecorder
from core.config import load_config
from core.credentials import Credentials
from core.events import EventBus, MessageEvent
from core.rate_limiter import ManualClock, Priority, RateLimiterService, RateRule, RateRuleError
from core.settings import SettingDef, SettingValidationError, SettingsStore
from core.simulator import FakeWorld
from core.warnings import evaluate_limiter

from .helpers import TEST_CHANNEL, Harness


class TestSettingsAuditFixes(unittest.TestCase):
    DEFS = (
        SettingDef("n", "int", "5"),        # str default for an int setting
        SettingDef("b", "bool", "true"),    # str default for a bool setting
        SettingDef("f", "float", 1.0, min_value=0.0),
    )

    def test_defaults_are_coerced(self) -> None:
        resolved = SettingsStore().resolve("p", self.DEFS, account="a")
        self.assertEqual(resolved["n"], 5)
        self.assertIs(resolved["b"], True)
        self.assertEqual(resolved["f"], 1.0)

    def test_infinity_and_nan_rejected_cleanly(self) -> None:
        with self.assertRaises(SettingValidationError):
            SettingDef("x", "int", 1).validate(float("inf"))
        with self.assertRaises(SettingValidationError):
            SettingDef("x", "float", 1.0).validate(float("nan"))

    def test_str_values_pass_isinf_guard(self) -> None:
        self.assertEqual(SettingDef("s", "str", "ok").validate("fine"), "fine")

    def test_bad_value_error_carries_plugin_path(self) -> None:
        store = SettingsStore(global_settings={"p": {"n": "junk"}})
        with self.assertRaises(SettingValidationError) as ctx:
            store.resolve("p", self.DEFS, account="a")
        self.assertIn("p.n", str(ctx.exception))
        self.assertEqual(ctx.exception.path, "p.n")


class TestEventBusAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def test_internal_cancellederror_is_isolated(self) -> None:
        bus = EventBus()
        ran: list[str] = []

        async def cancelled_handler(event):
            raise asyncio.CancelledError("inner cancel")

        async def after(event):
            ran.append("after")

        bus.subscribe(MessageEvent, cancelled_handler)
        bus.subscribe(MessageEvent, after)
        results = await bus.publish(MessageEvent(account="a"))
        self.assertEqual(ran, ["after"])  # later handler still ran
        self.assertIsInstance(results[0], asyncio.CancelledError)

    async def test_publish_nowait_keeps_strong_reference(self) -> None:
        bus = EventBus()
        seen: list = []
        bus.subscribe(MessageEvent, lambda e: seen.append(e.account))
        import gc

        task = bus.publish_nowait(MessageEvent(account="x"))
        gc.collect()  # would collect an unreferenced task
        await task
        self.assertEqual(seen, ["x"])
        self.assertFalse(bus._background)  # ref released after completion


class TestRateLimiterAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_honors_amount(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "10/10s"})
        grant = await limiter.acquire("message", "alpha", amount=4.0)
        self.assertEqual(grant.amount, 4.0)
        self.assertAlmostEqual(
            limiter.stats()["actions"]["message"]["global_bucket"]["tokens"], 6.0, places=2
        )

    async def test_amount_over_capacity_rejected(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "2/10s"})
        with self.assertRaises(RateRuleError):
            await limiter.acquire("message", "alpha", amount=3.0)
        with self.assertRaises(RateRuleError):
            limiter.try_acquire("message", "alpha", amount=-1)

    def test_non_finite_rules_rejected(self) -> None:
        with self.assertRaises(RateRuleError):
            RateRule.parse("1/" + "9" * 400)
        with self.assertRaises(RateRuleError):
            RateRule.parse("9" * 400 + "/1s")

    def test_delay_bounds_validated(self) -> None:
        from core.rate_limiter import MAX_DELAY_SECONDS

        limiter = RateLimiterService(default_rules={"message": "3/12s"})
        with self.assertRaises(RateRuleError):  # STRESS FIX: huge delay wedges fleet
            limiter.set_delay("message", 1e9, 2e9)
        with self.assertRaises(RateRuleError):  # STRESS FIX: non-finite bounds
            limiter.set_delay("message", float("inf"), float("inf"))
        with self.assertRaises(RateRuleError):
            limiter.set_delay("message", -0.1, 1.0)
        limiter.set_delay("message", 0.5, MAX_DELAY_SECONDS)  # cap itself is fine

    def test_nan_amount_rejected(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "3/12s"})
        with self.assertRaises(RateRuleError):  # STRESS FIX: NaN poisoned buckets
            limiter.try_acquire("message", "alpha", amount=float("nan"))
        # bucket state stays healthy after the rejection
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))

    def test_stats_refills_to_now(self) -> None:
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "3/10s"})
        limiter.try_acquire("message", "alpha")
        clock.advance(5.0)
        tokens = limiter.stats()["actions"]["message"]["global_bucket"]["tokens"]
        self.assertAlmostEqual(tokens, 3.0, places=3)  # refilled, not stale 2.0


class TestAccountAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_connect_is_honored(self) -> None:
        world = FakeWorld(mudae_user_id=1)
        from core.simulator import FakeClient

        session = AccountSession(
            AccountConfig(name="alpha"), FakeClient(world, 2001), EventBus(),
            RateLimiterService(default_rules={"message": "100/1s"}),
        )
        started = asyncio.create_task(session.start())
        await asyncio.sleep(0.01)
        await session.stop()          # races the in-flight connect
        await asyncio.sleep(0.05)
        await started
        await asyncio.sleep(0.05)
        self.assertEqual(session.status, AccountStatus.STOPPED)  # not RUNNING

    async def test_double_start_rejected(self) -> None:
        world = FakeWorld(mudae_user_id=1)
        from core.simulator import FakeClient

        session = AccountSession(
            AccountConfig(name="alpha"), FakeClient(world, 2001), EventBus(),
            RateLimiterService(default_rules={"message": "100/1s"}),
        )
        started = asyncio.create_task(session.start())
        await asyncio.sleep(0.01)
        with self.assertRaises(AccountError):  # CONNECTING counts as started
            await session.start()
        await started

    def test_config_robustness(self) -> None:
        with self.assertRaises(AccountError):
            AccountConfig(name=123)                       # non-string name
        with self.assertRaises(AccountError):
            AccountConfig.from_dict({"roles": "admin"})   # missing name
        with self.assertRaises(AccountError):
            AccountConfig.from_dict({"name": "alpha", "roles": "admin"})  # roles as string
        with self.assertRaises(AccountError):
            ReconnectPolicy.from_dict({"totally_unknown_key": 5})  # unknown policy key


class TestContainerAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def test_remove_running_account_leaves_no_side_effects(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"])
        await harness.container.start_account("alpha")
        with self.assertRaises(Exception):
            harness.container.remove_account("alpha")  # refused
        runtime = harness.container._accounts["alpha"]
        self.assertFalse(runtime.stop_requested)       # no premature mutations
        self.assertEqual(runtime.session.status.value, "running")
        self.assertIn("alpha", harness.container.account_names())
        await harness.container.stop_account("alpha")

    async def test_stop_is_idempotent_for_hooks(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"])
        spy = harness.fixture_module("spy")
        spy.reset()
        await harness.container.start_account("alpha")
        await harness.container.stop_account("alpha")
        await harness.container.stop_account("alpha")   # second stop: no hooks
        self.assertEqual(spy.CALLS.count("disable:alpha"), 1)

    async def test_wait_for_returns_on_first_match(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"])
        await harness.container.start_account("alpha")

        async def deliver_soon():
            await asyncio.sleep(0.02)
            harness.world.deliver(TEST_CHANNEL, harness.owner_id, "match")

        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.create_task(deliver_soon())
        found = await harness.container.wait_for(
            "alpha", lambda e: e.content == "match", timeout=2.0
        )
        elapsed = loop.time() - started
        self.assertIsNotNone(found)
        self.assertLess(elapsed, 1.0)  # returned immediately, no idle window
        await task
        await harness.container.stop_account("alpha")


class TestWarningsAuditFixes(unittest.TestCase):
    def test_override_only_action_is_reviewed(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "3/12s"})
        limiter.set_delay("message", 0.8, 2.4)
        limiter.set_account_rule("alpha", "react", "4/2s")  # no global default
        warnings = evaluate_limiter(limiter)
        self.assertTrue(any(w.code == "below_floor" and "alpha:react" in w.message for w in warnings))

    def test_disabled_account_delay_flagged(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "3/12s"})
        limiter.set_delay("message", 0.8, 2.4)
        limiter.set_delay("message", 0.0, 0.0, account="alpha")  # override: off
        warnings = evaluate_limiter(limiter)
        self.assertTrue(any(w.code == "no_human_delay" and "alpha" in w.message for w in warnings))

    def test_narrow_delay_boundary_is_stable(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "3/12s"})
        limiter.set_delay("message", 0.8, 1.1)  # spread 0.30000000000000004
        self.assertFalse(any(w.code == "narrow_delay" for w in evaluate_limiter(limiter)))


class TestAuditApiFixes(unittest.TestCase):
    def test_tail_zero_returns_empty(self) -> None:
        recorder = AuditRecorder()
        recorder.record("a", "out", "message", 1, "x")
        self.assertEqual(recorder.tail(0), [])
        self.assertEqual(recorder.tail(-3), [])

    def test_history_shape_normalized(self) -> None:
        class FakeDB:
            def audit_log(self, limit=200, account=None):
                return [{"id": 7, "ts": 1234.56789, "account": "a", "direction": "out",
                         "action": "message", "channel_id": 1, "content": "x",
                         "wait_ms": 0, "delay_ms": 0, "note": None}]

        entries = AuditRecorder(database=FakeDB()).history()
        self.assertNotIn("id", entries[0])
        self.assertEqual(entries[0]["ts"], 1234.568)


class TestCredentialsConfigFixes(unittest.TestCase):
    def test_short_tokens_fully_masked(self) -> None:
        creds = Credentials.from_values({"DISCORD_TOKEN_X": "abc"})
        self.assertEqual(creds.redacted()["DISCORD_TOKEN_X"], "***")

    def test_non_dict_json_rejected_with_filename(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "rate_limits.json").write_text('["message"]', encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_config(base)
            self.assertIn("rate_limits.json", str(ctx.exception))


class TestSimulatorAuditFixes(unittest.IsolatedAsyncioTestCase):
    async def test_cached_snapshot_no_dm_leak(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha", "beta"])
        await harness.container.start_account("alpha")
        await harness.container.start_account("beta")
        mudae_dm = harness.world.dm_channel_for(harness.user_ids["alpha"], harness.mudae_id)
        beta_snapshot = harness.session("beta").cached_snapshot()
        leaked = [d for d in beta_snapshot["dm_channels"] if d["id"] == mudae_dm]
        self.assertEqual(leaked, [])  # beta must not see alpha's DM
        alpha_snapshot = harness.session("alpha").cached_snapshot()
        self.assertTrue(any(d["id"] == mudae_dm for d in alpha_snapshot["dm_channels"]))
        await harness.container.stop_all()

    def test_dm_id_collision_resolved(self) -> None:
        world = FakeWorld(mudae_user_id=1)
        first = world.dm_channel_for(100, 8100)   # hash-colliding pair
        second = world.dm_channel_for(101, 181)
        self.assertNotEqual(first, second)        # sequential fallback engaged


class TestTextutilsAuditFixes(unittest.TestCase):
    def test_budget_boundary_fits_one_char(self) -> None:
        from plugins.soulcopy.textutils import split_into_chunks

        # budget = 2 - len("P") = 1: exactly one char per chunk fits
        chunks = split_into_chunks("ab", 2, "P")
        self.assertEqual(chunks, ["a", "b"])

    def test_budget_below_one_char_raises(self) -> None:
        from plugins.soulcopy.textutils import split_into_chunks

        with self.assertRaises(ValueError):
            split_into_chunks("ab", 1, "P")


class TestLoaderAuditFixes(unittest.TestCase):
    def test_unicode_plugin_name_rejected(self) -> None:
        import sys
        from pathlib import Path

        from core.plugin_loader import PluginLoadError, PluginLoader

        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugins"
            (plugin_dir / "café").mkdir(parents=True)
            (plugin_dir / "café" / "__init__.py").write_text(
                "from core.plugin_base import PluginBase\n"
                "class Café(PluginBase):\n"
                "    name = 'café'\n",
                encoding="utf-8",
            )
            sys.path.insert(0, str(root))
            try:
                with self.assertRaises(PluginLoadError):
                    PluginLoader(plugin_dir).discover()
            finally:
                sys.path.remove(str(root))


import tempfile  # noqa: E402 - used inside test bodies above


if __name__ == "__main__":
    unittest.main()
