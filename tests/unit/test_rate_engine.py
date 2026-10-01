"""Unit tests for core/rate_limit_manager/engine.py (A4).

Deterministic pacing via a ManualClock + a _sleep seam double that advances
the clock instead of blocking (and, for wake tests, a gated variant).

Covers: pacing basics, per-account vs global keys, jitter bounds with a
seeded RNG, priority/FIFO waiter ordering, server cooldowns (account + fleet
global), events, stats shape, set_floor raise-only semantics, wake-on-policy-
change, waiter cancellation, and input validation.
"""
from __future__ import annotations

import asyncio
import unittest

from core.errors import RatePolicyError, UnsafePolicyError
from core.event_bus import EventBus, RateLimitTriggered
from core.priority import Priority
from core.rate_limit_manager import Grant, RateLimitManager
from core.rate_limit_manager.engine import GLOBAL_KEY, ManualClock

ACTION = "message"


class StubLogger:
    """LogService-shaped capture double."""

    def __init__(self) -> None:
        self.entries = []

    def _add(self, severity, source, message, **ctx):
        self.entries.append((severity, source, message))

    def info(self, source, message, **ctx):
        self._add("INFO", source, message, **ctx)

    def warning(self, source, message, **ctx):
        self._add("WARNING", source, message, **ctx)

    def error(self, source, message, **ctx):
        self._add("ERROR", source, message, **ctx)


class SleepAdvanceManager(RateLimitManager):
    """_sleep seam double: records sleeps and advances the manual clock."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sleeps = []

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self._clock.advance(seconds)


class GatedSleepManager(RateLimitManager):
    """_sleep seam double that blocks until the test opens the gate.

    Used to observe wake-on-policy-change: a waiter parked on a long sleep
    must abandon it (get cancelled) and recompute when a policy changes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gates = []  # list of (seconds, asyncio.Event)

    async def _sleep(self, seconds):
        event = asyncio.Event()
        self.gates.append((seconds, event))
        await event.wait()
        self._clock.advance(seconds)


def make_manager(manager_cls=SleepAdvanceManager, interval=None,
                 jitter=(0.0, 0.0), jitter_seed=7):
    """Build a deterministic manager (ManualClock at t=0 + sleep seam)."""
    clock = ManualClock(0.0)
    manager = manager_cls(
        EventBus(), StubLogger(), None, clock=clock, jitter_seed=jitter_seed
    )
    if interval is not None:
        manager.set_policy_values(ACTION, interval, jitter[0], jitter[1])
    return manager


class PacingBasics(unittest.IsolatedAsyncioTestCase):
    """First grant is free; the second waits out the effective interval."""

    async def test_first_grant_immediate(self):
        manager = make_manager(interval=2.5)
        grant = await manager.acquire(ACTION)
        self.assertEqual(grant.wait_s, 0.0)
        self.assertEqual(grant.jitter_s, 0.0)
        self.assertEqual(grant.reason, "")
        self.assertEqual(grant.action, ACTION)
        self.assertIsNone(grant.account)

    async def test_second_grant_waits_interval(self):
        manager = make_manager(interval=2.5)
        await manager.acquire(ACTION)
        grant = await manager.acquire(ACTION)
        self.assertAlmostEqual(grant.wait_s, 2.5, places=6)
        self.assertEqual(grant.reason, "pacing")
        self.assertAlmostEqual(manager._clock.now(), 2.5, places=6)

    async def test_third_grant_waits_from_second_fire(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION)
        await manager.acquire(ACTION)          # granted at t=2.0
        grant = await manager.acquire(ACTION)  # waits from last fire (2.0)
        self.assertAlmostEqual(grant.wait_s, 2.0, places=6)
        self.assertAlmostEqual(manager._clock.now(), 4.0, places=6)

    async def test_no_policy_paces_at_floor(self):
        manager = make_manager()  # no stored policy -> interval = floor
        first = await manager.acquire(ACTION)
        self.assertEqual(first.wait_s, 0.0)
        second = await manager.acquire(ACTION)
        self.assertAlmostEqual(second.wait_s, 1.5, places=6)

    async def test_pacing_event_published(self):
        bus = EventBus()
        clock = ManualClock(0.0)
        manager = SleepAdvanceManager(bus, StubLogger(), None, clock=clock)
        manager.set_policy_values(ACTION, 3.0)
        seen = []
        bus.subscribe(RateLimitTriggered, lambda e: seen.append(e))
        await manager.acquire(ACTION)
        await manager.acquire(ACTION)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0].wait_s, 3.0, places=6)
        self.assertEqual(seen[0].reason, "pacing")
        self.assertEqual(seen[0].action, ACTION)
        # no event when nothing was waited
        await manager.acquire(ACTION)
        self.assertEqual(len(seen), 2)  # this one also waited; still one per wait
        self.assertEqual(seen[1].reason, "pacing")

    async def test_grant_is_plain_dataclass(self):
        manager = make_manager(interval=2.0)
        grant = await manager.acquire(ACTION, account="alpha", plugin="soulcopy")
        self.assertIsInstance(grant, Grant)
        self.assertEqual(grant.account, "alpha")
        self.assertEqual(grant.plugin, "soulcopy")


