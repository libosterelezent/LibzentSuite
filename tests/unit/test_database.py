"""Database layer tests — ASYNC aiosqlite edition (agent D deliverable).

Covers: migration idempotence (file + :memory:), every repository
round-trip, cascade deletes (incl. api_cache), pruning, filters,
transaction rollback visibility, constraint errors wrapped as
DatabaseError, and 20 concurrent writers on the single connection
(no SQLITE_BUSY, event loop stays responsive).
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from database.connection import DatabaseError as ConnectionDatabaseError
from core.errors import DatabaseError as CoreDatabaseError
from database.db import Database
from database.models import AccountRecord, ProfileRecord, RatePolicyRecord

#: both flavors are RuntimeError subclasses raised by different layers
#: (repositories import core.errors, base wraps into connection) — accept either
ANY_DB_ERROR = (ConnectionDatabaseError, CoreDatabaseError)


class AsyncDatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    """One connected Database per test; closed even when the test fails."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-db-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"

    async def asyncSetUp(self) -> None:
        self.db = Database(self.path)
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)

    def make(self, **kwargs) -> Database:
        """A SECOND Database over the same file (reopen scenarios)."""
        return Database(self.path, **kwargs)


class TestMigrations(AsyncDatabaseTestCase):
    async def test_migrate_then_reopen_is_noop(self) -> None:
        # connect() already applied everything; an explicit second call no-ops
        self.assertEqual(await self.db.migrate(), 0)
        db2 = self.make()
        self.assertEqual(await db2.connect(), 0)  # reopen same file
        await db2.close()

    async def test_fresh_file_applies_all_migrations(self) -> None:
        db = Database(Path(self.tmp.name) / "fresh.db")
        applied = await db.connect()
        self.assertGreaterEqual(applied, 1)
        await db.close()

    async def test_memory_mode(self) -> None:
        db = Database(":memory:")
        await db.connect()
        try:
            self.assertEqual(db.path, ":memory:")
            await db.accounts.add("alpha")
            self.assertEqual((await db.accounts.get("alpha")).name, "alpha")
        finally:
            await db.close()

    async def test_connect_required_before_use(self) -> None:
        from database.connection import DatabaseError

        db = Database(Path(self.tmp.name) / "unconnected.db")
        with self.assertRaises(DatabaseError):
            await db.kv.set("x", "1")


class TestAccounts(AsyncDatabaseTestCase):
    async def test_add_get_list_roundtrip(self) -> None:
        await self.db.accounts.add("alpha", token_ref="DISCORD_TOKEN_ALPHA", sync_group="g1")
        await self.db.accounts.add("beta")
        names = [r.name for r in await self.db.accounts.list()]
        self.assertEqual(names, ["alpha", "beta"])
        rec = await self.db.accounts.get("alpha")
        self.assertIsInstance(rec, AccountRecord)
        self.assertEqual(rec.token_ref, "DISCORD_TOKEN_ALPHA")
        self.assertEqual(rec.sync_group, "g1")
        self.assertTrue(rec.enabled)
        self.assertIsNone(await self.db.accounts.get("nope"))

    async def test_duplicate_name_raises(self) -> None:
        await self.db.accounts.add("alpha")
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.accounts.add("alpha")

    async def test_empty_name_rejected(self) -> None:
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.accounts.add("   ")

    async def test_enable_and_settings(self) -> None:
        await self.db.accounts.add("alpha")
        await self.db.accounts.set_enabled("alpha", False)
        self.assertFalse((await self.db.accounts.get("alpha")).enabled)
        await self.db.accounts.set_settings("alpha", {"theme": "dark"})
        self.assertEqual((await self.db.accounts.get("alpha")).settings, {"theme": "dark"})
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.accounts.set_enabled("ghost", True)

    async def test_profiles_assign_unassign(self) -> None:
        await self.db.accounts.add("alpha")
        await self.db.accounts.assign_profile("alpha", "roller")
        await self.db.accounts.assign_profile("alpha", "sniper")
        self.assertEqual(await self.db.accounts.profiles_of("alpha"), ["roller", "sniper"])
        await self.db.accounts.unassign_profile("alpha", "roller")
        self.assertEqual(await self.db.accounts.profiles_of("alpha"), ["sniper"])
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.accounts.assign_profile("ghost", "roller")

    async def test_delete_cascades(self) -> None:
        await self.db.accounts.add("alpha")
        await self.db.accounts.assign_profile("alpha", "roller")
        await self.db.plugins.set_state("alpha", "soulcopy", False)
        await self.db.plugins.set_setting("alpha", "soulcopy", "trigger", "x")
        await self.db.account_settings.set("alpha", "reconnect_enabled", False)
        await self.db.cache.set("alpha", "dm", "42", 4242)
        self.assertTrue(await self.db.accounts.delete("alpha"))
        self.assertIsNone(await self.db.accounts.get("alpha"))
        self.assertEqual(await self.db.plugins.settings("alpha", "soulcopy"), {})
        self.assertIsNone(await self.db.plugins.state("alpha", "soulcopy"))
        self.assertEqual(await self.db.account_settings.get("alpha"), {})
        self.assertEqual(await self.db.accounts.profiles_of("alpha"), [])
        self.assertIsNone(await self.db.cache.get("alpha", "dm", "42"))  # api_cache cascade
        self.assertFalse(await self.db.accounts.delete("alpha"))


