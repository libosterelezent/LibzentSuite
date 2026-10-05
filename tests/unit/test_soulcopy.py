"""Soulcopy plugin tests: textutils pipeline + the full flow over fake services."""
from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace as NS
from typing import List, Optional

from core.credentials import Credentials
from core.errors import FlowBlocked, SchedulerError, TargetGone
from core.event_bus import EventBus, Event, MessageReceived
from core.plugin_manager import PluginManager
from core.priority import Priority
from core.settings_manager import SettingsManager
from plugins.soulcopy.plugin import SoulcopyPlugin, flow_block_reason
from plugins.soulcopy.textutils import (
    DEFAULT_PREFIX,
    build_soulcopy_chunks,
    collapse_pages,
    split_into_chunks,
    strip_page_header,
)

OWNER_ID = 1001
MUDAE_ID = 999_999
TRIGGER_CHANNEL = 500
DM_CHANNEL = 1_000_000 + OWNER_ID * 7919 + MUDAE_ID  # deterministic pair id


class FakeServices:
    """PluginServices stand-in: records sends, publishes collected pages."""

    def __init__(self, bus: EventBus, pages: List[str]) -> None:
        self._bus = bus
        self._pages = list(pages)
        self.sent: list = []
        self.account = "alpha"
        self.plugin = "soulcopy"

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({
            "MUDAE_BOT_ID": str(MUDAE_ID),
            "OWNER_USER_IDS": str(OWNER_ID),
        })

    @property
    def settings(self) -> dict:
        return {}

    async def send_message(self, channel_id, content, *, priority=Priority.LOW):
        self.sent.append((channel_id, content, int(priority)))
        return None

    async def send_dm(self, user_id, content, *, priority=Priority.LOW):
        self.sent.append((f"dm:{user_id}", content, int(priority)))
        return None

    async def open_dm(self, user_id) -> int:
        return DM_CHANNEL

    async def wait_for_message(self, check, timeout):
        return None

    async def collect_messages(self, check, idle_timeout):
        """Deliver the scripted Mudae pages as MessageReceived events."""
        events = [
            MessageReceived(
                account=self.account, message_id=i, channel_id=DM_CHANNEL,
                author_id=MUDAE_ID, content=page, dm=True,
            )
            for i, page in enumerate(self._pages)
        ]
        return [e for e in events if check(e)]

    def publish(self, event: Event) -> None:
        self._bus.publish_nowait(event)

    def validate_channel(self, channel_id) -> None:
        if channel_id == TRIGGER_CHANNEL:
            return
        raise TargetGone(self.account, f"channel {channel_id} is not in the cache")

    def describe_channel(self, channel_id) -> str:
        return "#grind" if channel_id == TRIGGER_CHANNEL else f"channel-{channel_id}"


async def make_plugin(pages: List[str], settings_overrides=None):
    """Build a live SoulcopyPlugin instance through the real manager
    (async: attach_account reads persisted plugin state)."""
    bus = EventBus()
    global_layer = {"soulcopy": settings_overrides or {}}
    manager = PluginManager(
        bus, logging.getLogger("t"),
        SettingsManager(bus, logging.getLogger("t"), global_layer=global_layer),
        plugin_dir="plugins",
    )
    manager.discover()
    await manager.attach_account("alpha", allowed={"soulcopy"})
    services = FakeServices(bus, pages)
    asyncio.get_event_loop_policy()
    return manager, services


def dm_event(message_id: int, content, author=MUDAE_ID, channel=DM_CHANNEL):
    return MessageReceived(
        account="alpha", message_id=message_id, channel_id=channel,
        author_id=author, content=content, dm=True,
    )


async def build_flow(services, overrides=None, allowed=("soulcopy",),
                     bus=None):
    bus = bus or EventBus()
    manager = PluginManager(
        bus, logging.getLogger("t"),
        SettingsManager(bus, logging.getLogger("t"),
                        global_layer={"soulcopy": overrides or {}}),
        plugin_dir="plugins",
    )
    manager.discover()
    await manager.attach_account("alpha", allowed=set(allowed))
    await manager.activate_account("alpha", services)
    return bus, manager


