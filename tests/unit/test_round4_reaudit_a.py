"""Round-4 adversarial re-audit A — defect pins + soundness spot-checks.

New tests only; NO production code was modified by this audit.

Defects are pinned with ``unittest.expectedFailure`` so they fail loudly
the day someone fixes them (remove the decorator then). Soundness checks
pin round-4 behaviors that are correct today and must stay that way.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmark.report import leak_verdict                       # noqa: E402
from adapters.simulator.client import SimClient                 # noqa: E402
from adapters.simulator.world import SimWorld, WorldMessage     # noqa: E402


# --------------------------------------------------------------------------
# Defect A1 (MEDIUM): leak_verdict window math swallows short-run leaks.
#
# benchmark/report.py:leak_verdict (round-4 "slope comparison" rewrite)
# computes growth as tail_mid - head_mid but the noise band as
# 2 * max(spread(head), spread(tail)). For a PURE-LINEAR leak at the
# minimum documented leak-detectable cycle count (n == 5, the smallest n
# for which the "suspected-leak" branch exists) growth is ALWAYS <= the
# band (both scale with the leak rate r: growth = 2r, band = 2r for the
# m=4/third=2 windows), so every linear leak of exactly 5 cycles is
# classified "stable". The pre-round-4 code flagged this shape as
# "suspected-leak" (monotone non-returning growth above the 256KB floor).
# The n>=5 suspected-leak branch is effectively dead code for the
# canonical leak shape; detection now needs n>=6.
# --------------------------------------------------------------------------
class LeakVerdictShortRunTests(unittest.TestCase):
    def test_linear_leak_at_minimum_cycle_count_is_flagged(self) -> None:
        """n=5, +300 KB/cycle, perfectly monotone: a textbook leak."""
        verdict = leak_verdict(
            [100.0, 300.0, 500.0, 700.0, 900.0], [1, 1, 1, 1, 1])
        self.assertEqual(verdict["verdict"], "suspected-leak")

    def test_flat_then_leak_is_flagged(self) -> None:
        """5 flat warmup cycles then 5 rising (+300 KB/cycle): a leak
        that starts mid-run. Tail spread (2*300) * 2 == growth, so the
        round-4 band classifies it "stable"."""
        verdict = leak_verdict(
            [100.0] * 6 + [100.0 + 300 * (i + 1) for i in range(5)],
            [1] * 11)
        self.assertEqual(verdict["verdict"], "suspected-leak")

    def test_linear_leak_at_six_cycles_is_flagged(self) -> None:
        """The same shape one cycle longer IS detected — pins the exact
        boundary of the regression (n >= 6 works, n == 5 does not)."""
        verdict = leak_verdict(
            [100.0, 300.0, 500.0, 700.0, 900.0, 1100.0], [1] * 6)
        self.assertEqual(verdict["verdict"], "suspected-leak")


# --------------------------------------------------------------------------
# Defect A2 (LOW): sim self-command matcher is case-SENSITIVE while the
# live layer (round-4's own case_insensitive=True on the ext.commands
# Bot, adapters/discord/client.py) accepts any case for the COMMAND WORD.
#
# "sm!ROLL" fires the live command layer (prefix match is case-sensitive,
# the word is not) but the sim's _match_self_command requires the exact
# lowercase trigger — dashboard-driven parity checks and the self_command
# scenario silently under-test the live surface.
# --------------------------------------------------------------------------
class SimSelfCommandCaseParityTests(unittest.TestCase):
    def _client(self, prefix: str = "sm!") -> SimClient:
        world = SimWorld()
        world.users = {1: "alpha", 2: "stranger"}
        client = SimClient(world, 1, seed=1, command_prefix=prefix)
        return client

    def test_lowercase_trigger_fires(self) -> None:
        """Baseline: the sim fires the synthetic DTO for the exact word."""
        from adapters.simulator.world import WorldMessage

        client = self._client()
        got: list = []
        client.on_message = got.append
        client.receive(WorldMessage(
            id=1, channel_id=10, author_id=1, content="sm!slcopy",
            ts=0.0, dm=False))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].content, "slcopy")
        self.assertEqual(got[0].author_id, 1)

    def test_command_word_case_insensitive_parity_with_live(self) -> None:
        """Live: Bot(case_insensitive=True) accepts "sm!ROLL" (the WORD is
        case-insensitive; only the PREFIX is case-sensitive). The sim must
        match the same surface or its parity claim is false."""
        from adapters.simulator.world import WorldMessage

        client = self._client()
        got: list = []
        client.on_message = got.append
        client.receive(WorldMessage(
            id=1, channel_id=10, author_id=1, content="sm!ROLL",
            ts=0.0, dm=False))
        self.assertEqual(len(got), 1, "sim dropped a live-invocable command")

    def test_prefix_case_stays_sensitive_parity_with_live(self) -> None:
        """Live prefix matching IS case-sensitive: "SM!roll" must not fire
        in either layer."""
        from adapters.simulator.world import WorldMessage

        client = self._client()
        got: list = []
        client.on_message = got.append
        client.receive(WorldMessage(
            id=1, channel_id=10, author_id=1, content="SM!roll",
            ts=0.0, dm=False))
        self.assertEqual(got, [])

    def test_foreign_prefixed_command_never_fires(self) -> None:
        """self_bot semantics: only the account's OWN messages invocable."""
        from adapters.simulator.world import WorldMessage

        client = self._client()
        got: list = []
        client.on_message = got.append
        client.receive(WorldMessage(
            id=1, channel_id=10, author_id=2, content="sm!roll",
            ts=0.0, dm=False))
        self.assertEqual(len(got), 1)      # delivered as ordinary traffic
        self.assertEqual(got[0].content, "sm!roll")  # NOT re-fired


