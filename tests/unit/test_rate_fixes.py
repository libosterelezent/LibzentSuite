"""Regression tests for the audited rate-engine fixes.

Covers:
  (a) prune_account fails parked waiters with RatePolicyError (they used to
      be stranded forever when their waiter heap was deleted);
  (b) PER-KEY wake events: a grant on one key does not re-park waiters on
      other keys (the old single shared event woke the whole fleet), and a
      key's own grant still wakes its successors — no lost wakeups;
  (c) warnings(): a stored policy FASTER than the learned default surfaces
      as faster_than_learned, and metronomic default pacing (zero jitter)
      surfaces as default_no_jitter;
  (d) learned-default pacing draws its jitter from the documented
      DEFAULT_JITTER window (no-policy traffic is not metronomic);
  plus ctor deprecation of the removed lane kwargs and wake-event dict
  hygiene (no unbounded growth).
"""
from __future__ import annotations

import asyncio
import unittest

from core.errors import RatePolicyError
from core.event_bus import EventBus
from core.rate_limit_manager import RateLimitManager
from core.rate_limit_manager.engine import DEFAULT_JITTER

from tests.unit.test_rate_engine import (
    ACTION,
    GatedSleepManager,
    StubLogger,
    make_manager,
)


def _plain_manager(**kwargs) -> RateLimitManager:
    """Real-clock, real-sleep manager (the wake paths must actually fire)."""
    return RateLimitManager(EventBus(), StubLogger(), None, **kwargs)


async def _settle(predicate, ticks: int = 100):
    """Yield to the loop until predicate() is true (bounded)."""
    for _ in range(ticks):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


class PruneAccountFailsWaiters(unittest.IsolatedAsyncioTestCase):
    """(a) deleting an account's heaps fails its parked waiters, promptly."""

    async def test_pruned_waiter_raises_rate_policy_error_not_hang(self):
        manager = _plain_manager()
        manager.set_policy_values(ACTION, 60.0)
        await manager.acquire(ACTION, account="alpha")  # stamps t=0
        waiter = asyncio.ensure_future(manager.acquire(ACTION, account="alpha"))
        self.assertTrue(await _settle(
            lambda: ("alpha", ACTION) in manager._pending))
        await manager.prune_account("alpha")
        results = await asyncio.wait_for(
            asyncio.gather(waiter, return_exceptions=True), 2.0)
        self.assertIsInstance(results[0], RatePolicyError)
        self.assertIn("removed", str(results[0]))
        # engine residue is gone (heap, wake event)
        self.assertNotIn(("alpha", ACTION), manager._pending)
        self.assertNotIn(("alpha", ACTION), manager._wake_events)

    async def test_pruned_group_waiter_on_other_key_also_fails(self):
        """A grouped account's waiter parks on its OWN key — prune still
        finds and fails it (and wakes every key so it observes fast)."""
        manager = _plain_manager()
        manager.set_policy_values(ACTION, 60.0)
        manager.set_account_group("alpha", "g")
        manager.set_account_group("beta", "g")
        await manager.acquire(ACTION, account="beta")  # stamps group clock
        waiter = asyncio.ensure_future(manager.acquire(ACTION, account="alpha"))
        self.assertTrue(await _settle(
            lambda: ("alpha", ACTION) in manager._pending))
        await manager.prune_account("alpha")
        results = await asyncio.wait_for(
            asyncio.gather(waiter, return_exceptions=True), 2.0)
        self.assertIsInstance(results[0], RatePolicyError)


