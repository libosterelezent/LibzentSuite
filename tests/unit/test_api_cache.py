"""Persistent API-cache tests: entity caching per account (anti-flag trim).

Covers the repository round-trip, the three-layer DM resolution order
(memory -> database -> ONE client call), invalidation on TargetGone, and
the DB-cached services.fetch_user. The database layer is async (aiosqlite):
every test awaits connect()/close() and the repository calls.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.account_manager.cache import (
    DiscordCache,
    invalidate_dm_channel,
    resolve_dm_channel,
)
from core.account_manager.services import AccountPluginServices
from core.credentials import Credentials
from core.event_bus import EventBus
from database.db import Database


class FakeClient:
    """Counts REST-touching calls (open_dm / fetch_user)."""

    def __init__(self) -> None:
        self.open_dm_calls: list = []
        self.fetch_user_calls: list = []

    async def open_dm(self, user_id):
        self.open_dm_calls.append(user_id)
        return 1_000_000 + user_id

    async def fetch_user(self, user_id):
        self.fetch_user_calls.append(user_id)
        return SimpleNamespace(id=user_id, name=f"user{user_id}")


class AsyncDbTestCase(unittest.IsolatedAsyncioTestCase):
    """One connected :memory: database per async test."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()


class TestCacheRepository(AsyncDbTestCase):
    async def test_roundtrip_and_scoping(self) -> None:
        await self.db.cache.set("alpha", "dm", "42", 999)
        self.assertEqual(await self.db.cache.get("alpha", "dm", "42"), 999)
        # rows are per-account: beta never sees alpha's resolution
        self.assertIsNone(await self.db.cache.get("beta", "dm", "42"))
        self.assertTrue(await self.db.cache.delete("alpha", "dm", "42"))
        self.assertIsNone(await self.db.cache.get("alpha", "dm", "42"))

    async def test_corrupt_row_tolerated(self) -> None:
        async with self.db.connection.write() as conn:
            await conn.execute(
                "INSERT INTO api_cache VALUES ('a','dm','1','{broken',0)")
        self.assertIsNone(await self.db.cache.get("a", "dm", "1"))

    async def test_counts_and_drop(self) -> None:
        await self.db.cache.set("a", "dm", "1", 1)
        await self.db.cache.set("a", "user", "2", {"id": 2})
        await self.db.cache.set("b", "dm", "1", 2)
        self.assertEqual(await self.db.cache.counts(), {"a": 2, "b": 1})
        self.assertEqual(await self.db.cache.drop_account("a", "dm"), 1)
        self.assertEqual(await self.db.cache.counts(), {"a": 1, "b": 1})


class TestDmResolutionLayers(AsyncDbTestCase):
    async def test_memory_then_db_then_one_client_call(self) -> None:
        cache = DiscordCache("alpha")
        client = FakeClient()
        # 1st resolution: miss everywhere -> ONE client call, persisted
        cid = await resolve_dm_channel(cache, client, 77, "alpha", self.db.cache)
        self.assertEqual(client.open_dm_calls, [77])
        self.assertEqual(await self.db.cache.get("alpha", "dm", "77"), cid)

        # 2nd resolution: served from memory, no new API call
        await resolve_dm_channel(cache, None, 77, "alpha", self.db.cache)
        self.assertEqual(len(client.open_dm_calls), 1)

        # 3rd: fresh memory (simulated restart) -> served from the DB
        fresh_cache = DiscordCache("alpha")
        cid2 = await resolve_dm_channel(
            fresh_cache, client, 77, "alpha", self.db.cache)
        self.assertEqual(cid2, cid)
        self.assertEqual(len(client.open_dm_calls), 1)  # STILL one call

        # invalidation -> next resolution re-opens exactly once
        await invalidate_dm_channel(fresh_cache, self.db.cache, 77, "alpha")
        self.assertIsNone(await self.db.cache.get("alpha", "dm", "77"))
        await resolve_dm_channel(fresh_cache, client, 77, "alpha", self.db.cache)
        self.assertEqual(len(client.open_dm_calls), 2)


class TestServicesFetchUserCached(AsyncDbTestCase):
    async def test_fetch_user_cached_per_account(self) -> None:
        client = FakeClient()
        services = AccountPluginServices(
            "alpha", "soulcopy", bus=EventBus(), scheduler=None,
            credentials=Credentials.from_values({}),
            cache=DiscordCache("alpha"), client_getter=lambda: client,
            api_cache_repo=self.db.cache,
        )
        user = await services.fetch_user(999)
        self.assertEqual(user.id, 999)
        self.assertEqual(client.fetch_user_calls, [999])
        # second call: served from the DB, no new fetch
        user2 = await services.fetch_user(999)
        self.assertEqual(user2.id, 999)
        self.assertEqual(user2.name, "user999")
        self.assertEqual(len(client.fetch_user_calls), 1)
        # rows are account-scoped
        self.assertIsNone(await self.db.cache.get("beta", "user", "999"))


if __name__ == "__main__":
    unittest.main()
