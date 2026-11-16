"""Unit tests for adapters.simulator.world.SimWorld (offline, synthetic ids)."""
from __future__ import annotations

import asyncio
import time
import unittest

from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_BETA_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_MUDAE_ID,
    SYNTHETIC_OWNER_ID,
    SimWorld,
)
from core.errors import ServerRateLimit


class _Recorder:
    """Minimal world subscriber standing in for a connected client."""

    def __init__(self) -> None:
        self.received = []

    def receive(self, message) -> None:
        self.received.append(message)


def _basic_world() -> SimWorld:
    """Owner + two account users + mudae + one guild channel."""
    world = SimWorld()
    world.add_user(SYNTHETIC_OWNER_ID, "owner")
    world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
    world.add_user(SYNTHETIC_BETA_ID, "beta")
    world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
    world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
    return world


class TestTopology(unittest.TestCase):
    """users/channels registration and lookups."""

    def test_users_and_channels_register(self) -> None:
        world = _basic_world()
        self.assertEqual(world.users[SYNTHETIC_OWNER_ID], "owner")
        self.assertTrue(world.channel_exists(SYNTHETIC_GUILD_CHANNEL))
        self.assertEqual(
            world.channels[SYNTHETIC_GUILD_CHANNEL], "guild-channel"
        )
        self.assertFalse(world.dm_channels.get(SYNTHETIC_GUILD_CHANNEL))

    def test_dm_ids_deterministic_and_symmetric(self) -> None:
        world = _basic_world()
        first = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        again = world.dm_channel_for(SYNTHETIC_MUDAE_ID, SYNTHETIC_ALPHA_ID)
        self.assertEqual(first, again)  # symmetric pair -> one channel
        other = world.dm_channel_for(SYNTHETIC_BETA_ID, SYNTHETIC_MUDAE_ID)
        self.assertNotEqual(first, other)  # distinct pairs never merge
        self.assertTrue(world.dm_channels[first])  # flagged as a DM channel

    def test_dm_id_collision_falls_back(self) -> None:
        """A pre-squatted hash id must not merge two conversations."""
        world = SimWorld()
        pair = (SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)  # alpha < mudae
        squatted = 1_000_000 + pair[0] * 7919 + pair[1]
        world.add_channel(squatted, "occupied")
        resolved = world.dm_channel_for(pair[0], pair[1])
        self.assertNotEqual(resolved, squatted)
        self.assertTrue(resolved >= 2_000_000_000)  # sequential fallback range
        self.assertEqual(world.channels[squatted], "occupied")  # untouched

    def test_dm_pairs_of_filters_per_channel(self) -> None:
        world = _basic_world()
        channel = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_BETA_ID)
        self.assertEqual(
            world.dm_pairs_of(channel),
            [(SYNTHETIC_ALPHA_ID, SYNTHETIC_BETA_ID)],
        )
        self.assertEqual(world.dm_pairs_of(SYNTHETIC_GUILD_CHANNEL), [])


class TestDelivery(unittest.TestCase):
    """deliver(): history + journal + subscriber push."""

    def test_deliver_pushes_to_subscribers_and_history(self) -> None:
        world = _basic_world()
        recorder = _Recorder()
        world.subscribers.append(recorder)
        message = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "hello"
        )
        self.assertEqual(recorder.received, [message])
        self.assertEqual(
            world.messages_of(SYNTHETIC_GUILD_CHANNEL), ["hello"]
        )
        self.assertIn(message, world.journal)

    def test_deliver_unknown_channel_raises_key_error(self) -> None:
        world = _basic_world()
        with self.assertRaises(KeyError):
            world.deliver(424242, SYNTHETIC_OWNER_ID, "void")


class _Boom:
    """A subscriber whose callback always fails."""

    user_id = 0

    def receive(self, message) -> None:
        raise RuntimeError("boom")