def trigger(bus, content, author=OWNER_ID, channel=TRIGGER_CHANNEL,
            message_id=1):
    bus.publish_nowait(MessageReceived(
        account="alpha", message_id=message_id, channel_id=channel,
        author_id=author, content=content,
    ))


async def wait_flow_done(services, expected_sends=2, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if len(services.sent) >= expected_sends:
            await asyncio.sleep(0.05)
            return True
        await asyncio.sleep(0.01)
    return False


class StreamCollectServices(FakeServices):
    """collect_messages delegates to a real AccountPluginServices on the
    shared bus — real idle-window + subscription semantics."""

    def __init__(self, bus: EventBus) -> None:
        super().__init__(bus, [])
        from core.account_manager.services import AccountPluginServices
        self._collector = AccountPluginServices(
            "alpha", "soulcopy", bus=bus, scheduler=None,
            credentials=None, cache=None,
        )

    async def collect_messages(self, check, idle_timeout):
        return await self._collector.collect_messages(check, idle_timeout)


class MixedStreamServices(FakeServices):
    """collect_messages over a prebuilt event list (arbitrary order/authors)."""

    def __init__(self, bus: EventBus, events) -> None:
        super().__init__(bus, [])
        self.events = list(events)

    async def collect_messages(self, check, idle_timeout):
        return [e for e in self.events if check(e)]


class TestTextUtils(unittest.TestCase):
    def test_strip_page_header(self) -> None:
        page = "Soulmates\n---------\nAlice\nBob"
        self.assertEqual(strip_page_header(page), ["Alice", "Bob"])
        self.assertEqual(strip_page_header("Just\nLines"), ["Just", "Lines"])
        self.assertEqual(strip_page_header("Soulmates\n---------\n"), [])

    def test_collapse_pages(self) -> None:
        stream = collapse_pages(["Soulmates\n---------\nAlice\nBob", "Dana\nEve"])
        self.assertEqual(stream, "Alice$Bob$Dana$Eve")
        self.assertEqual(collapse_pages(["Soulmates\n---------\n"]), "")

    def test_split_into_chunks_bounds(self) -> None:
        chunks = split_into_chunks("a" * 30, 10, "")
        self.assertTrue(all(len(c) <= 10 for c in chunks))
        self.assertEqual("".join(chunks), "a" * 30)
        with self.assertRaises(ValueError):
            split_into_chunks("x", 5, "$soulcopy ")

    def test_split_hostile_budgets(self) -> None:
        with self.assertRaises(ValueError):
            split_into_chunks("x", 0, "")
        with self.assertRaises(ValueError):
            split_into_chunks("x", -5, "")
        with self.assertRaises(ValueError):
            split_into_chunks("x", 1, "$soulcopy ")
        self.assertEqual(split_into_chunks("ab", 1, ""), ["a", "b"])

    def test_chunk_exactly_2000_u16(self) -> None:
        prefix = DEFAULT_PREFIX
        entry = "a" * (2000 - len(prefix))
        out = build_soulcopy_chunks([entry])
        self.assertEqual(out, [prefix + entry])
        self.assertEqual(len(out[0].encode("utf-16-le")) // 2, 2000)

    def test_chunk_exactly_2000_u16_astral(self) -> None:
        prefix = DEFAULT_PREFIX
        budget = 2000 - len(prefix.encode("utf-16-le")) // 2
        self.assertEqual(budget % 2, 0)
        page = "😀" * (budget // 2)
        out = build_soulcopy_chunks([page])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].encode("utf-16-le")) // 2, 2000)
        for chunk in out:
            chunk.encode("utf-16-le").decode("utf-16-le")

    def test_oversize_entry_hard_split_no_surrogate_break(self) -> None:
        page = "😀" * 3000
        out = build_soulcopy_chunks([page])
        self.assertGreater(len(out), 1)
        stream = "".join(c[len(DEFAULT_PREFIX):] for c in out)
        self.assertEqual(stream, page)
        for chunk in out:
            encoded = chunk.encode("utf-16-le")
            self.assertLessEqual(len(encoded) // 2, 2000)
            encoded.decode("utf-16-le")

    def test_whitespace_pages_produce_no_chunks(self) -> None:
        self.assertEqual(build_soulcopy_chunks(["   \n\t \n"]), [])
        self.assertEqual(build_soulcopy_chunks(["", "  "]), [])
        self.assertEqual(build_soulcopy_chunks(["Soulmates\n   \n \t"]), [])

    def test_whitespace_lines_dropped_entries_preserved(self) -> None:
        page = "Soulmates\n---------\nAlice\n   \n\t\nBob"
        self.assertEqual(build_soulcopy_chunks([page]), [DEFAULT_PREFIX + "Alice$Bob"])

    def test_header_edge_shapes(self) -> None:
        self.assertEqual(strip_page_header("Soulmates"), [])
        self.assertEqual(strip_page_header("Soulmates\n"), [])
        self.assertEqual(strip_page_header("My Soulmates list\n---\nAlice"),
                         ["Alice"])
        self.assertEqual(strip_page_header(None), [])
        self.assertEqual(collapse_pages([None, ""]), "")

    def test_zero_width_entries_pass_through(self) -> None:
        page = "Alice\u200b\nBob\ufeff"
        self.assertEqual(build_soulcopy_chunks([page]),
                         [DEFAULT_PREFIX + "Alice\u200b$Bob\ufeff"])

    def test_split_respects_joiners(self) -> None:
        chunks = split_into_chunks("aa$bb$cc", 5, "")
        self.assertEqual(chunks, ["aa$bb", "cc"])

    def test_build_soulcopy_chunks(self) -> None:
        out = build_soulcopy_chunks(["Soulmates\n---------\nAlice\nBob"], max_chunk_length=2000)
        self.assertEqual(out, [DEFAULT_PREFIX + "Alice$Bob"])
        self.assertEqual(build_soulcopy_chunks(["Soulmates\n---------\n"]), [])

    def test_flow_block_reason(self) -> None:
        self.assertEqual(flow_block_reason(TargetGone("a", "gone")), "gone")
        self.assertEqual(flow_block_reason(ValueError("x")), "ValueError: x")


class TestSoulcopyFlow(unittest.IsolatedAsyncioTestCase):
    async def run_flow(self, pages, content="slcopy", author=OWNER_ID,
                       overrides=None):
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(
                bus, logging.getLogger("t"),
                global_layer={"soulcopy": overrides or {}},
            ),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"soulcopy"})
        services = FakeServices(bus, pages)
        await manager.activate_account("alpha", services)
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=1, channel_id=TRIGGER_CHANNEL,
            author_id=author, content=content,
        ))
        # let the dispatch + flow run to completion
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(services.sent) >= 2 and services.sent[-1][1].startswith("$soulcopy "):
                break
        await asyncio.sleep(0.05)
        return services, manager

    async def test_full_flow(self) -> None:
        services, _ = await self.run_flow(
            ["Soulmates\n---------\nAlice\nBob\nCharlie"],
            overrides={"inter_chunk_delay": 0.0},
        )
        # 1 export request + 1 chunk back to the trigger channel
        self.assertEqual(len(services.sent), 2)
        export_channel, export_content, export_prio = services.sent[0]
        self.assertEqual(export_channel, TRIGGER_CHANNEL)
        self.assertEqual(export_content, "$slud-s")
        self.assertEqual(export_prio, int(Priority.NORMAL))
        chunk_channel, chunk_content, chunk_prio = services.sent[1]
        self.assertEqual(chunk_channel, TRIGGER_CHANNEL)
        self.assertEqual(chunk_content, "$soulcopy Alice$Bob$Charlie")
        self.assertEqual(chunk_prio, int(Priority.LOW))

    async def test_multichunk(self) -> None:
        roster = "\n".join(f"user{i:04d}" for i in range(60))
        services, _ = await self.run_flow(
            [roster], overrides={"max_chunk_length": 200,
                                 "inter_chunk_delay": 0.0})
        chunks = [c for _ch, c, _p in services.sent if c.startswith("$soulcopy ")]
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= 200 for c in chunks))

    async def test_non_owner_ignored(self) -> None:
        services, _ = await self.run_flow(["Soulmates\nx"], author=4242)
        self.assertEqual(services.sent, [])

    async def test_wrong_content_ignored(self) -> None:
        services, _ = await self.run_flow(["x"], content="not-the-trigger")
        self.assertEqual(services.sent, [])

    async def test_target_gone_aborts_clean(self) -> None:
        """A vanished trigger channel aborts BEFORE any send (no partials)."""
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"),
                            global_layer={"soulcopy": {}}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"soulcopy"})
        services = FakeServices(bus, ["page"])
        await manager.activate_account("alpha", services)
        # trigger in a channel the services does not know -> TargetGone
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=2, channel_id=987,
            author_id=OWNER_ID, content="slcopy",
        ))
        await asyncio.sleep(0.1)
        self.assertEqual(services.sent, [])  # flow aborted, nothing sent

    async def test_failed_export_cancels_collector(self) -> None:
        """A failed export send must cancel the page collector immediately
        (a lingering collector breaks the K16 single-flow invariant)."""

        class CollectingServices(FakeServices):
            def __init__(self, bus, pages, fail_export=False):
                super().__init__(bus, pages)
                self.collecting = 0
                self.fail_export = fail_export

            async def send_message(self, channel_id, content, *,
                                   priority=Priority.LOW):
                if self.fail_export and content == "$slud-s":
                    raise SchedulerError("task 7 failed: boom")
                return await super().send_message(
                    channel_id, content, priority=priority)

            async def collect_messages(self, check, idle_timeout):
                self.collecting += 1
                try:
                    await asyncio.sleep(idle_timeout)  # real idle window
                    return []
                finally:
                    self.collecting -= 1

        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"),
                            global_layer={"soulcopy": {"dm_read_timeout": 5.0}}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"soulcopy"})
        services = CollectingServices(bus, ["page"], fail_export=True)
        await manager.activate_account("alpha", services)
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=2, channel_id=TRIGGER_CHANNEL,
            author_id=OWNER_ID, content="slcopy",
        ))
        await asyncio.sleep(0.2)
        self.assertEqual(services.sent, [])
        self.assertEqual(services.collecting, 0)  # cancelled, not lingering
        state = {s["name"]: s for s in manager.states("alpha")}["soulcopy"]
        self.assertEqual(state["error_count"], 0)  # clean abort, not a crash


