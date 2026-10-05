"""Unit tests for the GlobalSendLane (FarmLite-style sequential sends).

Deterministic via the same ManualClock + sleep-seam doubles the engine tests
use. Covers: strict serialization + prefixrate spacing, priority order,
pause-at-0 + resume, disable-mid-wait drain, wake-on-config-change (no
oversleep), cross-account serialization through acquire(), Grant.reason
"lane", stats/warnings surfacing, kv persistence, and input validation.
"""
from __future__ import annotations

import asyncio
import unittest

from core.errors import RatePolicyError
from core.event_bus import EventBus
from core.priority import Priority
from core.rate_limit_manager import RateLimitManager
from core.rate_limit_manager.engine import GLOBAL_KEY, ManualClock
from core.rate_limit_manager.lane import GlobalSendLane

from tests.unit.test_rate_engine import (
    GatedSleepManager,
    SleepAdvanceManager,
    StubLogger,
    make_manager,
)

ACTION = "message"


class RealSleepManager(RateLimitManager):
    """Uses the REAL asyncio.sleep — for ordering/hold/drain/wake semantics
    that need genuine task interleaving (values stay tiny)."""


def make_lane_manager(manager_cls=SleepAdvanceManager, *, enabled=True, prefixrate=1.5):
    """Deterministic (ManualClock+seam) by default; RealSleepManager runs
    without an injected clock so real time actually moves."""
    clock = None if manager_cls is RealSleepManager else ManualClock(0.0)
    manager = manager_cls(
        EventBus(), StubLogger(), None, clock=clock, jitter_seed=7,
        lane_enabled=enabled, lane_prefixrate=prefixrate,
    )
    return manager


class TestLaneSerialization(unittest.IsolatedAsyncioTestCase):
    async def test_grants_strictly_sequential_with_spacing(self) -> None:
        """N waiters through the lane: exactly one grant per prefixrate
        window, in (priority, seq) order — the FarmLite guarantee."""
        manager = make_lane_manager(prefixrate=1.0)
        grant_times = []

        async def worker(i: int):
            await manager.acquire(ACTION, account=f"acct{i}")
            grant_times.append((i, manager._clock.now()))

        await asyncio.gather(*(worker(i) for i in range(5)))
        times = [t for _, t in grant_times]
        self.assertEqual(times, sorted(times))                     # ordered
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.95 for g in gaps), gaps)         # spaced
        self.assertEqual(manager.lane_status()["grants"], 5)

    async def test_priority_jumps_the_queue(self) -> None:
        manager = make_lane_manager(RealSleepManager, prefixrate=0.3)
        order = []
        await manager.acquire(ACTION, account="primer")  # stamp last_grant

        async def worker(i, prio):
            await manager.acquire(ACTION, account=f"a{i}", priority=prio)
            order.append(i)

        low = asyncio.create_task(worker(1, Priority.LOW))
        await asyncio.sleep(0.02)   # LOW queues behind the spacing window
        high = asyncio.create_task(worker(2, Priority.HIGH))
        await asyncio.sleep(0.02)
        low2 = asyncio.create_task(worker(3, Priority.LOW))
        await asyncio.gather(low, high, low2)
        self.assertEqual(order[0], 2)  # HIGH admitted first despite arriving 2nd

    async def test_disabled_lane_is_passthrough(self) -> None:
        manager = make_lane_manager(enabled=False)
        grant_times = []

        async def worker(i):
            await manager.acquire(ACTION, account=f"acct{i}")
            grant_times.append(manager._clock.now())

        await asyncio.gather(*(worker(i) for i in range(4)))
        self.assertEqual(grant_times, [0.0, 0.0, 0.0, 0.0])  # no queueing
        self.assertEqual(manager.lane_status()["grants"], 0)

    async def test_two_accounts_never_send_back_to_back(self) -> None:
        """Server-wide guarantee: interleaved accounts still spaced."""
        manager = make_lane_manager(prefixrate=2.0)
        times = []

        async def worker(acct):
            for _ in range(2):
                await manager.acquire(ACTION, account=acct)
                times.append(manager._clock.now())

        await asyncio.gather(worker("alpha"), worker("beta"))
        times.sort()
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 1.95 for g in gaps), gaps)


