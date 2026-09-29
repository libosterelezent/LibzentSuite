"""Event bus: dispatch, isolation, unsubscribe, subclass routing."""
from __future__ import annotations

import asyncio
import unittest

from core.events import Event, EventBus, MessageEvent, ReadyEvent


class TestEventBus(unittest.IsolatedAsyncioTestCase):
    async def test_sync_and_async_handlers(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        def sync_handler(event: Event) -> str:
            seen.append(f"sync:{event.account}")
            return "sync-ok"

        async def async_handler(event: Event) -> str:
            seen.append(f"async:{event.account}")
            return "async-ok"

        bus.subscribe(MessageEvent, sync_handler)
        bus.subscribe(MessageEvent, async_handler)
        results = await bus.publish(MessageEvent(account="alpha"))
        self.assertEqual(results, ["sync-ok", "async-ok"])
        self.assertEqual(seen, ["sync:alpha", "async:alpha"])

    async def test_failing_handler_is_isolated(self) -> None:
        bus = EventBus()

        async def boom(event: Event) -> None:
            raise ValueError("plugin crash")

        async def after(event: Event) -> str:
            return "still-ran"

        bus.subscribe(MessageEvent, boom)
        bus.subscribe(MessageEvent, after)
        results = await bus.publish(MessageEvent(account="alpha"))
        self.assertIsInstance(results[0], ValueError)
        self.assertEqual(results[1], "still-ran")

    async def test_unsubscribe(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        def handler(event: Event) -> None:
            calls.append(event.account)

        unsub = bus.subscribe(MessageEvent, handler)
        await bus.publish(MessageEvent(account="a"))
        unsub()
        await bus.publish(MessageEvent(account="b"))
        self.assertEqual(calls, ["a"])
        self.assertEqual(bus.subscriber_count(MessageEvent), 0)

    async def test_base_class_subscription_receives_subclasses(self) -> None:
        bus = EventBus()
        base_seen: list[str] = []
        specific_seen: list[str] = []
        bus.subscribe(Event, lambda e: base_seen.append(e.name))
        bus.subscribe(MessageEvent, lambda e: specific_seen.append(e.name))
        await bus.publish(ReadyEvent(account="alpha"))
        await bus.publish(MessageEvent(account="alpha"))
        self.assertEqual(base_seen, ["ReadyEvent", "MessageEvent"])
        self.assertEqual(specific_seen, ["MessageEvent"])  # no ReadyEvent leak

    async def test_publish_nowait_uses_running_loop(self) -> None:
        bus = EventBus()
        calls: list[str] = []
        bus.subscribe(ReadyEvent, lambda e: calls.append(e.account))
        bus.publish_nowait(ReadyEvent(account="alpha"))
        await asyncio.sleep(0)
        self.assertEqual(calls, ["alpha"])


if __name__ == "__main__":
    unittest.main()
