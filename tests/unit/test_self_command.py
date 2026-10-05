"""Self-command layer: the framework account commands ITSELF via a prefix.

discord.py-self's ext.commands Bot in self_bot=True mode processes only
the account's OWN messages (other users are skipped by _skip_check).
This test suite pins the adapter's integration: the factory wiring, the
prefix routing, the synthetic DTO that re-enters the plugin pipeline,
and the isolation invariants (other users' commands never fire; commands
never become events; RESUME replays never re-fire).

Round-4 F2-3: the LibraryBacked classes below construct the REAL
ext.commands Bot(self_bot=True) from the installed fork and drive the
adapter's own handler with forged messages — offline, no network — so
the dispatch-order assumptions are pinned against library truth, not
imitated.
"""
from __future__ import annotations

import asyncio
import importlib.util
import unittest
from types import SimpleNamespace as NS

from adapters.discord.client import RealClientFactory, RealDiscordClient


def _make_client(prefix: str = "sm!") -> RealDiscordClient:
    return RealDiscordClient("token-x", command_prefix=prefix)


def _forged(mid: int, author_id: int, content: str) -> NS:
    """A message the fork's Context/Command machinery accepts offline.

    Needs the attributes the 2.1.0 call path reads: author.bot
    (process_commands guard), message._state (Context.__init__) and
    message.attachments (_AttachmentIterator in Command.prepare).
    """
    return NS(id=mid, author=NS(id=author_id, bot=False),
              channel=NS(id=42, guild=NS(id=10)), content=content,
              attachments=[], created_at=None, _state=NS())


def _library_available() -> bool:
    return importlib.util.find_spec("discord") is not None


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

    async def test_self_trigger_dto_carries_author_and_channel(self):
        """Round-4 F2-7: the synthetic DTO is not author/channel-blind —
        the author projection names the account, the channel projection
        is derived from ctx.channel, and created_at rides when present."""
        client = _make_client()
        seen: list = []
        client.on_message = seen.append
        client.user_id = 555
        client.account = "alpha"
        channel = NS(id=42, name="grind", guild=NS(id=10))
        ctx = NS(message=NS(id=99, created_at=None), channel=channel)
        client._publish_self_trigger(ctx, "slcopy")
        dto = seen[0]
        self.assertIsNotNone(dto.author)
        self.assertEqual(dto.author.id, 555)
        self.assertEqual(dto.author.name, "alpha")
        self.assertIsNotNone(dto.channel)
        self.assertEqual(dto.channel.id, 42)
        self.assertEqual(dto.channel.guild_id, 10)
        self.assertIsNone(dto.created_at)  # pinned: None is acceptable
        # and with a real created_at on the source message it rides along
        import datetime as _dt
        stamp = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
        ctx2 = NS(message=NS(id=100, created_at=stamp), channel=channel)
        client._publish_self_trigger(ctx2, "roll")
        self.assertEqual(seen[1].created_at, stamp)