class TestLanePauseResume(unittest.IsolatedAsyncioTestCase):
    async def test_prefixrate_zero_pauses_and_resume_wakes(self) -> None:
        """FarmLite semantics: 0 holds every submission until a non-zero
        rate is set; setting one releases them."""
        manager = make_lane_manager(prefixrate=0.0)  # paused
        self.assertTrue(manager.lane_status()["paused"])
        done = []

        async def worker(i):
            await manager.acquire(ACTION, account=f"acct{i}")
            done.append(i)

        tasks = [asyncio.create_task(worker(i)) for i in range(3)]
        await asyncio.sleep(0.05)
        self.assertEqual(done, [])  # everyone holds

        manager.set_lane_prefixrate(0.5)  # resume
        await asyncio.gather(*tasks)
        self.assertEqual(sorted(done), [0, 1, 2])
        self.assertFalse(manager.lane_status()["paused"])

    async def test_pause_mid_flight_holds_later_waiters(self) -> None:
        manager = make_lane_manager(RealSleepManager, prefixrate=0.5)
        await manager.acquire(ACTION, account="alpha")  # t=0
        manager.set_lane_prefixrate(0.0)                # pause NOW
        second = asyncio.create_task(manager.acquire(ACTION, account="beta"))
        await asyncio.sleep(0.05)
        self.assertFalse(second.done())                 # holding
        manager.set_lane_prefixrate(1.0)
        await asyncio.wait_for(second, timeout=2.5)     # released ~1s later

    async def test_paused_lane_surfaces_a_warning(self) -> None:
        manager = make_lane_manager(prefixrate=0.0)
        codes = {w["code"] for w in manager.warnings()}
        self.assertIn("lane_paused", codes)


