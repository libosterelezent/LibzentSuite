"""Roll plugin v2 tests: rolling loop, cooldowns, userstack, key limits."""
from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace as NS

from core.credentials import Credentials
from core.event_bus import EventBus, MessageReceived
from core.errors import AccessDenied, TargetGone
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
              wait=0.3, channel=CHANNEL):
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
        account="alpha", message_id=1, channel_id=channel,
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

    def test_hostile_inputs(self) -> None:
        self.assertIsNone(parse_pause_minutes("limited to **forever** min"))
        self.assertIsNone(parse_pause_minutes("limited to min"))
        self.assertIsNone(parse_pause_minutes("**** min"))
        self.assertEqual(parse_pause_minutes("**0045** min"), 45)
        self.assertEqual(parse_pause_minutes("**45**min"), 45)
        self.assertEqual(parse_pause_minutes("**45**   MIN"), 45)
        self.assertEqual(parse_pause_minutes("**999999999999** min"), 999999999999)
        self.assertIsNone(parse_pause_minutes(None))


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

    async def test_target_gone_aborts_clean(self) -> None:
        """A trigger in a vanished channel aborts with ONE clean line —
        never as a plugin crash (error counter must stay at zero)."""
        services, manager = await flow("roll", channel=987, wait=0.2)
        self.assertEqual(services.sent, [])
        state = {s["name"]: s for s in manager.states("alpha")}["roll"]
        self.assertEqual(state["error_count"], 0)
        self.assertIsNone(state["last_error"])


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

    async def test_userstack_disable_takes_effect_immediately(self) -> None:
        """After Mudae refuses $us once, later low-rolls responses must NOT
        re-send $us in the same session (live setting flips with the
        persisted state)."""
        low = {"content": "", "message": NS(
            embeds=[NS(description="Some character",
                       footer_text="alpha · 2 ROLLS LEFT")], content="")}
        responses = [
            low,                                            # roll 1 response
            {"content": "You don't have enough rolls stacked!"},  # $us reply
            low,                                            # roll 2 response
            low,                                            # roll 3 response
        ]
        services, manager = await flow(
            "roll", responses=responses,
            overrides={"roll_interval": 1.5, "response_timeout": 2.0},
            wait=5.5,
        )
        self.assertEqual(sum(1 for _, c, _ in services.sent if c == "$us 20"), 1)
        self.assertFalse(services._state["userstack_available"])
        self.assertIs(manager.live_settings("alpha", "roll")["userstack"], False)


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


def low_rolls_response():
    return {"content": "", "message": NS(
        embeds=[NS(description="Some character",
                   footer_text="alpha · 2 ROLLS LEFT")], content="")}


class TestHostileResponses(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_embed_shapes_do_not_kill_loop(self) -> None:
        shapes = [
            {"content": "", "message": NS(embeds=[NS()])},
            {"content": "", "message": NS(
                embeds=[NS(footer_text=None, description=None)])},
            {"content": None, "message": NS(embeds=[])},
            {"content": "", "message": NS(embeds=17)},
            {"content": "", "message": NS(embeds=["junk"])},
        ]
        responses = shapes + [
            low_rolls_response(),
            {"content": "You don't have enough rolls stacked!"},
        ]
        services, manager = await flow(
            "roll", responses=responses,
            overrides={"roll_interval": 1.5, "response_timeout": 2.0},
            wait=15.0,
        )
        commands = [c for _, c, _ in services.sent]
        self.assertGreaterEqual(commands.count("$wa"), 2)
        self.assertEqual(commands.count("$us 20"), 1)
        state = {s["name"]: s for s in manager.states("alpha")}["roll"]
        self.assertEqual(state["error_count"], 0)
        self.assertIsNone(state["last_error"])
        self.assertIs(manager.live_settings("alpha", "roll")["userstack"], False)

    async def test_key_limit_sets_cooldown_from_ru_reply(self) -> None:
        embed = NS(
            description="Some character\n"
                        "(You reached the limit of 1000 keys per hour!)",
            footer_text="alpha · #30",
        )
        responses = [
            {"content": "", "message": NS(embeds=[embed], content="")},
            {"content": "You have **10** keys | reset in **37** min"},
        ]
        services, _ = await flow(
            "roll", responses=responses,
            overrides={"roll_interval": 1.5, "response_timeout": 2.0},
            wait=2.5,
        )
        commands = [c for _, c, _ in services.sent]
        self.assertIn("$ru", commands)
        self.assertEqual(services._cooldowns.get("roulette"), 37 * 60)

    async def test_unrelated_us_reply_does_not_latch_disable(self) -> None:
        responses = [
            low_rolls_response(),
            {"content": "unrelated Mudae chatter"},
            low_rolls_response(),
        ]
        services, manager = await flow(
            "roll", responses=responses,
            overrides={"roll_interval": 1.5, "response_timeout": 2.0},
            wait=4.5,
        )
        commands = [c for _, c, _ in services.sent]
        self.assertEqual(commands.count("$us 20"), 2)
        self.assertNotIn("userstack_available", services._state)
        self.assertIs(manager.live_settings("alpha", "roll")["userstack"], True)

    async def test_persisted_unavailable_state_disables_on_load(self) -> None:
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"), global_layer={}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"roll"})
        services = RollServices([low_rolls_response()])
        services._state["userstack_available"] = False
        await manager.activate_account("alpha", services)
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=1, channel_id=CHANNEL,
            author_id=OWNER_ID, content="roll"))
        await asyncio.sleep(2.0)
        commands = [c for _, c, _ in services.sent]
        self.assertNotIn("$us 20", commands)
        self.assertIs(manager.live_settings("alpha", "roll")["userstack"], False)


