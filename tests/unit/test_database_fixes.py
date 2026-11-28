"""Regression tests for the audited database-layer fixes.

One test per audited defect:
(a) account-delete kv sweep escapes LIKE wildcards — an account named
    'alfa_bot' must not sweep 'alfaXbot' rows ('_' is any-single-char
    in an unescaped LIKE),
(b) account-delete cascade covers account_plugins (a re-created
    same-named account must not inherit the dead account's grants),
(c) the telemetry ring keeps sliding past maxlen (tail forensics),
(d) two Database objects first-booting one fresh file in parallel both
    succeed (ALTER migration 'duplicate column name' race), plus a
    deterministic re-apply of the ALTER migration,
(e) log messages are capped at 2000 chars.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path

from libzentsuite.database.db import Database
from libzentsuite.database.schema import MIGRATIONS, latest_version
from libzentsuite.database.telemetry import DbTelemetry


class AsyncDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """One connected Database per test; closed even when the test fails."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-dbfix-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"

    async def asyncSetUp(self) -> None:
        self.db = Database(self.path)
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)


class TestAccountDeleteKvSweepEscapesWildcards(AsyncDatabaseTestCase):
    """(a) the kv backstop sweep must respect LIKE metacharacters."""

    async def test_underscore_account_delete_spares_wildcard_neighbour(self) -> None:
        await self.db.accounts.add("alfa_bot")
        await self.db.accounts.add("alfaXbot")
        await self.db.kv.set("pstate:account:alfa_bot:exporter:k", "1")
        await self.db.kv.set("pstate:account_plugin:alfa_bot/exporter:k", "2")
        await self.db.kv.set("pstate:account:alfaXbot:exporter:k", "3")
        await self.db.kv.set("pstate:account_plugin:alfaXbot/exporter:k", "4")

        self.assertTrue(await self.db.accounts.delete("alfa_bot"))

        # the deleted account's own plugin state is gone (both prefixes)…
        self.assertIsNone(await self.db.kv.get("pstate:account:alfa_bot:exporter:k"))
        self.assertIsNone(await self.db.kv.get("pstate:account_plugin:alfa_bot/exporter:k"))
        # …while alfaXbot — one unescaped-'_' match away — keeps everything
        self.assertEqual(await self.db.kv.get("pstate:account:alfaXbot:exporter:k"), "3")
        self.assertEqual(await self.db.kv.get("pstate:account_plugin:alfaXbot/exporter:k"), "4")


class TestAccountDeleteCascadesAccountPlugins(AsyncDatabaseTestCase):
    """(b) orphaned account_plugins rows would leak grants to a re-created
    same-named account — the delete cascade must remove them."""

    async def test_delete_removes_plugin_assignments(self) -> None:
        await self.db.accounts.add("alpha")
        await self.db.account_plugins.assign_plugin("alpha", "exporter")
        await self.db.account_plugins.assign_plugin("alpha", "roll")
        self.assertEqual(await self.db.account_plugins.plugins_of("alpha"), ["exporter", "roll"])

        self.assertTrue(await self.db.accounts.delete("alpha"))

        self.assertEqual(await self.db.account_plugins.plugins_of("alpha"), [])
        self.assertEqual(await self.db.account_plugins.all_assignments(), {})

        # and a re-created same-named account starts from a clean slate
        await self.db.accounts.add("alpha")
        self.assertEqual(await self.db.account_plugins.plugins_of("alpha"), [])


class TestTelemetryRingEviction(unittest.IsolatedAsyncioTestCase):
    """(c) deque(maxlen) must auto-evict — the old len<maxlen guard froze
    the ring at the FIRST 1024 records."""

    async def test_ring_keeps_appending_past_maxlen(self) -> None:
        tele = DbTelemetry()
        tele.enabled = True  # bypass LZ_DB_TELEMETRY; deterministic
        capacity = tele._ring.maxlen
        assert capacity is not None  # the telemetry ring is always bounded
        total = capacity + 200
        for i in range(total):
            tele.record("read", (i + 1) * 0.001)  # ms == i + 1

        snap = tele.snapshot()
        self.assertEqual(snap["requests"]["total"], total)
        self.assertEqual(snap["requests"]["by_kind"]["read"], total)
        self.assertEqual(snap["requests"]["errors"], 0)

        ring = tele._ring
        capacity = ring.maxlen
        assert capacity is not None  # the telemetry ring is always bounded
        self.assertEqual(len(ring), capacity)  # full, not frozen short
        # holding the NEWEST records proves eviction ran: the first entry
        # is record #(total - maxlen + 1), the last is record #total —
        # the frozen-ring bug would show 1 and maxlen instead
        self.assertEqual(ring[0][1], total - capacity + 1)
        self.assertEqual(ring[-1][1], total)
        self.assertEqual(snap["read_ms"]["max"], float(total))