class _AsyncBlocker:
    """A subscriber that parks async work off the dispatch path (never
    blocks the sync fan-out, never finishes before the test ends)."""

    user_id = 0

    def __init__(self) -> None:
        self.parked = []

    def receive(self, message) -> None:
        self.parked.append(
            asyncio.get_running_loop().create_task(asyncio.sleep(999))
        )


class TestSubscriberIsolation(unittest.TestCase):
    """One failing subscriber must not lose the event for the others."""

    def test_failing_subscriber_does_not_block_later_subscribers(self) -> None:
        world = _basic_world()
        good = _Recorder()
        world.subscribers.append(_Boom())
        world.subscribers.append(good)
        message = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "hello"
        )
        self.assertEqual(good.received, [message])

    def test_cancel_pending_replies_stops_inflight_pages(self) -> None:
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one", "page-two"]},
            reply_delay=30.0,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")

        async def _drive() -> None:
            world.deliver(
                SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s"
            )
            await asyncio.sleep(0.05)
            self.assertTrue(world._responder_tasks)  # reply mid-flight
            world.cancel_pending_replies()
            await asyncio.sleep(0.05)

        asyncio.run(_drive())
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        self.assertEqual(world.messages_of(dm), [])  # pages never landed


class TestSubscriberIsolationAsync(unittest.IsolatedAsyncioTestCase):
    """The scripted responder survives a failing subscriber (async world)."""

    async def test_failing_subscriber_does_not_kill_the_responder(self) -> None:
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one"]},
            reply_delay=0.01,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        world.subscribers.append(_Boom())
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s")
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        await world.wait_for_quiet(dm, quiet=0.2, limit=2.0)
        self.assertEqual(world.messages_of(dm), ["page-one"])

    async def test_subscriber_raising_on_every_event_loses_nothing(self) -> None:
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one", "page-two"]},
            reply_delay=0.01,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        good = _Recorder()
        world.subscribers.append(_Boom())  # raises on EVERY event
        world.subscribers.append(good)
        trigger = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s"
        )
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        await world.wait_for_quiet(dm, quiet=0.2, limit=3.0)
        contents = [m.content for m in good.received]
        self.assertEqual(
            contents, ["$slud-s", "page-one", "page-two"]
        )  # trigger + both pages: nothing lost to the failing neighbor
        self.assertEqual(good.received[0].id, trigger.id)

    async def test_async_blocking_subscriber_does_not_stall_delivery(self) -> None:
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one"]},
            reply_delay=0.01,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        blocker = _AsyncBlocker()
        good = _Recorder()
        world.subscribers.append(blocker)
        world.subscribers.append(good)
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s")
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        # the parked sleep(999) must not hold up later events: the page
        # still lands within the (sub-second) quiet window
        await world.wait_for_quiet(dm, quiet=0.15, limit=2.0)
        self.assertEqual(world.messages_of(dm), ["page-one"])
        self.assertEqual(len(good.received), 2)
        for parked in blocker.parked:
            parked.cancel()


class TestCancelPendingReplies(unittest.IsolatedAsyncioTestCase):
    """cancel_pending_replies(): completion race + no stale reuse."""

    async def test_cancel_at_completion_instant_is_harmless(self) -> None:
        """Cancel arriving the instant the reply task finished must be a
        quiet no-op (no error, no lost evidence)."""
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one"]},
            reply_delay=0.0,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s")
        for _ in range(4):  # let the task run to completion, uncancelled
            await asyncio.sleep(0)
        world.cancel_pending_replies()  # races the done callback
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        self.assertEqual(world.messages_of(dm), ["page-one"])

    async def test_second_flow_after_cancel_reuses_nothing_stale(self) -> None:
        """After one cancelled flow, a NEW flow on the same world gets
        fresh responder tasks and still delivers."""
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one"]},
            reply_delay=0.01,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)

        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s")
        await asyncio.sleep(0.02)
        world.cancel_pending_replies()
        await asyncio.sleep(0.02)  # cancellation settles
        self.assertEqual(world.messages_of(dm), [])

        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s")
        await world.wait_for_quiet(dm, quiet=0.15, limit=2.0)
        self.assertEqual(world.messages_of(dm), ["page-one"])
        world.cancel_pending_replies()  # idempotent second call