class TestLaneRobustness(unittest.IsolatedAsyncioTestCase):
    async def test_disable_mid_wait_drains_everyone(self) -> None:
        manager = make_lane_manager(RealSleepManager, prefixrate=5.0)
        await manager.acquire(ACTION, account="primer")  # stamp last_grant
        done = []

        async def worker(i):
            await manager.acquire(ACTION, account=f"acct{i}")
            done.append(i)

        tasks = [asyncio.create_task(worker(i)) for i in range(4)]
        await asyncio.sleep(0.05)
        self.assertEqual(done, [])
        manager.set_lane_enabled(False)  # drain!
        await asyncio.gather(*tasks)
        self.assertEqual(sorted(done), [0, 1, 2, 3])
        self.assertFalse(manager.lane_status()["enabled"])

    async def test_cancelled_head_never_blocks(self) -> None:
        manager = make_lane_manager(RealSleepManager, prefixrate=0.4)
        await manager.acquire(ACTION, account="primer")  # stamp last_grant
        lane = manager._lane
        first = asyncio.create_task(lane.enter(1))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(lane.enter(1))
        await asyncio.sleep(0.02)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        # second must complete within one spacing window despite the
        # cancelled entry having been ahead of it in the heap
        await asyncio.wait_for(second, timeout=1.0)
        self.assertGreaterEqual(lane.grants, 2)

    async def test_wake_on_prefixrate_change_no_oversleep(self) -> None:
        """A waiter parked on a long spacing must abandon the sleep when the
        prefixrate is lowered (FarmLite's set_prefixrate wake, made real)."""
        manager = make_lane_manager(RealSleepManager, prefixrate=10.0)
        await manager.acquire(ACTION, account="alpha")   # grant at t=0
        waiter = asyncio.create_task(manager.acquire(ACTION, account="beta"))
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        manager.set_lane_prefixrate(1.0)                 # wake + recompute
        # NOT the 10s sleep — budget generous enough for multi-second
        # loop stalls on a loaded host (wake → grant is ~1s at rate 1.0)
        await asyncio.wait_for(waiter, timeout=10.0)
        self.assertFalse(waiter.cancelled())

    async def test_validation(self) -> None:
        manager = make_lane_manager()
        with self.assertRaises(RatePolicyError):
            manager.set_lane_prefixrate(-1)
        with self.assertRaises(RatePolicyError):
            manager.set_lane_prefixrate(float("nan"))
        with self.assertRaises(RatePolicyError):
            manager.set_lane_prefixrate(10**9)
        manager.set_lane_prefixrate(0)  # pause is legal
        self.assertEqual(manager.lane_status()["prefixrate"], 0.0)

    async def test_constructor_falls_back_on_bad_prefixrate(self) -> None:
        """A malformed config value must never crash the constructor: the
        lane falls back to 1.5s AND the boot baseline is the sanitized
        value (the raw one used to survive into _lane_defaults)."""
        from tests.unit.test_rate_engine import StubLogger as _StubLogger

        for bad in ("abc", None, float("nan"), -3.0, 10**12):
            manager = RateLimitManager(
                EventBus(), _StubLogger(), None, lane_prefixrate=bad,
            )
            self.assertEqual(manager.lane_status()["prefixrate"], 1.5)
            self.assertEqual(manager._lane_defaults, (False, 1.5))

    async def test_constructor_sanitizes_every_malformed_shape(self) -> None:
        """True/False/'1.5'/[]/{} never crash; float()-coercible shapes land
        as their numeric value (bools become 1.0/0.0 — 0.0 only ever PAUSES,
        and paused() requires the lane enabled), everything else falls back
        to 1.5. _lane_defaults always mirrors the SANITIZED state."""
        cases = [
            (True, 1.0), (False, 0.0), ("1.5", 1.5), ("nan", 1.5),
            ("abc", 1.5), (None, 1.5), (-3.0, 1.5), (10**12, 1.5),
            ([], 1.5), ({}, 1.5), ([1.5], 1.5), ({"rate": 2.0}, 1.5),
        ]
        for raw, expected in cases:
            manager = RateLimitManager(
                EventBus(), StubLogger(), None, lane_prefixrate=raw
            )
            self.assertEqual(manager.lane_status()["prefixrate"], expected)
            self.assertEqual(manager._lane_defaults, (False, expected))

    async def test_load_persisted_never_resurrects_a_bad_ctor_value(self) -> None:
        """No kv row -> the boot baseline is the SANITIZED constructor
        state; the raw malformed value can never come back at boot."""
        kv = FakeKv()
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, kv_repo=kv, lane_prefixrate="abc"
        )
        await manager.load_persisted()
        self.assertEqual(manager.lane_status()["prefixrate"], 1.5)
        self.assertEqual(manager._lane_defaults, (False, 1.5))

    async def test_row_with_invalid_prefixrate_falls_back_whole(self) -> None:
        """A PARSEABLE row whose prefixrate is rejected (over the one-day
        cap) is corrupt as a unit: the constructor defaults apply for BOTH
        fields — the row's `enabled` must not be half-applied and the
        constructor's custom rate must not be silently replaced by 1.5."""
        kv = FakeKv()
        kv.rows["ratelimit:lane"] = {"enabled": True, "prefixrate": 1e9}
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0),
            kv_repo=kv, lane_enabled=False, lane_prefixrate=0.75,
        )
        await manager.load_persisted()
        status = manager.lane_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["prefixrate"], 0.75)


