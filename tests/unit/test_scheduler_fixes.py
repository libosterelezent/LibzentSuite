"""Regression tests for the audited scheduler/event-bus fixes.

Covers, one class per audited finding:
  (a) cancel-while-parked-in-acquire: the acquire is preemptible, so the
      lane serves the NEXT queued task promptly instead of wedging for
      the whole (429) cooldown, and the cancelled task is terminal;
  (b) budget-expiry is TERMINAL: the sender may already have the request
      on the wire, so the sender is invoked EXACTLY ONCE (no duplicate
      send) even with retry budget left;
  (c) control-plane events are never dropped by main-queue overflow;
  (d) the Python 3.10 (no Task.cancelling) isolation path: an internally
      cancelling handler must not wedge the dispatcher;
  (e) LOW anti-starvation aging (LOW_STARVATION_S) fires for an aged LOW
      task under sustained NORMAL load, while a young LOW still waits
      strictly behind every NORMAL.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from core.event_bus import Event, EventBus, MessageReceived, TaskCancelled, TaskCompleted
from core.priority import Priority
from core.task_scheduler import Task, TaskScheduler
from core.task_scheduler.scheduler import LOW_STARVATION_S

from tests.unit.test_task_scheduler import (
    FakeAudit,
    FakeGrant,
    FakeHistoryRepo,
    FakeLimiter,
    FakeLogger,
)


# --------------------------------------------------------- (a) preemption
class _HugeCooldownLimiter:
    """Stub limiter whose FIRST acquire parks for a full cooldown (60s).

    Later acquires return immediately — the preemption is what frees the
    cooldown for the account's next task.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.acquire_calls = 0

    async def acquire(self, action, account, plugin, priority):
        self.acquire_calls += 1
        self.entered.set()
        if self.acquire_calls == 1:
            await asyncio.sleep(60.0)  # MAX_INTERVAL-scale cooldown
        return FakeGrant(action=action, account=account, plugin=plugin)

    def report_server_limit(self, *args, **kwargs):  # pragma: no cover
        pass


class TestCancelWhileInAcquire(unittest.IsolatedAsyncioTestCase):
    def _scheduler(self, limiter, sender) -> TaskScheduler:
        scheduler = TaskScheduler(
            EventBus(), FakeLogger(), limiter, sender, FakeAudit(),
            default_timeout=5.0, history_repo=FakeHistoryRepo(),
        )
        scheduler.start()
        self.addAsyncCleanup(scheduler.stop, drain=False, timeout=2.0)
        return scheduler

    async def test_cancel_preempts_acquire_and_frees_the_lane(self) -> None:
        limiter = _HugeCooldownLimiter()
        sent: list = []

        async def sender(task: Task):
            sent.append(task.id)
            return None

        scheduler = self._scheduler(limiter, sender)
        t1 = scheduler.submit("alpha", "p", "message", channel_id=1)
        self.assertTrue(await asyncio.wait_for(limiter.entered.wait(), 2.0))
        t2 = scheduler.submit("alpha", "p", "message", channel_id=2)  # queued

        started = time.monotonic()
        self.assertTrue(scheduler.cancel(t1.id))
        self.assertEqual(t1.status, "cancelled")  # terminal immediately

        # the NEXT task gets its turn promptly (nowhere near the 60s park)
        deadline = time.monotonic() + 1.0
        while not sent and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        self.assertTrue(sent, "lane stayed wedged in the cancelled acquire")
        self.assertLess(time.monotonic() - started, 1.0)

        while not t2.is_terminal and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual(t2.status, "completed")
        self.assertEqual(sent, [t2.id])  # t1 never reached the sender
        self.assertEqual(t1.status, "cancelled")

    async def test_cancel_running_parked_in_acquire_terminal_cancelled(self) -> None:
        """A task parked in acquire is status-QUEUED; cancel drives it
        terminal CANCELLED and emits the TaskCancelled ack."""
        from tests.unit.test_task_scheduler import EventRecorder

        limiter = _HugeCooldownLimiter()

        async def sender(task: Task):
            return None

        scheduler = self._scheduler(limiter, sender)
        recorder = EventRecorder(scheduler._bus)
        self.addAsyncCleanup(recorder.close)
        task = scheduler.submit("alpha", "p", "message", channel_id=1)
        self.assertTrue(await asyncio.wait_for(limiter.entered.wait(), 2.0))

        self.assertTrue(scheduler.cancel(task.id))
        self.assertEqual(task.status, "cancelled")
        await asyncio.sleep(0.05)  # let the finish tail publish the event
        cancelled = [e for e in recorder.of(TaskCancelled) if e.task_id == task.id]
        self.assertEqual(len(cancelled), 1)
        # acquire was entered once; the preemption aborted it before t2
        self.assertEqual(limiter.acquire_calls, 1)
        await asyncio.sleep(0.1)  # the parked 60s acquire stays aborted
        self.assertEqual(limiter.acquire_calls, 1)