class TestScriptedResponder(unittest.IsolatedAsyncioTestCase):
    """Command-matched pages arrive via DM, in order, with delays."""

    async def test_pages_delivered_to_requesters_dm_with_delay(self) -> None:
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one", "page-two"]},
            reply_delay=0.08,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_user(SYNTHETIC_BETA_ID, "beta")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")

        world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s"
        )
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        await world.wait_for_quiet(dm, quiet=0.25, limit=5.0)

        self.assertEqual(
            world.messages_of(dm), ["page-one", "page-two"]
        )  # right channel (alpha's DM), right order
        beta_dm = world.dm_channel_for(SYNTHETIC_BETA_ID, SYNTHETIC_MUDAE_ID)
        self.assertEqual(world.messages_of(beta_dm), [])  # nobody else's DM

        # per-page delay honored: the two pages are >= ~1 delay apart
        history = world.history[dm]
        self.assertGreaterEqual(len(history), 2)
        gap = history[1].ts - history[0].ts
        self.assertGreaterEqual(gap, 0.05)

    async def test_responder_ignores_unregistered_and_own_commands(self) -> None:
        world = SimWorld(responder_id=SYNTHETIC_MUDAE_ID, reply_delay=0.01)
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        world.add_response("$slud-s", ["page"])

        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "smalltalk")
        await asyncio.sleep(0.05)
        self.assertEqual(world.journal[-1].content, "smalltalk")  # no reply

        # the responder echoing its own command must not loop
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_MUDAE_ID, "$slud-s")
        await asyncio.sleep(0.05)
        replies = [
            m
            for m in world.journal
            if m.dm and m.author_id == SYNTHETIC_MUDAE_ID
        ]
        self.assertEqual(replies, [])


class TestWaitForQuiet(unittest.IsolatedAsyncioTestCase):
    """The audited quiet-window fix: return on silence, not on the limit."""

    async def test_returns_after_quiet_not_full_limit(self) -> None:
        world = _basic_world()
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "ping")
        started = time.monotonic()
        await world.wait_for_quiet(
            SYNTHETIC_GUILD_CHANNEL, quiet=0.15, limit=5.0
        )
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.5)  # far below the hard cap

    async def test_traffic_resets_the_quiet_window(self) -> None:
        world = _basic_world()

        async def _chatter() -> None:
            await asyncio.sleep(0.1)
            world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "x")

        task = asyncio.get_running_loop().create_task(_chatter())
        started = time.monotonic()
        await world.wait_for_quiet(
            SYNTHETIC_GUILD_CHANNEL, quiet=0.2, limit=5.0
        )
        elapsed = time.monotonic() - started
        await task
        # the 0.1s chatter must have pushed the return past 0.2s from START
        self.assertGreaterEqual(elapsed, 0.25)