class PacingKeys(unittest.IsolatedAsyncioTestCase):
    """account=None paces the shared global key; accounts pace their own."""

    async def test_per_account_keys_independent(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION, account="alpha")
        second = await manager.acquire(ACTION, account="alpha")
        beta = await manager.acquire(ACTION, account="beta")
        self.assertAlmostEqual(second.wait_s, 2.0, places=6)
        self.assertEqual(beta.wait_s, 0.0)  # beta has its own key
        self.assertIn("alpha:message", manager.stats()["last_fire_ages"])

    async def test_account_none_uses_global_key(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION)
        grant = await manager.acquire(ACTION)
        self.assertAlmostEqual(grant.wait_s, 2.0, places=6)
        self.assertIn(f"{GLOBAL_KEY}:{ACTION}", manager.stats()["last_fire_ages"])

    async def test_global_and_account_keys_do_not_interfere(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION)                 # global key fires
        scoped = await manager.acquire(ACTION, account="alpha")
        self.assertEqual(scoped.wait_s, 0.0)          # alpha key untouched


class Jitter(unittest.IsolatedAsyncioTestCase):
    """Post-grant jitter: seeded bounds respected, never moves the pacing clock."""

    async def test_jitter_within_bounds_seeded(self):
        manager = make_manager(interval=2.0, jitter=(0.2, 0.8), jitter_seed=42)
        first = await manager.acquire(ACTION)
        self.assertGreaterEqual(first.jitter_s, 0.2)
        self.assertLessEqual(first.jitter_s, 0.8)
        # pacing boundary is measured from grant time, so wait + prior jitter
        # always sums back to the interval
        second = await manager.acquire(ACTION)
        self.assertAlmostEqual(first.jitter_s + second.wait_s, 2.0, places=6)
        self.assertGreaterEqual(second.jitter_s, 0.2)
        self.assertLessEqual(second.jitter_s, 0.8)

    async def test_jitter_deterministic_with_same_seed(self):
        async def draws():
            manager = make_manager(interval=2.0, jitter=(0.1, 0.9), jitter_seed=99)
            grant = await manager.acquire(ACTION)
            return grant.jitter_s
        self.assertEqual(await draws(), await draws())

    async def test_no_jitter_policy_means_no_jitter_sleep(self):
        manager = make_manager(interval=2.0)  # jitter (0, 0)
        grant = await manager.acquire(ACTION)
        self.assertEqual(grant.jitter_s, 0.0)
        self.assertEqual(manager.sleeps, [])  # nothing waited yet at all