class TestAccountSettings(AsyncDatabaseTestCase):
    async def test_set_get_delete(self) -> None:
        await self.db.accounts.add("alpha")
        self.assertEqual(await self.db.account_settings.get("alpha"), {})
        await self.db.account_settings.set("alpha", "reconnect_max_delay", 30)
        await self.db.account_settings.set("alpha", "reconnect_enabled", True)
        got = await self.db.account_settings.get("alpha")
        self.assertEqual(got["reconnect_max_delay"], 30)
        self.assertTrue(got["reconnect_enabled"])
        self.assertTrue(await self.db.account_settings.delete("alpha", "reconnect_enabled"))
        self.assertNotIn("reconnect_enabled", await self.db.account_settings.get("alpha"))

    async def test_list_accounts_helper(self) -> None:
        await self.db.account_settings.set("alpha", "k", 1)
        await self.db.account_settings.set("beta", "k", 2)
        self.assertEqual(await self.db.account_settings.list_accounts(), ["alpha", "beta"])


class TestProfiles(AsyncDatabaseTestCase):
    async def test_upsert_get_list_delete(self) -> None:
        await self.db.profiles.upsert("roller", ["roll"], "rolls")
        rec = await self.db.profiles.get("roller")
        self.assertIsInstance(rec, ProfileRecord)
        self.assertEqual(rec.plugins, ["roll"])
        self.assertEqual(rec.description, "rolls")
        await self.db.profiles.upsert("roller", ["snipe", "roll"], "rolls+snipes")
        got = await self.db.profiles.get("roller")
        self.assertEqual(got.plugins, ["roll", "snipe"])  # canonical sorted form
        self.assertEqual(got.description, "rolls+snipes")
        self.assertEqual(len(await self.db.profiles.list()), 1)
        self.assertTrue(await self.db.profiles.delete("roller"))
        self.assertFalse(await self.db.profiles.delete("roller"))
        self.assertIsNone(await self.db.profiles.get("roller"))

    async def test_delete_profile_drops_assignments(self) -> None:
        await self.db.accounts.add("alpha")
        await self.db.profiles.upsert("roller", ["roll"])
        await self.db.accounts.assign_profile("alpha", "roller")
        await self.db.profiles.delete("roller")
        self.assertEqual(await self.db.accounts.profiles_of("alpha"), [])


class TestPlugins(AsyncDatabaseTestCase):
    async def test_metadata(self) -> None:
        await self.db.plugins.upsert_metadata("soulcopy", "1.2.0", "desc", [{"name": "trigger"}])
        meta = await self.db.plugins.metadata_for("soulcopy")
        self.assertEqual(meta["version"], "1.2.0")
        self.assertEqual(meta["settings"], [{"name": "trigger"}])
        await self.db.plugins.upsert_metadata("soulcopy", "1.3.0", "desc2", [])
        self.assertEqual((await self.db.plugins.metadata_for("soulcopy"))["version"], "1.3.0")
        self.assertEqual(len(await self.db.plugins.metadata()), 1)

    async def test_state_defaults_none_then_set(self) -> None:
        self.assertIsNone(await self.db.plugins.state("alpha", "soulcopy"))
        await self.db.plugins.set_state("alpha", "soulcopy", False)
        self.assertFalse(await self.db.plugins.state("alpha", "soulcopy"))
        await self.db.plugins.set_state("alpha", "soulcopy", True)
        self.assertTrue(await self.db.plugins.state("alpha", "soulcopy"))

    async def test_settings_and_all_account_settings(self) -> None:
        await self.db.plugins.set_setting("alpha", "soulcopy", "trigger", "go")
        await self.db.plugins.set_setting("beta", "soulcopy", "trigger", "go2")
        await self.db.plugins.set_setting("beta", "roll", "command", "$m")
        self.assertEqual(await self.db.plugins.setting("alpha", "soulcopy", "trigger"), "go")
        self.assertEqual(await self.db.plugins.settings("beta", "roll"), {"command": "$m"})
        all_layers = await self.db.plugins.all_account_settings()
        self.assertEqual(all_layers["beta"]["soulcopy"]["trigger"], "go2")


