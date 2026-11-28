"""Unit tests for core.expect: the send-coupled reactive window —
the Track B race regression, cursor consumption, deadline semantics,
teardown discipline, account isolation, and the facade wiring."""

from __future__ import annotations

import asyncio
import unittest
from typing import TYPE_CHECKING, Any, NoReturn

from libzentsuite.core.account_manager import DiscordCache
from libzentsuite.core.account_manager.services import (
    AccountPluginServices,
    MessageWaiterRegistry,
)
from libzentsuite.core.credentials import Credentials
from libzentsuite.core.errors import AccountError
from libzentsuite.core.event_bus import EventBus, MessageReceived, TaskCompleted
from libzentsuite.core.expect import ExpectWindow
from tests.unit.helpers import FakeScheduler, default_snapshot

if TYPE_CHECKING:
    from collections.abc import Callable


class SeamRecorder:
    """A subscribe seam that records handlers and feeds events directly —
    the window under test never knows the bus."""

    def __init__(self, account: str = "alpha") -> None:
        self.account = account
        self.handlers: list[Any] = []

    def subscribe(self, handler: Any) -> Any:
        self.handlers.append(handler)

        def _unsubscribe() -> None:
            if handler in self.handlers:
                self.handlers.remove(handler)

        return _unsubscribe

    def feed(
        self, message_id: int, content: str = "x", account: str | None = None
    ) -> None:
        """Deliver one MessageReceived to every registered handler."""
        event = MessageReceived(
            account=account if account is not None else self.account,
            message_id=message_id,
            channel_id=100,
            author_id=42,
            content=content,
        )
        for handler in list(self.handlers):
            handler(event)


def make_window(
    check: Callable[[MessageReceived], bool],
    timeout: float,
    seam: SeamRecorder | None = None,
) -> ExpectWindow:
    """A window over the recorder seam (mechanics tests, no bus)."""
    seam = seam if seam is not None else SeamRecorder()
    return ExpectWindow(
        account=seam.account,
        plugin="probe",
        check=check,
        timeout=timeout,
        subscribe=seam.subscribe,
    )


def make_services(
    *,
    bus: EventBus | None = None,
    scheduler: FakeScheduler | None = None,
    waiter_registry: MessageWaiterRegistry | None = None,
) -> AccountPluginServices:
    """Direct AccountPluginServices construction (the house pattern)."""
    cache = DiscordCache("alpha")
    cache.ingest_snapshot(default_snapshot())
    return AccountPluginServices(
        "alpha",
        "probe",
        bus=bus or EventBus(),
        scheduler=scheduler or FakeScheduler(),
        credentials=Credentials.from_values({"PRIMARY_BOT_ID": "123"}),
        cache=cache,
        waiter_registry=waiter_registry,
    )


class ExpectWindowMechanicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_buffered_before_match_is_returned(self) -> None:
        """REGRESSION (Track B): a reply landing during the send's queue
        wait — before match() is even called — is matched. A wait_for
        issued after the send loses exactly this event."""
        seam = SeamRecorder()
        async with make_window(lambda e: "wa" in e.content, 1.0, seam) as window:
            seam.feed(1, content="unrelated")
            seam.feed(2, content="the $wa reply")
            reply = await window.match()
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message_id, 2)

    async def test_match_wakes_on_live_event(self) -> None:
        seam = SeamRecorder()
        # the 2.0 budget is headroom, never awaited: 2x the watchdog
        # below. A missed wake fails at the watchdog first; reaching the
        # budget needs a >=2s mid-test loop stall — the suite's 0.6s idle
        # windows would break long before this test does.
        async with make_window(lambda e: True, 2.0, seam) as window:
            pending = asyncio.create_task(window.match())
            await asyncio.sleep(0)  # one loop turn: the task parks (FIFO)
            seam.feed(1, content="reply")
            reply = await asyncio.wait_for(pending, 1.0)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message_id, 1)

    async def test_match_timeout_returns_none(self) -> None:
        seam = SeamRecorder()
        async with make_window(lambda e: True, 0.05, seam) as window:
            self.assertIsNone(await window.match())

    async def test_sequential_matches_consume_in_order(self) -> None:
        seam = SeamRecorder()
        async with make_window(lambda e: e.content.startswith("m"), 1.0, seam) as window:
            seam.feed(1, content="m1")
            seam.feed(2, content="noise")
            seam.feed(3, content="m2")
            first = await window.match()
            second = await window.match()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual((first.message_id, second.message_id), (1, 3))

    async def test_collect_gathers_n_across_arrivals(self) -> None:
        seam = SeamRecorder()
        # same budget discipline as test_match_wakes_on_live_event:
        # headroom at 2x the watchdog, never awaited on the green path
        async with make_window(lambda e: "page" in e.content, 2.0, seam) as window:
            seam.feed(1, content="page 0")
            pending = asyncio.create_task(window.collect(2))
            await asyncio.sleep(0)  # one loop turn: the collector parks
            seam.feed(2, content="page 1")
            pages = await asyncio.wait_for(pending, 1.0)
        self.assertEqual([p.message_id for p in pages], [1, 2])

    async def test_collect_partial_on_deadline(self) -> None:
        seam = SeamRecorder()
        async with make_window(lambda e: "page" in e.content, 0.05, seam) as window:
            seam.feed(1, content="page 0")
            seam.feed(2, content="noise")
            pages = await window.collect(3)
        self.assertEqual([p.message_id for p in pages], [1])

    async def test_collect_rejects_invalid_n(self) -> None:
        seam = SeamRecorder()
        async with make_window(lambda e: True, 1.0, seam) as window:
            for bad in (0, -1, 1.5, None, True):
                with self.assertRaises(AccountError):
                    await window.collect(bad)

    async def test_first_of_matches_alternative_checks(self) -> None:
        seam = SeamRecorder()
        # the window's own check never matches: first_of must not
        # consult it
        async with make_window(lambda e: False, 1.0, seam) as window:
            seam.feed(1, content="noise")
            reply = await window.first_of(
                lambda e: e.content == "nope",
                lambda e: e.content == "noise",
            )
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message_id, 1)

    async def test_first_of_requires_checks(self) -> None:
        seam = SeamRecorder()
        async with make_window(lambda e: True, 1.0, seam) as window:
            with self.assertRaises(AccountError):
                await window.first_of()

    async def test_raising_check_surfaces_as_account_error(self) -> None:
        seam = SeamRecorder()

        def bad_check(_event: MessageReceived) -> NoReturn:
            raise ValueError("check exploded")

        async with make_window(bad_check, 1.0, seam) as window:
            seam.feed(1, content="x")
            with self.assertRaises(AccountError) as ctx:
                await window.match()
        self.assertIn("check exploded", str(ctx.exception))

    async def test_awaits_after_exit_raise(self) -> None:
        seam = SeamRecorder()
        window = make_window(lambda e: True, 1.0, seam)
        async with window:
            pass
        with self.assertRaises(RuntimeError):
            await window.match()

    async def test_exit_releases_parked_await(self) -> None:
        """The live-window discipline: exiting releases a parked await —
        an abandoned waiter completes (RuntimeError), never dangles —
        and the subscription is unwired. Manual __aenter__/__aexit__
        because the parked task outlives the block."""
        seam = SeamRecorder()
        window = make_window(lambda e: True, 5.0, seam)
        await window.__aenter__()
        pending = asyncio.create_task(window.match())
        await asyncio.sleep(0)  # one loop turn: the task parks (FIFO)
        await window.__aexit__(None, None, None)
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(pending, 1.0)
        self.assertEqual(seam.handlers, [])

    async def test_cancellation_propagates_and_unwires(self) -> None:
        seam = SeamRecorder()
        window = make_window(lambda e: True, 5.0, seam)
        await window.__aenter__()
        pending = asyncio.create_task(window.match())
        await asyncio.sleep(0)  # one loop turn: the task parks (FIFO)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await window.__aexit__(None, None, None)
        self.assertEqual(seam.handlers, [])

    async def test_reentry_after_close_rejected(self) -> None:
        seam = SeamRecorder()
        window = make_window(lambda e: True, 1.0, seam)
        async with window:
            pass
        with self.assertRaises(RuntimeError):
            await window.__aenter__()

    async def test_deadline_bounds_parking_not_matching(self) -> None:
        """The deadline bounds how long an await parks; a buffered match
        stays readable after it lapsed (the window observed it)."""
        seam = SeamRecorder()
        async with make_window(lambda e: "hit" in e.content, 0.05, seam) as window:
            seam.feed(1, content="hit")
            # canary entered AFTER the first window, so its expiry —
            # awaited to None — proves the first deadline has lapsed:
            # deadline order is entry order, no wall-clock margin
            async with make_window(lambda e: False, 0.05, seam) as canary:
                self.assertIsNone(await canary.match())
            reply = await window.match()  # no park: the buffered match
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message_id, 1)

    async def test_flood_eviction_keeps_the_tail_matchable(self) -> None:
        seam = SeamRecorder()
        async with make_window(lambda e: "hit" in e.content, 1.0, seam) as window:
            for index in range(ExpectWindow.RING_MAX + 50):
                seam.feed(index, content="noise")
            seam.feed(ExpectWindow.RING_MAX + 50, content="hit")
            reply = await window.match()
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message_id, ExpectWindow.RING_MAX + 50)

    async def test_flood_eviction_drops_the_head_match(self) -> None:
        """Honest degradation: a match evicted before consumption is
        gone (bounded memory — the flood trade)."""
        seam = SeamRecorder()
        async with make_window(lambda e: "hit" in e.content, 0.05, seam) as window:
            seam.feed(0, content="hit")  # will be evicted by the flood
            for index in range(1, ExpectWindow.RING_MAX + 50):
                seam.feed(index, content="noise")
            self.assertIsNone(await window.match())


class ExpectFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_coupled_flow_matches_reply_during_queue_wait(self) -> None:
        """The plan's canonical flow: the reply lands while the send
        still awaits its ack — inside the window, it matches."""
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        async with services.expect(lambda e: e.content == "reply", 2.0) as window:
            pending_send = asyncio.create_task(services.send_message(100, "$wa"))
            await asyncio.sleep(0)  # one loop turn: submit is synchronous
            self.assertEqual(len(scheduler.submitted), 1)
            await bus.publish(
                MessageReceived(
                    account="alpha",
                    message_id=9,
                    channel_id=100,
                    author_id=777,
                    content="reply",
                )
            )
            task = scheduler.submitted[0]
            await bus.publish(
                TaskCompleted(
                    account="alpha", task_id=task.id, duration_s=0.1, summary="sent"
                )
            )
            self.assertIsNone(await asyncio.wait_for(pending_send, 1.0))
            reply = await window.match()
        self.assertIsNotNone(reply)
        self.assertEqual(reply.content, "reply")

    async def test_expect_validates_arguments(self) -> None:
        services = make_services()
        for bad_timeout in (0, -1, None):
            with self.assertRaises(AccountError):
                services.expect(lambda e: True, bad_timeout)
        with self.assertRaises(AccountError):
            services.expect("not callable", 1.0)

    async def test_registry_path_matches_own_account(self) -> None:
        bus = EventBus()
        registry = MessageWaiterRegistry(bus)
        services = make_services(bus=bus, waiter_registry=registry)
        async with services.expect(lambda e: e.content == "hit", 1.0) as window:
            pending = asyncio.create_task(window.match())
            await asyncio.sleep(0)  # one loop turn: the waiter parks
            await bus.publish(
                MessageReceived(
                    account="alpha", message_id=2, channel_id=1, author_id=1, content="hit"
                )
            )
            reply = await asyncio.wait_for(pending, 1.0)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message_id, 2)

    async def test_registry_path_never_sees_other_accounts(self) -> None:
        bus = EventBus()
        registry = MessageWaiterRegistry(bus)
        services = make_services(bus=bus, waiter_registry=registry)
        async with services.expect(lambda e: True, 0.05) as window:
            await bus.publish(
                MessageReceived(
                    account="beta", message_id=1, channel_id=1, author_id=1, content="x"
                )
            )
            self.assertIsNone(await window.match())
        self.assertEqual(registry.pending(), {"alpha": 0})

    async def test_bus_path_never_sees_other_accounts(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)  # no registry: the fallback path
        async with services.expect(lambda e: True, 0.05) as window:
            await bus.publish(
                MessageReceived(
                    account="beta", message_id=1, channel_id=1, author_id=1, content="x"
                )
            )
            self.assertIsNone(await window.match())


if __name__ == "__main__":
    unittest.main()