class TestLaneInGrant(unittest.IsolatedAsyncioTestCase):
    async def test_reason_and_wait_accounting(self) -> None:
        manager = make_lane_manager(prefixrate=1.0)
        await manager.acquire(ACTION, account="alpha")            # t=0
        grant = await manager.acquire(ACTION, account="beta")     # waited ~1s
        self.assertEqual(grant.reason, "lane")
        self.assertGreaterEqual(grant.wait_s, 0.9)

    async def test_server_cooldown_still_wins_reason(self) -> None:
        manager = make_lane_manager(prefixrate=1.0)
        manager.report_server_limit("alpha", ACTION, 3.0)
        grant = await manager.acquire(ACTION, account="alpha")
        self.assertEqual(grant.reason, "server")


class FakeKv:
    """Async kv-repo double (the real KvRepository is aiosqlite-backed)."""

    def __init__(self):
        self.rows = {}

    async def get(self, key, default=None):
        return self.rows.get(key, default)

    async def set(self, key, value):
        self.rows[key] = value


class TestLanePersistence(unittest.IsolatedAsyncioTestCase):
    """Lane persistence split: the ENGINE keeps memory only; the kv row is
    READ at boot (``await load_persisted()``) and WRITTEN by the ControlAPI
    layer. These tests pin the engine side of that contract."""

    async def test_kv_roundtrip_via_load_persisted(self) -> None:
        import json

        clock = ManualClock(0.0)
        kv = FakeKv()
        kv.rows["ratelimit:lane"] = json.dumps(
            {"enabled": True, "prefixrate": 0.75}
        )
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, clock=clock, kv_repo=kv,
            lane_enabled=False, lane_prefixrate=1.5,
        )
        # constructor defaults apply until the persisted row is loaded
        self.assertFalse(manager.lane_status()["enabled"])
        await manager.load_persisted()
        self.assertTrue(manager.lane_status()["enabled"])
        self.assertEqual(manager.lane_status()["prefixrate"], 0.75)

        # engine mutations are MEMORY-ONLY now (ControlAPI persists)
        manager.set_lane_enabled(False)
        manager.set_lane_prefixrate(2.0)
        lane = manager.lane_status()
        self.assertFalse(lane["enabled"])
        self.assertEqual(lane["prefixrate"], 2.0)
        self.assertEqual(
            json.loads(kv.rows["ratelimit:lane"]),
            {"enabled": True, "prefixrate": 0.75},  # the kv row is untouched
        )

    async def test_ctor_defaults_apply_without_kv_row(self) -> None:
        kv = FakeKv()
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, kv_repo=kv,
            lane_enabled=True, lane_prefixrate=0.75,
        )
        await manager.load_persisted()  # no row -> constructor defaults win
        self.assertTrue(manager.lane_status()["enabled"])
        self.assertEqual(manager.lane_status()["prefixrate"], 0.75)

    async def test_corrupt_kv_row_falls_back_to_defaults(self) -> None:
        kv = FakeKv()
        kv.rows["ratelimit:lane"] = "{not json"
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, kv_repo=kv,
            lane_enabled=True, lane_prefixrate=0.75,
        )
        await manager.load_persisted()  # logged + skipped, never fatal
        self.assertTrue(manager.lane_status()["enabled"])
        self.assertEqual(manager.lane_status()["prefixrate"], 0.75)

    async def test_corrupt_kv_falls_back(self) -> None:
        kv = FakeKv()
        kv.rows["ratelimit:lane"] = "{not json"
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0), kv_repo=kv
        )
        self.assertFalse(manager.lane_status()["enabled"])  # safe default
        await manager.load_persisted()
        self.assertFalse(manager.lane_status()["enabled"])  # still safe
        self.assertEqual(manager.lane_status()["prefixrate"], 1.5)

    async def test_string_bool_enabled_row_is_parsed_not_rejected(self) -> None:
        """Round-3: a hand-edited "true"/"false" STRING in 'enabled' used
        to work (truthy) before round-2's strict-bool check made the whole
        row corrupt — parse the two spellings explicitly; anything else is
        still corrupt and falls back to the constructor defaults."""
        kv = FakeKv()
        kv.rows["ratelimit:lane"] = {"enabled": "true", "prefixrate": 0.75}
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, kv_repo=kv,
            lane_enabled=False, lane_prefixrate=1.5,
        )
        await manager.load_persisted()
        self.assertTrue(manager.lane_status()["enabled"])
        self.assertEqual(manager.lane_status()["prefixrate"], 0.75)

        kv = FakeKv()
        kv.rows["ratelimit:lane"] = {"enabled": "False", "prefixrate": 2.0}
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, kv_repo=kv,
            lane_enabled=True, lane_prefixrate=1.5,
        )
        await manager.load_persisted()
        self.assertFalse(manager.lane_status()["enabled"])  # "False" honored
        self.assertEqual(manager.lane_status()["prefixrate"], 2.0)

        for bad in ("yes", "off", "1", 1, 0, None, [True]):
            kv = FakeKv()
            kv.rows["ratelimit:lane"] = {"enabled": bad, "prefixrate": 0.75}
            manager = RateLimitManager(
                EventBus(), StubLogger(), None, kv_repo=kv,
                lane_enabled=False, lane_prefixrate=1.5,
            )
            await manager.load_persisted()  # logged + skipped, never fatal
            self.assertFalse(manager.lane_status()["enabled"], bad)
            self.assertEqual(manager.lane_status()["prefixrate"], 1.5)


