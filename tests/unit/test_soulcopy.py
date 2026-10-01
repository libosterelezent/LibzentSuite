"""Soulcopy plugin tests: textutils pipeline + the full flow over fake services."""
from __future__ import annotations

import asyncio
import logging
import unittest
from typing import List, Optional

from core.credentials import Credentials
from core.errors import FlowBlocked, TargetGone
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


def make_plugin(pages: List[str], settings_overrides=None):
    """Build a live SoulcopyPlugin instance through the real manager."""
    bus = EventBus()
    global_layer = {"soulcopy": settings_overrides or {}}
    manager = PluginManager(
        bus, logging.getLogger("t"),
        SettingsManager(bus, logging.getLogger("t"), global_layer=global_layer),
        plugin_dir="plugins",
    )
    manager.discover()
    manager.attach_account("alpha", allowed={"soulcopy"})
    services = FakeServices(bus, pages)
    asyncio.get_event_loop_policy()
    return manager, services


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
                       overrides=None) -> FakeServices:
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
        manager.attach_account("alpha", allowed={"soulcopy"})
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
        return services

    async def test_full_flow(self) -> None:
        services = await self.run_flow(
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
        services = await self.run_flow([roster], overrides={"max_chunk_length": 200,
                                                            "inter_chunk_delay": 0.0})
        chunks = [c for _ch, c, _p in services.sent if c.startswith("$soulcopy ")]
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= 200 for c in chunks))

    async def test_non_owner_ignored(self) -> None:
        services = await self.run_flow(["Soulmates\nx"], author=4242)
        self.assertEqual(services.sent, [])

    async def test_wrong_content_ignored(self) -> None:
        services = await self.run_flow(["x"], content="not-the-trigger")
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
        manager.attach_account("alpha", allowed={"soulcopy"})
        services = FakeServices(bus, ["page"])
        await manager.activate_account("alpha", services)
        # trigger in a channel the services does not know -> TargetGone
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=2, channel_id=987,
            author_id=OWNER_ID, content="slcopy",
        ))
        await asyncio.sleep(0.1)
        self.assertEqual(services.sent, [])  # flow aborted, nothing sent


if __name__ == "__main__":
    unittest.main()
