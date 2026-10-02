"""Roll plugin v2 tests: rolling loop, cooldowns, userstack, key limits."""
from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace as NS

from core.credentials import Credentials
from core.event_bus import EventBus, MessageReceived
from core.errors import TargetGone
from core.plugin_manager import PluginManager
from core.priority import Priority
from core.settings_manager import SettingsManager
from plugins.roll.plugin import parse_pause_minutes

OWNER_ID = 1001
MUDAE_ID = 999999
CHANNEL = 500


class RollServices:
    """Fake services that simulate Mudae's responses."""

    def __init__(self, mudae_responses=None):
        self.sent = []
        self.account = "alpha"
        self.plugin = "roll"
        self._responses = list(mudae_responses or [])
        self._state = {}
        self._cooldowns = {}

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({
            "MUDAE_BOT_ID": str(MUDAE_ID), "OWNER_USER_IDS": str(OWNER_ID),
        })

    @property
    def settings(self) -> dict:
        return {}

    async def send_message(self, channel_id, content, *, priority=Priority.LOW):
        self.sent.append((channel_id, content, int(priority)))
        return None

    async def send_dm(self, user_id, content, *, priority=Priority.LOW):
        pass

    async def open_dm(self, user_id):
        return 1

    async def wait_for_message(self, check, timeout):
        if self._responses:
            resp = self._responses.pop(0)
            event = MessageReceived(
                account=self.account, message_id=resp.get("id", 1),
                channel_id=CHANNEL, author_id=MUDAE_ID,
                content=resp.get("content", ""),
                message=resp.get("message"),
            )
            if check(event):
                return event
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

    # cooldown service mock
    async def cooldown_set(self, key, duration, **kwargs):
        self._cooldowns[key] = duration

    async def cooldown_remaining(self, key):
        return self._cooldowns.get(key, 0)

    async def cooldown_active(self, key):
        return key in self._cooldowns and self._cooldowns[key] > 0

    async def cooldown_clear(self, key):
        return self._cooldowns.pop(key, None) is not None

    async def cooldown_list(self):
        return [{"cooldown_key": k, "remaining_s": v}
                for k, v in self._cooldowns.items()]

    # plugin state mock
    async def state_get(self, key, default=None):
        return self._state.get(key, default)

    async def state_set(self, key, value):
        self._state[key] = value


async def flow(content, author=OWNER_ID, overrides=None, responses=None,
              wait=0.3):
    """Trigger the plugin and let it run for `wait` seconds."""
    bus = EventBus()
    manager = PluginManager(
        bus, logging.getLogger("t"),
        SettingsManager(bus, logging.getLogger("t"),
                        global_layer={"roll": overrides or {}}),
        plugin_dir="plugins",
    )
    manager.discover()
    await manager.attach_account("alpha", allowed={"roll"})
    services = RollServices(responses)
    await manager.activate_account("alpha", services)
    bus.publish_nowait(MessageReceived(
        account="alpha", message_id=1, channel_id=CHANNEL,
        author_id=author, content=content,
    ))
    await asyncio.sleep(wait)
    return services, manager


class TestParsePauseMinutes(unittest.TestCase):
    def test_extracts_minutes(self) -> None:
        self.assertEqual(parse_pause_minutes("**45** min"), 45)
        self.assertEqual(parse_pause_minutes("limited to **10** min go away"), 10)

    def test_no_match(self) -> None:
        self.assertIsNone(parse_pause_minutes("no timer here"))
        self.assertIsNone(parse_pause_minutes(""))


class TestRollTriggers(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_starts_rolling(self) -> None:
        services, _ = await flow("roll", wait=0.2)
        # at least one roll command was sent
        self.assertTrue(any(cmd.startswith("$") for _, cmd, _ in services.sent))

    async def test_non_owner_ignored(self) -> None:
        services, _ = await flow("roll", author=4242, wait=0.2)
        self.assertEqual(services.sent, [])

    async def test_other_content_ignored(self) -> None:
        services, _ = await flow("hello", wait=0.2)
        self.assertEqual(services.sent, [])

    async def test_custom_command(self) -> None:
        services, _ = await flow("roll", overrides={"command": "$wg"}, wait=0.2)
        self.assertTrue(any(cmd == "$wg" for _, cmd, _ in services.sent))


class TestCooldownParsing(unittest.IsolatedAsyncioTestCase):
    async def test_roulette_limited_sets_cooldown(self) -> None:
        responses = [
            {"content": "**alpha**, the roulette is limited to **45** min"},
        ]
        services, _ = await flow("roll", responses=responses, wait=0.3)
        # cooldown was set for 45 minutes = 2700 seconds
        self.assertIn("roulette", services._cooldowns)
        self.assertEqual(services._cooldowns["roulette"], 2700)

    async def test_key_limit_sends_ru(self) -> None:
        embed = NS(
            description="Some character\n(You reached the limit of 1000 keys per hour!)",
            footer_text="alpha · 2 ROLLS LEFT",
        )
        responses = [
            {"content": "", "message": NS(embeds=[embed], content="")},
        ]
        services, _ = await flow("roll", responses=responses, wait=0.3)
        # $us 20 was sent (low rolls) or $ru (key limit)
        commands = [cmd for _, cmd, _ in services.sent]
        self.assertTrue(any("$us" in c or "$ru" in c for c in commands),
                        f"no userstack/key command sent: {commands}")


class TestStopTrigger(unittest.IsolatedAsyncioTestCase):
    async def test_stop_trigger_stops(self) -> None:
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"), global_layer={}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"roll"})
        services = RollServices()
        await manager.activate_account("alpha", services)

        # start rolling
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=1, channel_id=CHANNEL,
            author_id=OWNER_ID, content="roll"))
        await asyncio.sleep(0.2)
        self.assertTrue(any(c.startswith("$") for _, c, _ in services.sent))

        # stop
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=2, channel_id=CHANNEL,
            author_id=OWNER_ID, content="rollstop"))
        await asyncio.sleep(0.2)

        count_before = len(services.sent)
        await asyncio.sleep(0.3)
        count_after = len(services.sent)
        # no more sends after stop (allowing for in-flight completion)
        self.assertLessEqual(count_after - count_before, 1)


if __name__ == "__main__":
    unittest.main()