class TestServerLimits(unittest.IsolatedAsyncioTestCase):
    """One-shot 429 queue: fires once, matches channels, records evidence."""

    def _armed(self, **kwargs) -> SimWorld:
        world = _basic_world()
        world.server_limit_next(
            channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.5, global_=True
        )
        return world

    async def test_one_shot_consumed_once(self) -> None:
        world = self._armed()
        limit = world.take_server_limit(SYNTHETIC_ALPHA_ID, SYNTHETIC_GUILD_CHANNEL)
        self.assertIsInstance(limit, ServerRateLimit)
        self.assertEqual(limit.retry_after, 0.5)
        self.assertTrue(limit.global_)
        self.assertEqual(len(world.blocked_sends), 1)  # recorded with ts
        self.assertGreater(world.blocked_sends[0]["ts"], 0.0)
        # consumed: the next intake finds nothing
        self.assertIsNone(
            world.take_server_limit(SYNTHETIC_ALPHA_ID, SYNTHETIC_GUILD_CHANNEL)
        )

    async def test_channel_scoped_entry_ignores_other_channels(self) -> None:
        world = _basic_world()
        other = 9_000_001
        world.add_channel(other, "elsewhere")
        world.server_limit_next(channel_id=other, retry_after=0.3)
        self.assertIsNone(
            world.take_server_limit(SYNTHETIC_ALPHA_ID, SYNTHETIC_GUILD_CHANNEL)
        )
        limit = world.take_server_limit(SYNTHETIC_ALPHA_ID, other)
        self.assertIsInstance(limit, ServerRateLimit)
        self.assertEqual(limit.retry_after, 0.3)
        self.assertFalse(limit.global_)

    async def test_user_scoped_entry_ignores_other_users(self) -> None:
        world = _basic_world()
        world.server_limit_next(
            channel_id=None, retry_after=0.2, user_id=SYNTHETIC_ALPHA_ID
        )
        self.assertIsNone(
            world.take_server_limit(SYNTHETIC_BETA_ID, SYNTHETIC_GUILD_CHANNEL)
        )
        self.assertIsNotNone(
            world.take_server_limit(SYNTHETIC_ALPHA_ID, SYNTHETIC_GUILD_CHANNEL)
        )


class TestRichEventParity(unittest.IsolatedAsyncioTestCase):
    """Edit/reaction events cross with the SAME rich shape as live."""

    def _rich_world(self) -> SimWorld:
        from adapters.model import DiscordComponent, DiscordEmbed

        world = _basic_world()
        embed = DiscordEmbed(title="roster", description="Alice\nBob")
        button = DiscordComponent(
            type=2, type_name="button", custom_id="roll-once", label="Roll")
        row = DiscordComponent(
            type=1, type_name="action_row", components=(button,))
        world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "rich payload",
            author_name="owner", embeds=(embed,), components=(row,), guild_id=7,
        )
        return world

    async def test_edit_event_carries_rich_fields(self) -> None:
        """Edit parity: live edits carry the full convert_message shape;
        the sim used to push a minimal 5-field DTO (embeds dropped)."""
        world = self._rich_world()
        seen = []
        subscriber = type("Sub", (), {
            "user_id": SYNTHETIC_ALPHA_ID, "receive": lambda self, m: None,
            "on_message_edit": staticmethod(seen.append),
        })()
        world.subscribers.append(subscriber)
        world.edit_message(1, "edited rich payload")
        self.assertEqual(len(seen), 1)
        edited = seen[0]
        self.assertEqual(edited.content, "edited rich payload")
        self.assertEqual(edited.embeds[0].title, "roster")
        self.assertEqual(edited.components[0].components[0].custom_id,
                         "roll-once")
        self.assertEqual(edited.channel.name, "guild-channel")

    async def test_edit_event_filters_author_client(self) -> None:
        world = self._rich_world()
        seen = []
        subscriber = type("Sub", (), {
            "user_id": SYNTHETIC_OWNER_ID, "receive": lambda self, m: None,
            "on_message_edit": staticmethod(seen.append),
        })()
        world.subscribers.append(subscriber)  # the author's own client
        world.edit_message(1, "self edit")
        # own edits never echo back — AUDIT FIX: the live adapter's rich
        # edit/delete/reaction handlers were flipped to this same
        # others-only polarity (raw+sim parity); previously they were
        # inverted and published ONLY self-authored cached events.
        self.assertEqual(seen, [])

    async def test_reaction_raw_is_handle_tuple_like_live(self) -> None:
        """Live pushes ``(reaction, user)`` library handles — never None."""
        world = self._rich_world()
        seen = []
        subscriber = type("Sub", (), {
            "user_id": SYNTHETIC_ALPHA_ID, "receive": lambda self, m: None,
            "on_reaction": staticmethod(lambda *args: seen.append(args)),
        })()
        world.subscribers.append(subscriber)
        world.react(SYNTHETIC_OWNER_ID, 1, "👍")
        self.assertEqual(len(seen), 1)
        kind, mid, cid, gid, uid, emoji, raw = seen[0]
        self.assertEqual(kind, "add")
        self.assertEqual(uid, SYNTHETIC_OWNER_ID)
        self.assertEqual(emoji, "👍")
        reaction, user = raw  # a (reaction, user) tuple, live-style
        self.assertEqual(reaction.emoji, "👍")
        self.assertEqual(reaction.count, 1)
        self.assertFalse(reaction.me)
        self.assertEqual(user.id, SYNTHETIC_OWNER_ID)
        self.assertEqual(user.name, "owner")

    async def test_to_discord_message_is_the_one_boundary_builder(self) -> None:
        """deliveries (client.receive) and edits share to_discord_message."""
        from adapters.simulator.world import to_discord_message

        world = self._rich_world()
        message = world.journal[-1]
        dto = to_discord_message(world, message)
        self.assertEqual(dto.id, message.id)
        self.assertEqual(dto.author.name, "owner")
        self.assertEqual(dto.embeds, message.embeds)
        self.assertEqual(dto.components, message.components)
        self.assertEqual(dto.channel.type, "text")


