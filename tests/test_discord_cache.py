"""DiscordCache: real-time cache-first validation + invalidation contract."""
from __future__ import annotations

import unittest

from core.discord_cache import CacheManager, DiscordCache
from core.errors import TargetGone


def sample_snapshot() -> dict:
    return {
        "available": True,
        "user": {"id": 2001, "name": "alpha"},
        "guilds": [{"id": 7, "name": "Guild", "channels": [{"id": 500, "name": "grind"}]}],
        "dm_channels": [{"id": 9001, "recipient": 432610292342587392}],
    }


class TestDiscordCache(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = DiscordCache("alpha")
        self.cache.ingest_snapshot(sample_snapshot())

    def test_ingest_populates_and_ready(self) -> None:
        self.assertTrue(self.cache.ready)
        self.assertEqual(self.cache.user_id, 2001)
        self.assertEqual(self.cache.channels[500].name, "grind")
        self.assertEqual(self.cache.channels[9001].recipient, 432610292342587392)

    def test_validate_known_channel(self) -> None:
        self.assertEqual(self.cache.validate_channel(500).channel_id, 500)

    def test_validate_unknown_raises_target_gone(self) -> None:
        with self.assertRaises(TargetGone) as ctx:
            self.cache.validate_channel(999999)
        self.assertIn("not in the account's cache", ctx.exception.reason)

    def test_invalidate_marks_and_notifies(self) -> None:
        seen: list[tuple] = []
        unsubscribe = self.cache.on_invalidate(lambda acct, cid, reason: seen.append((acct, cid, reason)))
        self.assertTrue(self.cache.invalidate_channel(500, "deleted by admin"))
        with self.assertRaises(TargetGone):
            self.cache.validate_channel(500)
        self.assertEqual(seen, [("alpha", 500, "deleted by admin")])
        unsubscribe()
        self.assertTrue(self.cache.invalidate_channel(500, "again"))  # listener gone: no raise
        self.assertEqual(seen.__len__(), 1)

    def test_invalidate_unknown_channel_is_noop(self) -> None:
        self.assertFalse(self.cache.invalidate_channel(12345, "x"))

    def test_snapshot_shape(self) -> None:
        snap = self.cache.snapshot()
        self.assertTrue(snap["ready"])
        self.assertEqual(snap["channels_total"], 2)
        self.assertEqual(snap["guilds"], [{"id": 7, "name": "Guild", "channels": 1}])

    def test_sparse_snapshot_ignored(self) -> None:
        cache = DiscordCache("beta")
        cache.ingest_snapshot({"available": False})
        self.assertFalse(cache.ready)


class TestCacheManager(unittest.TestCase):
    def test_per_account_caches_are_isolated(self) -> None:
        manager = CacheManager()
        first = manager.cache_for("alpha")
        second = manager.cache_for("beta")
        first.ingest_snapshot(sample_snapshot())
        # isolation: beta's cache never saw alpha's channels
        with self.assertRaises(TargetGone):
            second.validate_channel(500)
        self.assertIs(manager.cache_for("alpha"), first)

    def test_snapshot_per_account(self) -> None:
        manager = CacheManager()
        manager.cache_for("alpha").ingest_snapshot(sample_snapshot())
        snap = manager.snapshot()
        self.assertIn("alpha", snap)
        self.assertTrue(snap["alpha"]["ready"])


if __name__ == "__main__":
    unittest.main()