class WaiterOrdering(unittest.IsolatedAsyncioTestCase):
    """Concurrent waiters on ONE pacing key: priority first, then FIFO."""

    async def test_high_priority_granted_before_low(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION)  # fires at t=0
        order = []

        async def worker(name, priority):
            await manager.acquire(ACTION, priority=priority)
            order.append(name)

        low = asyncio.ensure_future(worker("low", Priority.LOW))
        high = asyncio.ensure_future(worker("high", Priority.HIGH))
        await asyncio.gather(low, high)
        self.assertEqual(order, ["high", "low"])

    async def test_fifo_within_same_priority(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION)
        order = []

        async def worker(name):
            await manager.acquire(ACTION, priority=Priority.NORMAL)
            order.append(name)

        first = asyncio.ensure_future(worker("first"))
        second = asyncio.ensure_future(worker("second"))
        await asyncio.gather(first, second)
        self.assertEqual(order, ["first", "second"])

    async def test_different_keys_do_not_queue_behind_each_other(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION, account="alpha")
        await manager.acquire(ACTION, account="beta")
        # both accounts already fired; a fresh action on gamma is not blocked
        grant = await manager.acquire(ACTION, account="gamma")
        self.assertEqual(grant.wait_s, 0.0)

    async def test_cancelled_waiter_does_not_wedge_the_queue(self):
        manager = make_manager(interval=6.0)
        await manager.acquire(ACTION)
        doomed = asyncio.ensure_future(manager.acquire(ACTION))
        await asyncio.sleep(0)  # let it enqueue + park
        doomed.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await doomed
        grant = await manager.acquire(ACTION, account="other")  # other key fine
        self.assertEqual(grant.account, "other")


class ServerCooldowns(unittest.IsolatedAsyncioTestCase):
    """429 cooldowns: account-scoped + fleet-wide global, always respected."""

    async def test_account_cooldown_waits_retry_after(self):
        manager = make_manager(interval=2.0)
        manager.report_server_limit("alpha", ACTION, 2.0)
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), 2.0)
        grant = await manager.acquire(ACTION, account="alpha")
        self.assertGreaterEqual(grant.wait_s, 2.0)
        self.assertEqual(grant.reason, "server")
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), 0.0)

    async def test_server_event_published(self):
        bus = EventBus()
        manager = SleepAdvanceManager(bus, StubLogger(), None, clock=ManualClock())
        seen = []
        bus.subscribe(RateLimitTriggered, lambda e: seen.append(e))
        manager.report_server_limit("alpha", ACTION, 1.5)
        await manager.acquire(ACTION, account="alpha")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].reason, "server")
        self.assertAlmostEqual(seen[0].wait_s, 1.5, places=6)
        self.assertEqual(seen[0].account, "alpha")

    async def test_global_cooldown_cools_every_account(self):
        # one manager per account: a shared clock would let the cooldown
        # elapse while the first account waits, hiding the fleet effect.
        for account in ("alpha", "beta", "gamma"):
            manager = make_manager(interval=2.0)
            manager.report_server_limit("alpha", ACTION, 2.0, global_=True)
            self.assertEqual(manager.cooldown_remaining(account="beta"), 2.0)
            grant = await manager.acquire(ACTION, account=account)
            self.assertGreaterEqual(grant.wait_s, 2.0)
            self.assertEqual(grant.reason, "server")

    async def test_fleet_global_cooldown_is_not_action_scoped(self):
        manager = make_manager(interval=2.0)
        manager.report_server_limit("alpha", ACTION, 2.0, global_=True)
        grant = await manager.acquire("poke", account="beta")  # different action
        self.assertGreaterEqual(grant.wait_s, 2.0)
        self.assertEqual(grant.reason, "server")

    async def test_cooldown_takes_max_with_existing(self):
        manager = make_manager(interval=2.0)
        manager.report_server_limit("alpha", ACTION, 1.0)
        manager.report_server_limit("alpha", ACTION, 3.0)
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), 3.0)

    async def test_cooldown_remaining_filters(self):
        manager = make_manager()
        manager.report_server_limit("alpha", ACTION, 2.0)
        manager.report_server_limit("beta", "poke", 3.0)
        self.assertAlmostEqual(manager.cooldown_remaining(), 3.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining("alpha"), 2.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining(action="poke"), 3.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining("beta", "poke"), 3.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining("alpha", "poke"), 0.0, places=6)

    async def test_negative_retry_after_clamped_nonfinite_rejected(self):
        manager = make_manager()
        manager.report_server_limit("alpha", ACTION, -5.0)
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), 0.0)
        with self.assertRaises(RatePolicyError):
            manager.report_server_limit("alpha", ACTION, float("nan"))
        with self.assertRaises(RatePolicyError):
            manager.report_server_limit("alpha", ACTION, "soon")

    async def test_server_cooldown_waits_before_pacing(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION, account="alpha")  # last fire at t=0
        manager.report_server_limit("alpha", ACTION, 1.0)
        grant = await manager.acquire(ACTION, account="alpha")
        # server cooldown (1s) elapses first, then pacing to t=2
        self.assertAlmostEqual(grant.wait_s, 2.0, places=6)
        self.assertEqual(grant.reason, "server")

    async def test_cooldown_reported_mid_wait_extends_it(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION, account="alpha")
        task = asyncio.ensure_future(manager.acquire(ACTION, account="alpha"))
        await asyncio.sleep(0)  # waiter parked, sleeping off its 2s of pacing
        manager.report_server_limit("alpha", ACTION, 5.0)  # wakes + extends
        grant = await task
        self.assertGreaterEqual(grant.wait_s, 5.0)
        self.assertEqual(grant.reason, "server")