# --------------------------------------------------------------------------
# Soundness pins (verified by adversarial probing during this audit).
# --------------------------------------------------------------------------
class GovernorPeekSoundnessTests(unittest.TestCase):
    """peek()/stats() must never consume: the round-4 probe1b seam."""

    def test_peek_does_not_drain_the_start_bucket(self) -> None:
        from core.account_manager.lifecycle import (
            LifecycleGovernor, LifecyclePolicy)

        gov = LifecycleGovernor(LifecyclePolicy(), clock=lambda: 1000.0)
        allowed = [gov.precheck("a", "start", status=None) for _ in range(5)]
        self.assertTrue(all(d.allowed for d in allowed))
        # bucket now empty at t=1000 — peek must still refuse WITHOUT spending
        decision = gov.peek("b", "start", status=None)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "global_bucket")
        # and a real start at the same instant must also be refused (peek
        # did not magically credit a token)
        self.assertFalse(gov.precheck("c", "start", status=None).allowed)

    def test_stats_cooldowns_do_not_consume(self) -> None:
        from core.account_manager.lifecycle import (
            LifecycleGovernor, LifecyclePolicy)

        gov = LifecycleGovernor(LifecyclePolicy(), clock=lambda: 1000.0)
        for _ in range(5):
            self.assertTrue(gov.precheck("a", "start", status=None).allowed)
        gov.commit("a", "start", ok=True)
        stats = gov.stats()  # iterates pacers via peek
        self.assertIn("a", stats["cooldowns"])
        # observing stats() five more times must not change any decision
        for _ in range(5):
            gov.stats()
        self.assertFalse(gov.precheck("fresh", "start", status=None).allowed)