class TestRatePolicies(AsyncDatabaseTestCase):
    async def test_roundtrip_all_levels(self) -> None:
        await self.db.policies.set("global", "", "message", 2.0, 0.1, 0.5)
        await self.db.policies.set("account", "alpha", "message", 3.0, 0.0, 1.0)
        await self.db.policies.set("plugin", "soulcopy", "dm", 4.0, 0.2, 0.6)
        await self.db.policies.set("account_plugin", "alpha/soulcopy", "dm", 5.0, 0.0, 0.0)
        rec = await self.db.policies.get("global", "", "message")
        self.assertIsInstance(rec, RatePolicyRecord)
        self.assertEqual(rec.interval, 2.0)
        self.assertEqual(rec.jitter_min, 0.1)
        self.assertEqual((await self.db.policies.get("account", "alpha", "message")).interval, 3.0)
        self.assertEqual(len(await self.db.policies.list()), 4)
        self.assertTrue(await self.db.policies.delete("account", "alpha", "message"))
        self.assertFalse(await self.db.policies.delete("account", "alpha", "message"))
        self.assertIsNone(await self.db.policies.get("account", "alpha", "message"))

    async def test_owner_normalization_and_validation(self) -> None:
        # None and '' both address the global row
        await self.db.policies.set("global", None, "message", 2.0, 0.0, 0.0)
        self.assertIsNotNone(await self.db.policies.get("global", "", "message"))
        with self.assertRaises(ANY_DB_ERROR):  # non-global needs an owner
            await self.db.policies.set("account", "", "message", 2.0, 0.0, 0.0)
        with self.assertRaises(ANY_DB_ERROR):  # jitter order violated
            await self.db.policies.set("global", "", "dm", 2.0, 0.5, 0.1)


class TestAuditAndLogs(AsyncDatabaseTestCase):
    async def test_audit_recent_oldest_first_and_prune(self) -> None:
        db = Database(self.path, audit_keep=5)
        await db.connect()
        try:
            for i in range(10):
                await db.audit.add({"ts": 1700000000.0 + i, "account": "alpha",
                                    "direction": "out", "action": "message",
                                    "channel_id": 1, "content": f"m{i}"})
            rows = await db.audit.recent(limit=100)
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[0]["content"], "m5")  # oldest of the kept, first
            self.assertEqual(rows[-1]["content"], "m9")
            rows_alpha = await db.audit.recent(limit=3, account="alpha")
            self.assertEqual(len(rows_alpha), 3)
        finally:
            await db.close()

    async def test_audit_content_truncated(self) -> None:
        await self.db.audit.add({"ts": 1.0, "account": "a", "direction": "out",
                                 "action": "message", "content": "x" * 900})
        rows = await self.db.audit.recent(1)
        self.assertEqual(len(rows[0]["content"]), 500)

    async def test_logs_filters_and_prune(self) -> None:
        db = Database(self.path, log_keep=4)
        await db.connect()
        try:
            for i in range(8):
                await db.logs.add({"ts": float(i), "severity": "INFO" if i % 2 else "ERROR",
                                   "source": "scheduler" if i < 4 else "core",
                                   "account": "alpha", "message": f"line{i}"})
            kept = await db.logs.recent(limit=100)
            self.assertEqual(len(kept), 4)
            errs = await db.logs.recent(limit=100, severity="error")  # case-insensitive
            self.assertTrue(all(r["severity"] == "ERROR" for r in errs))
            srcs = await db.logs.recent(limit=100, source="core")
            self.assertTrue(all(r["source"] == "core" for r in srcs))
            acct = await db.logs.recent(limit=100, account="alpha")
            self.assertEqual(len(acct), 4)
        finally:
            await db.close()


class TestTaskHistory(AsyncDatabaseTestCase):
    async def test_add_update_recent(self) -> None:
        tid = await self.db.tasks.add({"name": "t", "account": "alpha", "plugin": "soulcopy",
                                       "action": "message", "status": "completed",
                                       "submitted_at": 1.0, "duration_s": 0.5})
        self.assertIsInstance(tid, int)
        await self.db.tasks.update(tid, status="failed", error="boom")
        rows = await self.db.tasks.recent(limit=10)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["error"], "boom")
        only = await self.db.tasks.recent(limit=10, account="beta")
        self.assertEqual(only, [])

    async def test_summary_alias_and_unknown_field(self) -> None:
        tid = await self.db.tasks.add({"account": "a", "result_summary": "ok"})
        rows = await self.db.tasks.recent(limit=1)
        self.assertEqual(rows[0]["summary"], "ok")
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.tasks.update(tid, typo_field="x")
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.tasks.update(999999, status="failed")


