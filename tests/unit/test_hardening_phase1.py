"""Phase-1 hardening regressions (database / logging / boot / credentials).

Every test here pins a defect found in the audit; each failed on the
pre-audit code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core._asyncutil import outer_cancel_pending
from core._persist_queue import PersistQueue
from core.bootstrap import build
from core.cooldowns import CooldownService
from core.credential_service import CredentialService
from core.credentials import Credentials, CredentialsError
from core.errors import CredentialsError as CoreCredentialsError
from core.errors import DatabaseError
from core.event_bus import EventBus, Event
from core.logging_service import LogService, ServiceLogger
from database.connection import DatabaseError as ConnDatabaseError
from database.db import Database

from pathlib import Path

#: fixture plugins (the framework ships none; the fixture keeps the boot
#: path exercising plugin discovery/attachment)
FIXTURE_PLUGINS = (
    Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
)


class _TmpCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-p1-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    async def open_db(self, name: str = "t.db") -> Database:
        db = Database(self.dir / name)
        await db.connect()
        self.addAsyncCleanup(db.close)
        return db


class TestDatabaseLayer(_TmpCase):
    async def test_single_database_error_class(self) -> None:
        self.assertIs(ConnDatabaseError, DatabaseError)
        db = await self.open_db()
        await db.accounts.add("a")
        with self.assertRaises(DatabaseError) as ctx:
            await db.accounts.add("a")
        # not re-wrapped ("database add account failed: ...")
        self.assertEqual(str(ctx.exception), "account 'a' already exists")

    async def test_file_created_private(self) -> None:
        await self.open_db()
        mode = stat.S_IMODE(os.stat(self.dir / "t.db").st_mode)
        self.assertEqual(mode & 0o077, 0)

    async def test_failed_migration_is_atomic(self) -> None:
        bad = ((1, "bad", "CREATE TABLE ok_t (x INTEGER);\nTHIS IS NOT SQL;"),)
        db = Database(self.dir / "m.db")
        with mock.patch("database.db.MIGRATIONS", bad):
            with self.assertRaises(DatabaseError):
                await db.connect()
        # connection was released and nothing half-applied remained
        self.assertFalse(db.connection.connected)
        import sqlite3
        con = sqlite3.connect(self.dir / "m.db")
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        rows = con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        con.close()
        self.assertNotIn("ok_t", tables)
        self.assertEqual(rows, 0)

    async def test_reader_never_sees_uncommitted_rows(self) -> None:
        db = await self.open_db()
        inside = asyncio.Event()
        release = asyncio.Event()

        async def writer() -> None:
            async with db.connection.write() as conn:
                await conn.execute(
                    "INSERT INTO kv (key, value) VALUES ('k', 'half')")
                inside.set()
                await release.wait()
                raise RuntimeError("abort")

        task = asyncio.create_task(writer())
        await inside.wait()
        reader = asyncio.create_task(db.connection.fetch_all("SELECT * FROM kv"))
        await asyncio.sleep(0.05)
        self.assertFalse(reader.done())  # waits for the transaction
        release.set()
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(await reader, [])  # rolled back, never visible

    async def test_task_can_read_and_nest_inside_own_transaction(self) -> None:
        db = await self.open_db()
        async with db.connection.write() as conn:
            await conn.execute("INSERT INTO kv (key, value) VALUES ('a','1')")
            rows = await db.connection.fetch_all("SELECT * FROM kv")  # no deadlock
            self.assertEqual(len(rows), 1)
            async with db.connection.write() as inner:
                await inner.execute("INSERT INTO kv (key, value) VALUES ('b','2')")
        self.assertEqual(len(await db.connection.fetch_all("SELECT * FROM kv")), 2)

    async def test_close_waits_for_inflight_write(self) -> None:
        db = await self.open_db()
        started = asyncio.Event()

        async def slow_write() -> None:
            async with db.connection.write() as conn:
                await conn.execute("INSERT INTO kv (key, value) VALUES ('k','v')")
                started.set()
                await asyncio.sleep(0.2)

        task = asyncio.create_task(slow_write())
        await started.wait()
        await db.close()           # must wait, not cut the transaction off
        await task
        db2 = await self.open_db("t.db")
        self.assertEqual(len(await db2.connection.fetch_all("SELECT * FROM kv")), 1)

    async def test_delete_account_removes_credentials_and_cooldowns(self) -> None:
        db = await self.open_db()
        await db.accounts.add("a")
        await db.accounts.add("b")
        for acct in ("a", "b"):
            await db.credentials.store(acct, b"cipher-" + acct.encode())
            await db.cooldowns.upsert(account=acct, plugin="p", cooldown_key="k",
                                      started_at=1.0, expires_at=2e12)
        self.assertTrue(await db.accounts.delete("a"))
        self.assertIsNone(await db.credentials.load("a"))
        self.assertEqual(await db.cooldowns.active_for("a"), [])
        self.assertEqual(await db.credentials.load("b"), b"cipher-b")
        self.assertEqual(len(await db.cooldowns.active_for("b")), 1)

    async def test_corrupt_cooldown_metadata_does_not_crash_listing(self) -> None:
        db = await self.open_db()
        await db.cooldowns.upsert(account="a", plugin="p", cooldown_key="good",
                                  started_at=1.0, expires_at=2e12,
                                  metadata={"x": 1})
        await db.cooldowns.upsert(account="a", plugin="p", cooldown_key="bad",
                                  started_at=1.0, expires_at=2e12)
        await db.connection.execute(
            "UPDATE cooldowns SET metadata_json='{broken' WHERE cooldown_key='bad'")
        rows = await db.cooldowns.active_for("a")
        by_key = {r["cooldown_key"]: r for r in rows}
        self.assertEqual(by_key["good"]["metadata"], {"x": 1})
        self.assertEqual(by_key["bad"]["metadata"], {})

    async def test_cooldown_restore_single_query_all_accounts(self) -> None:
        db = await self.open_db()
        for acct in ("a", "b", "c"):
            await db.cooldowns.upsert(account=acct, plugin="p", cooldown_key="k",
                                      started_at=1.0, expires_at=2e12)
        await db.cooldowns.upsert(account="a", plugin="p", cooldown_key="old",
                                  started_at=1.0, expires_at=5.0)
        svc = CooldownService(db.cooldowns)
        self.assertEqual(await svc.restore(), {"restored": 3, "expired_cleared": 1})
        self.assertTrue(await svc.is_active("b", "p", "k"))

    async def test_cooldown_rejects_non_finite_duration(self) -> None:
        svc = CooldownService(None)
        for bad in (math.inf, math.nan, "abc"):
            with self.assertRaises(ValueError):
                await svc.set("a", "p", "k", bad)
        await svc.set("a", "p", "k", 5)  # normal path still fine

    async def test_add_many_one_batch_prunes_once_and_keeps_newest(self) -> None:
        db = Database(self.dir / "p.db", log_keep=5, audit_keep=5)
        await db.connect()
        self.addAsyncCleanup(db.close)
        n = await db.logs.add_many(
            [{"message": f"m{i}", "severity": "info", "source": "s"}
             for i in range(12)])
        self.assertEqual(n, 12)
        rows = await db.logs.recent(100)
        self.assertEqual([r["message"] for r in rows], [f"m{i}" for i in range(7, 12)])
        self.assertEqual(rows[0]["severity"], "INFO")
        await db.audit.add_many([{"account": "a", "content": str(i)} for i in range(9)])
        self.assertEqual(len(await db.audit.recent(100)), 5)


class TestTaskIdsSurviveRestart(_TmpCase):
    async def _boot(self, db_name: str, restore: bool, n: int) -> list:
        from core.task_scheduler import TaskScheduler
        from tests.unit.test_task_scheduler import (
            FakeAudit, FakeLimiter, FakeLogger, FakeSender)

        db = Database(self.dir / db_name)
        await db.connect()
        sched = TaskScheduler(EventBus(), FakeLogger(), FakeLimiter(),
                              FakeSender(), FakeAudit(), default_timeout=5.0,
                              history_repo=db.tasks)
        if restore:
            await sched.restore_sequence()
        sched.start()
        for _ in range(n):
            sched.submit("alpha", "p", "message", channel_id=1, content="x")
        await sched.stop(drain=True, timeout=5.0)
        ids = [r["id"] for r in await db.tasks.recent(100)]
        await db.close()
        return ids

    async def test_second_boot_archives_its_tasks(self) -> None:
        first = await self._boot("r.db", restore=True, n=3)
        self.assertEqual(first, [1, 2, 3])
        second = await self._boot("r.db", restore=True, n=3)
        # the restarted process archived ALL of its tasks, ids continuing
        self.assertEqual(second, [1, 2, 3, 4, 5, 6])

    async def test_without_restore_the_old_collision_is_real(self) -> None:
        """Documents the defect restore_sequence() fixes."""
        await self._boot("c.db", restore=False, n=3)
        again = await self._boot("c.db", restore=False, n=3)
        self.assertEqual(again, [1, 2, 3])  # new tasks silently NOT archived


class TestCancellationHelper(unittest.IsolatedAsyncioTestCase):
    async def test_check_does_not_consume_cancellation(self) -> None:
        seen = {}

        async def victim() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                before = task.cancelling()
                seen["pending"] = outer_cancel_pending()
                seen["unchanged"] = task.cancelling() == before
                raise

        task = asyncio.create_task(victim())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(seen["pending"])
        self.assertTrue(seen["unchanged"])  # old idiom decremented it

    async def test_internal_handler_cancel_is_isolated_not_propagated(self) -> None:
        bus = EventBus()

        async def selfcancel(event) -> None:  # noqa: ANN001
            raise asyncio.CancelledError()

        got = []
        bus.subscribe(Event, selfcancel)
        bus.subscribe(Event, lambda e: got.append(1))
        results = await bus.publish(Event())
        self.assertEqual(got, [1])  # later handlers still ran
        self.assertIsInstance(results[0], asyncio.CancelledError)


class TestPersistQueue(unittest.IsolatedAsyncioTestCase):
    class Repo:
        def __init__(self, fail_batches=False, bad=None) -> None:
            self.batches, self.singles = [], []
            self.fail_batches, self.bad = fail_batches, bad

        async def add_many(self, items) -> int:
            if self.fail_batches:
                raise RuntimeError("batch down")
            self.batches.append(list(items))
            return len(items)

        async def add(self, item) -> None:
            if item.get("n") == self.bad:
                raise RuntimeError("bad row")
            self.singles.append(item)

    async def test_batches_and_preserves_order_on_stop(self) -> None:
        repo = self.Repo()
        pq = PersistQueue("t")
        await pq.start(repo, batch=10, interval=0.01)
        for i in range(35):
            pq.put({"n": i})
        await pq.stop(2.0)
        flat = [x["n"] for b in repo.batches for x in b]
        self.assertEqual(flat, list(range(35)))
        self.assertTrue(all(len(b) <= 10 for b in repo.batches))
        self.assertEqual(pq.stats(), {"db_failures": 0, "dropped": 0})

    async def test_bad_row_isolated_when_batch_fails(self) -> None:
        repo = self.Repo(fail_batches=True, bad=2)
        pq = PersistQueue("t")
        await pq.start(repo, batch=10, interval=0.01)
        for i in range(5):
            pq.put({"n": i})
        await pq.stop(2.0)
        self.assertEqual([x["n"] for x in repo.singles], [0, 1, 3, 4])
        self.assertEqual(pq.stats()["db_failures"], 1)

    async def test_cancelled_stop_leaves_queue_restartable(self) -> None:
        repo = self.Repo()
        pq = PersistQueue("t")
        await pq.start(repo, batch=10, interval=0.01)
        stopper = asyncio.ensure_future(pq.stop(2.0))
        await asyncio.sleep(0)          # parked inside stop()'s first wait
        stopper.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stopper
        self.assertFalse(pq._stopping)  # used to stay True forever
        self.assertTrue(pq._draining.done())
        await asyncio.wait_for(pq.stop(1.0), 2.0)   # must not hang or join a dead future

    async def test_overflow_counted(self) -> None:
        pq = PersistQueue("t", max_inflight=3)
        for i in range(5):
            pq.put({"n": i})
        self.assertEqual(pq.stats()["dropped"], 2)

    async def test_log_service_uses_batches(self) -> None:
        repo = self.Repo()
        logs = LogService()
        await logs.start_persistence(repo, batch=20, interval=0.01)
        for i in range(50):
            logs.record("INFO", "t", f"m{i}")
        await logs.stop_persistence()
        self.assertEqual(sum(len(b) for b in repo.batches), 50)


class TestLoggingDetails(unittest.TestCase):
    def test_exception_logger_includes_location(self) -> None:
        logs = LogService()
        log = ServiceLogger(logs, "x")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("failed")
        msg = logs.tail(1)[0]["message"]
        self.assertIn("ValueError: boom @ test_hardening_phase1.py:", msg)

    def test_tail_filters_and_limits(self) -> None:
        logs = LogService()
        for i in range(10):
            logs.record("INFO" if i % 2 else "ERROR", "s", f"m{i}", account="a")
        out = logs.tail(3, severity="error")
        self.assertEqual([e["message"] for e in out], ["m4", "m6", "m8"])
        self.assertEqual(logs.tail(0), [])


class TestCredentials(_TmpCase):
    def test_single_credentials_error_class(self) -> None:
        self.assertIs(CredentialsError, CoreCredentialsError)

    def test_env_bom_and_inline_comments(self) -> None:
        env = Credentials.parse_env(
            '\ufeffPRIMARY_BOT_ID=123 # the bot\nTOKEN=ab#cd\nQ="x # y"\n')
        self.assertEqual(env, {"PRIMARY_BOT_ID": "123", "TOKEN": "ab#cd", "Q": "x # y"})
        self.assertEqual(Credentials(env).primary_bot_id, 123)

    def test_owner_ids_parsed_once(self) -> None:
        creds = Credentials({"OWNER_USER_IDS": "1, 2;3"})
        first = creds.owner_user_ids
        self.assertEqual(first, frozenset({1, 2, 3}))
        self.assertIs(creds.owner_user_ids, first)

    async def test_key_file_created_private_and_reused(self) -> None:
        path = self.dir / "k" / "credentials.key"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LZ_MASTER_KEY", None)
            svc = CredentialService(None, key_path=path)
            _ = svc.fernet
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o077, 0)
            key = path.read_text()
            self.assertEqual(CredentialService(None, key_path=path)
                             ._load_or_create_key().decode(), key)

    async def test_corrupt_key_is_credentials_error(self) -> None:
        path = self.dir / "bad.key"
        path.write_text("not-a-fernet-key")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LZ_MASTER_KEY", None)
            with self.assertRaises(CredentialsError):
                _ = CredentialService(None, key_path=path).fernet


class TestBootstrapRobustness(_TmpCase):
    def _config(self) -> Path:
        cfg = self.dir / "cfg"
        cfg.mkdir()
        return cfg

    async def test_failed_build_releases_everything(self) -> None:
        db = Database(self.dir / "b.db")
        creds = Credentials.from_values({"PRIMARY_BOT_ID": "1"})

        # a mid-build failure AFTER some cleanups registered (a malformed
        # rate_limits spec used to be the trigger, but it is now — by
        # design — a skip-and-warn, so fail a later boot step instead)
        async def boom(*args, **kwargs):
            raise ValueError("mid-build failure")

        with mock.patch("core.bootstrap._seed_config_defaults", boom):
            with self.assertRaises(ValueError):
                await build(config_dir=str(self._config()),
                            plugin_dir=str(FIXTURE_PLUGINS),
                            client_factory=lambda n, t: None, credentials=creds,
                            database=db, quiet=True)
        self.assertFalse(db.connection.connected)
        root = logging.getLogger("libzent")
        from core.logging_service import StdlibBridge
        self.assertFalse([h for h in root.handlers if isinstance(h, StdlibBridge)])

    async def test_malformed_policy_spec_no_longer_bricks_boot(self) -> None:
        db = Database(self.dir / "m.db")
        creds = Credentials.from_values({"PRIMARY_BOT_ID": "1"})
        cfg = self.dir / "cfg-mal"
        cfg.mkdir()
        (cfg / "rate_limits.json").write_text(json.dumps(
            {"global": {"message": {"interval": "oops"}}}))
        fw = await build(config_dir=str(cfg),
                         plugin_dir=str(FIXTURE_PLUGINS),
                         client_factory=lambda n, t: None, credentials=creds,
                         database=db, quiet=True)
        await fw.shutdown()
        self.assertFalse(db.connection.connected)

    async def test_shutdown_survives_failing_step_and_closes_db(self) -> None:
        db = Database(self.dir / "s.db")
        creds = Credentials.from_values({"PRIMARY_BOT_ID": "1"})
        fw = await build(config_dir=str(self._config()),
                         plugin_dir=str(FIXTURE_PLUGINS),
                         client_factory=lambda n, t: None, credentials=creds,
                         database=db, quiet=True)

        async def boom() -> None:
            raise RuntimeError("stop_all exploded")

        fw.accounts.stop_all = boom
        await fw.shutdown()
        self.assertFalse(db.connection.connected)
        await fw.shutdown()  # idempotent

    async def test_boot_logs_are_persisted(self) -> None:
        db = Database(self.dir / "l.db")
        creds = Credentials.from_values({"PRIMARY_BOT_ID": "1"})
        fw = await build(config_dir=str(self._config()),
                         plugin_dir=str(FIXTURE_PLUGINS),
                         client_factory=lambda n, t: None, credentials=creds,
                         database=db, quiet=True)
        await fw.logs.stop_persistence()
        rows = await db.logs.recent(500)
        await fw.shutdown()
        self.assertGreater(len(rows), 0)


class TestConfigValidation(_TmpCase):
    def test_bad_policy_shapes_fail_loud_with_message(self) -> None:
        from core.config import load_config
        cfg = self.dir / "c"
        cfg.mkdir()
        (cfg / "rate_limits.json").write_text('{"global": [1,2]}')
        with self.assertRaisesRegex(ValueError, "must be JSON objects"):
            load_config(cfg)
        (cfg / "rate_limits.json").write_text('{"lane": {"prefixrate": "x"}}')
        with self.assertRaisesRegex(ValueError, "prefixrate"):
            load_config(cfg)

    def test_bom_json_tolerated(self) -> None:
        from core.config import load_config
        cfg = self.dir / "c2"
        cfg.mkdir()
        (cfg / "global_settings.json").write_bytes(b'\xef\xbb\xbf{"p": {"a": 1}}')
        self.assertEqual(load_config(cfg).global_settings, {"p": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
