"""Foundation tests: async DB layer, credentials, cooldowns, plugin state.

Covers spec §35/§36/§38/§39: clean connect/migrate on :memory: and file,
transaction commit/rollback, constraint failures, busy handling, the
credential encrypt->persist->restart->decrypt cycle + no-leak checks,
cooldown restart math (absolute expiry), account isolation, and the
scoped plugin-state API.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from core.cooldowns import CooldownService
from core.credential_service import CredentialService
from core.plugin_state import PluginStateService
from database.db import Database


class AsyncTestCase(unittest.IsolatedAsyncioTestCase):
    def tmp_db(self) -> Database:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-db-")
        self.addCleanup(self.tmp.cleanup)
        return Database(Path(self.tmp.name) / "t.db")


class TestAsyncDatabase(AsyncTestCase):
    async def test_connect_migrate_idempotent(self) -> None:
        db = self.tmp_db()
        applied = await db.connect()
        self.assertGreaterEqual(applied, 3)  # all migrations on a fresh file
        self.assertEqual(await db.connect(), 0)  # idempotent
        await db.close()

    async def test_transaction_commit_and_rollback(self) -> None:
        db = self.tmp_db()
        await db.connect()
        await db.kv.set("keep", "1")
        with self.assertRaises(RuntimeError):
            async with db.connection.write() as conn:
                await conn.execute(
                    "INSERT INTO kv (key, value) VALUES ('both','x')")
                raise RuntimeError("boom")
        self.assertIsNone(await db.kv.get("both"))          # rolled back
        self.assertEqual(await db.kv.get("keep"), "1")      # untouched
        await db.close()

    async def test_constraint_failure_is_clean(self) -> None:
        db = self.tmp_db()
        await db.connect()
        await db.accounts.add("alpha")
        from database.connection import DatabaseError
        with self.assertRaises(DatabaseError):
            await db.accounts.add("alpha")  # PK violation -> wrapped error
        self.assertEqual(len(await db.accounts.list()), 1)
        await db.close()

    async def test_concurrent_writes_serialize_without_busy(self) -> None:
        """20 parallel writers on ONE connection: ordered, no SQLITE_BUSY,
        and the event loop stays responsive (aiosqlite off-thread)."""
        db = self.tmp_db()
        await db.connect()
        loop = asyncio.get_running_loop()

        async def writer(i: int) -> None:
            await db.kv.set(f"k{i}", str(i))

        t0 = loop.time()
        tick = loop.create_task(self._keep_ticking(loop))
        await asyncio.gather(*(writer(i) for i in range(20)))
        tick.cancel()
        stalled = max(0.0, (loop.time() - t0) - 0.0)
        self.assertLess(stalled, 5.0)  # generous CI bound
        self.assertEqual(await db.kv.get("k19"), "19")
        await db.close()

    async def _keep_ticking(self, loop) -> None:
        try:
            while True:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            return

    async def test_memory_database(self) -> None:
        db = Database(":memory:")
        await db.connect()
        await db.accounts.add("alpha")
        self.assertEqual((await db.accounts.list())[0].name, "alpha")
        await db.close()

    async def test_connect_required_before_use(self) -> None:
        db = self.tmp_db()
        from database.connection import DatabaseError
        with self.assertRaises(DatabaseError):
            await db.kv.set("x", "1")

    async def test_close_idempotent_and_double_close(self) -> None:
        db = self.tmp_db()
        await db.connect()
        await asyncio.gather(db.close(), db.close())
        await db.close()
        self.assertFalse(db.connection.connected)

    async def test_thread_bridge_via_run_coroutine_threadsafe(self) -> None:
        """Ops submitted from a thread with NO running loop work when
        routed to the owning loop (the supported bridge)."""
        db = self.tmp_db()
        await db.connect()
        loop = asyncio.get_running_loop()
        failures: list = []

        def worker() -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    db.kv.set("t", "bridge"), loop).result(5)
                got = asyncio.run_coroutine_threadsafe(
                    db.kv.get("t"), loop).result(5)
                if got != "bridge":
                    failures.append(f"readback {got!r}")
            except BaseException as exc:
                failures.append(exc)

        await loop.run_in_executor(None, worker)
        self.assertEqual(failures, [])
        self.assertEqual(await db.kv.get("t"), "bridge")
        await db.close()

    async def test_reuse_after_close_on_new_event_loop(self) -> None:
        """Round 3: close() reborn the loop-bound locks (guard, connect
        lock, migrate lock); the SAME Database service reopens cleanly
        on a different event loop and reads the same file."""
        db = self.tmp_db()
        await db.connect()
        await db.kv.set("k", "v1")
        await db.close()

        readback: list = []

        def second_loop() -> None:
            async def go() -> None:
                await db.connect()
                readback.append(await db.kv.get("k"))
                await db.close()
            asyncio.run(go())

        await asyncio.get_running_loop().run_in_executor(None, second_loop)
        self.assertEqual(readback, ["v1"])

    async def test_same_database_on_second_event_loop_rejected(self) -> None:
        """Two loops driving one connection corrupt the lock/sqlite
        session; the guard must fail loud with a clean DatabaseError."""
        from core.errors import DatabaseError
        db = self.tmp_db()
        await db.connect()
        await db.kv.set("bind", "1")

        def other_loop() -> None:
            async def go() -> None:
                await db.kv.set("x", "1")
            asyncio.run(go())

        with self.assertRaises(DatabaseError):
            await asyncio.get_running_loop().run_in_executor(None, other_loop)
        self.assertEqual(await db.kv.get("bind"), "1")
        await db.close()


class TestCredentialService(AsyncTestCase):
    async def test_encrypt_persist_restart_decrypt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-cred-") as tmp:
            keyfile = Path(tmp) / "credentials.key"
            db_path = Path(tmp) / "t.db"
            db = Database(db_path)
            await db.connect()
            service = CredentialService(db.credentials, key_path=keyfile)
            await service.store("alpha", "super-secret-token")
            self.assertEqual(await service.resolve("alpha"), "super-secret-token")
            await db.close()

            # "restart": fresh service, SAME key file + SAME database
            db2 = Database(db_path)
            await db2.connect()
            service2 = CredentialService(db2.credentials, key_path=keyfile)
            self.assertEqual(await service2.resolve("alpha"), "super-secret-token")
            await db2.close()

    async def test_no_plaintext_leak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-cred-") as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            service = CredentialService(db.credentials,
                                        key_path=Path(tmp) / "k.key")
            await service.store("alpha", "PLAINTEXT-MARKER-XYZ")
            await db.close()
            raw = (Path(tmp) / "t.db").read_bytes()
            self.assertNotIn(b"PLAINTEXT-MARKER-XYZ", raw)  # at rest
            self.assertNotIn("PLAINTEXT-MARKER-XYZ", repr(service))  # repr
            self.assertNotIn("PLAINTEXT-MARKER-XYZ",
                             str(service.status("alpha")))  # status words

    async def test_precedence_and_import(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-cred-") as tmp:
            db = Database(":memory:")
            await db.connect()
            service = CredentialService(db.credentials,
                                        key_path=Path(tmp) / "k1.key")
            # missing -> None (NO env fallback exists anymore)
            self.assertIsNone(await service.resolve("alpha"))
            self.assertEqual(service.status("alpha"), "missing")
            # explicit import stores it
            report = await service.import_from_env(
                {"alpha": "env-tok", "beta": ""})
            self.assertEqual(report["imported"], ["alpha"])
            self.assertEqual(await service.resolve("alpha"), "env-tok")
            # re-import skips existing
            report2 = await service.import_from_env({"alpha": "other"})
            self.assertEqual(report2["skipped"], ["alpha"])
            self.assertEqual(await service.resolve("alpha"), "env-tok")
            await db.close()

    async def test_delete_and_bad_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-cred-") as tmp:
            db = Database(":memory:")
            await db.connect()
            service = CredentialService(db.credentials,
                                        key_path=Path(tmp) / "k2.key")
            await service.store("alpha", "tok")
            self.assertTrue(await service.delete("alpha"))
            self.assertIsNone(await service.resolve("alpha"))
            with self.assertRaises(Exception):
                await service.store("alpha", "")  # empty refused
            await db.close()


class TestCooldownService(AsyncTestCase):
    async def test_restart_restores_remaining_not_full(self) -> None:
        """45-min cooldown, restart after 10 -> ~35 remaining (spec §23)."""
        clock = {"now": 1000.0}
        db = Database(":memory:")
        await db.connect()
        service = CooldownService(db.cooldowns, time_fn=lambda: clock["now"])
        await service.set("alpha", "roll", "roulette", 45 * 60,
                          source="discord_message")
        clock["now"] += 10 * 60  # framework "restarts" 10 minutes later
        service2 = CooldownService(db.cooldowns, time_fn=lambda: clock["now"])
        report = await service2.restore()
        self.assertEqual(report["restored"], 1)
        remaining = await service2.remaining("alpha", "roll", "roulette")
        self.assertAlmostEqual(remaining, 35 * 60, delta=1)
        await db.close()

    async def test_expired_cleared_on_restore(self) -> None:
        clock = {"now": 0.0}
        db = Database(":memory:")
        await db.connect()
        service = CooldownService(db.cooldowns, time_fn=lambda: clock["now"])
        await service.set("a", "roll", "short", 30)
        clock["now"] += 60
        service2 = CooldownService(db.cooldowns, time_fn=lambda: clock["now"])
        report = await service2.restore()
        self.assertEqual(report["expired_cleared"], 1)
        self.assertFalse(await service2.is_active("a", "roll", "short"))
        await db.close()

    async def test_account_isolation(self) -> None:
        db = Database(":memory:")
        await db.connect()
        service = CooldownService(db.cooldowns)
        await service.set("alpha", "roll", "roulette", 500)
        self.assertFalse(await service.is_active("beta", "roll", "roulette"))
        self.assertEqual(
            len(await service.active_for("beta")), 0)
        self.assertEqual(
            len(await service.active_for("alpha")), 1)
        cleared = await service.delete_account("alpha")
        self.assertEqual(cleared, 1)
        self.assertFalse(await service.is_active("alpha", "roll", "roulette"))
        await db.close()

    async def test_set_replaces_and_clear(self) -> None:
        db = Database(":memory:")
        await db.connect()
        service = CooldownService(db.cooldowns)
        await service.set("a", "p", "k", 100)
        await service.set("a", "p", "k", 900)  # replace
        self.assertGreater(await service.remaining("a", "p", "k"), 500)
        self.assertTrue(await service.clear("a", "p", "k"))
        self.assertFalse(await service.is_active("a", "p", "k"))
        await db.close()


class TestPluginStateService(AsyncTestCase):
    async def test_scopes_roundtrip_and_isolation(self) -> None:
        db = Database(":memory:")
        await db.connect()
        state = PluginStateService(db.kv)
        await state.set_global("g", {"n": 1})
        await state.set_account("alpha", "counter", 5)
        await state.set_plugin("roll", "mode", "auto")
        await state.set_pair("alpha", "roll", "streak", 3)

        self.assertEqual(await state.get_global("g"), {"n": 1})
        self.assertEqual(await state.get_account("alpha", "counter"), 5)
        self.assertEqual(await state.get_plugin("roll", "mode"), "auto")
        self.assertEqual(await state.get_pair("alpha", "roll", "streak"), 3)

        # isolation: account scope never leaks to beta
        self.assertIsNone(await state.get_account("beta", "counter"))
        # scope keys are namespaced: raw kv never collides with plain keys
        await db.kv.set("counter", "plain")
        self.assertNotEqual(await state.get_account("alpha", "counter"), "plain")
        # deletion
        self.assertTrue(await state.delete_pair("alpha", "roll", "streak"))
        self.assertIsNone(await state.get_pair("alpha", "roll", "streak"))
        await db.close()

    async def test_restart_persistence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-state-") as tmp:
            path = Path(tmp) / "t.db"
            db = Database(path)
            await db.connect()
            state = PluginStateService(db.kv)
            await state.set_pair("alpha", "roll", "wins", 42)
            await db.close()
            db2 = Database(path)
            await db2.connect()
            state2 = PluginStateService(db2.kv)
            self.assertEqual(await state2.get_pair("alpha", "roll", "wins"), 42)
            await db2.close()

    async def test_invalid_keys_rejected(self) -> None:
        db = Database(":memory:")
        await db.connect()
        state = PluginStateService(db.kv)
        from core.errors import PluginError
        with self.assertRaises(PluginError):
            state._key("global", None, "")
        await db.close()


if __name__ == "__main__":
    unittest.main()