class TestSoulcopyHostileTriggers(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_variants(self) -> None:
        services = FakeServices(None, ["Soulmates\n---------\nAlice"])
        bus, manager = await build_flow(
            services, overrides={"inter_chunk_delay": 0.0})
        rejects = [
            "SLCOPY", "slcopy2", "1slcopy", "slcopy x", "x slcopy",
            "sl copy", "slcopy\u200b", "\u200bslcopy", "slcop", "slcoppy",
            "", "   ", "$slcopy", "slCopy",
        ]
        for i, content in enumerate(rejects):
            trigger(bus, content, message_id=100 + i)
        rich = NS(embeds=[NS(description="x")], content="")
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=200, channel_id=TRIGGER_CHANNEL,
            author_id=OWNER_ID, content="", message=rich,
        ))
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=201, channel_id=TRIGGER_CHANNEL,
            author_id=OWNER_ID, content=None,
        ))
        await asyncio.sleep(0.15)
        self.assertEqual(services.sent, [])
        trigger(bus, "slcopy", message_id=300)
        self.assertTrue(await wait_flow_done(services, 2),
                        f"real trigger never ran: {services.sent}")
        exports = [c for _ch, c, _p in services.sent if c == "$slud-s"]
        self.assertEqual(len(exports), 1)
        state = {s["name"]: s for s in manager.states("alpha")}["soulcopy"]
        self.assertEqual(state["error_count"], 0)

    async def test_padded_trigger_fires(self) -> None:
        services = FakeServices(None, ["Alice"])
        bus, _ = await build_flow(
            services, overrides={"inter_chunk_delay": 0.0})
        for i, content in enumerate(["  slcopy  ", "\tslcopy\n", " slcopy"]):
            trigger(bus, content, message_id=400 + i)
            self.assertTrue(await wait_flow_done(services, 2))
            services.sent.clear()

    async def test_whitespace_only_pages_send_nothing(self) -> None:
        services = FakeServices(None, ["   \n\t", ""])
        bus, _ = await build_flow(
            services, overrides={"inter_chunk_delay": 0.0})
        trigger(bus, "slcopy")
        await asyncio.sleep(0.2)
        exports = [c for _ch, c, _p in services.sent if c == "$slud-s"]
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(services.sent), 1)