# ------------------------------------------------- (b) budget-expiry terminal
class TestBudgetExpiryTerminal(unittest.IsolatedAsyncioTestCase):
    async def test_budget_expiry_is_terminal_and_sender_runs_once(self) -> None:
        calls: list = []

        async def slow_sender(task: Task):
            calls.append(task.id)
            await asyncio.sleep(0.5)  # blows the 0.05s attempt budget

        scheduler = TaskScheduler(
            EventBus(), FakeLogger(), FakeLimiter(), slow_sender, FakeAudit(),
            default_timeout=0.05, history_repo=FakeHistoryRepo(),
        )
        scheduler.start()
        self.addAsyncCleanup(scheduler.stop, drain=False, timeout=2.0)
        task = scheduler.submit("alpha", "p", "message", retries=5)

        deadline = time.monotonic() + 3.0
        while not task.is_terminal and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertEqual(task.status, "timed_out")  # terminal timeout...
        self.assertEqual(len(calls), 1)  # ...sender invoked EXACTLY ONCE
        self.assertEqual(task.attempts, 0)  # no retry consumed
        self.assertIn("outcome unknown", task.error)  # documented rationale
        self.assertIsNone(task.wire_sent)  # unknown -> audited (safe side)


# ------------------------------------------- (c) control-plane never dropped
def _msg(i: int) -> MessageReceived:
    return MessageReceived(account="a", message_id=i, channel_id=1,
                           author_id=1, content=f"m{i}")


class TestControlPlaneNeverDropped(unittest.IsolatedAsyncioTestCase):
    async def test_task_acks_survive_main_queue_overflow(self) -> None:
        bus = EventBus()
        bus.MAX_QUEUE = 8  # tiny main bound for the test
        gate = asyncio.Event()
        acks: list = []

        async def slow(event: Event) -> None:  # parks the dispatcher
            await gate.wait()

        bus.subscribe(MessageReceived, slow)
        bus.subscribe(TaskCompleted, lambda e: acks.append(e.task_id))

        bus.publish_nowait(_msg(0))  # dispatcher parks inside slow_handler
        await asyncio.sleep(0.02)
        ack_ids = []
        for i in range(1, 40):
            bus.publish_nowait(_msg(i))  # main queue overflows (drop-newest)
            if i % 2 == 0:
                ack_ids.append(i)
                bus.publish_nowait(TaskCompleted(account="a", task_id=i))

        stats = bus.stats()
        self.assertGreater(stats["dropped"], 0)  # main queue degraded...
        self.assertEqual(stats["control_dropped"], 0)  # ...acks did not
        self.assertGreater(stats["control_queued"], 0)  # they were routed

        gate.set()
        await bus.drain(timeout=10.0)
        self.assertEqual(acks, ack_ids)  # EVERY Task* ack was delivered
        self.assertEqual(bus.stats()["control_queued"], 0)

    async def test_control_queue_priority_over_main(self) -> None:
        """Ack events drain ahead of older main-queue telemetry."""
        bus = EventBus()
        order: list = []
        gate = asyncio.Event()

        async def slow(event: Event) -> None:
            await gate.wait()

        bus.subscribe(MessageReceived, slow)
        bus.subscribe(Event, lambda e: order.append(type(e).__name__))
        bus.publish_nowait(_msg(0))
        await asyncio.sleep(0.02)
        for i in range(1, 6):
            bus.publish_nowait(_msg(i))
        bus.publish_nowait(TaskCompleted(account="a", task_id=99))
        gate.set()
        await bus.drain(timeout=5.0)
        # msg(0) was already in flight, but the ack outranked the five
        # earlier-queued main events
        self.assertEqual(order[0], "MessageReceived")
        self.assertEqual(order[1], "TaskCompleted")
        self.assertEqual(order.count("MessageReceived"), 6)


