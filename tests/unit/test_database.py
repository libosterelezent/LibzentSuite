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
import threading
import unittest
from pathlib import Path
from unittest import mock

from core.errors import DatabaseError as CoreDatabaseError
from database.connection import DatabaseError as ConnectionDatabaseError
from database.db import Database
from database.models import AccountRecord, RatePolicyRecord
from database.schema import MIGRATIONS, latest_version

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

    async def test_misordered_migrations_rejected(self) -> None:
        bad = ((2, "later", ""), (1, "earlier", ""))
        with mock.patch("database.db.MIGRATIONS", bad), self.assertRaises(ANY_DB_ERROR):
            await self.db.migrate()

    async def test_duplicate_migration_versions_rejected(self) -> None:
        bad = ((1, "first", ""), (1, "second", ""))
        with mock.patch("database.db.MIGRATIONS", bad), self.assertRaises(ANY_DB_ERROR):
            await self.db.migrate()

    async def test_bad_list_leaves_fresh_file_untouched(self) -> None:
        """The version guard fires BEFORE any SQL: a bad list applies
        nothing (not even the schema_migrations bookkeeping table)."""
        path = Path(self.tmp.name) / "bad.db"
        bad = ((2, "later", ""), (1, "earlier", ""))
        with mock.patch("database.db.MIGRATIONS", bad):
            db = Database(path)
            with self.assertRaises(ANY_DB_ERROR):
                await db.connect()
        import sqlite3 as s3
        raw = s3.connect(path)
        tables = {r[0] for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        raw.close()
        self.assertEqual(tables, set())

    async def test_mid_script_failure_rolls_back_completely(self) -> None:
        """A migration failing mid-script leaves NOTHING: no half-built
        table, no bookkeeping row, prior data intact."""
        await self.db.kv.set("sentinel", "1")
        version = latest_version()
        broken = MIGRATIONS + [(
            version + 1, "half table",
            "CREATE TABLE half_a (id INTEGER PRIMARY KEY);\n"
            "INSERT INTO nowhere_at_all VALUES (1);")]
        await self.db.close()
        with mock.patch("database.db.MIGRATIONS", broken):
            db2 = Database(self.path)
            with self.assertRaises(ANY_DB_ERROR) as ctx:
                await db2.connect()
            self.assertIn("migration", str(ctx.exception))
            await db2.close()  # already closed by the failed connect; idempotent
        db3 = Database(self.path)
        await db3.connect()
        try:
            leftovers = await db3.connection.fetch_all(
                "SELECT name FROM sqlite_master WHERE name = 'half_a'")
            self.assertEqual(leftovers, [])
            stamped = await db3.connection.fetch_all(
                "SELECT version FROM schema_migrations WHERE version = ?",
                (version + 1,))
            self.assertEqual(stamped, [])
            self.assertEqual(await db3.kv.get("sentinel"), "1")
        finally:
            await db3.close()

    async def test_appended_migration_applies_on_old_db(self) -> None:
        """A NEW migration appended today applies cleanly to a DB last
        opened by older code (the append-only contract)."""
        version = latest_version()
        addon = MIGRATIONS + [(
            version + 1, "addon table",
            "CREATE TABLE sm_addon (id INTEGER PRIMARY KEY);")]
        await self.db.close()
        with mock.patch("database.db.MIGRATIONS", addon):
            db2 = Database(self.path)
            applied = await db2.connect()
            try:
                self.assertEqual(applied, 1)
                rows = await db2.connection.fetch_all(
                    "SELECT name FROM sqlite_master WHERE name = 'sm_addon'")
                self.assertEqual(len(rows), 1)
                stamps = await db2.connection.fetch_all(
                    "SELECT version FROM schema_migrations ORDER BY version")
                self.assertEqual([r["version"] for r in stamps],
                                 [m[0] for m in addon])
            finally:
                await db2.close()
        # older code meeting the newer DB: warned, but boots fine
        db3 = Database(self.path)
        self.assertEqual(await db3.connect(), 0)
        await db3.close()

    async def test_future_stamped_db_warns_but_boots(self) -> None:
        await self.db.connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (99999, 'from the future', 0)")
        with self.assertLogs("libzent.database", level="WARNING") as logs:
            self.assertEqual(await self.db.migrate(), 0)
        self.assertTrue(any("NEWER" in m for m in logs.output))

    async def test_concurrent_connect_runs_one_migration_pass(self) -> None:
        db = Database(Path(self.tmp.name) / "cc.db")
        results = await asyncio.gather(db.connect(), db.connect())
        self.assertEqual(sorted(results), [0, len(MIGRATIONS)])
        await db.close()


class TestAccounts(AsyncDatabaseTestCase):
    async def test_add_get_list_roundtrip(self) -> None:
        await self.db.accounts.add("alpha", token_ref="DISCORD_TOKEN_ALPHA", workspace="g1")
        await self.db.accounts.add("beta")
        names = [r.name for r in await self.db.accounts.list()]
        self.assertEqual(names, ["alpha", "beta"])
        rec = await self.db.accounts.get("alpha")
        self.assertIsInstance(rec, AccountRecord)
        self.assertEqual(rec.token_ref, "DISCORD_TOKEN_ALPHA")
        self.assertEqual(rec.workspace, "g1")
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

    async def test_delete_cascades(self) -> None:
        await self.db.accounts.add("alpha")
        await self.db.plugins.set_state("alpha", "exporter", False)
        await self.db.plugins.set_setting("alpha", "exporter", "trigger", "x")
        await self.db.account_settings.set("alpha", "reconnect_enabled", False)
        await self.db.cache.set("alpha", "dm", "42", 4242)
        self.assertTrue(await self.db.accounts.delete("alpha"))
        self.assertIsNone(await self.db.accounts.get("alpha"))
        self.assertEqual(await self.db.plugins.settings("alpha", "exporter"), {})
        self.assertIsNone(await self.db.plugins.state("alpha", "exporter"))
        self.assertEqual(await self.db.account_settings.get("alpha"), {})
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


class TestPlugins(AsyncDatabaseTestCase):
    async def test_metadata(self) -> None:
        await self.db.plugins.upsert_metadata("exporter", "1.2.0", "desc", [{"name": "trigger"}])
        meta = await self.db.plugins.metadata_for("exporter")
        self.assertEqual(meta["version"], "1.2.0")
        self.assertEqual(meta["settings"], [{"name": "trigger"}])
        await self.db.plugins.upsert_metadata("exporter", "1.3.0", "desc2", [])
        self.assertEqual((await self.db.plugins.metadata_for("exporter"))["version"], "1.3.0")
        self.assertEqual(len(await self.db.plugins.metadata()), 1)

    async def test_state_defaults_none_then_set(self) -> None:
        self.assertIsNone(await self.db.plugins.state("alpha", "exporter"))
        await self.db.plugins.set_state("alpha", "exporter", False)
        self.assertFalse(await self.db.plugins.state("alpha", "exporter"))
        await self.db.plugins.set_state("alpha", "exporter", True)
        self.assertTrue(await self.db.plugins.state("alpha", "exporter"))

    async def test_settings_and_all_account_settings(self) -> None:
        await self.db.plugins.set_setting("alpha", "exporter", "trigger", "go")
        await self.db.plugins.set_setting("beta", "exporter", "trigger", "go2")
        await self.db.plugins.set_setting("beta", "roll", "command", "$m")
        self.assertEqual(await self.db.plugins.setting("alpha", "exporter", "trigger"), "go")
        self.assertEqual(await self.db.plugins.settings("beta", "roll"), {"command": "$m"})
        all_layers = await self.db.plugins.all_account_settings()
        self.assertEqual(all_layers["beta"]["exporter"]["trigger"], "go2")

    async def test_delete_setting(self) -> None:
        """Round 3 (F7 unpin support): remove ONE override; missing rows
        are a clean False (idempotent), sibling rows stay untouched."""
        await self.db.plugins.set_setting("alpha", "exporter", "trigger", "go")
        await self.db.plugins.set_setting("alpha", "exporter", "other", 2)
        await self.db.plugins.set_setting("beta", "exporter", "trigger", "x")
        self.assertTrue(
            await self.db.plugins.delete_setting("alpha", "exporter", "trigger"))
        self.assertIsNone(
            await self.db.plugins.setting("alpha", "exporter", "trigger"))
        self.assertEqual(  # same (account, plugin): only the one key went
            await self.db.plugins.settings("alpha", "exporter"), {"other": 2})
        self.assertEqual(  # same key elsewhere is untouched
            await self.db.plugins.setting("beta", "exporter", "trigger"), "x")
        self.assertFalse(  # idempotent on a missing row
            await self.db.plugins.delete_setting("alpha", "exporter", "trigger"))
        self.assertFalse(
            await self.db.plugins.delete_setting("ghost", "plugin", "key"))
        self.assertNotIn(  # gone from the preload layer too
            "trigger",
            (await self.db.plugins.all_account_settings())["alpha"]["exporter"])

    async def test_delete_setting_signature_pinned(self) -> None:
        """SettingsManager (F7) calls this duck-typed — parameter names
        and order ARE the contract."""
        import inspect
        sig = inspect.signature(type(self.db.plugins).delete_setting)
        self.assertEqual(list(sig.parameters),
                         ["self", "account", "plugin", "key"])


class TestRatePolicies(AsyncDatabaseTestCase):
    async def test_roundtrip_all_levels(self) -> None:
        await self.db.policies.set("global", "", "message", 2.0, 0.1, 0.5)
        await self.db.policies.set("account", "alpha", "message", 3.0, 0.0, 1.0)
        await self.db.policies.set("plugin", "exporter", "dm", 4.0, 0.2, 0.6)
        await self.db.policies.set("account_plugin", "alpha/exporter", "dm", 5.0, 0.0, 0.0)
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

    async def test_delete_account_escapes_like_wildcards(self) -> None:
        await self.db.policies.set("account_plugin", "a_b/x", "dm", 2.0, 0.0, 0.0)
        await self.db.policies.set("account_plugin", "axb/x", "dm", 3.0, 0.0, 0.0)
        removed = await self.db.policies.delete_account("a_b")
        self.assertEqual(removed, 1)  # '_' must NOT match the 'axb' row
        self.assertIsNone(await self.db.policies.get("account_plugin", "a_b/x", "dm"))
        self.assertIsNotNone(
            await self.db.policies.get("account_plugin", "axb/x", "dm"))


    async def test_delete_sweeps_access_row_exact_match(self) -> None:
        """Review #5: the access row is deleted by EXACT key, never a
        prefix sweep — deleting 'bob' must not wipe bobby's admin row."""
        await self.db.accounts.add("bob", token_ref="T1")
        await self.db.kv.set("access:account:bob", {"is_admin": True})
        await self.db.kv.set("access:account:bobby", {"is_admin": True})
        await self.db.kv.set("access:account:bob2", {"prefix_mode": "mention"})
        # pstate rows still sweep by prefix (their keys carry separators)
        await self.db.kv.set("pstate:account:bob:plug/x", {"a": 1})
        await self.db.kv.set("pstate:account:bobby:plug/x", {"b": 2})
        await self.db.accounts.delete("bob")
        self.assertIsNone(await self.db.kv.get("access:account:bob"))
        self.assertEqual(await self.db.kv.get("access:account:bobby"),
                         {"is_admin": True})
        self.assertEqual(await self.db.kv.get("access:account:bob2"),
                         {"prefix_mode": "mention"})
        self.assertIsNone(await self.db.kv.get("pstate:account:bob:plug/x"))
        self.assertEqual(await self.db.kv.get("pstate:account:bobby:plug/x"),
                         {"b": 2})


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

    async def test_audit_non_string_content_coerced(self) -> None:
        await self.db.audit.add({"ts": 1.0, "account": "a", "direction": "out",
                                 "action": "message", "content": 12345})
        rows = await self.db.audit.recent(1)
        self.assertEqual(rows[0]["content"], "12345")

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
        tid = await self.db.tasks.add({"name": "t", "account": "alpha", "plugin": "exporter",
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


class TestConnectionRobustness(AsyncDatabaseTestCase):
    """Round 2: the connection bridge under abuse."""

    async def test_wal_and_busy_timeout_pragmas_active(self) -> None:
        mode = await self.db.connection.fetch_one("PRAGMA journal_mode")
        self.assertEqual(str(mode["journal_mode"]).lower(), "wal")
        busy = await self.db.connection.fetch_one("PRAGMA busy_timeout")
        self.assertEqual(busy[0], 5000)
        fk = await self.db.connection.fetch_one("PRAGMA foreign_keys")
        self.assertEqual(fk["foreign_keys"], 1)

    async def test_gather_inside_write_raises_clear_deadlock_error(self) -> None:
        """The classic _TaskGuard reentrancy trap: a write() body that
        awaits gathered child DB work deadlocks the single connection.
        The bounded wait converts the hang into a clear DatabaseError and
        the outer transaction rolls back."""
        async def inner() -> None:
            await self.db.kv.set("inner", "1")

        async def outer() -> None:
            async with self.db.connection.write() as conn:
                await conn.execute(
                    "INSERT INTO kv (key, value) VALUES ('outer','x')")
                await asyncio.gather(inner())

        with mock.patch("database.connection.LOCK_WAIT_TIMEOUT_S", 0.3):
            with self.assertRaises(ANY_DB_ERROR) as ctx:
                await asyncio.wait_for(outer(), 10)
            self.assertIn("lock not acquired", str(ctx.exception))
        self.assertIsNone(await self.db.kv.get("outer"))  # rolled back
        await self.db.kv.set("usable", "1")               # connection healthy
        self.assertEqual(await self.db.kv.get("usable"), "1")

    async def test_close_during_inflight_write(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        outcome: dict = {}

        async def slow_write() -> None:
            try:
                async with self.db.connection.write() as conn:
                    await conn.execute(
                        "INSERT INTO kv (key, value) VALUES ('w','1')")
                    started.set()
                    await release.wait()
            except BaseException as exc:
                outcome["err"] = exc

        task = asyncio.create_task(slow_write())
        await started.wait()
        await self.db.connection.close(timeout=0.2)  # gives up, closes anyway
        release.set()
        await asyncio.wait_for(task, 5)
        self.assertIsNotNone(outcome.get("err"))  # clean error, no hang
        self.assertFalse(self.db.connection.connected)
        db2 = Database(self.path)
        await db2.connect()  # the uncommitted row was rolled back
        try:
            self.assertIsNone(await db2.kv.get("w"))
        finally:
            await db2.close()

    async def test_double_and_triple_close(self) -> None:
        await asyncio.gather(self.db.close(), self.db.close())
        await self.db.close()
        self.assertFalse(self.db.connection.connected)

    async def test_thread_submitted_op_via_run_coroutine_threadsafe(self) -> None:
        """An op submitted from a thread with no running loop must work
        when routed to the owning loop (the supported bridge)."""
        loop = asyncio.get_running_loop()
        errors: list = []

        def worker() -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.db.kv.set("threaded", "ok"), loop).result(5)
                got = asyncio.run_coroutine_threadsafe(
                    self.db.kv.get("threaded"), loop).result(5)
                if got != "ok":
                    errors.append(f"bad readback {got!r}")
            except BaseException as exc:
                errors.append(exc)

        await loop.run_in_executor(None, worker)
        self.assertEqual(errors, [])
        self.assertEqual(await self.db.kv.get("threaded"), "ok")

    async def test_second_event_loop_rejected_cleanly(self) -> None:
        """Driving the SAME Database from a second event loop corrupts
        the lock/sqlite session; it must fail loud, not silently work."""
        await self.db.kv.set("bind", "1")  # binds the guard to this loop

        def other_loop() -> None:
            async def go() -> None:
                await self.db.kv.set("x", "1")
            asyncio.run(go())

        with self.assertRaises(ANY_DB_ERROR):
            await asyncio.get_running_loop().run_in_executor(None, other_loop)
        self.assertEqual(await self.db.kv.get("bind"), "1")  # still usable


class TestCrossLoopLifecycle(AsyncDatabaseTestCase):
    """Round 3: one Database, sequential event loops.

    connect() binds/checks the loop BEFORE opening anything, and a clean
    close() reborn the loop-bound primitives (guard, connect lock,
    migrate lock). So: a closed Database is reusable on a NEW loop, and
    a connect() from a foreign loop while open fails loud at CONNECT —
    never after silently opening a second aiosqlite worker thread
    (non-daemon: a leak hangs interpreter shutdown).
    """

    @staticmethod
    async def _await_thread_settle(before: set, timeout: float = 5.0) -> set:
        """Wait until no NEW named threads remain (worker exit is async)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            current = {t.name for t in threading.enumerate()}
            if not (current - before):
                return current
            await asyncio.sleep(0.02)
        return {t.name for t in threading.enumerate()}

    async def test_connect_after_close_works_in_new_loop(self) -> None:
        db = Database(Path(self.tmp.name) / "reuse.db")
        await db.connect()
        await db.kv.set("gen", "1")
        await db.close()
        self.assertFalse(db.connection.connected)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: None)  # warm executor pool
        before = {t.name for t in threading.enumerate()}
        readback: list = []

        def second_loop() -> None:
            async def go() -> None:
                await db.connect()
                readback.append(await db.kv.get("gen"))  # same file
                await db.kv.set("gen2", "2")
                await db.close()
            asyncio.run(go())

        await loop.run_in_executor(None, second_loop)
        after = await self._await_thread_settle(before)
        self.assertEqual(readback, ["1"])
        self.assertFalse(db.connection.connected)
        self.assertEqual(after - before, set())  # no leftover worker thread

    async def test_connect_from_second_loop_while_open_raises_no_leak(self) -> None:
        db = Database(Path(self.tmp.name) / "open.db")
        await db.connect()
        await db.kv.set("bind", "1")  # guard bound to THIS loop

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: None)
        before = {t.name for t in threading.enumerate()}

        def second_loop() -> None:
            async def go() -> None:
                await db.connect()  # must fail BEFORE opening any worker
            asyncio.run(go())

        with self.assertRaises(ANY_DB_ERROR) as ctx:
            await loop.run_in_executor(None, second_loop)
        self.assertIn("multiple event loops", str(ctx.exception))
        after = await self._await_thread_settle(before)
        self.assertEqual(after - before, set())  # refused -> nothing leaked
        # the ORIGINAL connection is untouched and still usable
        await db.kv.set("usable", "1")
        self.assertEqual(await db.kv.get("usable"), "1")
        await db.close()


class TestWriteSelectSnapshotHygiene(AsyncDatabaseTestCase):
    """Round 3 regression for the round-2 SQLITE_BUSY class: a SELECT
    fully consumed inside write() (cursor closed via ``async with``)
    must not pin this connection's read snapshot across ANOTHER
    connection's commit — the next BEGIN IMMEDIATE here succeeds."""

    async def test_select_in_write_releases_snapshot_across_other_commit(self) -> None:
        import aiosqlite

        await self.db.kv.set("seed", "1")
        other = await aiosqlite.connect(str(self.path), isolation_level=None)
        try:
            async with self.db.connection.write() as conn, conn.execute(
                "SELECT value FROM kv WHERE key = 'seed'"
            ) as cur:
                self.assertEqual(len(await cur.fetchall()), 1)
            # a commit by the OTHER connection lands between our writes
            await other.execute(
                "INSERT INTO kv (key, value) VALUES ('ext', '\"ext-v\"')")
            # a pinned stale snapshot would fail here BUSY immediately —
            # not retryable by busy_timeout
            async with self.db.connection.write() as conn:
                await conn.execute(
                    "INSERT INTO kv (key, value) VALUES ('mine', '\"mine-v\"')")
        finally:
            await other.close()
        self.assertEqual(await self.db.kv.get("ext"), "ext-v")
        self.assertEqual(await self.db.kv.get("mine"), "mine-v")


class TestTwoDatabasesSameFile(AsyncDatabaseTestCase):
    """The 'sim runner + live' pattern: two Database objects, one file."""

    async def test_interleaved_read_write_transactions_no_busy(self) -> None:
        db2 = self.make()
        await db2.connect()
        try:
            async def churn(db: Database, tag: str) -> None:
                for i in range(60):
                    async with db.connection.write() as conn:
                        async with conn.execute("SELECT 1 FROM kv LIMIT 1") as cur:
                            await cur.fetchall()
                        await conn.execute(
                            "INSERT INTO kv (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (tag, str(i)))
                    await db.audit.add(
                        {"account": tag, "action": "churn", "content": str(i)})
            await asyncio.gather(churn(self.db, "live"), churn(db2, "sim"))
            self.assertEqual(str(await self.db.kv.get("live")), "59")
            self.assertEqual(str(await self.db.kv.get("sim")), "59")
            self.assertEqual(len(await self.db.audit.recent(limit=200)), 120)
        finally:
            await db2.close()


class TestUpsertRaces(AsyncDatabaseTestCase):
    """Concurrent writers hitting the same identity."""

    async def test_concurrent_account_adds_same_name(self) -> None:
        results = await asyncio.gather(
            *(self.db.accounts.add("clash") for _ in range(25)),
            return_exceptions=True)
        ok = [r for r in results if not isinstance(r, BaseException)]
        errs = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(errs), 24)
        for exc in errs:
            self.assertIsInstance(exc, ANY_DB_ERROR)
        self.assertEqual(len(await self.db.accounts.list()), 1)

    async def test_concurrent_kv_sets_same_key(self) -> None:
        await asyncio.gather(
            *(self.db.kv.set("k", str(i)) for i in range(25)))
        value = await self.db.kv.get("k")
        self.assertIn(value, {str(i) for i in range(25)})
        rows = await self.db.connection.fetch_all(
            "SELECT COUNT(*) AS n FROM kv WHERE key = 'k'")
        self.assertEqual(rows[0]["n"], 1)

    async def test_concurrent_cache_and_cooldown_upserts(self) -> None:
        await asyncio.gather(*(
            self.db.cache.set("a", "dm", "42", i) for i in range(25)))
        rows = await self.db.connection.fetch_all(
            "SELECT COUNT(*) AS n FROM api_cache")
        self.assertEqual(rows[0]["n"], 1)

        await asyncio.gather(*(
            self.db.cooldowns.upsert(
                account="a", plugin="p", cooldown_key="k",
                started_at=0.0, expires_at=float(i))
            for i in range(25)))
        rows = await self.db.connection.fetch_all(
            "SELECT COUNT(*) AS n FROM cooldowns")
        self.assertEqual(rows[0]["n"], 1)


class TestPruningBounds(AsyncDatabaseTestCase):
    """audit_keep / log_keep / task keep exactness under storms."""

    async def test_audit_prune_exact_boundary_and_storm(self) -> None:
        db = Database(self.path, audit_keep=10)
        await db.connect()
        try:
            for i in range(10):  # exactly keep: nothing pruned yet
                await db.audit.add({"account": "a", "action": "x",
                                    "content": f"m{i}"})
            self.assertEqual(len(await db.audit.recent(limit=100)), 10)
            oldest = (await db.audit.recent(limit=100))[0]["content"]
            self.assertEqual(oldest, "m0")

            batch = [{"account": "a", "action": "x", "content": f"b{j}"}
                     for j in range(5)]
            storm = [db.audit.add({"account": "a", "action": "x",
                                   "content": f"s{j}"})
                     for j in range(15)]
            await db.audit.add_many(batch)
            await asyncio.gather(*storm)
            rows = await db.audit.recent(limit=100)
            self.assertEqual(len(rows), 10)
            self.assertNotIn("m0", [r["content"] for r in rows])
            newest = rows[-1]["content"]
            self.assertTrue(newest.startswith("s"), newest)
        finally:
            await db.close()

    async def test_audit_add_many_empty_is_noop(self) -> None:
        self.assertEqual(await self.db.audit.add_many([]), 0)
        self.assertEqual(await self.db.logs.add_many([]), 0)
        self.assertEqual(await self.db.audit.recent(limit=10), [])

    async def test_audit_keep_zero_prunes_everything(self) -> None:
        db = Database(self.path, audit_keep=0)
        await db.connect()
        try:
            await db.audit.add({"account": "a", "action": "x"})
            await db.audit.add_many(
                [{"account": "a", "action": "x"} for _ in range(5)])
            self.assertEqual(await db.audit.recent(limit=100), [])
        finally:
            await db.close()

    async def test_task_history_custom_keep(self) -> None:
        from database.repositories.tasks import TaskHistoryRepository
        repo = TaskHistoryRepository(self.db, keep=5)
        for i in range(30):
            await repo.add({"account": "a", "action": f"t{i}",
                            "status": "done"})
        rows = await repo.recent(limit=100)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["action"], "t25")
        self.assertEqual(rows[-1]["action"], "t29")


class TestLikeIsolation(AsyncDatabaseTestCase):
    """Every LIKE in repositories/ must treat % _ \\ as literals and stay
    unicode-exact and case-sensitive (case_sensitive_like=ON)."""

    async def test_kv_prefix_wildcards_unicode_case(self) -> None:
        for key, val in [("a%b", 1), ("a_b", 2), ("a\\b", 3), ("aXb", 4),
                         ("àb", 5), ("Ab", 6), ("b", 7)]:
            await self.db.kv.set(key, val)
        self.assertEqual(set(await self.db.kv.prefix("a")),
                         {"a%b", "a_b", "a\\b", "aXb"})
        self.assertEqual(set(await self.db.kv.prefix("a%")), {"a%b"})
        self.assertEqual(set(await self.db.kv.prefix("a_")), {"a_b"})
        self.assertEqual(set(await self.db.kv.prefix("a\\")), {"a\\b"})
        self.assertEqual(set(await self.db.kv.prefix("à")), {"àb"})
        self.assertEqual(await self.db.kv.delete_prefix("a%"), 1)
        self.assertEqual(set(await self.db.kv.prefix("a")),
                         {"a_b", "a\\b", "aXb"})
        self.assertEqual(await self.db.kv.get("àb"), 5)

    async def test_policy_delete_account_wildcards_and_unicode(self) -> None:
        for owner in ("a%b/x", "a\\b/x", "a_b/x", "à/x", "axb/x"):
            await self.db.policies.set(
                "account_plugin", owner, "dm", 2.0, 0.0, 0.0)
        await self.db.policies.set("account", "a%b", "dm", 3.0, 0.0, 0.0)

        self.assertEqual(await self.db.policies.delete_account("a%b"), 2)
        self.assertIsNone(
            await self.db.policies.get("account_plugin", "a%b/x", "dm"))
        self.assertIsNone(await self.db.policies.get("account", "a%b", "dm"))
        survivors = {(r.level, r.owner) for r in await self.db.policies.list()}
        self.assertIn(("account_plugin", "axb/x"), survivors)  # no _ leak
        self.assertIn(("account_plugin", "a_b/x"), survivors)
        self.assertIn(("account_plugin", "a\\b/x"), survivors)
        self.assertIn(("account_plugin", "à/x"), survivors)

        self.assertEqual(await self.db.policies.delete_account("à"), 1)
        self.assertEqual(await self.db.policies.delete_account("a\\b"), 1)
        remaining = len(await self.db.policies.list())
        self.assertEqual(remaining, 2)  # a_b/x and axb/x


class TestRoundtripEdges(AsyncDatabaseTestCase):
    """NULL columns, absurd lengths, non-serializable JSON payloads."""

    async def test_null_columns_roundtrip(self) -> None:
        await self.db.audit.add({"ts": 1.0, "account": "a", "action": "x"})
        row = (await self.db.audit.recent(1))[0]
        self.assertIsNone(row["note"])
        self.assertIsNone(row["channel_id"])
        self.assertEqual(row["wait_ms"], 0.0)
        self.assertEqual(row["jitter_ms"], 0.0)

        await self.db.logs.add({"message": "m"})
        lrow = (await self.db.logs.recent(1))[0]
        self.assertIsNone(lrow["account"])
        self.assertIsNone(lrow["plugin"])
        self.assertIsNone(lrow["task_id"])
        self.assertIsNone(lrow["event"])
        self.assertEqual(lrow["severity"], "INFO")

        tid = await self.db.tasks.add({"account": "a", "action": "b"})
        await self.db.tasks.update(tid, error=None, summary=None)
        trow = (await self.db.tasks.recent(1))[0]
        self.assertIsNone(trow["error"])
        self.assertIsNone(trow["summary"])

    async def test_absurd_lengths(self) -> None:
        name = "long" * 25_000
        await self.db.accounts.add(name)
        self.assertEqual((await self.db.accounts.get(name)).name, name)
        key = "k" * 100_000
        await self.db.kv.set(key, {"deep": True})
        self.assertEqual(await self.db.kv.get(key), {"deep": True})
        await self.db.cache.set("a", "dm", "9" * 50_000, 1)
        self.assertEqual(await self.db.cache.get("a", "dm", "9" * 50_000), 1)

    async def test_non_serializable_json_raises_databaseerror(self) -> None:
        with self.assertRaisesRegex(ANY_DB_ERROR, "JSON-serializable"):
            await self.db.cache.set("a", "dm", "1", object())
        with self.assertRaisesRegex(ANY_DB_ERROR, "JSON-serializable"):
            await self.db.cooldowns.upsert(
                account="a", plugin="p", cooldown_key="k",
                started_at=0.0, expires_at=1.0, metadata={"s": set()})
        with self.assertRaisesRegex(ANY_DB_ERROR, "JSON-serializable"):
            await self.db.kv.set("k", object())
        with self.assertRaisesRegex(ANY_DB_ERROR, "JSON-serializable"):
            await self.db.accounts.set_settings("a", {"v": object()})
        with self.assertRaisesRegex(ANY_DB_ERROR, "JSON-serializable"):
            await self.db.account_settings.set("a", "k", object())
        with self.assertRaisesRegex(ANY_DB_ERROR, "JSON-serializable"):
            await self.db.plugins.set_setting("a", "p", "k", object())
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.plugins.upsert_metadata("p", "1", "d", [object()])
        # repo-level non-dict trail entries are refused loudly too
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.audit.add(["not", "a", "dict"])
        with self.assertRaises(ANY_DB_ERROR):
            await self.db.logs.add(42)


class TestQueryPlans(AsyncDatabaseTestCase):
    """EXPLAIN QUERY PLAN: the hot lookups must stay index-backed."""

    async def plan(self, sql: str, params: tuple = ()) -> str:
        rows = await self.db.connection.fetch_all(
            "EXPLAIN QUERY PLAN " + sql, params)
        return " | ".join(str(r["detail"]) for r in rows).upper()

    async def test_hot_point_lookups_use_indexes(self) -> None:
        cases = [
            ("SELECT value_json FROM api_cache WHERE account=? AND kind=? AND key=?",
             ("a", "dm", "1"), "SEARCH API_CACHE USING INDEX"),
            ("SELECT * FROM accounts WHERE name = ?",
             ("a",), "SEARCH ACCOUNTS USING INDEX"),
            ("SELECT * FROM rate_limit_policies WHERE level=? AND owner=? AND action=?",
             ("global", "", "m"), "SEARCH RATE_LIMIT_POLICIES USING INDEX"),
            ("SELECT value FROM kv WHERE key = ?",
             ("k",), "SEARCH KV USING INDEX"),
            ("SELECT ciphertext FROM credentials WHERE account=?",
             ("a",), "SEARCH CREDENTIALS USING INDEX"),
            ("SELECT * FROM cooldowns WHERE account=? AND plugin=? AND cooldown_key=? AND expires_at>?",
             ("a", "p", "k", 0.0), "SEARCH COOLDOWNS USING INDEX"),
            ("SELECT * FROM task_history WHERE account=? ORDER BY id DESC LIMIT ?",
             ("a", 10), "SEARCH TASK_HISTORY USING INDEX"),
            ("SELECT * FROM audit_logs WHERE account=? ORDER BY id DESC LIMIT ?",
             ("a", 10), "SEARCH AUDIT_LOGS USING INDEX"),
            ("SELECT * FROM logs WHERE account=? AND severity=? ORDER BY id DESC LIMIT ?",
             ("a", "ERROR", 10), "SEARCH LOGS USING INDEX"),
        ]
        for sql, params, expect in cases:
            with self.subTest(sql=sql):
                self.assertIn(expect, await self.plan(sql, params))

    async def test_tail_queries_walk_rowid_no_temp_sort(self) -> None:
        for sql in ("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
                    "SELECT * FROM logs ORDER BY id DESC LIMIT ?",
                    "SELECT * FROM task_history ORDER BY id DESC LIMIT ?"):
            with self.subTest(sql=sql):
                self.assertNotIn("TEMP B-TREE", await self.plan(sql, (10,)))


if __name__ == "__main__":
    unittest.main()