class WakeMechanics(unittest.IsolatedAsyncioTestCase):
    """Policy changes wake parked waiters so they recompute their wait."""

    async def _let_loop_settle(self, manager, expected_gates: int) -> None:
        """Yield to the event loop until the gated sleeper count settles.

        Waking is multi-tick: the waker task completes, the parked waiter
        cancels its old sleeper, then parks a fresh one. A bounded poll keeps
        the test independent of exact task-scheduling order.
        """
        for _ in range(50):
            if len(manager.gates) >= expected_gates:
                await asyncio.sleep(0)
                return
            await asyncio.sleep(0)
        self.fail(f"waiter did not re-park after wake (gates={manager.gates!r})")

    async def test_shorter_interval_wakes_and_recomputes(self):
        manager = make_manager(manager_cls=GatedSleepManager, interval=6.0)
        await manager.acquire(ACTION)  # fires at t=0
        task = asyncio.ensure_future(manager.acquire(ACTION))
        await self._let_loop_settle(manager, 1)
        self.assertAlmostEqual(manager.gates[0][0], 6.0, places=6)

        manager.set_policy_values(ACTION, 2.5)  # still >= floor: allowed
        await self._let_loop_settle(manager, 2)
        self.assertAlmostEqual(manager.gates[1][0], 2.5, places=6)
        self.assertFalse(manager.gates[0][1].is_set())  # old sleeper abandoned

        manager.gates[1][1].set()  # release the recomputed sleep; clock -> 2.5
        grant = await task
        self.assertAlmostEqual(grant.wait_s, 2.5, places=6)
        self.assertEqual(grant.reason, "pacing")

    async def test_clear_policy_wakes_waiters_to_floor(self):
        manager = make_manager(manager_cls=GatedSleepManager, interval=6.0)
        await manager.acquire(ACTION)
        task = asyncio.ensure_future(manager.acquire(ACTION))
        await self._let_loop_settle(manager, 1)
        manager.clear_policy("global", None, ACTION)
        await self._let_loop_settle(manager, 2)
        self.assertAlmostEqual(manager.gates[-1][0], 1.5, places=6)  # floor
        manager.gates[-1][1].set()
        grant = await task
        self.assertAlmostEqual(grant.wait_s, 1.5, places=6)


