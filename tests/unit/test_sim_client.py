"""Unit tests for adapters.simulator.client.SimClient (offline, synthetic)."""
from __future__ import annotations

import unittest

from adapters.base import DiscordMessage
from adapters.simulator.client import (
    LATENCY_MAX_MS,
    LATENCY_MIN_MS,
    SimClient,
)
from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_BETA_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_MUDAE_ID,
    SYNTHETIC_OWNER_ID,
    SimWorld,
)
from adapters.simulator import client as client_module
from core.errors import ServerRateLimit, TargetGone


def _world() -> SimWorld:
    world = SimWorld()
    world.add_user(SYNTHETIC_OWNER_ID, "owner")
    world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
    world.add_user(SYNTHETIC_BETA_ID, "beta")
    world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
    world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
    return world


async def _connected(world: SimWorld, user_id: int, **kwargs) -> SimClient:
    """A client that already joined the world."""
    client = SimClient(world, user_id, **kwargs)
    await client.connect()
    return client


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    """connect/disconnect idempotence and the unexpected-drop path."""

    async def test_connect_disconnect_idempotent(self) -> None:
        world = _world()
        client = SimClient(world, SYNTHETIC_ALPHA_ID)
        await client.connect()
        await client.connect()  # second connect must not double-subscribe
        self.assertEqual(world.subscribers.count(client), 1)
        self.assertTrue(client.connected)

        await client.disconnect()
        await client.disconnect()  # second disconnect stays quiet
        self.assertFalse(client.connected)
        self.assertNotIn(client, world.subscribers)
        self.assertEqual(world.subscribers.count(client), 0)

    async def test_drop_fires_on_disconnect_once(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        reasons = []
        client.on_disconnect = reasons.append
        client.drop("simulated network drop")
        self.assertEqual(reasons, ["simulated network drop"])
        self.assertFalse(client.connected)
        client.drop("again")  # already offline: no second callback
        self.assertEqual(reasons, ["simulated network drop"])

    async def test_receive_skips_own_messages(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen = []
        client.on_message = seen.append
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "mine")
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "theirs")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].content, "theirs")
        self.assertIsInstance(seen[0], DiscordMessage)

    async def test_fail_connect_mode_raises(self) -> None:
        world = _world()
        client = SimClient(world, SYNTHETIC_ALPHA_ID, fail_connect=True)
        with self.assertRaises(ConnectionError):
            await client.connect()
        self.assertFalse(client.connected)


