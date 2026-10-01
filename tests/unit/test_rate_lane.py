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
        await asyncio.wait_for(waiter, timeout=3.0)      # NOT the 10s sleep
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
    def __init__(self):
        self.rows = {}

    def get(self, key, default=None):
        return self.rows.get(key, default)

    def set(self, key, value):
        self.rows[key] = value


class TestLanePersistence(unittest.IsolatedAsyncioTestCase):
    async def test_kv_roundtrip(self) -> None:
        import json

        clock = ManualClock(0.0)
        kv = FakeKv()
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, clock=clock, kv_repo=kv,
            lane_enabled=True, lane_prefixrate=0.75,
        )
        self.assertTrue(manager.lane_status()["enabled"])
        self.assertEqual(manager.lane_status()["prefixrate"], 0.75)
        stored = json.loads(kv.rows["ratelimit:lane"])
        self.assertEqual(stored, {"enabled": True, "prefixrate": 0.75})

        manager.set_lane_enabled(False)
        manager.set_lane_prefixrate(2.0)
        stored = json.loads(kv.rows["ratelimit:lane"])
        self.assertEqual(stored, {"enabled": False, "prefixrate": 2.0})

        # a fresh manager on the same kv restores the persisted state
        second = RateLimitManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0), kv_repo=kv
        )
        self.assertFalse(second.lane_status()["enabled"])
        self.assertEqual(second.lane_status()["prefixrate"], 2.0)

    async def test_corrupt_kv_falls_back(self) -> None:
        kv = FakeKv()
        kv.rows["ratelimit:lane"] = "{not json"
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0), kv_repo=kv
        )
        self.assertFalse(manager.lane_status()["enabled"])  # safe default


if __name__ == "__main__":
    unittest.main()
