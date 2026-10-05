"""FINAL-PHASE EventBus tests: coalesced dispatch, burst bounds, ordering.

F5(c): publish_nowait feeds a bounded FIFO drained by one dispatcher in
slices. These pin: delivery completeness, per-account FIFO, overflow
drop-newest accounting, slice yielding, reentrant publish, and the
ack-deferral invariant (dispatch happens across an await, never in the
publisher's own step).
"""
from __future__ import annotations

import asyncio
import unittest

from core import event_bus as eb
from core.event_bus import (
    AccountDisconnected,
    AccountFailed,
    Event,
    EventBus,
    MessageEdited,
    MessageReceived,
    RateLimitTriggered,
    ReactionAdded,
    ScenarioFinished,
    SettingChanged,
    TaskCancelled,
    TaskFailed,
)


def _msg(account: str, i: int) -> MessageReceived:
    return MessageReceived(account=account, message_id=i, channel_id=500,
                           author_id=1, content=f"m{i}")


class CoalescedDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_completeness_after_drain(self) -> None:
        bus = EventBus()
        seen: list = []
        bus.subscribe(MessageReceived, seen.append)
        for i in range(10_000):
            bus.publish_nowait(_msg("alpha", i))
        await bus.drain(timeout=30.0)
        self.assertEqual(len(seen), 10_000)
        self.assertEqual(bus.stats()["dropped"], 0)
        self.assertEqual(bus.stats()["queued"], 0)
        self.assertEqual([m.message_id for m in seen],
                         list(range(10_000)))  # global FIFO

    async def test_per_account_fifo_under_interleaved_burst(self) -> None:
        bus = EventBus()
        per_account: dict = {}
        def handler(event: Event) -> None:
            per_account.setdefault(event.account, []).append(event.message_id)
        bus.subscribe(MessageReceived, handler)
        for i in range(6_000):
            bus.publish_nowait(_msg(("alpha", "beta", "gamma")[i % 3], i))
        await bus.drain(timeout=30.0)
        for name, ids in per_account.items():
            self.assertEqual(ids, sorted(ids), f"{name} lost FIFO order")
            self.assertEqual(len(ids), 2_000)

    async def test_overflow_drops_newest_and_counts(self) -> None:
        bus = EventBus()
        gate = asyncio.Event()
        seen: list = []
        async def slow_handler(event: Event) -> None:
            await gate.wait()
            seen.append(event)
        bus.subscribe(MessageReceived, slow_handler)
        bus.MAX_QUEUE = 8  # tiny bound for the test
        # the dispatcher parks inside the async handler; queue fills
        bus.publish_nowait(_msg("a", 0))
        await asyncio.sleep(0.02)
        for i in range(1, 20):
            bus.publish_nowait(_msg("a", i))
        self.assertEqual(bus.stats()["dropped"], 20 - 1 - 8)
        self.assertEqual(bus.stats()["queued"], 8)
        gate.set()
        await bus.drain(timeout=10.0)
        # first (in-flight) + queued 8 delivered; newest 11 dropped
        self.assertEqual(len(seen), 9)
        ids = [m.message_id for m in seen]
        self.assertEqual(ids, sorted(ids))  # delivered order stays FIFO

    async def test_control_events_survive_flood_overflow(self) -> None:
        """F4/2: control-plane tails are exempt from the flood cap.

        Queue full of MessageReceived at MAX_QUEUE → an AccountDisconnected
        (the shutdown tail event class) is still admitted via the reserved
        lane and delivered after the flood.
        """
        bus = EventBus()
        gate = asyncio.Event()
        seen: list = []
        async def slow(event: Event) -> None:
            await gate.wait()
            seen.append(event)
        bus.subscribe(Event, slow)  # catch-all: both planes observed
        bus.MAX_QUEUE = 8
        bus.CONTROL_RESERVE = 4
        bus.publish_nowait(_msg("a", 0))
        await asyncio.sleep(0.02)  # dispatcher parks inside slow
        for i in range(1, 20):     # flood class fills to cap, drops 11
            bus.publish_nowait(_msg("a", i))
        self.assertEqual(bus.stats()["dropped"], 20 - 1 - 8)
        self.assertEqual(bus.stats()["queued"], 8)
        bus.publish_nowait(AccountDisconnected(reason="shutdown"))
        self.assertEqual(bus.stats()["queued"], 9)      # admitted, not dropped
        self.assertEqual(bus.stats()["dropped"], 20 - 1 - 8)
        gate.set()
        await bus.drain(timeout=10.0)
        self.assertEqual(len(seen), 10)  # 9 flood (in-flight + 8 queued) + 1 control
        self.assertEqual(type(seen[-1]).__name__, "AccountDisconnected")

    async def test_control_lane_is_also_bounded(self) -> None:
        """F4/2: the reserved lane is headroom, not infinity — control
        events drop (counted, logged) once MAX_QUEUE+RESERVE is hit."""
        bus = EventBus()
        gate = asyncio.Event()
        async def slow(event: Event) -> None:
            await gate.wait()
        bus.subscribe(Event, slow)
        bus.MAX_QUEUE = 8
        bus.CONTROL_RESERVE = 4
        bus.publish_nowait(_msg("a", 0))
        await asyncio.sleep(0.02)
        for i in range(1, 20):     # flood to cap: 8 queued, 11 dropped
            bus.publish_nowait(_msg("a", i))
        for i in range(6):         # control fills the 4-slot lane, drops 2
            bus.publish_nowait(AccountFailed(error=f"e{i}"))
        self.assertEqual(bus.stats()["queued"], 12)     # 8 flood + 4 control
        self.assertEqual(bus.stats()["dropped"], 11 + 2)
        gate.set()
        await bus.drain(timeout=10.0)
        self.assertEqual(bus.stats()["queued"], 0)

    async def test_control_plane_classification(self) -> None:
        """F4/2: the enumerated control vocabulary + the flood default."""
        control = set(eb._CONTROL_TYPES)
        for et in (AccountDisconnected, AccountFailed, TaskFailed,
                   TaskCancelled, RateLimitTriggered, SettingChanged,
                   ScenarioFinished, eb.AccountConnected):
            self.assertIn(et, control)
        for et in (MessageReceived, MessageEdited, eb.MessageDeleted,
                   ReactionAdded):
            self.assertNotIn(et, control)
        # unclassified custom Events default to the flood plane
        class Custom(Event):
            pass
        self.assertFalse(eb._is_control(Custom()))
        self.assertTrue(eb._is_control(TaskFailed(error="x")))

    async def test_slice_budget_yields_the_loop(self) -> None:
        bus = EventBus()
        yields: list = []
        seen: list = []
        bus.subscribe(MessageReceived, seen.append)
        task = asyncio.create_task(bus._drain_queue()) if False else None
        # fill the queue synchronously (no dispatcher yet)
        for i in range(5_000):
            bus._queue.append(_msg("a", i))
        async def ticker() -> None:
            while len(seen) < 5_000:
                yields.append(len(seen))
                await asyncio.sleep(0)
        tick = asyncio.create_task(ticker())
        bus.publish_nowait(_msg("a", 9_999))  # spawns the dispatcher
        await bus.drain(timeout=30.0)
        await asyncio.wait({tick}, timeout=5)
        # the ticker got multiple slices DURING the 5k-event drain —
        # the dispatcher did not hog the loop in one callback
        mid_drain = [y for y in yields if 0 < y < 5_000]
        self.assertGreaterEqual(len(mid_drain), 3,
                                f"dispatcher hogged the loop: {yields[:5]}")

    async def test_reentrant_publish_from_handler(self) -> None:
        bus = EventBus()
        seen: list = []
        def reentrant(event: Event) -> None:
            seen.append(event.message_id)
            if event.message_id == 0:
                bus.publish_nowait(_msg(event.account, 1))
        bus.subscribe(MessageReceived, reentrant)
        bus.publish_nowait(_msg("alpha", 0))
        await bus.drain(timeout=5.0)
        self.assertEqual([m for m in seen], [0, 1])

    async def test_dispatch_deferred_across_await(self) -> None:
        """The TaskAck no-lost-ack invariant: a subscribe in the SAME step
        as submit must see the completion event of that submit."""
        bus = EventBus()
        order: list = []
        def handler(event: Event) -> None:
            order.append("handler")
        # simulate: publish (enqueue), then subscribe, both sync in one step
        bus.publish_nowait(_msg("alpha", 0))
        bus.subscribe(MessageReceived, handler)
        order.append("subscribed")
        await bus.drain(timeout=5.0)
        # dispatch happened AFTER the subscription step — the new handler
        # received the event that was published before it existed
        self.assertEqual(order, ["subscribed", "handler"])

    async def test_dispatcher_recreates_after_idle(self) -> None:
        bus = EventBus()
        seen: list = []
        bus.subscribe(MessageReceived, seen.append)
        bus.publish_nowait(_msg("a", 0))
        await bus.drain(timeout=5.0)
        await asyncio.sleep(0.01)  # dispatcher exits on empty queue
        bus.publish_nowait(_msg("a", 1))
        await bus.drain(timeout=5.0)
        self.assertEqual([m.message_id for m in seen], [0, 1])

    async def test_awaited_publish_results_unchanged(self) -> None:
        """The direct awaited path is NOT batched — results list intact."""
        bus = EventBus()
        bus.subscribe(MessageReceived, lambda e: "ok")
        results = await bus.publish(_msg("a", 0))
        self.assertEqual(results, ["ok"])

    async def test_stats_shape(self) -> None:
        bus = EventBus()
        stats = bus.stats()
        self.assertEqual(set(stats), {"queued", "dispatched", "dropped"})


if __name__ == "__main__":
    unittest.main()