class PerKeyWakeEvents(unittest.IsolatedAsyncioTestCase):
    """(b) grants wake only the granting key; no key is ever lost."""

    async def test_unrelated_grant_does_not_repark_other_keys(self):
        """10 parked waiters on distinct keys + one grant on an unrelated
        key: every waiter stays parked exactly once (re-park count <= 1).
        Under the old single shared event every waiter woke, recomputed
        and re-parked (the wake storm)."""
        manager = make_manager(manager_cls=GatedSleepManager, interval=30.0)
        accounts = [f"acct{i}" for i in range(10)]
        for account in accounts:
            await manager.acquire(ACTION, account=account)  # stamp t=0
        waiters = {
            account: asyncio.ensure_future(manager.acquire(ACTION, account=account))
            for account in accounts
        }
        self.assertTrue(await _settle(lambda: len(manager.gates) == 10))
        parks_after_initial = [len(manager.gates)]

        # one grant on a completely unrelated key (immediate — fresh key)
        grant = await manager.acquire(ACTION, account="zeta")
        self.assertEqual(grant.wait_s, 0.0)
        for _ in range(20):  # let any (wrong) fleet-wide wake propagate
            await asyncio.sleep(0)
        parks_after_grant = len(manager.gates)
        self.assertEqual(parks_after_grant, 10)
        self.assertEqual(parks_after_grant, parks_after_initial[0])

        # NO lost wakeups: each key's own release still grants its waiter
        # (index-paired release: waiter i parked gate i, in order)
        for index in reversed(range(len(accounts))):
            manager.gates[index][1].set()  # release this waiter's sleep
            await asyncio.wait_for(waiters[accounts[index]], 2.0)
        self.assertEqual(manager._wake_events, {})  # all keys cleaned up

    async def test_grant_wakes_same_key_successors_promptly(self):
        """A key's own grant wakes its parked successors immediately — the
        successor must NOT sit out the POLL_INTERVAL backstop (a lost wake
        would delay it ~2s; the prompt wake lands it ~interval later)."""
        manager = _plain_manager()
        manager.set_policy_values(ACTION, 1.5)
        await manager.acquire(ACTION)  # stamps t=0
        first = asyncio.ensure_future(manager.acquire(ACTION))
        second = asyncio.ensure_future(manager.acquire(ACTION))
        await asyncio.wait_for(first, 2.5)  # head grants at ~1.5s (the floor)
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(second, 3.0)  # << POLL_INTERVAL would be a lost wake
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 2.5)  # prompt wake lands ~interval, not at the 2s poll

    async def test_global_mutations_wake_every_key(self):
        """Policy edits (a global-affecting mutation) wake ALL keys."""
        manager = make_manager(manager_cls=GatedSleepManager, interval=30.0)
        await manager.acquire(ACTION, account="alpha")
        await manager.acquire(ACTION, account="beta")
        waiters = [
            asyncio.ensure_future(manager.acquire(ACTION, account=account))
            for account in ("alpha", "beta")
        ]
        self.assertTrue(await _settle(lambda: len(manager.gates) == 2))
        manager.set_policy_values(ACTION, 10.0)  # wake-all mutation
        self.assertTrue(await _settle(lambda: len(manager.gates) == 4))
        self.assertAlmostEqual(manager.gates[2][0], 10.0, places=6)
        self.assertAlmostEqual(manager.gates[3][0], 10.0, places=6)
        # drain, index-paired (waiter i re-parked gate 2+i, in order)
        for index in reversed(range(len(waiters))):
            manager.gates[2 + index][1].set()
            await asyncio.wait_for(waiters[index], 2.0)

    async def test_wake_event_table_tracks_live_waiters_only(self):
        """Events are created on first park and dropped when a key's last
        waiter exits — the table cannot grow unbounded."""
        manager = _plain_manager()
        manager.set_policy_values(ACTION, 1.5)
        for round_no in range(5):
            await manager.acquire(ACTION, account=f"acct{round_no}")
            await manager.acquire(ACTION, account=f"acct{round_no}")
        self.assertEqual(manager._wake_events, {})


class LearnedDefaultWarnings(unittest.IsolatedAsyncioTestCase):
    """(c) warnings() composes the learned-default reviews."""

    async def test_faster_than_learned_policy_warns(self):
        manager = _plain_manager()
        manager.set_policy_values("message", 2.0)  # 2.0 < learned 2.6
        warnings = manager.warnings()
        entries = [w for w in warnings if w["code"] == "faster_than_learned"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["level"], "warn")
        self.assertIn(
            "policy message@global 2.00s is faster than the learned "
            "zero-throttle rate 2.60s",
            entries[0]["message"],
        )

    async def test_slower_than_learned_policy_does_not_warn(self):
        manager = _plain_manager()
        manager.set_policy_values("message", 2.6)  # exactly the learned rate
        manager.set_policy_values("dm", 3.0)
        for warning in manager.warnings():
            self.assertNotEqual(warning["code"], "faster_than_learned")

    async def test_default_no_jitter_entry_when_metronomic(self):
        """DEFAULT_JITTER is monkeypatched to zero: every default-paced
        action with no stored policy flags the metronomic pacing."""
        import core.rate_limit_manager.engine as engine

        manager = _plain_manager()
        saved = engine.DEFAULT_JITTER
        engine.DEFAULT_JITTER = (0.0, 0.0)
        try:
            warnings = manager.warnings()
        finally:
            engine.DEFAULT_JITTER = saved
        codes = [w["code"] for w in warnings]
        self.assertIn("default_no_jitter", codes)
        message = next(
            w["message"] for w in warnings if w["code"] == "default_no_jitter")
        self.assertIn("metronomic", message)

    async def test_defaults_with_jitter_are_clean(self):
        manager = _plain_manager()
        self.assertEqual(manager.warnings(), [])


