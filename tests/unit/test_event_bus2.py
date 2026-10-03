"""Round-2 hostile audit coverage for core.event_bus (agent 12 of 15).

Pins the dispatch contract under sabotage: mutation-under-iteration
(unsubscribe/subscribe DURING dispatch — snapshot semantics), the
BaseException policy (custom BaseException isolated; KeyboardInterrupt /
SystemExit propagate as process signals), drain() with nothing pending,
typed-event mismatch, subclass delivery, and sequential sync/async
handler ordering (a blocking sync handler delays later handlers BY
DESIGN — publish is an ordered inline dispatch, not a thread pool).
"""
from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import dataclass

from core.event_bus import (
    Event,
    EventBus,
    SettingChanged,
    TaskCompleted,
    TaskFailed,
    TaskQueued,
)


class Kaboom(BaseException):
    """NOT an Exception: exercises the BaseException isolation policy."""


@dataclass
class TaskFailedSub(TaskFailed):
    """Typed-vocabulary subclass (inherits all defaulted fields)."""


class TestMutationDuringDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_unsubscribe_of_other_during_dispatch_snapshot(self) -> None:
        bus = EventBus()
        seen: list = []

        def b(event: Event) -> None:
            seen.append("b")

        def a(event: Event) -> None:
            seen.append("a")
            unsub_b()  # sabotage: remove b WHILE dispatching

        bus.subscribe(Event, a)
        unsub_b = bus.subscribe(Event, b)
        await bus.publish(SettingChanged())
        self.assertEqual(seen, ["a", "b"])  # snapshot: b sees THIS event
        await bus.publish(SettingChanged())
        self.assertEqual(seen, ["a", "b", "a"])  # gone from the next one

    async def test_self_unsubscribe_during_dispatch(self) -> None:
        bus = EventBus()
        calls: list = []
        unsub = None

        def a(event: Event) -> None:
            calls.append("a")
            unsub()
            unsub()  # idempotent

        unsub = bus.subscribe(Event, a)
        await bus.publish(SettingChanged())
        await bus.publish(SettingChanged())
        self.assertEqual(calls, ["a"])

    async def test_subscribe_during_dispatch_not_seen_this_round(self) -> None:
        bus = EventBus()
        seen: list = []

        def late(event: Event) -> None:
            seen.append("late")

        def a(event: Event) -> None:
            seen.append("a")
            bus.subscribe(Event, late)

        bus.subscribe(Event, a)
        results = await bus.publish(SettingChanged())
        self.assertEqual(seen, ["a"])  # snapshot: late missed THIS event
        await bus.publish(SettingChanged())
        self.assertEqual(seen, ["a", "a", "late"])

    async def test_double_unsubscribe_then_publish(self) -> None:
        bus = EventBus()
        seen: list = []
        unsub = bus.subscribe(Event, lambda e: seen.append(1))
        unsub()
        unsub()
        await bus.publish(SettingChanged())
        self.assertEqual(seen, [])


class TestBaseExceptionPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_custom_base_exception_isolated(self) -> None:
        bus = EventBus()
        ran: list = []

        def hostile(event: Event) -> None:
            raise Kaboom("not an Exception")

        bus.subscribe(Event, hostile)
        bus.subscribe(Event, lambda e: ran.append("after"))
        results = await bus.publish(SettingChanged())
        self.assertEqual(ran, ["after"])            # later handler still ran
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], Kaboom)   # captured, not raised

    async def test_system_exit_propagates(self) -> None:
        bus = EventBus()
        ran: list = []

        def hostile(event: Event) -> None:
            raise SystemExit(3)

        bus.subscribe(Event, lambda e: ran.append("before"))
        bus.subscribe(Event, hostile)
        bus.subscribe(Event, lambda e: ran.append("never"))
        with self.assertRaises(SystemExit):
            await bus.publish(SettingChanged())
        self.assertEqual(ran, ["before"])          # sequential up to the raise

    async def test_keyboard_interrupt_propagates(self) -> None:
        bus = EventBus()

        def hostile(event: Event) -> None:
            raise KeyboardInterrupt()

        bus.subscribe(Event, hostile)
        with self.assertRaises(KeyboardInterrupt):
            await bus.publish(SettingChanged())

    async def test_sync_handler_exception_does_not_poison_results(self) -> None:
        bus = EventBus()
        bus.subscribe(TaskFailed, lambda e: 1 / 0)
        bus.subscribe(TaskFailed, lambda e: "ok")
        results = await bus.publish(TaskFailed())
        self.assertIsInstance(results[0], ZeroDivisionError)
        self.assertEqual(results[1], "ok")


class TestDrainAndTyping(unittest.IsolatedAsyncioTestCase):
    async def test_drain_with_zero_subscribers_and_no_pending(self) -> None:
        bus = EventBus()
        t0 = time.perf_counter()
        await bus.drain(timeout=0.5)
        self.assertLess(time.perf_counter() - t0, 0.4)  # returned immediately

    async def test_drain_awaits_fire_and_forget_backlog(self) -> None:
        bus = EventBus()
        done: list = []

        async def slow(event: Event) -> None:
            await asyncio.sleep(0.05)
            done.append(1)

        bus.subscribe(SettingChanged, slow)
        tasks = [bus.publish_nowait(SettingChanged()) for _ in range(5)]
        await bus.drain(timeout=2.0)
        self.assertEqual(len(done), 5)
        self.assertTrue(all(t.done() for t in tasks))
        self.assertEqual(bus._background, set())     # strong refs released

    async def test_typed_mismatch_no_delivery(self) -> None:
        bus = EventBus()
        seen: list = []
        bus.subscribe(TaskFailed, lambda e: seen.append(type(e).__name__))
        await bus.publish(TaskCompleted())
        await bus.publish(TaskQueued())
        self.assertEqual(seen, [])
        await bus.publish(TaskFailed())
        self.assertEqual(seen, ["TaskFailed"])

    async def test_subclass_event_reaches_base_subscribers(self) -> None:
        bus = EventBus()
        task_seen: list = []
        base_seen: list = []
        bus.subscribe(TaskFailed, lambda e: task_seen.append(1))
        bus.subscribe(Event, lambda e: base_seen.append(type(e).__name__))
        await bus.publish(TaskFailedSub())
        self.assertEqual(task_seen, [1])
        self.assertEqual(base_seen, ["TaskFailedSub"])
        await bus.publish(TaskFailedSub(error="x"))
        self.assertEqual(task_seen, [1, 1])

    async def test_publish_order_sync_then_async(self) -> None:
        """Sequential dispatch: a sync handler that busy-waits delays the
        async handler behind it (pinned as the intended inline semantic)."""
        bus = EventBus()
        order: list = []

        def blocking_sync(event: Event) -> None:
            time.sleep(0.05)
            order.append("sync")

        async def follower(event: Event) -> None:
            order.append("async")

        bus.subscribe(SettingChanged, blocking_sync)
        bus.subscribe(SettingChanged, follower)
        await bus.publish(SettingChanged())
        self.assertEqual(order, ["sync", "async"])

    async def test_results_in_invocation_order(self) -> None:
        bus = EventBus()

        async def h1(event: Event) -> str:
            await asyncio.sleep(0.01)
            return "h1"

        bus.subscribe(SettingChanged, h1)
        bus.subscribe(SettingChanged, lambda e: "h2")
        results = await bus.publish(SettingChanged())
        self.assertEqual(results, ["h1", "h2"])


if __name__ == "__main__":
    unittest.main()
