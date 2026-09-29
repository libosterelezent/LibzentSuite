"""End-to-end simulations over the in-memory world (no network, no real tokens)."""
from __future__ import annotations

import asyncio
import unittest

from core.simulator import SYNTHETIC_BETA_ID, SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, SimulatedCluster

from .helpers import ROOT

MUDAE_ID = 432610292342587392  # comes from env in production; fixed here for the sim
PREFIX = "$soulcopy "


def make_cluster() -> SimulatedCluster:
    return SimulatedCluster(
        mudae_bot_id=MUDAE_ID,
        plugin_dir=str(ROOT / "plugins"),
        settings_overrides={
            "soulcopy": {
                "dm_read_timeout": 0.5,
                "inter_chunk_delay": 0.01,
                "max_chunk_length": 60,
            }
        },
    )


class TestSoulcopySimulation(unittest.IsolatedAsyncioTestCase):
    async def test_full_soulcopy_flow(self) -> None:
        cluster = make_cluster()
        await cluster.start()
        try:
            transcript = await cluster.run_soulcopy_transcript()
        finally:
            await cluster.stop()
        # owner trigger, export command, then soulcopy chunks
        self.assertEqual(transcript[0], "slcopy")
        self.assertEqual(transcript[1], "$slud-s")
        chunks = transcript[2:]
        self.assertGreaterEqual(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith(PREFIX), chunk)
            self.assertLessEqual(len(chunk), 60, chunk)
        rejoined = "$".join(chunk[len(PREFIX):] for chunk in chunks)
        self.assertEqual(rejoined, "Alice$Bob$Charlie$Dana$Eve$Finn")
        # Mudae's DM pages reached the account's DM channel
        dm_id = cluster.world.dm_channel_for(2001, MUDAE_ID)
        self.assertEqual(len(cluster.world.messages_of(dm_id)), 2)

    async def test_non_owner_cannot_trigger(self) -> None:
        cluster = make_cluster()
        await cluster.start()
        try:
            await cluster.owner_says("slcopy", author_id=4242)  # not in OWNER_USER_IDS
            await cluster.world.wait_for_quiet(SYNTHETIC_GUILD_CHANNEL, quiet=0.4, limit=2.0)
            self.assertEqual(cluster.world.messages_of(SYNTHETIC_GUILD_CHANNEL), ["slcopy"])
        finally:
            await cluster.stop()

    async def test_idle_account_stays_silent(self) -> None:
        """beta runs no plugins -> sees the traffic but never acts on it."""
        cluster = make_cluster()
        await cluster.start()
        try:
            await cluster.owner_says("slcopy")
            await cluster.world.wait_for_quiet(SYNTHETIC_GUILD_CHANNEL, quiet=0.5, limit=8.0)
            transcript = cluster.world.messages_of(SYNTHETIC_GUILD_CHANNEL)
            self.assertEqual(transcript.count("$slud-s"), 1)  # only alpha acted
            self.assertNotIn("slcopy-beta", "".join(transcript))
        finally:
            await cluster.stop()

    async def test_sync_group_broadcast(self) -> None:
        cluster = make_cluster()
        await cluster.start()
        try:
            results = await cluster.container.broadcast(
                "grinders",
                lambda session: session.send_message(
                    SYNTHETIC_GUILD_CHANNEL, f"hello from {session.name}"
                ),
            )
            self.assertEqual(sorted(results), ["alpha", "beta"])
            self.assertEqual(
                cluster.world.messages_of(SYNTHETIC_GUILD_CHANNEL),
                ["hello from alpha", "hello from beta"],
            )
        finally:
            await cluster.stop()


if __name__ == "__main__":
    unittest.main()