class LearnedDefaultJitter(unittest.IsolatedAsyncioTestCase):
    """(d) no-policy acquires draw jitter from the DEFAULT_JITTER window."""

    class _FixedRng:
        """RNG stand-in: records the window, returns a fixed draw."""

        def __init__(self, draw: float) -> None:
            self.draw = draw
            self.calls: list = []

        def uniform(self, low: float, high: float) -> float:
            self.calls.append((low, high))
            return self.draw

    async def test_no_policy_acquire_draws_from_default_window(self):
        manager = _plain_manager()
        rng = self._FixedRng(0.5)
        manager._jitter_rng = rng
        grant = await manager.acquire("message")  # learned default 2.6s
        self.assertEqual(rng.calls, [DEFAULT_JITTER])
        self.assertEqual(grant.jitter_s, 0.5)
        self.assertEqual(manager.effective("message")["jitter"],
                         list(DEFAULT_JITTER))
        # a second no-policy default acquire keeps drawing from the window
        grant2 = await manager.acquire("message")
        self.assertEqual(rng.calls, [DEFAULT_JITTER, DEFAULT_JITTER])
        self.assertGreaterEqual(grant2.jitter_s, DEFAULT_JITTER[0])
        self.assertLessEqual(grant2.jitter_s, DEFAULT_JITTER[1])

    async def test_unknown_action_keeps_zero_jitter(self):
        """Actions without a learned default pace at the conservative
        UNSTUDIED_ACTION_INTERVAL (1.2 s) with zero jitter — the
        documented no-default branch (never the bare floor)."""
        manager = _plain_manager()
        rng = self._FixedRng(0.5)
        manager._jitter_rng = rng
        grant = await manager.acquire("unstudied-action")
        self.assertEqual(grant.jitter_s, 0.0)
        self.assertEqual(rng.calls, [])
        self.assertEqual(
            manager.effective("unstudied-action")["jitter"], [0.0, 0.0])

    async def test_stored_policy_does_not_inherit_default_jitter(self):
        manager = _plain_manager()
        rng = self._FixedRng(0.5)
        manager._jitter_rng = rng
        manager.set_policy_values("message", 2.0)  # displaces the default
        grant = await manager.acquire("message")
        self.assertEqual(grant.jitter_s, 0.0)  # stored policy has no jitter
        self.assertEqual(rng.calls, [])
        self.assertEqual(manager.effective("message")["jitter"], [0.0, 0.0])


class LaneDeprecation(unittest.IsolatedAsyncioTestCase):
    """The removed lane kwargs are accepted (runner compat) + warned."""

    def _logger(self) -> StubLogger:
        logger = StubLogger()
        assert logger.entries == []
        return logger

    async def test_lane_kwargs_logged_deprecated(self):
        logger = self._logger()
        RateLimitManager(
            EventBus(), logger, None, lane_enabled=False, lane_prefixrate=1.5)
        self.assertTrue(
            any("deprecated" in message for _sev, _src, message in logger.entries),
            logger.entries)

    async def test_no_lane_kwargs_no_warning(self):
        logger = self._logger()
        RateLimitManager(EventBus(), logger, None)
        self.assertEqual(
            [e for e in logger.entries if "deprecated" in e[2]], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class UnstudiedActionDefaultTests(unittest.IsolatedAsyncioTestCase):
    """The default rate for unstudied actions is 1.2 s (never the bare
    floor); learned defaults and stored policies keep their contracts."""

    def test_unstudied_effective_is_the_conservative_default(self):
        from core.rate_limit_manager import UNSTUDIED_ACTION_INTERVAL
        from core.rate_limit_manager.engine import RateLimitManager
        manager = RateLimitManager(EventBus(), StubLogger(), None)
        self.assertEqual(UNSTUDIED_ACTION_INTERVAL, 1.5)
        self.assertEqual(manager.effective("poke")["interval"], 1.5)
        # learned defaults are untouched by the fallback
        self.assertEqual(manager.effective("message")["interval"], 2.6)

    def test_floor_above_default_dominates(self):
        from core.rate_limit_manager.engine import RateLimitManager
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, floor=3.0)
        self.assertEqual(manager.effective("poke")["interval"], 3.0)

    def test_stored_policy_displaces_the_default(self):
        from core.rate_limit_manager.engine import RateLimitManager
        manager = RateLimitManager(EventBus(), StubLogger(), None)
        manager.set_policy_values("poke", 2.0)   # distinct pace proves displacement
        self.assertEqual(manager.effective("poke")["interval"], 2.0)