# ------------------------------------------- (d) 3.10 isolation (no wedge)
class TestPy310IsolationPath(unittest.IsolatedAsyncioTestCase):
    """Simulate an interpreter without Task.cancelling: the bus helpers
    answer 'unattributable' (cancellation_attributable -> False,
    outer_cancel_pending -> True), which is exactly what 3.10 produces."""

    def _patch_310(self):
        import core.event_bus as eb
        return (
            mock.patch.object(eb, "cancellation_attributable",
                              lambda task=None: False),
            mock.patch.object(eb, "outer_cancel_pending",
                              lambda task=None, *, unknown=False: unknown or True),
        )

    async def test_internal_handler_cancel_does_not_wedge(self) -> None:
        bus = EventBus()
        seen: list = []

        def poison(event: Event) -> None:
            raise asyncio.CancelledError()  # internal, not a task cancel

        bus.subscribe(MessageReceived, poison)
        bus.subscribe(MessageReceived, lambda e: seen.append(e.message_id))

        p1, p2 = self._patch_310()
        with p1, p2:
            bus.publish_nowait(_msg(0))
            bus.publish_nowait(_msg(1))  # must still be delivered
            await bus.drain(timeout=5.0)

        self.assertEqual(seen, [0, 1])  # no wedge: everything delivered
        self.assertGreaterEqual(bus.stats()["unattributed_cancels"], 1)

    async def test_dead_dispatcher_is_respawned_not_hotspun(self) -> None:
        """A cancel killing the dispatcher mid-drain (3.10 attribution:
        unattributable) must not wedge or hot-spin the bus — drain()
        respawns the dispatcher and the remaining events are delivered.
        (Pre-fix: drain()'s no-pending branch slept 0 in a loop while the
        queue stayed full; nothing ever respawned the dispatcher.)"""
        bus = EventBus()
        bus.SLICE_EVENTS = 1  # yield after every event: a deterministic
        # landing point for the cancel (outside publish's isolation)
        seen: list = []

        def cancel_dispatcher(event: Event) -> None:
            if bus._dispatcher is not None and event.message_id == 0:
                bus._dispatcher.cancel()

        bus.subscribe(MessageReceived, cancel_dispatcher)
        bus.subscribe(MessageReceived, lambda e: seen.append(e.message_id))

        p1, p2 = self._patch_310()
        with p1, p2:
            bus.publish_nowait(_msg(0))
            for i in range(1, 5):
                bus.publish_nowait(_msg(i))
            await bus.drain(timeout=5.0)

        stats = bus.stats()
        self.assertEqual(stats["queued"], 0)  # queue fully drained
        self.assertIn(4, seen)  # later events were delivered (no wedge)
        self.assertIn(0, seen)  # the in-flight event was not lost either