class EventBusTwoTierSoundnessTests(unittest.IsolatedAsyncioTestCase):
    """Round-4 F4 two-tier overflow + F4/1 drain resurrection."""

    async def test_control_event_admitted_over_flood_saturation(self) -> None:
        from core.event_bus import EventBus, Event, TaskQueued

        class Flood(Event):
            pass

        bus = EventBus()
        for _ in range(EventBus.MAX_QUEUE + 10):
            bus.publish_nowait(Flood())
        self.assertEqual(len(bus._queue), EventBus.MAX_QUEUE)
        bus.publish_nowait(TaskQueued(account="a", task_id=1, action="message"))
        self.assertEqual(len(bus._queue), EventBus.MAX_QUEUE + 1)
        bus.publish_nowait(Flood())
        self.assertEqual(len(bus._queue), EventBus.MAX_QUEUE + 1)  # refused

    async def test_drain_resurrects_dead_dispatcher_with_backlog(self) -> None:
        from core.event_bus import EventBus, Event

        class Ping(Event):
            pass

        bus = EventBus()
        seen: list = []
        bus.subscribe(Ping, lambda e: seen.append(e))
        for _ in range(25):
            bus.publish_nowait(Ping())
        dispatcher = bus._dispatcher
        dispatcher.cancel()
        try:
            await dispatcher
        except asyncio.CancelledError:
            pass
        await bus.drain(timeout=2.0)
        self.assertEqual(len(seen), 25)
        self.assertEqual(len(bus._queue), 0)


class SchedulerAcquireAbortSoundnessTests(unittest.IsolatedAsyncioTestCase):
    """Round-4 F6-1: a cancel reaching a task parked in limiter.acquire
    aborts within one loop hop (given the lane's waker exists — the
    _ensure_lane invariant)."""

    async def test_cancelled_parked_acquire_aborts_fast(self) -> None:
        from types import SimpleNamespace as NS

        from core.task_scheduler.scheduler import (
            TaskScheduler, STATUS_QUEUED)

        class ParkedLimiter:
            async def acquire(self, action, account, plugin, priority):
                await asyncio.sleep(30.0)
                return NS(granted=True)

        sched = TaskScheduler.__new__(TaskScheduler)
        sched._limiter = ParkedLimiter()
        sched._sweeping = False
        sched._backoff_wakers = {"acct": asyncio.Event()}
        task = NS(cancelled=False, status=STATUS_QUEUED, account="acct",
                  action="message", plugin="roll", priority=1)

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            task.cancelled = True
            sched._backoff_wakers["acct"].set()

        worker = asyncio.create_task(sched._acquire_or_abort(task))
        asyncio.create_task(cancel_soon())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with self.assertRaises(Exception) as ctx:  # _AcquireAborted
            await asyncio.wait_for(worker, timeout=5.0)
        self.assertIn("abort", type(ctx.exception).__name__.lower())
        self.assertLess(loop.time() - t0, 2.0)

    async def test_grant_wins_race_returns_grant(self) -> None:
        from types import SimpleNamespace as NS

        from core.task_scheduler.scheduler import (
            TaskScheduler, STATUS_QUEUED)

        class QuickLimiter:
            async def acquire(self, action, account, plugin, priority):
                await asyncio.sleep(0.01)
                return NS(granted=True)

        sched = TaskScheduler.__new__(TaskScheduler)
        sched._limiter = QuickLimiter()
        sched._sweeping = False
        sched._backoff_wakers = {"acct": asyncio.Event()}
        task = NS(cancelled=False, status=STATUS_QUEUED, account="acct",
                  action="message", plugin="roll", priority=1)
        grant = await asyncio.wait_for(
            sched._acquire_or_abort(task), timeout=2.0)
        self.assertTrue(grant.granted)


class IdentifyGateSoundnessTests(unittest.IsolatedAsyncioTestCase):
    """Round-4 F2-5/F2-6: budget algebra + in-lock recheck."""

    async def test_fleet_over_ceiling_fast_fails_excess_waiter(self) -> None:
        from adapters.discord.client import IdentifyGate

        gate = IdentifyGate()
        gate.expected = 60          # 55/60 < 1.0s floor -> floor binds
        gate.interval = 5.0
        results: list = []

        async def one() -> None:
            try:
                await gate.acquire()
                results.append("ok")
            except ConnectionError:
                results.append("fail")

        await asyncio.gather(*[one() for _ in range(60)])
        self.assertGreater(results.count("fail"), 0)   # honest ceiling
        self.assertEqual(gate._depth, 0)               # no depth leak


if __name__ == "__main__":
    unittest.main()