class TestLaneWireGapsWithJitter(unittest.IsolatedAsyncioTestCase):
    """Round-3 wire-gap fix (probe P2/P7): the lane clock is stamped at
    admission + the caller's jitter draw (the expected wire instant), so a
    much smaller second draw can no longer compress or INVERT the
    cross-account wire gap below prefixrate."""

    async def test_lane_wire_gap_holds_under_jitter_differential(self) -> None:
        # seed 1217: draws ~2.880 then ~0.150 in (0, 3) — under the old
        # post-grant-only jitter the second wire left 1.23s BEFORE the
        # first (probe P7's inversion)
        clock = ManualClock(0.0)
        manager = SleepAdvanceManager(
            EventBus(), StubLogger(), None, clock=clock, jitter_seed=1217,
            lane_enabled=True, lane_prefixrate=1.5,
        )
        manager.set_policy_values(ACTION, 1.5, 0.0, 3.0)
        wires = []
        for account in ("acctA", "acctB", "acctC"):
            await manager.acquire(ACTION, account=account)
            wires.append(clock.now())  # WIRE instant = post-jitter return
        gaps = [b - a for a, b in zip(wires, wires[1:])]
        self.assertTrue(all(g >= 1.5 - 1e-9 for g in gaps), gaps)


class TestLaneGrantIntegrity(unittest.IsolatedAsyncioTestCase):
    """A waiter cancelled BETWEEN its gate grant and the user code must not
    leak its spacing slot: the stamp was taken at grant time, so the next
    waiter still gets spaced admission."""

    async def test_cancel_after_grant_still_spaces_successor(self) -> None:
        async def instant_wait(seconds: float) -> None:
            await asyncio.sleep(0)

        lane = GlobalSendLane(
            clock=ManualClock(0.0),
            wait_either=instant_wait,
            on_change=lambda: None,
            log=lambda level, message: None,
        )
        lane.set_enabled(True)
        lane.set_prefixrate(1.0)
        await lane.enter(1)  # prime: stamps _last_grant at t=0
        first = asyncio.create_task(lane.enter(1))
        for _ in range(10):
            await asyncio.sleep(0)  # parks behind the 1.0 spacing window
        self.assertFalse(first.done())
        # grant the head from OUTSIDE while `first` is suspended, then
        # cancel it BEFORE it resumes — all in one synchronous section
        lane._clock.advance(1.0)
        lane._grant_ready()
        stamp = lane._last_grant
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(lane.enter(1))
        ticks = 0
        while not second.done() and ticks < 200:
            lane._clock.advance(0.1)
            await asyncio.sleep(0)
            ticks += 1
        await second
        self.assertGreaterEqual(lane._clock.now(), stamp + 1.0)
        self.assertGreaterEqual(lane.grants, 3)