class TestParallelFirstBoot(AsyncDatabaseTestCase):
    """(d) two Database objects racing connect() on a fresh file — the
    documented parallel-first-boot contract (guarded stamps, no-ops DDL,
    tolerated ALTER losers)."""

    async def test_two_databases_race_connect_on_fresh_file(self) -> None:
        path = Path(self.tmp.name) / "race.db"
        db1 = Database(path)
        db2 = Database(path)
        results = await asyncio.gather(db1.connect(), db2.connect(), return_exceptions=True)
        try:
            for r in results:
                if isinstance(r, BaseException):
                    raise AssertionError(f"parallel first boot failed: {r!r}")
                self.assertIsInstance(r, int)
            # consistent bookkeeping: every version stamped exactly once
            rows = await db1.connection.fetch_all("SELECT version FROM schema_migrations")
            versions = sorted(row["version"] for row in rows)
            self.assertEqual(versions, list(range(1, latest_version() + 1)))
            dupes = await db1.connection.fetch_all(
                "SELECT version FROM schema_migrations GROUP BY version HAVING COUNT(*) > 1"
            )
            self.assertEqual(dupes, [])
        finally:
            await db1.close()
            await db2.close()

    async def test_duplicate_column_race_tolerated_and_not_double_stamped(self) -> None:
        """Deterministic pin of the loser's path: re-applying the ALTER
        migration over an already-migrated file hits exactly the parallel
        winner's 'duplicate column name' — tolerated, never double-stamped."""
        alter = [(v, n, s) for v, n, s in MIGRATIONS if "ALTER TABLE" in s.upper()]
        self.assertTrue(
            alter, "race guard is only exercised while an ALTER-carrying migration exists"
        )
        version, name, sql = alter[-1]
        # the loser path pinned directly: applying an already-applied
        # ALTER migration through the guarded applier hits the winner's
        # error, tolerates it, and never double-stamps
        await self.db._apply_guarded(version, name, sql)
        rows = await self.db.connection.fetch_all(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = ?", (version,)
        )
        self.assertLessEqual(rows[0]["n"], 1)  # never double-stamped


class TestLogMessageCap(AsyncDatabaseTestCase):
    """(e) log messages are capped at 2000 chars (audit content: 500)."""

    async def test_message_capped_at_2000(self) -> None:
        await self.db.logs.add({"severity": "error", "source": "t", "message": "x" * 5000})
        rows = await self.db.logs.recent(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["message"]), 2000)
        self.assertTrue(rows[0]["message"].startswith("x"))

    async def test_message_boundaries_untouched(self) -> None:
        await self.db.logs.add({"message": "y" * 2000})  # exactly at the cap
        await self.db.logs.add({"message": "short one"})
        rows = await self.db.logs.recent(10)
        self.assertEqual(rows[-1]["message"], "short one")  # round-trips verbatim
        self.assertEqual(len(rows[-2]["message"]), 2000)


if __name__ == "__main__":
    unittest.main()


class TestCancelDuringBeginDoesNotWedgeConnection(AsyncDatabaseTestCase):
    async def test_cancel_during_begin_then_write_still_works(self) -> None:
        async def writer() -> None:
            async with self.db.connection.write():
                await asyncio.sleep(10)

        for ticks in (0, 1, 2, 3, 5):
            task = asyncio.ensure_future(writer())
            for _ in range(ticks):
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.05)
            await self.db.kv.set(f"k{ticks}", ticks)  # used to fail: tx stuck
            self.assertEqual(await self.db.kv.get(f"k{ticks}"), ticks)


class TestTelemetryPercentiles(unittest.TestCase):
    def test_percentiles_stay_within_observed_range_and_json_safe(self) -> None:
        import json

        t = DbTelemetry()
        t.enabled = True
        for _ in range(10):
            t.record("read", 0.00005)  # 0.05 ms
        snap = t.snapshot()
        self.assertLessEqual(snap["read_ms"]["p50"], 0.1)
        t.record("commit", 0.5)  # 500 ms: overflow bucket
        snap = t.snapshot()
        self.assertNotEqual(snap["commit_ms"]["p95"], float("inf"))
        json.dumps(snap, allow_nan=False)


class TestMigrationToleratedAlterKeepsOtherStatements(AsyncDatabaseTestCase):
    async def test_existing_column_does_not_lose_profile_table(self) -> None:
        await self.db.close()
        import sqlite3

        raw = sqlite3.connect(self.path)
        raw.execute("DROP TABLE IF EXISTS plugin_profiles")
        raw.execute("DELETE FROM schema_migrations WHERE version >= 7")
        raw.commit()
        cols = [r[1] for r in raw.execute("PRAGMA table_info(account_plugins)")]
        self.assertIn("profile", cols)  # column survives the rollback
        # v10's fold reads sync_groups — recreate its shape (v11 dropped it)
        raw.execute("""
            CREATE TABLE IF NOT EXISTS sync_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)
        """)
        # v11 also dropped accounts.sync_group — v10's backfill reads it
        account_cols = [r[1] for r in raw.execute("PRAGMA table_info(accounts)")]
        if "sync_group" not in account_cols:
            raw.execute("ALTER TABLE accounts ADD COLUMN sync_group TEXT")
        raw.close()
        db2 = Database(self.path)
        await db2.connect()
        self.addAsyncCleanup(db2.close)
        await db2.plugin_profiles.create("roll", "fast", "d", {})


class TestStoredNullSettingSurvivesRead(AsyncDatabaseTestCase):
    async def test_json_null_override_is_not_dropped(self) -> None:
        await self.db.plugins.set_setting("alpha", "roll", "limit", None)
        self.assertEqual(await self.db.plugins.settings("alpha", "roll"), {"limit": None})
        self.assertEqual(
            (await self.db.plugins.all_account_settings())["alpha"]["roll"], {"limit": None}
        )
