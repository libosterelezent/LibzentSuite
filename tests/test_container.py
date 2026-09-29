"""Container: lifecycle, event routing, plugin isolation, sync groups."""
from __future__ import annotations

import asyncio
import unittest

from core.account import AccountConfig, AccountStatus
from core.container import ContainerError
from core.rate_limiter import Priority

from .helpers import TEST_CHANNEL, Harness


class TestContainerLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = Harness("tests/fixtures/plugins", ["alpha", "beta"])
        # fixture state must be read through the loader-loaded module (single copy)
        self.spy = self.harness.fixture_module("spy")
        self.spy.reset()

    async def test_add_account_resolves_settings(self) -> None:
        runtime = self.harness.container._accounts["alpha"]
        self.assertEqual(
            runtime.plugins["spy"].settings,
            {"greeting": "hi", "volume": 5, "loud": False},
        )

    async def test_start_stop_status_transitions(self) -> None:
        container = self.harness.container
        self.assertEqual(container.status()[0]["status"], "created")
        await container.start_account("alpha")
        self.assertEqual(container.status()[0]["status"], "running")
        await container.stop_account("alpha")
        self.assertEqual(container.status()[0]["status"], "stopped")

    async def test_duplicate_account_rejected(self) -> None:
        with self.assertRaises(ContainerError):
            self.harness.container.add_account(AccountConfig(name="alpha"))

    async def test_unknown_account_rejected(self) -> None:
        with self.assertRaises(ContainerError):
            await self.harness.container.start_account("ghost")

    async def test_lifecycle_hook_order(self) -> None:
        await self.harness.container.start_account("alpha")
        await self.harness.container.stop_account("alpha")
        self.assertEqual(self.spy.CALLS, ["load:alpha", "enable:alpha:hi", "disable:alpha"])


class TestEventRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = Harness("tests/fixtures/plugins", ["alpha"])
        self.spy = self.harness.fixture_module("spy")
        self.spy.reset()

    async def test_events_reach_enabled_plugin(self) -> None:
        await self.harness.container.start_account("alpha")
        self.harness.world.deliver(TEST_CHANNEL, self.harness.owner_id, "hello world")
        await asyncio.sleep(0.05)
        self.assertEqual(self.spy.EVENTS, [("alpha", self.harness.owner_id, "hello world")])

    async def test_disabled_plugin_skipped(self) -> None:
        container = self.harness.container
        await container.start_account("alpha")
        await container.enable_plugin("alpha", "spy", enabled=False)
        self.harness.world.deliver(TEST_CHANNEL, self.harness.owner_id, "ignored")
        await asyncio.sleep(0.05)
        self.assertEqual(self.spy.EVENTS, [])
        self.assertIn("disable:alpha", self.spy.CALLS)

    async def test_crashing_plugin_is_isolated(self) -> None:
        container = self.harness.container
        await container.start_account("alpha")
        plugin = container._accounts["alpha"].plugins["spy"].instance
        original = plugin.handle_event

        async def explosive(ctx, event):
            raise RuntimeError("plugin exploded")

        plugin.handle_event = explosive
        self.harness.world.deliver(TEST_CHANNEL, self.harness.owner_id, "boom")
        await asyncio.sleep(0.05)
        plugin.handle_event = original
        # container survives and a later dispatch still runs
        self.harness.world.deliver(TEST_CHANNEL, self.harness.owner_id, "still-alive")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.spy.EVENTS), 1)
        self.assertEqual(self.spy.EVENTS[0][2], "still-alive")

    async def test_collect_messages_idle_timeout(self) -> None:
        container = self.harness.container
        await container.start_account("alpha")

        async def deliver_later() -> None:
            await asyncio.sleep(0.03)
            self.harness.world.deliver(TEST_CHANNEL, self.harness.mudae_id, "page-1")
            await asyncio.sleep(0.03)
            self.harness.world.deliver(TEST_CHANNEL, self.harness.mudae_id, "page-2")

        task = asyncio.create_task(
            container.collect_messages(
                "alpha",
                check=lambda e: e.author_id == self.harness.mudae_id,
                timeout=0.2,
            )
        )
        await deliver_later()
        collected = await task
        self.assertEqual([e.content for e in collected], ["page-1", "page-2"])

    async def test_wait_for_returns_none_on_timeout(self) -> None:
        container = self.harness.container
        await container.start_account("alpha")
        nothing = await container.wait_for("alpha", lambda e: False, timeout=0.05)
        self.assertIsNone(nothing)


class TestSyncGroups(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = Harness("tests/fixtures/plugins", ["alpha", "beta"])
        self.spy = self.harness.fixture_module("spy")
        self.spy.reset()

    async def test_sync_groups_mapping(self) -> None:
        self.assertEqual(self.harness.container.sync_groups(), {"grp": ["alpha", "beta"]})

    async def test_broadcast_runs_on_all_members(self) -> None:
        await self.harness.container.start_all()
        results = await self.harness.container.broadcast(
            "grp",
            lambda session: session.send_message(TEST_CHANNEL, f"sync-ping-{session.name}"),
        )
        self.assertEqual(sorted(results), ["alpha", "beta"])
        self.assertEqual(
            self.harness.world.messages_of(TEST_CHANNEL),
            ["sync-ping-alpha", "sync-ping-beta"],
        )

    async def test_broadcast_skips_stopped_members(self) -> None:
        await self.harness.container.start_account("alpha")
        results = await self.harness.container.broadcast(
            "grp", lambda session: session.send_message(TEST_CHANNEL, "x")
        )
        self.assertEqual(sorted(results), ["alpha"])

    async def test_sends_go_through_rate_limiter(self) -> None:
        await self.harness.container.start_account("alpha")
        session = self.harness.session("alpha")
        await session.send_message(TEST_CHANNEL, "limited", priority=Priority.HIGH)
        # both the shared global bucket and the alpha bucket lost a token
        stats = self.harness.limiter.stats()
        message_stats = stats["actions"]["message"]
        self.assertLess(message_stats["global_bucket"]["tokens"], message_stats["global_bucket"]["capacity"])
        self.assertIn("alpha", message_stats["account_buckets"])


if __name__ == "__main__":
    unittest.main()
