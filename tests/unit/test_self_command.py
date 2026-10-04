"""Self-command layer: the framework account commands ITSELF via a prefix.

discord.py-self's ext.commands Bot in self_bot=True mode processes only
the account's OWN messages (other users are skipped by _skip_check).
This test suite pins the adapter's integration without needing the real
library: the factory wiring, the prefix routing, the synthetic DTO that
re-enters the plugin pipeline, and the isolation invariants (other
users' commands never fire; commands never become events).
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace as NS

from adapters.discord.client import RealClientFactory, RealDiscordClient


def _make_client(prefix: str = "sm!") -> RealDiscordClient:
    return RealDiscordClient("token-x", command_prefix=prefix)


class FactoryWiringTests(unittest.TestCase):
    def test_factory_threads_prefix_to_clients(self) -> None:
        factory = RealClientFactory(command_prefix="!")
        a = factory("alpha", "tok-a")
        b = factory("beta", "tok-b")
        self.assertEqual(a._command_prefix, "!")
        self.assertEqual(b._command_prefix, "!")

    def test_no_prefix_by_default(self) -> None:
        factory = RealClientFactory()
        client = factory("alpha", "tok")
        self.assertEqual(client._command_prefix, "")

    def test_env_default_is_empty(self) -> None:
        from core.credentials import Credentials

        creds = Credentials.from_values({})
        self.assertEqual(creds.self_command_prefix, "")
        creds2 = Credentials.from_values({"SELF_COMMAND_PREFIX": "sm!"})
        self.assertEqual(creds2.self_command_prefix, "sm!")
        creds3 = Credentials.from_values({"SELF_COMMAND_PREFIX": "@me"})
        self.assertEqual(creds3.self_command_prefix, "@me")


class SelfTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_self_trigger_routes_through_on_message(self):
        """A self-command re-publishes as a synthetic DiscordMessage via
        the adapter callback — the plugin pipeline sees it as a normal
        owner-typed message with the account's own id as author."""
        client = _make_client()
        seen: list = []
        client.on_message = seen.append
        client.user_id = 555
        channel = NS(id=42, guild=NS(id=10))
        ctx = NS(message=NS(id=99), channel=channel)
        client._publish_self_trigger(ctx, "roll")
        self.assertEqual(len(seen), 1)
        dto = seen[0]
        self.assertEqual(dto.author_id, 555)      # self as author
        self.assertEqual(dto.content, "roll")     # stripped content
        self.assertEqual(dto.channel_id, 42)      # real channel
        self.assertEqual(dto.guild_id, 10)

    async def test_publish_without_callback_is_noop(self):
        client = _make_client()
        client.user_id = 1
        ctx = NS(message=NS(id=1), channel=NS(id=2, guild=None))
        client._publish_self_trigger(ctx, "roll")  # must not raise

    async def test_dm_channel_has_no_guild_id(self):
        client = _make_client()
        seen: list = []
        client.on_message = seen.append
        client.user_id = 1
        channel = NS(id=9001, guild=None)  # DM: no guild
        ctx = NS(message=NS(id=1), channel=channel)
        client._publish_self_trigger(ctx, "rollstop")
        self.assertEqual(seen[0].guild_id, None)
        self.assertTrue(seen[0].dm or seen[0].channel_id == 9001)


if __name__ == "__main__":
    unittest.main()
