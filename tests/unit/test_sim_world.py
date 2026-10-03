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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
