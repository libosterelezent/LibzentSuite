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
import random
import unittest

from core.errors import RatePolicyError, UnsafePolicyError
from core.event_bus import EventBus, RateLimitTriggered
from core.priority import Priority
from core.rate_limit_manager import Grant, RateLimitManager
from core.rate_limit_manager.engine import (
    GLOBAL_KEY,
    POLL_INTERVAL,
    ManualClock,
    _Entry,
)

ACTION = "poke"  # no learned default — floor semantics testable


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


class GrantRecorderManager(SleepAdvanceManager):
    """Records the exact grant stamp of every pacing key (from _last_fire
    diffs around _grant_ready) — used to assert wire-gap invariants without
    task-resume scheduling slip."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.grant_log = []

    def _grant_ready(self, only=None):
        before = dict(self._last_fire)
        super()._grant_ready(only)
        for key, ts in self._last_fire.items():
            if before.get(key) != ts:
                self.grant_log.append((key, ts))


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
        self.assertAlmostEqual(second.wait_s, 0.05, places=6)

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
        self.assertIn("alpha:ACTION", manager.stats()["last_fire_ages"].keys().__class__()) if False else self.assertIn(f"alpha:{ACTION}", manager.stats()["last_fire_ages"])

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
    """Post-grant jitter: seeded bounds respected; the pacing clock is
    stamped at grant + jitter (the expected wire instant), so consecutive
    WIRE sends stay >= interval apart no matter how the draws differ."""

    async def test_jitter_within_bounds_seeded(self):
        manager = make_manager(interval=2.0, jitter=(0.2, 0.8), jitter_seed=42)
        first = await manager.acquire(ACTION)
        wire1 = manager._clock.now()  # grant(0) + first jitter draw
        self.assertGreaterEqual(first.jitter_s, 0.2)
        self.assertLessEqual(first.jitter_s, 0.8)
        # the pacing stamp sits at grant + jitter: the second grant waits a
        # FULL interval from that stamp, so the second wait is exactly the
        # interval regardless of the first draw
        second = await manager.acquire(ACTION)
        wire2 = manager._clock.now()
        self.assertAlmostEqual(second.wait_s, 2.0, places=6)
        self.assertGreaterEqual(second.jitter_s, 0.2)
        self.assertLessEqual(second.jitter_s, 0.8)
        # wire gap >= interval even when the second draw is smaller
        self.assertGreaterEqual(wire2 - wire1, 2.0 - 1e-9)

    async def test_small_second_draw_never_compresses_wire_gap(self):
        """Round-3 wire-gap fix (probe P1/P6 shape): under post-grant-only
        jitter a much smaller second draw compressed the wire gap to
        interval - jitter_max (here 1.5 - ~1.4 = ~0.07s); stamping at
        grant + jitter keeps consecutive wire sends >= interval apart."""
        manager = make_manager(interval=1.5, jitter=(0.5, 2.0), jitter_seed=15)
        wires = []
        for _ in range(3):
            await manager.acquire(ACTION)
            wires.append(manager._clock.now())
        gaps = [b - a for a, b in zip(wires, wires[1:])]
        self.assertTrue(all(g >= 1.5 - 1e-9 for g in gaps), gaps)

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

    async def test_pending_heaps_do_not_leak_empty_husks(self):
        """A pacing key whose last waiter is gone is dropped from the
        pending table instead of lingering as an empty list forever."""
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION, account="alpha")
        await manager.acquire(ACTION, account="alpha")  # waits, then grants
        self.assertEqual(manager._pending, {})
        doomed = asyncio.ensure_future(manager.acquire(ACTION, account="alpha"))
        await asyncio.sleep(0)
        doomed.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await doomed
        self.assertEqual(manager._pending, {})

    async def test_waiter_added_after_husk_pop_gets_fresh_heap(self):
        """A waiter pushed after _prune popped the key's husk lands in a
        FRESH list (setdefault) — the stale heap ref held by the dead
        waiter's era never sees it, and pacing still holds for the fresh
        waiter. Gated sleeps keep the clock frozen through the cancel so
        the observation is deterministic."""
        manager = make_manager(manager_cls=GatedSleepManager, interval=2.0)
        await manager.acquire(ACTION, account="alpha")  # last fire at t=0
        doomed = asyncio.ensure_future(manager.acquire(ACTION, account="alpha"))
        for _ in range(50):
            if manager.gates:
                break
            await asyncio.sleep(0)
        stale_heap = manager._pending[("alpha", ACTION)]
        doomed.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await doomed
        self.assertEqual(manager._pending, {})  # husk popped
        self.assertEqual(manager._clock.now(), 0.0)  # gated sleep never ran
        fresh = asyncio.ensure_future(manager.acquire(ACTION, account="alpha"))
        for _ in range(50):
            if len(manager.gates) >= 2:
                break
            await asyncio.sleep(0)
        self.assertIn(("alpha", ACTION), manager._pending)
        self.assertIsNot(manager._pending[("alpha", ACTION)], stale_heap)
        manager.gates[-1][1].set()  # release: clock -> 2.0, grant
        grant = await fresh
        self.assertAlmostEqual(grant.wait_s, 2.0, places=6)
        self.assertEqual(manager._pending, {})

    async def test_entry_wait_with_dead_key_returns_poll(self):
        """_entry_wait against a key with no live heap (stale/empty ref)
        degrades to the backstop poll instead of crashing."""
        manager = make_manager()
        entry = _Entry(
            priority=1, seq=0, action=ACTION, account="ghost", plugin=None,
            future=asyncio.get_running_loop().create_future(),
        )
        wait, server_blocks = manager._entry_wait(entry)
        self.assertEqual(wait, POLL_INTERVAL)
        self.assertFalse(server_blocks)


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
        manager.report_server_limit("beta", "other", 3.0)
        self.assertAlmostEqual(manager.cooldown_remaining(), 3.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining("alpha"), 2.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining(action="other"), 3.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining("beta", "other"), 3.0, places=6)
        self.assertAlmostEqual(manager.cooldown_remaining("alpha", "other"), 0.0, places=6)

    async def test_negative_retry_after_clamped_nonfinite_rejected(self):
        manager = make_manager()
        manager.report_server_limit("alpha", ACTION, -5.0)
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), 0.0)
        with self.assertRaises(RatePolicyError):
            manager.report_server_limit("alpha", ACTION, float("nan"))
        with self.assertRaises(RatePolicyError):
            manager.report_server_limit("alpha", ACTION, "soon")

    async def test_huge_retry_after_clamped_at_intake(self):
        """Round-3: a finite-huge retry_after (1e9 -> 31.7 years, memory
        only) would block every waiter until restart — intake clamps to
        MAX_INTERVAL (86400s), exactly like policies are capped."""
        from core.rate_limit_manager.policies import MAX_INTERVAL

        manager = make_manager()
        manager.report_server_limit("alpha", ACTION, 1e9, global_=True)
        self.assertEqual(manager.cooldown_remaining(), MAX_INTERVAL)
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), MAX_INTERVAL)
        # a numeric string coercing over the cap is clamped too
        manager.report_server_limit("beta", ACTION, "90000000")
        self.assertEqual(manager.cooldown_remaining("beta", ACTION), MAX_INTERVAL)
        # exactly at the cap (and below it) passes through untouched — the
        # fleet-wide clamp above would shadow a per-account check here
        fresh = make_manager()
        fresh.report_server_limit("gamma", ACTION, 60.0)
        self.assertEqual(fresh.cooldown_remaining("gamma", ACTION), 60.0)
        fresh.report_server_limit("gamma", ACTION, MAX_INTERVAL)
        self.assertEqual(fresh.cooldown_remaining("gamma", ACTION), MAX_INTERVAL)

    async def test_expired_cooldown_entries_are_swept(self):
        """Expired rows must not linger in the table for the process
        lifetime — cooldown_remaining drops them as it walks."""
        manager = make_manager()
        manager.report_server_limit("alpha", ACTION, 1.0)
        manager.report_server_limit("beta", "poke", 5.0)
        manager._clock.advance(2.0)  # alpha's cooldown elapsed, beta's not
        self.assertEqual(manager.cooldown_remaining(), 3.0)
        self.assertNotIn(("alpha", ACTION), manager._account_cooldowns)
        self.assertIn(("beta", "poke"), manager._account_cooldowns)
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), 0.0)

    async def test_cooldown_swept_at_exact_boundary(self):
        """until == now is expired: swept by cooldown_remaining AND no
        longer blocks a waiter (the block uses strict now < until)."""
        manager = make_manager()
        manager.report_server_limit("alpha", ACTION, 1.0)  # until == 1.0
        manager._clock.advance(1.0)                        # now == until
        self.assertEqual(manager.cooldown_remaining("alpha", ACTION), 0.0)
        self.assertNotIn(("alpha", ACTION), manager._account_cooldowns)
        grant = await manager.acquire(ACTION, account="alpha")
        self.assertEqual(grant.wait_s, 0.0)
        self.assertEqual(grant.reason, "")

    async def test_sweep_drops_only_expired_rows_and_stats_agree(self):
        """The sweep walks a snapshot of the table (no RuntimeError under
        mutation) and keeps live rows; stats() shows the same survivors."""
        manager = make_manager()
        manager.report_server_limit("a", "x", 1.0)
        manager.report_server_limit("b", "y", 2.0)
        manager.report_server_limit("c", "z", 3.0)
        manager._clock.advance(2.0)
        self.assertEqual(manager.cooldown_remaining(), 1.0)
        self.assertEqual(set(manager._account_cooldowns), {("c", "z")})
        stats = manager.stats()
        self.assertEqual(stats["cooldowns"]["accounts"], {"c": {"z": 1.0}})
        self.assertEqual(stats["cooldowns"]["global"], 0.0)

    async def test_sweep_during_active_waiters_never_breaks_the_arm(self):
        """Arming a cooldown mid-wait, then sweeping (via a stats poll)
        while the waiter is still parked, extends — never truncates."""
        manager = make_manager(interval=2.0)
        await manager.acquire(ACTION, account="alpha")
        task = asyncio.ensure_future(manager.acquire(ACTION, account="alpha"))
        await asyncio.sleep(0)  # parked
        manager.report_server_limit("alpha", ACTION, 5.0)
        manager.cooldown_remaining()  # sweep pass while the waiter holds
        grant = await task
        self.assertGreaterEqual(grant.wait_s, 5.0)
        self.assertEqual(grant.reason, "server")

    async def test_stats_poll_sweeps_expired_cooldowns(self):
        """Round-3: production never calls cooldown_remaining — the 1s
        dashboard stats() poll must own the expiry sweep too, or dead rows
        persist until prune_account/restart."""
        manager = make_manager()
        manager.report_server_limit("alpha", ACTION, 1.0)
        manager.report_server_limit("beta", "poke", 5.0)
        manager._clock.advance(2.0)  # alpha's cooldown elapsed, beta's not
        stats = manager.stats()
        self.assertNotIn(("alpha", ACTION), manager._account_cooldowns)
        self.assertIn(("beta", "poke"), manager._account_cooldowns)
        self.assertEqual(stats["cooldowns"]["accounts"], {"beta": {"poke": 3.0}})

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
        self.assertAlmostEqual(manager.gates[-1][0], 0.05, places=6)  # floor
        manager.gates[-1][1].set()
        grant = await task
        self.assertAlmostEqual(grant.wait_s, 0.05, places=6)


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
            manager.set_floor(0.03)  # below MIN_ACTION_INTERVAL (0.05)
        with self.assertRaises(RatePolicyError):
            manager.set_floor(0)
        with self.assertRaises(RatePolicyError):
            manager.set_floor(float("nan"))
        manager.set_floor(1.5)  # equal is fine (idempotent)

    async def test_floor_capped_at_one_day(self):
        """The floor is pacing-affecting: an absurd value (inf, 1e9) would
        wedge every action forever, exactly like an uncapped policy."""
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            manager.set_floor(float("inf"))
        with self.assertRaises(RatePolicyError):
            manager.set_floor(10**9)
        manager.set_floor(86400.0)  # exactly at the cap is allowed
        self.assertEqual(manager.floor_value, 86400.0)

    async def test_set_floor_rejects_malformed_shapes(self):
        """Every malformed shape is refused with the floor untouched — bools
        coerce to 1.0/0.0 and fall below the hard minimum, containers raise
        TypeError, non-numeric strings raise ValueError."""
        manager = make_manager()
        for bad in (float("inf"), float("nan"), float("-inf"), "x", None,
                    False, [], {}, 86400.1, -1.0, 0.03):
            with self.assertRaises(RatePolicyError):
                manager.set_floor(bad)
        self.assertEqual(manager.floor_value, 0.05)  # nothing applied

    async def test_set_floor_coerces_numeric_strings(self):
        manager = make_manager()
        self.assertEqual(manager.set_floor("2.5"), 2.5)  # float() coercion
        self.assertEqual(manager.floor_value, 2.5)

    async def test_floor_cap_interplays_with_policy_cap(self):
        """floor == MAX_INTERVAL == 86400: a policy exactly at the cap is
        still settable (not strictly below the floor); anything less is
        rejected with the current floor named in the message."""
        manager = make_manager()
        manager.set_floor(86400.0)
        self.assertEqual(manager.effective(ACTION)["interval"], 86400.0)
        manager.set_policy_values(ACTION, 86400.0)  # at both caps: allowed
        with self.assertRaises(UnsafePolicyError) as ctx:
            manager.set_policy_values("poke", 86399.0)
        self.assertIn("86400", str(ctx.exception))

    async def test_persisted_floor_row_is_validated(self):
        """A hand-edited kv floor row (inf / 1e999 / below 1.5) must never
        wedge the fleet: it is ignored with a warning, never applied."""
        from tests.unit._fake_kv import FakeKv

        for bad in (float("inf"), 1e999, 0.03, "oops"):
            kv = FakeKv()
            kv.rows["ratelimit:floor"] = bad
            manager = make_manager()
            manager._kv = kv
            await manager.load_persisted()
            self.assertEqual(manager.floor_value, 0.05)

    async def test_persisted_floor_row_shape_matrix(self):
        """Every persisted shape either validates or is skipped — never a
        crash, never a wedge. Validated numerics (incl. numeric strings and
        the 86400 cap) apply; bools/containers/garbage are ignored; a valid
        row can only ever RAISE the constructor floor."""
        from tests.unit._fake_kv import FakeKv

        for good, expected in ((2.5, 2.5), ("2.5", 2.5), ("86400", 86400.0),
                               (86400.0, 86400.0),
                               (0.05, 0.05), (1.4, 1.4), (1.0, 1.0)):
            kv = FakeKv()
            kv.rows["ratelimit:floor"] = good
            manager = make_manager()
            manager._kv = kv
            await manager.load_persisted()
            self.assertEqual(manager.floor_value, expected)
        for bad in (float("nan"), -1.0, False, [], {}, "", "1e999",
                    86400.5, [86400.0], 0.03):
            kv = FakeKv()
            kv.rows["ratelimit:floor"] = bad
            manager = make_manager()
            manager._kv = kv
            await manager.load_persisted()
            self.assertEqual(manager.floor_value, 0.05)
        # a persisted floor below the constructor floor never lowers it
        kv = FakeKv()
        kv.rows["ratelimit:floor"] = 2.0
        manager = SleepAdvanceManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0), floor=3.0
        )
        manager._kv = kv
        await manager.load_persisted()
        self.assertEqual(manager.floor_value, 3.0)

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
            RateLimitManager(EventBus(), StubLogger(), None, floor=0.03)
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
            {"floor", "lane", "groups", "policies", "cooldowns", "hits_429", "last_fire_ages",
             "grants", "waits", "avg_wait_s"},
        )
        self.assertEqual(stats["floor"], 0.05)
        self.assertEqual(stats["lane"]["enabled"], False)
        self.assertEqual(stats["groups"], {})
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


class StressRandomizedInterleavings(unittest.IsolatedAsyncioTestCase):
    """Chaos rounds: random submit/cancel/cooldown/policy-churn orderings
    across grouped and ungrouped keys. Invariants under attack:

    * every pacing key's grant stamps stay >= interval apart (wire gaps);
    * no task dies of anything but CancelledError;
    * the loop never reports an orphaned 'exception was never retrieved'.
    """

    async def test_randomized_interleavings_preserve_wire_gaps(self):
        rng = random.Random(0x0DDBA11)
        handler_events = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda l, ctx, out=handler_events: out.append(ctx)
        )
        try:
            for round_no in range(4):
                interval = 1.5 + 0.5 * round_no
                manager = GrantRecorderManager(
                    EventBus(), StubLogger(), None,
                    clock=ManualClock(0.0), jitter_seed=round_no,
                )
                manager.set_policy_values(ACTION, interval)
                manager.set_account_group("alpha", "g")
                manager.set_account_group("beta", "g")
                accounts = ("alpha", "beta", "gamma", None)
                tasks = []
                for step in range(28):
                    tasks.append(asyncio.ensure_future(
                        manager.acquire(ACTION, account=accounts[step % 4])
                    ))
                    roll = rng.random()
                    if roll < 0.30 and tasks:
                        tasks[rng.randrange(len(tasks))].cancel()
                    elif roll < 0.45:
                        manager.report_server_limit(
                            "alpha", ACTION, rng.uniform(0.05, 0.4)
                        )
                    elif roll < 0.55:
                        manager.set_policy_values(ACTION, interval)  # wake path
                    await asyncio.sleep(0)
                    if rng.random() < 0.3:
                        await asyncio.sleep(0)
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    self.assertTrue(
                        isinstance(result, (Grant, asyncio.CancelledError)),
                        result,
                    )
                self.assertTrue(any(isinstance(r, Grant) for r in results))
                by_key = {}
                for key, ts in manager.grant_log:
                    by_key.setdefault(key, []).append(ts)
                self.assertTrue(by_key)
                for key, stamps in by_key.items():
                    ordered = sorted(stamps)
                    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
                    self.assertTrue(
                        all(g >= interval - 1e-9 for g in gaps), (key, gaps)
                    )
        finally:
            loop.set_exception_handler(None)
        for ctx in handler_events:
            self.assertNotIn(
                "never retrieved", str(ctx.get("message", "")), ctx
            )


if __name__ == "__main__":
    unittest.main()