class TestKv(AsyncDatabaseTestCase):
    async def test_get_set_delete_prefix(self) -> None:
        self.assertIsNone(await self.db.kv.get("missing"))
        await self.db.kv.set("jobs", [1, 2])  # JSON types round-trip
        await self.db.kv.set("jobs:1", "a")
        self.assertEqual(await self.db.kv.get("jobs"), [1, 2])
        self.assertEqual(await self.db.kv.prefix("jobs:"), {"jobs:1": "a"})
        self.assertTrue(await self.db.kv.delete("jobs"))
        self.assertIsNone(await self.db.kv.get("jobs"))

    async def test_bad_keys_and_values(self) -> None:
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.kv.set("", "x")
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.kv.set("k", object())  # not JSON-serializable


class TestApiCache(AsyncDatabaseTestCase):
    async def test_roundtrip_scoping_and_invalidation(self) -> None:
        await self.db.cache.set("alpha", "dm", "42", 4242)
        await self.db.cache.set("beta", "dm", "42", 9999)
        self.assertEqual(await self.db.cache.get("alpha", "dm", "42"), 4242)
        self.assertEqual(await self.db.cache.get("beta", "dm", "42"), 9999)  # never shared
        self.assertEqual(await self.db.cache.counts(), {"alpha": 1, "beta": 1})
        self.assertEqual(await self.db.cache.drop_account("alpha"), 1)
        self.assertIsNone(await self.db.cache.get("alpha", "dm", "42"))
        self.assertTrue(await self.db.cache.delete("beta", "dm", "42"))


class TestTransactions(AsyncDatabaseTestCase):
    async def test_rollback_visibility(self) -> None:
        """A failed transaction leaves NOTHING behind — not even partially."""
        await self.db.kv.set("keep", "1")  # committed before the failed tx
        with self.assertRaises(RuntimeError):
            async with self.db.connection.write() as conn:
                await conn.execute("INSERT INTO kv (key, value) VALUES ('both','x')")
                await conn.execute("INSERT INTO kv (key, value) VALUES ('later','y')")
                raise RuntimeError("boom mid-transaction")
        self.assertIsNone(await self.db.kv.get("both"))       # rolled back
        self.assertIsNone(await self.db.kv.get("later"))      # rolled back
        self.assertEqual(await self.db.kv.get("keep"), "1")   # untouched

    async def test_constraint_error_wrapped_as_databaseerror(self) -> None:
        await self.db.accounts.add("alpha")
        with self.assertRaises(ANY_DB_ERROR) as ctx:
            await self.db.accounts.add("alpha")  # PK violation
        self.assertNotIsInstance(ctx.exception, (KeyError, ValueError))
        # the connection stays usable after the failed write
        await self.db.kv.set("still", "alive")
        self.assertEqual(await self.db.kv.get("still"), "alive")


class TestConcurrentWriters(AsyncDatabaseTestCase):
    async def test_twenty_writers_no_busy(self) -> None:
        """20 parallel writers on ONE connection: serialized by the write
        lock, all rows land, and the event loop never stalls (aiosqlite
        runs SQLite on its worker thread, off the loop)."""
        loop = asyncio.get_running_loop()
        tick_period = 0.002
        stalls: list[float] = []
        last = loop.time()
        done = asyncio.Event()

        async def ticker() -> None:
            nonlocal last
            try:
                while not done.is_set():
                    await asyncio.sleep(tick_period)
                    now = loop.time()
                    stalls.append(now - last - tick_period)
                    last = now
            except asyncio.CancelledError:
                return

        async def writer(i: int) -> None:
            await self.db.kv.set(f"k{i}", str(i))
            await self.db.account_settings.set("alpha", f"key{i}", i)

        await self.db.accounts.add("alpha")
        ticker_task = loop.create_task(ticker())
        try:
            await asyncio.gather(*(writer(i) for i in range(20)))
        finally:
            done.set()
            ticker_task.cancel()
            await asyncio.gather(ticker_task, return_exceptions=True)

        # all writes landed (no SQLITE_BUSY ate any of them)
        for i in range(20):
            self.assertEqual(await self.db.kv.get(f"k{i}"), str(i))
            settings = await self.db.account_settings.get("alpha")
            self.assertEqual(settings[f"key{i}"], i)
        # the loop stayed responsive while SQLite churned
        self.assertLess(max(stalls or [0.0]), 0.5, f"max loop stall {max(stalls or [0.0])*1000:.1f}ms")


if __name__ == "__main__":
    unittest.main()