class TestHostileMidFlow(unittest.IsolatedAsyncioTestCase):
    async def test_channel_deleted_mid_flow_aborts_clean(self) -> None:
        class VanishingChannelServices(RollServices):
            def __init__(self):
                super().__init__()
                self.validate_calls = 0

            def validate_channel(self, channel_id) -> None:
                self.validate_calls += 1
                if self.validate_calls > 2:
                    raise TargetGone("alpha", f"channel {channel_id} deleted")

        services = VanishingChannelServices()
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"),
                            global_layer={"roll": {"roll_interval": 1.5}}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"roll"})
        await manager.activate_account("alpha", services)
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=1, channel_id=CHANNEL,
            author_id=OWNER_ID, content="roll"))
        await asyncio.sleep(2.5)
        commands = [c for _, c, _ in services.sent]
        self.assertEqual(commands, ["$wa"])
        plugin = manager._accounts["alpha"]["roll"].instance
        self.assertFalse(plugin._rolling.get("alpha"))
        state = {s["name"]: s for s in manager.states("alpha")}["roll"]
        self.assertEqual(state["error_count"], 0)
        self.assertIsNone(state["last_error"])

    async def test_send_refused_mid_flow_aborts_clean(self) -> None:
        class RefusingSendServices(RollServices):
            def __init__(self):
                super().__init__()
                self.sends = 0

            async def send_message(self, channel_id, content, *,
                                   priority=Priority.LOW):
                self.sends += 1
                if self.sends > 1:
                    raise AccessDenied("alpha", "missing permissions")
                return await super().send_message(
                    channel_id, content, priority=priority)

        services = RefusingSendServices()
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"),
                            global_layer={"roll": {"roll_interval": 1.5}}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"roll"})
        await manager.activate_account("alpha", services)
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=1, channel_id=CHANNEL,
            author_id=OWNER_ID, content="roll"))
        await asyncio.sleep(2.5)
        commands = [c for _, c, _ in services.sent]
        self.assertEqual(commands, ["$wa"])
        plugin = manager._accounts["alpha"]["roll"].instance
        self.assertFalse(plugin._rolling.get("alpha"))
        state = {s["name"]: s for s in manager.states("alpha")}["roll"]
        self.assertEqual(state["error_count"], 0)
        self.assertIsNone(state["last_error"])

    async def test_repeated_start_stop_no_state_growth(self) -> None:
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"),
                            global_layer={"roll": {"roll_interval": 1.5}}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"roll"})
        services = RollServices()
        await manager.activate_account("alpha", services)
        plugin = manager._accounts["alpha"]["roll"].instance
        for i in range(3):
            bus.publish_nowait(MessageReceived(
                account="alpha", message_id=10 + i * 2, channel_id=CHANNEL,
                author_id=OWNER_ID, content="roll"))
            await asyncio.sleep(0.2)
            bus.publish_nowait(MessageReceived(
                account="alpha", message_id=11 + i * 2, channel_id=CHANNEL,
                author_id=OWNER_ID, content="rollstop"))
            await asyncio.sleep(0.2)
        self.assertEqual(plugin._tasks, {})
        self.assertLessEqual(set(plugin._rolling), {"alpha"})
        count_before = len(services.sent)
        await asyncio.sleep(0.5)
        self.assertEqual(len(services.sent), count_before)


class TestHostileSettingsAndReload(unittest.IsolatedAsyncioTestCase):
    async def test_hostile_userstack_values_disable_plugin(self) -> None:
        for value in ("20", 20, "twenty", -1, 2.5):
            with self.subTest(value=value):
                services, manager = await flow(
                    "roll", overrides={"userstack": value}, wait=0.15)
                self.assertEqual(services.sent, [])
                state = {s["name"]: s for s in manager.states("alpha")}["roll"]
                self.assertFalse(state["enabled"])
                self.assertTrue(state["last_error"].startswith("settings:"))

    async def test_none_content_trigger_ignored(self) -> None:
        services, manager = await flow(None, wait=0.15)
        self.assertEqual(services.sent, [])
        state = {s["name"]: s for s in manager.states("alpha")}["roll"]
        self.assertEqual(state["error_count"], 0)
        self.assertIsNone(state["last_error"])

    async def test_command_hot_reload_mid_roll(self) -> None:
        services, manager = await flow(
            "roll", overrides={"roll_interval": 1.5, "response_timeout": 2.0},
            wait=0.1)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while not services.sent and loop.time() < deadline:
            await asyncio.sleep(0.01)
        self.assertTrue(services.sent)
        await manager.set_setting("alpha", "roll", "command", "$wg")
        await asyncio.sleep(2.2)
        commands = [c for _, c, _ in services.sent]
        self.assertIn("$wa", commands)
        self.assertIn("$wg", commands)


if __name__ == "__main__":
    unittest.main()
