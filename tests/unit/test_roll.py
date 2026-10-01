"""Roll plugin tests: owner trigger -> one pipelined send."""
from __future__ import annotations

import asyncio
import logging
import unittest

from core.credentials import Credentials
from core.event_bus import EventBus, MessageReceived
from core.errors import TargetGone
from core.plugin_manager import PluginManager
from core.priority import Priority
from core.settings_manager import SettingsManager
from plugins.roll.plugin import RollPlugin

OWNER_ID = 1001
CHANNEL = 500


class RollServices:
    def __init__(self) -> None:
        self.sent = []
        self.account = "alpha"
        self.plugin = "roll"

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({
            "MUDAE_BOT_ID": "999999", "OWNER_USER_IDS": str(OWNER_ID),
        })

    @property
    def settings(self) -> dict:
        return {}

    async def send_message(self, channel_id, content, *, priority=Priority.LOW):
        self.sent.append((channel_id, content, int(priority)))
        return None

    async def send_dm(self, user_id, content, *, priority=Priority.LOW):
        self.sent.append((f"dm:{user_id}", content, int(priority)))

    async def open_dm(self, user_id):
        return 1

    async def wait_for_message(self, check, timeout):
        return None

    async def collect_messages(self, check, idle_timeout):
        return []

    def publish(self, event):
        pass

    def validate_channel(self, channel_id) -> None:
        if channel_id != CHANNEL:
            raise TargetGone("alpha", f"channel {channel_id} gone")

    def describe_channel(self, channel_id):
        return "#grind"


async def flow(content: str, author: int = OWNER_ID, overrides=None) -> RollServices:
    bus = EventBus()
    manager = PluginManager(
        bus, logging.getLogger("t"),
        SettingsManager(bus, logging.getLogger("t"),
                        global_layer={"roll": overrides or {}}),
        plugin_dir="plugins",
    )
    manager.discover()
    manager.attach_account("alpha", allowed={"roll"})
    services = RollServices()
    await manager.activate_account("alpha", services)
    bus.publish_nowait(MessageReceived(
        account="alpha", message_id=1, channel_id=CHANNEL,
        author_id=author, content=content,
    ))
    await asyncio.sleep(0.1)
    return services


class TestRoll(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_sends_command(self) -> None:
        services = await flow("roll")
        self.assertEqual(services.sent, [(CHANNEL, "$m", int(Priority.NORMAL))])

    async def test_custom_command_setting(self) -> None:
        services = await flow("roll", overrides={"command": "$wg"})
        self.assertEqual(services.sent[0][1], "$wg")

    async def test_non_owner_ignored(self) -> None:
        services = await flow("roll", author=4242)
        self.assertEqual(services.sent, [])

    async def test_other_content_ignored(self) -> None:
        services = await flow("hello")
        self.assertEqual(services.sent, [])


if __name__ == "__main__":
    unittest.main()