# ------------------------------------------------------- (e) LOW aging
class TestLowAging(unittest.IsolatedAsyncioTestCase):
    def _scheduler(self) -> TaskScheduler:
        scheduler = TaskScheduler(
            EventBus(), FakeLogger(), FakeLimiter(), self._sender, FakeAudit(),
            default_timeout=5.0, history_repo=FakeHistoryRepo(),
        )
        scheduler.start()
        self.addAsyncCleanup(scheduler.stop, drain=False, timeout=5.0)
        return scheduler

    async def _sender(self, task: Task):
        await asyncio.sleep(0)  # minimal send
        return None

    async def test_aged_low_runs_within_bounded_pops(self) -> None:
        scheduler = self._scheduler()
        gate = scheduler._limiter.block_acquires()
        low = scheduler.submit("alpha", "p", "message", channel_id=1,
                               priority=Priority.LOW)
        # backdate past the starvation threshold (monotonic age axis)
        low.submitted_mono = time.monotonic() - (LOW_STARVATION_S + 5.0)
        normals = [
            scheduler.submit("alpha", "p", "reaction", channel_id=1,
                             message_id=1, priority=Priority.NORMAL)
            for _ in range(50)
        ]
        await asyncio.sleep(0.05)  # first NORMAL parked in acquire

        gate.set()
        deadline = time.monotonic() + 10.0
        pending = [low, *normals]
        while any(not t.is_terminal for t in pending) and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        self.assertTrue(all(t.is_terminal for t in pending))
        order = [t.id for t in scheduler._finished_tasks]
        low_index = order.index(low.id)
        # aging fired: the LOW was served long before the 50 NORMALs
        # drained (deterministic rule: once every _LOW_AGING_POP_INTERVAL
        # pops), instead of never/last under strict priority
        self.assertGreater(low_index, 0)  # strictness held for the first pops
        self.assertLess(low_index, len(normals))

    async def test_young_low_still_strictly_last(self) -> None:
        """Without aging pressure (waited < LOW_STARVATION_S) the strict
        (priority, seq) heap is untouched: the LOW runs after EVERY
        NORMAL — the aging path must not fire early."""
        scheduler = self._scheduler()
        gate = scheduler._limiter.block_acquires()
        low = scheduler.submit("alpha", "p", "message", channel_id=1,
                               priority=Priority.LOW)
        normals = [
            scheduler.submit("alpha", "p", "reaction", channel_id=1,
                             message_id=1, priority=Priority.NORMAL)
            for _ in range(10)
        ]
        await asyncio.sleep(0.05)
        gate.set()
        deadline = time.monotonic() + 5.0
        pending = [low, *normals]
        while any(not t.is_terminal for t in pending) and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        order = [t.id for t in scheduler._finished_tasks]
        self.assertEqual(order.index(low.id), len(normals))  # dead last

    async def test_cancelled_low_does_not_break_the_low_lane(self) -> None:
        """cancel/cleanup must leave the LOW bookkeeping consistent: a
        cancelled stale head is pruned and a later aged LOW still ages."""
        scheduler = self._scheduler()
        gate = scheduler._limiter.block_acquires()
        stale = scheduler.submit("alpha", "p", "message", channel_id=1,
                                 priority=Priority.LOW)
        low = scheduler.submit("alpha", "p", "message", channel_id=2,
                               priority=Priority.LOW)
        low.submitted_mono = time.monotonic() - (LOW_STARVATION_S + 5.0)
        normals = [
            scheduler.submit("alpha", "p", "reaction", channel_id=1,
                             message_id=1, priority=Priority.NORMAL)
            for _ in range(12)
        ]
        self.assertTrue(scheduler.cancel(stale.id))
        await asyncio.sleep(0.05)
        gate.set()
        deadline = time.monotonic() + 5.0
        pending = [low, *normals]
        while any(not t.is_terminal for t in pending) and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual(stale.status, "cancelled")
        order = [t.id for t in scheduler._finished_tasks]
        self.assertLess(order.index(low.id), len(normals))  # aging still fired


if __name__ == "__main__":
    unittest.main()