class TestSoulcopyLatchAndCancel(unittest.IsolatedAsyncioTestCase):
    async def test_racing_triggers_single_flow(self) -> None:
        class GatedServices(FakeServices):
            def __init__(self, bus_pages):
                super().__init__(None, bus_pages)
                self.gate = asyncio.get_running_loop().create_future()
                self.collects = 0

            async def collect_messages(self, check, idle_timeout):
                self.collects += 1
                await self.gate
                return await super().collect_messages(check, idle_timeout)

        services = GatedServices(["Soulmates\n---------\nAlice"])
        bus, manager = await build_flow(
            services, overrides={"inter_chunk_delay": 0.0})
        plugin = manager._accounts["alpha"]["soulcopy"].instance
        trigger(bus, "slcopy", message_id=1)
        await asyncio.sleep(0.05)
        self.assertTrue(plugin._in_flight)
        trigger(bus, "slcopy", message_id=2)
        trigger(bus, "slcopy", message_id=3)
        await asyncio.sleep(0.1)
        self.assertEqual(services.collects, 1)
        services.gate.set_result(None)
        self.assertTrue(await wait_flow_done(services, 2))
        exports = [c for _ch, c, _p in services.sent if c == "$slud-s"]
        self.assertEqual(len(exports), 1)
        self.assertFalse(plugin._in_flight)
        trigger(bus, "slcopy", message_id=4)
        self.assertTrue(await wait_flow_done(services, 4))
        self.assertEqual(services.collects, 2)
        self.assertEqual(
            len([c for _ch, c, _p in services.sent if c == "$slud-s"]), 2)

    async def test_failed_export_reaps_collector_and_unlatches(self) -> None:
        class FailingExportServices(FakeServices):
            def __init__(self, bus):
                super().__init__(None, ["Soulmates\n---------\nAlice"])
                self.fail_export = True
                self.collecting = 0

            async def send_message(self, channel_id, content, *,
                                   priority=Priority.LOW):
                if self.fail_export and content == "$slud-s":
                    raise SchedulerError("task 9 failed: pacing rejected")
                return await super().send_message(
                    channel_id, content, priority=priority)

            async def collect_messages(self, check, idle_timeout):
                self.collecting += 1
                try:
                    await asyncio.sleep(0.2)
                    return []
                finally:
                    self.collecting -= 1

        services = FailingExportServices(None)
        bus, manager = await build_flow(
            services, overrides={"dm_read_timeout": 5.0,
                                 "inter_chunk_delay": 0.0})
        plugin = manager._accounts["alpha"]["soulcopy"].instance
        trigger(bus, "slcopy")
        await asyncio.sleep(0.25)
        self.assertEqual(services.collecting, 0)
        self.assertFalse(plugin._in_flight)
        state = {s["name"]: s for s in manager.states("alpha")}["soulcopy"]
        self.assertEqual(state["error_count"], 0)
        services.fail_export = False
        trigger(bus, "slcopy", message_id=2)
        self.assertTrue(await wait_flow_done(services, 1, timeout=3.0),
                        f"retrigger after abort never ran: {services.sent}")
        self.assertEqual(
            [c for _ch, c, _p in services.sent if c == "$slud-s"],
            ["$slud-s"])