class FloorSemantics(unittest.IsolatedAsyncioTestCase):
    """set_floor: raise-only, never below 1.5s, clamps effective pacing."""

    async def test_raise_floor(self):
        manager = make_manager()
        self.assertEqual(manager.set_floor(2.5), 2.5)
        self.assertEqual(manager.effective(ACTION)["interval"], 2.5)

    async def test_may_not_lower(self):
        manager = make_manager()
        manager.set_floor(2.5)
        with self.assertRaises(RatePolicyError):
            manager.set_floor(1.5)  # would lower
        with self.assertRaises(RatePolicyError):
            manager.set_floor(2.0)
        self.assertEqual(manager.stats()["floor"], 2.5)  # unchanged

    async def test_never_below_hard_minimum(self):
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            manager.set_floor(1.0)
        with self.assertRaises(RatePolicyError):
            manager.set_floor(0)
        with self.assertRaises(RatePolicyError):
            manager.set_floor(float("nan"))
        manager.set_floor(1.5)  # equal is fine (idempotent)

    async def test_raised_floor_rejects_new_policies(self):
        manager = make_manager()
        manager.set_floor(2.5)
        with self.assertRaises(UnsafePolicyError) as ctx:
            manager.set_policy_values(ACTION, 2.0)
        self.assertIn("2.5", str(ctx.exception))

    async def test_raised_floor_clamps_existing_policies(self):
        manager = make_manager(interval=1.5)
        manager.set_floor(3.0)
        eff = manager.effective(ACTION)
        self.assertEqual(eff["interval"], 3.0)
        self.assertEqual(len(eff["sources"]), 1)  # the stored policy is listed
        grant = await manager.acquire(ACTION)  # no prior fire: still immediate
        self.assertEqual(grant.wait_s, 0.0)

    async def test_init_floor_validated(self):
        with self.assertRaises(RatePolicyError):
            RateLimitManager(EventBus(), StubLogger(), None, floor=1.0)
        with self.assertRaises(RatePolicyError):
            RateLimitManager(EventBus(), StubLogger(), None, floor=float("nan"))
        # a raised initial floor is fine
        manager = RateLimitManager(EventBus(), StubLogger(), None, floor=2.0)
        self.assertEqual(manager.effective(ACTION)["interval"], 2.0)


class StatsShape(unittest.IsolatedAsyncioTestCase):
    """stats(): the exact pinned shape for the UI page."""

    async def test_stats_shape_and_counters(self):
        manager = make_manager(interval=2.0)
        manager.set_policy_values("poke", 4.0, level="account", owner="alpha")
        await manager.acquire(ACTION)
        await manager.acquire(ACTION)  # one pacing wait of 2.0
        manager.report_server_limit("beta", "poke", 1.5)
        stats = manager.stats()
        self.assertEqual(
            set(stats),
            {"floor", "policies", "cooldowns", "last_fire_ages", "grants",
             "waits", "avg_wait_s"},
        )
        self.assertEqual(stats["floor"], 1.5)
        self.assertEqual(len(stats["policies"]), 2)
        self.assertEqual(stats["cooldowns"]["global"], 0.0)
        self.assertEqual(stats["cooldowns"]["accounts"]["beta"]["poke"], 1.5)
        self.assertIn(f"{GLOBAL_KEY}:{ACTION}", stats["last_fire_ages"])
        self.assertEqual(stats["grants"], 2)
        self.assertEqual(stats["waits"], 1)
        self.assertAlmostEqual(stats["avg_wait_s"], 2.0, places=6)

    async def test_stats_avg_wait_zero_without_waits(self):
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION)
        stats = manager.stats()
        self.assertEqual(stats["waits"], 0)
        self.assertEqual(stats["avg_wait_s"], 0.0)


class Validation(unittest.IsolatedAsyncioTestCase):
    """acquire()/report_server_limit() boundary validation."""

    async def test_acquire_rejects_bad_action(self):
        manager = make_manager()
        for action in ("", "  ", None):
            with self.assertRaises(RatePolicyError):
                await manager.acquire(action)

    async def test_acquire_rejects_bad_account_or_plugin(self):
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            await manager.acquire(ACTION, account="")
        with self.assertRaises(RatePolicyError):
            await manager.acquire(ACTION, plugin=5)

    async def test_acquire_rejects_bad_priority(self):
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            await manager.acquire(ACTION, priority="bogus")

    async def test_report_server_limit_rejects_bad_args(self):
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            manager.report_server_limit("", ACTION, 1.0)
        with self.assertRaises(RatePolicyError):
            manager.report_server_limit("alpha", "", 1.0)


if __name__ == "__main__":
    unittest.main()