class LibraryBackedSelfCommandTests(unittest.IsolatedAsyncioTestCase):
    """Round-4 F2-3: the REAL ext.commands Bot from the installed fork,
    driven offline through the adapter's own handler wiring.

    Pins (against library truth, discord.py-self 2.1.0):
      * self_bot skip semantics — foreign invokers never fire commands;
      * own prefixed trigger fires EXACTLY ONCE, including across a
        gateway RESUME replay of the same message id (round-4 F2-1);
      * command words are case-insensitive, the prefix is not
        (round-4 F2-8);
      * unknown commands never reach the event bus.
    """

    def setUp(self) -> None:
        if not _library_available():
            self.skipTest("discord.py-self not installed")
        import discord
        from discord.ext import commands as ext_commands

        self._discord = discord
        self._ext = ext_commands
        # fresh notice flag per test so the one-time INFO fires per run
        import adapters.discord.client as adc
        self._adc = adc
        adc._self_command_notice_logged = False

    def _build_bot(self, client: RealDiscordClient):
        """The adapter's Bot, constructed exactly like connect() does."""
        bot = self._ext.Bot(
            command_prefix="sm!", self_bot=True, help_command=None,
            case_insensitive=True, max_ratelimit_timeout=30.0)
        client._discord = self._discord
        client._client = bot
        client._install_command_layer(self._discord)
        # post-READY identity, as on_ready would set it
        bot._connection.user = NS(id=555)
        client.user_id = 555
        # the library's dispatch path needs a loop for scheduled events
        bot.loop = asyncio.get_running_loop()
        return bot

    async def test_skip_check_semantics_match_self_bot(self) -> None:
        client = _make_client()
        bot = self._build_bot(client)
        skip = bot._skip_check  # bot.py:198-203 — x != y under self_bot
        self.assertIs(skip(555, 555), False)   # own messages pass
        self.assertIs(skip(666, 555), True)    # foreign are skipped
        # the adapter's own post-construction guard accepts the real bot
        client._assert_self_bot_semantics()

    async def test_self_bot_drift_fails_closed(self) -> None:
        """Round-4 F2-2: an inverted (or missing) skip-check must raise
        AccountError — never silently process everyone else."""
        from core.errors import AccountError

        client = _make_client()
        client.account = "alpha"
        inverted = NS(_skip_check=lambda x, y: x == y)  # user_bot drift
        client._client = inverted
        with self.assertRaises(AccountError):
            client._assert_self_bot_semantics()
        missing = NS()  # attribute gone entirely
        client._client = missing
        with self.assertRaises(AccountError):
            client._assert_self_bot_semantics()

    async def test_own_command_fires_once_and_replay_is_suppressed(self) -> None:
        client = _make_client()
        bot = self._build_bot(client)
        seen: list = []
        client.on_message = seen.append

        def synthetic() -> list:
            # self-command triggers republish with the account's own id
            # and the stripped word; foreign plugin events carry their
            # own author id and the RAW content
            return [e for e in seen if e.author_id == 555]

        # foreign invoker: 0 synthetic events (the library skips them in
        # command dispatch; the message still flows the PLUGIN path)
        await client._handle_library_message(_forged(1, 666, "sm!roll"))
        self.assertEqual(synthetic(), [])
        self.assertEqual([e.content for e in seen], ["sm!roll"])  # raw event

        # own invoker: exactly 1 synthetic trigger
        await client._handle_library_message(_forged(2, 555, "sm!roll"))
        self.assertEqual([e.content for e in synthetic()], ["roll"])

        # gateway RESUME replay of the SAME message id: still 1 total
        await client._handle_library_message(_forged(2, 555, "sm!roll"))
        self.assertEqual(len(synthetic()), 1)

        # a DISTINCT later command still fires (dedup is per message id)
        await client._handle_library_message(_forged(3, 555, "sm!rollstop"))
        self.assertEqual([e.content for e in synthetic()],
                         ["roll", "rollstop"])

    async def test_command_word_case_insensitive_prefix_is_not(self) -> None:
        client = _make_client()
        self._build_bot(client)
        seen: list = []
        client.on_message = seen.append
        await client._handle_library_message(_forged(4, 555, "sm!ROLL"))
        await client._handle_library_message(_forged(5, 555, "sm!Roll"))
        self.assertEqual([e.content for e in seen], ["roll", "roll"])
        await client._handle_library_message(_forged(6, 555, "SM!ROLL"))
        self.assertEqual(len(seen), 2)  # prefix itself stays case-sensitive

    async def test_unknown_command_never_reaches_event_bus(self) -> None:
        client = _make_client()
        self._build_bot(client)
        seen: list = []
        client.on_message = seen.append
        await client._handle_library_message(_forged(7, 555, "sm!typo"))
        await client._handle_library_message(_forged(8, 555, "roll"))
        await client._handle_library_message(_forged(9, 555, "sm!roll extra"))
        self.assertEqual(
            [e.content for e in seen], ["roll"])  # trailing args still fire

    async def test_event_path_still_publishes_foreign_messages(self) -> None:
        """With the command layer active the PLUGIN path is unchanged:
        foreign messages convert and publish (a command never becomes an
        event, an event never becomes a command)."""
        client = _make_client()
        bot = self._build_bot(client)
        seen: list = []
        client.on_message = seen.append
        foreign = NS(
            id=10, author=NS(id=999999, bot=True),
            channel=NS(id=42, name="grind", type=NS(name="text"),
                       guild=NS(id=10)),
            guild=NS(id=10), content="$mu", embeds=[], components=[],
            attachments=[], reactions=[], reference=None, created_at=None)
        await client._handle_library_message(foreign)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].content, "$mu")
        self.assertEqual(seen[0].author_id, 999999)
        # replay suppressed on the plugin path too
        await client._handle_library_message(foreign)
        self.assertEqual(len(seen), 1)

    async def test_notice_logs_fixed_trigger_words_once(self) -> None:
        """Round-4 F2-8: construction logs the fixed pass-through words
        exactly once per process."""
        import logging

        client = _make_client()
        logger = logging.getLogger("test.selfcmd.notice")
        logger.propagate = False
        with self.assertLogs(logger, level="INFO") as captured:
            self._adc._log_self_command_notice(logger)
            self._adc._log_self_command_notice(logger)  # second is a no-op
        self.assertEqual(len(captured.output), 1)
        self.assertIn("'roll'", captured.output[0])
        self.assertIn("'rollstop'", captured.output[0])
        self.assertIn("'slcopy'", captured.output[0])
        logger.propagate = True


if __name__ == "__main__":
    unittest.main()