class TestSends(unittest.IsolatedAsyncioTestCase):
    """All send paths: offline, 429, vanished channel, success."""

    async def test_send_while_not_connected_raises(self) -> None:
        world = _world()
        client = SimClient(world, SYNTHETIC_ALPHA_ID)  # never connected
        with self.assertRaises(ConnectionError):
            await client.send_message(SYNTHETIC_GUILD_CHANNEL, "hi")
        with self.assertRaises(ConnectionError):
            await client.send_dm(SYNTHETIC_OWNER_ID, "hi")

    async def test_pending_server_limit_raises_then_consumed(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        world.server_limit_next(
            channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.7, global_=True
        )
        with self.assertRaises(ServerRateLimit) as caught:
            await client.send_message(SYNTHETIC_GUILD_CHANNEL, "blocked")
        self.assertEqual(caught.exception.retry_after, 0.7)
        self.assertTrue(caught.exception.global_)
        # one-shot: the same send now goes through
        sent = await client.send_message(SYNTHETIC_GUILD_CHANNEL, "through")
        self.assertEqual(sent.content, "through")
        self.assertEqual(
            world.messages_of(SYNTHETIC_GUILD_CHANNEL)[-1], "through"
        )

    async def test_send_to_vanished_channel_raises_target_gone(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        client.delete_channel(SYNTHETIC_GUILD_CHANNEL)
        with self.assertRaises(TargetGone) as caught:
            await client.send_message(SYNTHETIC_GUILD_CHANNEL, "ghost")
        self.assertIn("vanished", str(caught.exception.reason))
        # DM path: a deleted DM channel is equally gone (and the deliberate
        # resurrection divergence is logged loudly by the world)
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        with self.assertLogs("slaughtermudae.simulator", level="WARNING"):
            world.channel_gone(dm)
        with self.assertRaises(TargetGone):
            await client.send_dm(SYNTHETIC_MUDAE_ID, "ghost-dm")

    async def test_vanished_dm_channel_does_not_consume_one_shot_limit(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        world.server_limit_next(channel_id=dm, retry_after=0.4)
        with self.assertLogs("slaughtermudae.simulator", level="WARNING"):
            world.channel_gone(dm)
        with self.assertRaises(TargetGone):
            await client.send_dm(SYNTHETIC_MUDAE_ID, "ghost-dm")
        self.assertEqual(world.blocked_sends, [])  # 429 not spent on a dead send
        world.add_channel(dm, "dm-restored", dm=True)
        with self.assertRaises(ServerRateLimit):  # still armed for a live send
            await client.send_message(dm, "later")

    async def test_armed_limit_with_live_channel_fires_on_send_dm(self) -> None:
        """Reverse of the ordering guard: channel exists, 429 armed on it ->
        ServerRateLimit fires (channel-first must not starve the 429)."""
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        self.assertTrue(world.channel_exists(dm))  # channel fine...
        world.server_limit_next(channel_id=dm, retry_after=0.6, global_=False)
        with self.assertRaises(ServerRateLimit) as caught:  # ...429 fires
            await client.send_dm(SYNTHETIC_MUDAE_ID, "blocked-dm")
        self.assertEqual(caught.exception.retry_after, 0.6)
        self.assertFalse(caught.exception.global_)
        self.assertEqual(len(world.blocked_sends), 1)  # recorded with evidence
        self.assertEqual(world.blocked_sends[0]["channel_id"], dm)
        self.assertEqual(world.messages_of(dm), [])  # nothing leaked through
        sent = await client.send_dm(SYNTHETIC_MUDAE_ID, "after")  # one-shot
        self.assertEqual(sent.content, "after")

    async def test_armed_limit_with_live_channel_fires_on_send_message(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        world.server_limit_next(
            channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.5
        )
        with self.assertRaises(ServerRateLimit):
            await client.send_message(SYNTHETIC_GUILD_CHANNEL, "blocked")
        self.assertEqual(len(world.blocked_sends), 1)
        await client.send_message(SYNTHETIC_GUILD_CHANNEL, "through")

    async def test_send_message_success_shape(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        sent = await client.send_message(SYNTHETIC_GUILD_CHANNEL, "payload")
        self.assertIsInstance(sent, DiscordMessage)
        self.assertEqual(sent.channel_id, SYNTHETIC_GUILD_CHANNEL)
        self.assertEqual(sent.author_id, SYNTHETIC_ALPHA_ID)
        self.assertEqual(sent.content, "payload")
        self.assertFalse(sent.dm)

    async def test_send_dm_success_shape(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        sent = await client.send_dm(SYNTHETIC_MUDAE_ID, "psst")
        self.assertTrue(sent.dm)
        expected_channel = world.dm_channel_for(
            SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID
        )
        self.assertEqual(sent.channel_id, expected_channel)
        self.assertIn("psst", world.messages_of(expected_channel))


class TestDmAndUsers(unittest.IsolatedAsyncioTestCase):
    """open_dm symmetry and fetch_user lookups."""

    async def test_open_dm_symmetric_between_clients(self) -> None:
        world = _world()
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        mudae = await _connected(world, SYNTHETIC_MUDAE_ID)
        from_alpha = await alpha.open_dm(SYNTHETIC_MUDAE_ID)
        from_mudae = await mudae.open_dm(SYNTHETIC_ALPHA_ID)
        self.assertEqual(from_alpha, from_mudae)

    async def test_fetch_user_known_and_unknown(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        user = await client.fetch_user(SYNTHETIC_OWNER_ID)
        self.assertEqual(user.id, SYNTHETIC_OWNER_ID)
        self.assertEqual(user.name, "owner")
        self.assertIsNone(await client.fetch_user(123456789))


class TestSnapshotAndLatency(unittest.IsolatedAsyncioTestCase):
    """cached_snapshot (DM filtering!) and the synthetic latency."""

    async def test_snapshot_unavailable_before_connect(self) -> None:
        world = _world()
        client = SimClient(world, SYNTHETIC_ALPHA_ID)
        self.assertEqual(client.cached_snapshot(), {"available": False})

    async def test_snapshot_filters_dms_to_this_account(self) -> None:
        world = _world()
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        await _connected(world, SYNTHETIC_BETA_ID)  # the other account
        alpha_dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        beta_dm = world.dm_channel_for(SYNTHETIC_BETA_ID, SYNTHETIC_MUDAE_ID)

        snapshot = alpha.cached_snapshot()
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["user"]["id"], SYNTHETIC_ALPHA_ID)
        dm_ids = [entry["id"] for entry in snapshot["dm_channels"]]
        self.assertIn(alpha_dm, dm_ids)      # own DM visible...
        self.assertNotIn(beta_dm, dm_ids)    # ...foreign DM filtered out
        guild_channel_ids = [
            channel["id"]
            for guild in snapshot["guilds"]
            for channel in guild["channels"]
        ]
        self.assertIn(SYNTHETIC_GUILD_CHANNEL, guild_channel_ids)
        self.assertNotIn(alpha_dm, guild_channel_ids)
        recipient = next(
            entry["recipient"]
            for entry in snapshot["dm_channels"]
            if entry["id"] == alpha_dm
        )
        self.assertEqual(recipient, SYNTHETIC_MUDAE_ID)

    async def test_snapshot_stops_listing_deleted_channel(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        self.assertTrue(client.delete_channel(SYNTHETIC_GUILD_CHANNEL))
        snapshot = client.cached_snapshot()
        listed = [
            channel["id"]
            for guild in snapshot["guilds"]
            for channel in guild["channels"]
        ]
        self.assertNotIn(SYNTHETIC_GUILD_CHANNEL, listed)

    async def test_latency_deterministic_and_in_band(self) -> None:
        world = _world()
        pinned = SimClient(world, SYNTHETIC_ALPHA_ID, seed=42)
        first, second = pinned.latency_ms, pinned.latency_ms
        self.assertTrue(
            LATENCY_MIN_MS <= first <= LATENCY_MAX_MS, first
        )
        self.assertTrue(
            LATENCY_MIN_MS <= second <= LATENCY_MAX_MS, second
        )
        replay = SimClient(world, SYNTHETIC_ALPHA_ID, seed=42)
        self.assertEqual(replay.latency_ms, first)  # same seed, same sequence
        snapshot = (await _connected(world, SYNTHETIC_ALPHA_ID)).cached_snapshot()
        self.assertTrue(
            LATENCY_MIN_MS <= snapshot["latency_ms"] <= LATENCY_MAX_MS
        )

    async def test_latency_band_constants_sane(self) -> None:
        self.assertLess(client_module.LATENCY_MIN_MS, client_module.LATENCY_MAX_MS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
