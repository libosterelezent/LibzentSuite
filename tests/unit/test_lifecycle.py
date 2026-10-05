"""PHASE-4 lifecycle governor tests: anti-thrash, bypass, forget, stats."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace as NS

from core.account_manager.lifecycle import (
    OUTCOME_RATE_LIMITED,
    LifecycleDecision,
    LifecycleGovernor,
    LifecyclePolicy,
    _TokenBucket,
)
from core.errors import AccountError

from tests.unit.helpers import make_manager


class _ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TokenBucketTests(unittest.TestCase):
    def test_burst_then_refill(self) -> None:
        clock = _ManualClock()
        bucket = _TokenBucket(3, 5.0, clock)
        for _ in range(3):
            ok, retry = bucket.try_acquire()
            self.assertTrue(ok)
        ok, retry = bucket.try_acquire()
        self.assertFalse(ok)
        self.assertGreater(retry, 0)
        clock.advance(5.0)
        ok, _ = bucket.try_acquire()
        self.assertTrue(ok)

    def test_partial_refill(self) -> None:
        clock = _ManualClock()
        bucket = _TokenBucket(2, 10.0, clock)
        bucket.try_acquire()
        bucket.try_acquire()
        ok, retry = bucket.try_acquire()
        self.assertFalse(ok)
        self.assertAlmostEqual(retry, 10.0, delta=0.01)
        clock.advance(5.0)  # half a token
        ok, retry = bucket.try_acquire()
        self.assertFalse(ok)
        self.assertAlmostEqual(retry, 5.0, delta=0.01)


class GovernorUnitTests(unittest.TestCase):
    def test_policy_validation(self) -> None:
        with self.assertRaises(ValueError):
            LifecyclePolicy(start_burst=0)
        with self.assertRaises(ValueError):
            LifecyclePolicy(restart_cooldown_s=-1)
        with self.assertRaises(ValueError):
            LifecyclePolicy(start_refill_s="fast")

    def test_stop_then_start_blocked_until_restart_cooldown(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        status = NS(value="stopped")
        self.assertTrue(gov.precheck("alpha", "start", status=status).allowed)
        gov.commit("alpha", "stop", ok=True)
        clock.advance(1.0)
        decision = gov.precheck("alpha", "start", status=status)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, OUTCOME_RATE_LIMITED)
        self.assertEqual(decision.reason, "account_cooldown")
        self.assertAlmostEqual(decision.retry_after_s, 9.0, delta=0.1)
        clock.advance(9.5)
        self.assertTrue(gov.precheck("alpha", "start", status=status).allowed)

    def test_stop_is_never_cooldown_gated(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        for _ in range(10):
            self.assertTrue(gov.precheck("alpha", "stop").allowed)
            gov.commit("alpha", "stop", ok=True)
            clock.advance(0.01)

    def test_failed_start_retry_cooldown(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        failed = NS(value="failed")
        gov.commit("alpha", "start", ok=False)  # a failed start happened
        clock.advance(1.0)
        decision = gov.precheck("alpha", "start", status=failed)
        self.assertFalse(decision.allowed)
        self.assertAlmostEqual(decision.retry_after_s, 14.0, delta=0.1)

    def test_invalid_credential_escalates_on_streak(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        invalid = NS(value="invalid_credential")
        gov.commit("alpha", "start", ok=False, invalid_credential=True)
        clock.advance(20.0)  # past the 15s short retry window
        # streak == 1: still the short window — a single bad classification
        # must not lock the account out for 5 minutes
        self.assertTrue(gov.precheck("alpha", "start", status=invalid).allowed)
        # a SECOND consecutive invalid credential escalates the lockout
        gov.commit("alpha", "start", ok=False, invalid_credential=True)
        clock.advance(20.0)  # past the short window again
        decision = gov.precheck("alpha", "start", status=invalid)
        self.assertFalse(decision.allowed)
        self.assertAlmostEqual(decision.retry_after_s, 280.0, delta=0.1)
        clock.advance(300.0)
        self.assertTrue(gov.precheck("alpha", "start", status=invalid).allowed)

    def test_bypass_and_disabled(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        gov.commit("alpha", "stop", ok=True)
        self.assertTrue(gov.precheck("alpha", "start", bypass=True).allowed)
        disabled = LifecycleGovernor(clock=clock, enabled=False)
        disabled.commit("alpha", "stop", ok=True)
        self.assertTrue(disabled.precheck("alpha", "start").allowed)

    def test_forget_clears_pacer(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        gov.commit("alpha", "stop", ok=True)
        gov.forget("alpha")
        self.assertTrue(gov.precheck("alpha", "start").allowed)

    def test_stats_shape(self) -> None:
        gov = LifecycleGovernor()
        stats = gov.stats()
        self.assertIn("counters", stats)
        self.assertIn("policy", stats)
        self.assertEqual(stats["policy"]["start_burst"], 5)

    def test_stats_and_retry_after_never_consume_tokens(self) -> None:
        """Round-4 (probe1b): observation must not drain the shared start
        bucket — stats()/retry_after() peek read-only; only precheck()
        (the production gate) spends capacity."""
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        for name in ("a", "b", "c", "d", "e"):
            gov.commit(name, "start", ok=True)  # start-legal pacers

        self.assertTrue(gov.precheck("fresh", "start").allowed)  # 4 left
        # one stats() observation across 5 pacers must not eat tokens
        stats = gov.stats()
        self.assertEqual(stats["counters"]["decisions"], 1)
        self.assertTrue(gov.precheck("fresh2", "start").allowed)  # 3 left
        # retry_after is read-only too — hammering it changes nothing
        for _ in range(10):
            self.assertEqual(gov.retry_after("fresh3", "start"), 0.0)
        self.assertEqual(gov.counters["decisions"], 2)
        self.assertTrue(gov.precheck("fresh4", "start").allowed)  # 2 left
        # peek labels bucket-refill waits distinctly from cooldowns
        self.assertTrue(gov.precheck("drain1", "start").allowed)  # 1 left
        self.assertTrue(gov.precheck("drain2", "start").allowed)  # 0 left
        decision = gov.peek("fresh5", "start")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "global_bucket")
        self.assertGreater(decision.retry_after_s, 0)
        # ...and the observations above consumed nothing: the refill wait
        # is exactly one 5s refill away, without a token spent by peeking
        self.assertAlmostEqual(decision.retry_after_s, 5.0, delta=0.1)

    def test_stats_cooldowns_label_reason_distinctly(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        gov.commit("alpha", "stop", ok=True)  # restart cooldown active
        stats = gov.stats()
        self.assertIn("alpha", stats["cooldowns"])
        entry = stats["cooldowns"]["alpha"]
        self.assertEqual(entry["reason"], "account_cooldown")
        self.assertGreater(entry["wait_s"], 0)
        self.assertEqual(gov.counters["decisions"], 0)  # still read-only


class GovernorManagerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_thrashing_start_stop_is_rejected(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        await h.manager.stop("alpha")
        # wire the governor AFTER the first cycle: the second immediate
        # start must be refused with the cooldown in the message
        clock = _ManualClock()
        clock.now = asyncio.get_running_loop().time()
        gov = LifecycleGovernor(clock=clock)
        gov.commit("alpha", "stop", ok=True)  # simulate the recorded stop
        h.manager._lifecycle = gov
        with self.assertRaises(AccountError) as ctx:
            await h.manager.start("alpha")
        self.assertIn("rate-limited", str(ctx.exception))
        self.assertIn("retry in", str(ctx.exception))
        self.assertEqual(h.manager.get("alpha").runtime.status.value,
                         "stopped")  # refusal did not mutate state

    async def test_estop_bypasses_governor(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        # drain the stop bucket entirely
        for _ in range(gov.policy.stop_burst):
            gov._stop_bucket.try_acquire()
        h.manager._lifecycle = gov
        out = h.manager.emergency_stop("alpha")  # must NOT raise
        self.assertTrue(out["stop_initiated"])
        await asyncio.sleep(0.1)
        self.assertEqual(h.manager.get("alpha").runtime.status.value,
                         "stopped")

    async def test_remove_forgets_pacer(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        gov = LifecycleGovernor(clock=_ManualClock())
        gov.commit("alpha", "stop", ok=True)
        h.manager._lifecycle = gov
        await h.manager.remove_account("alpha")
        self.assertNotIn("alpha", gov._pacers)

    async def test_stop_all_bypasses_governor(self) -> None:
        h = make_manager()
        for name in ("alpha", "beta"):
            await h.manager.add_account(name)
            await h.manager.start(name)
        gov = LifecycleGovernor(clock=_ManualClock())
        for _ in range(gov.policy.stop_burst):
            gov._stop_bucket.try_acquire()  # bucket bone dry
        h.manager._lifecycle = gov
        out = await h.manager.stop_all()  # teardown path: never blocked
        self.assertEqual(sorted(out["stopped"]), ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