class TestSoulcopyHostileDMStream(unittest.IsolatedAsyncioTestCase):
    async def test_stream_filtered_ordered_duplicated(self) -> None:
        events = [
            dm_event(1, "Soulmates\n---------\nBob"),
            dm_event(2, "Soulmates\n---------\nZed", channel=DM_CHANNEL + 1),
            dm_event(3, "garbage", author=MUDAE_ID + 1),
            dm_event(4, "Soulmates\n---------\nAlice"),
            dm_event(5, "Soulmates\n---------\nAlice"),
            dm_event(6, ""),
            dm_event(7, "   \n "),
            dm_event(8, "Soulmates"),
        ]
        services = MixedStreamServices(None, events)
        bus, _ = await build_flow(
            services, overrides={"inter_chunk_delay": 0.0})
        trigger(bus, "slcopy")
        self.assertTrue(await wait_flow_done(services, 2))
        chunk = [c for _ch, c, _p in services.sent
                 if c.startswith("$soulcopy ")][0]
        self.assertEqual(chunk, DEFAULT_PREFIX + "Bob$Alice$Alice")

    async def _wait_subscribed(self, bus, count=2, timeout=2.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(bus._subs.get(MessageReceived, [])) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("collector never subscribed")

    async def test_idle_timeout_expires_between_pages(self) -> None:
        bus = EventBus()
        services = StreamCollectServices(bus)
        # scaled-up constants (loaded-loop tolerance): the SEMANTIC pin is
        # the ratio — gap (1.5s) well above the idle window (0.5s) for the
        # expiry case, gap well below it for the reset case below
        bus, _ = await build_flow(
            services, overrides={"dm_read_timeout": 0.5,
                                 "inter_chunk_delay": 0.0}, bus=bus)
        trigger(bus, "slcopy")
        await self._wait_subscribed(bus)
        bus.publish_nowait(dm_event(10, "Soulmates\n---------\nAlice"))
        await asyncio.sleep(1.5)
        bus.publish_nowait(dm_event(11, "Soulmates\n---------\nLate"))
        self.assertTrue(await wait_flow_done(services, 2, timeout=8.0))
        chunks = [c for _ch, c, _p in services.sent
                  if c.startswith("$soulcopy ")]
        self.assertEqual(chunks, [DEFAULT_PREFIX + "Alice"])

    async def test_idle_window_resets_on_each_page(self) -> None:
        bus = EventBus()
        services = StreamCollectServices(bus)
        # idle window 1.5s vs page gaps 0.4s (>3x margin — a multi-100ms
        # loop stall between publishes cannot expire the window early)
        bus, _ = await build_flow(
            services, overrides={"dm_read_timeout": 1.5,
                                 "inter_chunk_delay": 0.0}, bus=bus)
        trigger(bus, "slcopy")
        await self._wait_subscribed(bus)
        for i, name in enumerate(("A", "B", "C")):
            bus.publish_nowait(dm_event(20 + i, name))
            await asyncio.sleep(0.4)
        self.assertTrue(await wait_flow_done(services, 2, timeout=8.0))
        chunks = [c for _ch, c, _p in services.sent
                  if c.startswith("$soulcopy ")]
        self.assertEqual(chunks, [DEFAULT_PREFIX + "A$B$C"])


class TestSoulcopyHostileSettings(unittest.IsolatedAsyncioTestCase):
    async def test_hostile_setting_values_disable_plugin(self) -> None:
        cases = [
            {"max_chunk_length": 2001},
            {"max_chunk_length": 0},
            {"max_chunk_length": -50},
            {"max_chunk_length": 49},
            {"dm_read_timeout": 0.0},
            {"dm_read_timeout": -1.0},
            {"inter_chunk_delay": -0.1},
        ]
        for override in cases:
            with self.subTest(override=override):
                services = FakeServices(None, ["Alice"])
                bus, manager = await build_flow(services, overrides=override)
                trigger(bus, "slcopy")
                await asyncio.sleep(0.15)
                self.assertEqual(
                    services.sent, [],
                    f"disabled plugin still acted for {override}")
                state = {s["name"]: s for s in manager.states("alpha")}["soulcopy"]
                self.assertFalse(state["enabled"])
                self.assertTrue(state["last_error"].startswith("settings:"))

    async def test_inter_chunk_delay_zero_sends_promptly(self) -> None:
        roster = "\n".join(f"user{i:04d}" for i in range(60))
        services = FakeServices(None, [roster])
        bus, _ = await build_flow(
            services, overrides={"max_chunk_length": 200,
                                 "inter_chunk_delay": 0.0})
        trigger(bus, "slcopy")
        loop = asyncio.get_running_loop()
        start = loop.time()
        for _ in range(300):
            await asyncio.sleep(0.01)
            if len([c for _ch, c, _p in services.sent
                    if c.startswith("$soulcopy ")]) >= 3:
                break
        elapsed = loop.time() - start
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(
            len([c for _ch, c, _p in services.sent
                 if c.startswith("$soulcopy ")]), 3)


class TestSoulcopyHotReload(unittest.IsolatedAsyncioTestCase):
    async def test_inter_chunk_delay_hot_reload_mid_flow(self) -> None:
        class FirstChunkHook(FakeServices):
            def __init__(self, bus):
                super().__init__(None, ["x"])
                self.first_chunk = asyncio.Event()
                self.chunk_times: List[float] = []

            async def send_message(self, channel_id, content, *,
                                   priority=Priority.LOW):
                if content.startswith("$soulcopy "):
                    self.first_chunk.set()
                    self.chunk_times.append(
                        asyncio.get_running_loop().time())
                return await super().send_message(
                    channel_id, content, priority=priority)

        roster = "\n".join(f"user{i:04d}" for i in range(60))
        services = FirstChunkHook(None)
        services._pages = [roster]
        bus, manager = await build_flow(
            services, overrides={"max_chunk_length": 200,
                                 "inter_chunk_delay": 0.4})

        async def _flip():
            await services.first_chunk.wait()
            await manager.set_setting("alpha", "soulcopy",
                                      "inter_chunk_delay", 0.0)

        flipper = asyncio.get_running_loop().create_task(_flip())
        trigger(bus, "slcopy")
        for _ in range(500):
            await asyncio.sleep(0.01)
            if len(services.chunk_times) >= 3:
                break
        await flipper
        self.assertGreaterEqual(len(services.chunk_times), 3)
        gaps = [b - a for a, b in zip(services.chunk_times,
                                      services.chunk_times[1:])]
        self.assertGreaterEqual(gaps[0], 0.3)
        for gap in gaps[1:]:
            self.assertLess(gap, 0.25, f"stale delay captured: gaps={gaps}")

    async def test_trigger_hot_reload(self) -> None:
        services = FakeServices(None, ["Alice"])
        bus, manager = await build_flow(
            services, overrides={"inter_chunk_delay": 0.0})
        trigger(bus, "slcopy")
        self.assertTrue(await wait_flow_done(services, 2))
        await manager.set_setting("alpha", "soulcopy", "trigger", "mirror")
        services.sent.clear()
        trigger(bus, "slcopy", message_id=2)
        await asyncio.sleep(0.15)
        self.assertEqual(services.sent, [])
        trigger(bus, "mirror", message_id=3)
        self.assertTrue(await wait_flow_done(services, 2))
        self.assertEqual(
            len([c for _ch, c, _p in services.sent if c == "$slud-s"]), 1)


if __name__ == "__main__":
    unittest.main()