class TestLanePauseStampede(unittest.TestCase):
    """prefixrate=0 with a pile of waiters, then resume: no starvation
    (everyone completes), no stampede (grants stay >= prefixrate apart).

    Plain TestCase + asyncio.run: 50 waiters x ~25 parks each is ~100x
    slower under IsolatedAsyncioTestCase's asyncio debug mode, and this
    scenario needs none of its machinery.
    """

    def test_fifty_waiters_pause_resume(self) -> None:
        asyncio.run(self._scenario())

    async def _scenario(self) -> None:
        manager = make_lane_manager(prefixrate=0.0)  # paused from boot
        lane = manager._lane
        stamps: list = []
        original = lane._grant_ready

        def recording_ready():
            before = lane._last_grant
            original()
            if lane._last_grant is not None and lane._last_grant != before:
                stamps.append(lane._last_grant)

        lane._grant_ready = recording_ready
        done = []

        async def worker(i: int) -> None:
            await manager.acquire(ACTION, account=f"acct{i}")
            done.append(i)

        tasks = [asyncio.create_task(worker(i)) for i in range(50)]
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(done, [])  # all 50 hold while paused

        manager.set_lane_prefixrate(0.02)  # resume
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        self.assertEqual(sorted(done), list(range(50)))  # no starvation
        self.assertEqual(len(stamps), 50)
        ordered = sorted(stamps)
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        self.assertTrue(all(g >= 0.02 - 1e-9 for g in gaps), gaps[:5])


class TestLaneChaos(unittest.IsolatedAsyncioTestCase):
    """Randomized submit/cancel/pause/resume orderings: consecutive lane
    grant stamps stay >= prefixrate apart, nobody starves, nothing leaks
    exception warnings into the loop."""

    async def test_randomized_lane_chaos_keeps_spacing(self) -> None:
        import random

        rng = random.Random(42)
        rate = 0.05
        manager = make_lane_manager(prefixrate=rate)
        lane = manager._lane
        stamps: list = []
        original = lane._grant_ready

        def recording_ready():
            before = lane._last_grant
            original()
            if lane._last_grant is not None and lane._last_grant != before:
                stamps.append(lane._last_grant)

        lane._grant_ready = recording_ready
        handler_events = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda l, ctx, out=handler_events: out.append(ctx)
        )
        try:
            tasks = []
            for step in range(30):
                tasks.append(asyncio.create_task(
                    manager.acquire(ACTION, account=f"a{step % 5}")
                ))
                roll = rng.random()
                if roll < 0.25 and tasks:
                    tasks[rng.randrange(len(tasks))].cancel()
                elif roll < 0.35:
                    manager.set_lane_prefixrate(0.0)  # pause mid-flight
                elif roll < 0.50:
                    manager.set_lane_prefixrate(rate)  # resume
                await asyncio.sleep(0)
            manager.set_lane_prefixrate(rate)  # never leave it paused
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=30
            )
        finally:
            loop.set_exception_handler(None)
        from core.rate_limit_manager import Grant

        for result in results:
            self.assertTrue(
                isinstance(result, (Grant, asyncio.CancelledError)), result
            )
        self.assertTrue(any(isinstance(r, Grant) for r in results))
        ordered = sorted(stamps)
        self.assertGreater(len(ordered), 1)
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        self.assertTrue(all(g >= rate - 1e-9 for g in gaps), gaps[:5])
        for ctx in handler_events:
            self.assertNotIn("never retrieved", str(ctx.get("message", "")), ctx)


if __name__ == "__main__":
    unittest.main()