class TestChannelGone(unittest.IsolatedAsyncioTestCase):
    """channel_gone(): snapshots stop listing it, sends raise afterwards."""

    async def test_channel_gone_hides_channel_and_blocks_sends(self) -> None:
        world = _basic_world()
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "before")
        self.assertTrue(world.channel_gone(SYNTHETIC_GUILD_CHANNEL))

        self.assertFalse(world.channel_exists(SYNTHETIC_GUILD_CHANNEL))
        self.assertNotIn(SYNTHETIC_GUILD_CHANNEL, world.channels)
        self.assertNotIn(SYNTHETIC_GUILD_CHANNEL, world.history)
        self.assertIn(SYNTHETIC_GUILD_CHANNEL, world.gone_channels)
        # later sends raise KeyError (the client layer maps this to TargetGone)
        with self.assertRaises(KeyError):
            world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "after")
        # the journal keeps the pre-deletion evidence for post-mortems
        self.assertEqual(
            [m.content for m in world.journal if m.channel_id == SYNTHETIC_GUILD_CHANNEL],
            ["before"],
        )

    async def test_channel_gone_twice_reports_false(self) -> None:
        world = _basic_world()
        self.assertTrue(world.channel_gone(SYNTHETIC_GUILD_CHANNEL))
        self.assertFalse(world.channel_gone(SYNTHETIC_GUILD_CHANNEL))  # already gone

    async def test_deleted_dm_pair_stays_dead_and_logs_divergence(self) -> None:
        """Pinned divergence, made LOUD: a deleted DM channel keeps its
        pair mapping, so the pair is unsendable forever (live Discord
        resurrects DM channels; the sim deliberately does not)."""
        world = _basic_world()
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        with self.assertLogs("libzent.simulator", level="WARNING") as logs:
            self.assertTrue(world.channel_gone(dm))
        self.assertTrue(
            any("deliberate sim divergence" in line for line in logs.output),
            logs.output,
        )
        # the pair mapping is retained: same id, forever gone
        self.assertEqual(
            world.dm_channel_for(SYNTHETIC_MUDAE_ID, SYNTHETIC_ALPHA_ID), dm
        )
        self.assertFalse(world.channel_exists(dm))
        self.assertNotIn(dm, world.channels)
        # a guild channel deletion stays QUIET (no divergence there)
        guild = _basic_world()
        with self.assertNoLogs("libzent.simulator"):
            guild.channel_gone(SYNTHETIC_GUILD_CHANNEL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
