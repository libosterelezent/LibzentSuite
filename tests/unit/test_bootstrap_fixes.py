"""Regression tests for the audited fixes (bootstrap/credentials/access/
cooldowns): await-race guards, fail-closed REMOTE_CONTROL, comment
parsing, symlink rejection, concurrent access-manager mutations and
malformed rate-limit specs must not brick the boot."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from core.access_manager import AccessManager
from core.bootstrap import Framework, _seed_config_defaults
from core.config import ProjectConfig
from core.cooldowns import CooldownService
from core.credential_service import CredentialService
from core.credentials import Credentials, CredentialsError
from core.event_bus import EventBus
from core.logging_service import LogService, ServiceLogger
from core.rate_limit_manager import RateLimitManager


class _TmpCase(unittest.IsolatedAsyncioTestCase):
    """Temp dir + a guaranteed-absent LZ_MASTER_KEY (repo style)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-fixes-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("LZ_MASTER_KEY", None)
        self.addCleanup(self._env.stop)


# ------------------------------------------------------ store/delete race
class RaceRepo:
    """Credential repo double whose store() PARKS after the repo write."""

    def __init__(self) -> None:
        self.rows: dict[str, bytes] = {}
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def accounts_with_credential(self) -> list[str]:
        return list(self.rows)

    async def store(self, account: str, ciphertext: bytes) -> None:
        self.rows[account] = ciphertext
        self.parked.set()
        await self.release.wait()

    async def load(self, account: str) -> bytes | None:
        return self.rows.get(account)

    async def delete(self, account: str) -> bool:
        existed = account in self.rows
        self.rows.pop(account, None)
        return existed


class TestStoreDeleteRace(_TmpCase):
    async def test_delete_during_store_repo_await_leaves_memory_clean(self) -> None:
        repo = RaceRepo()
        svc = CredentialService(repo, key_path=self.dir / "k.key")
        task = asyncio.create_task(svc.store("alpha", "tok"))
        await asyncio.wait_for(repo.parked.wait(), timeout=2)
        await svc.delete("alpha")  # racing delete completes mid-store
        repo.release.set()  # store() resumes after the repo write
        await task
        self.assertNotIn("alpha", svc._memory)
        self.assertNotIn("alpha", svc._runtime_sources)
        self.assertNotIn("alpha", svc._db_accounts)
        self.assertIsNone(await svc.resolve("alpha"))

    async def test_store_without_race_caches_normally(self) -> None:
        repo = RaceRepo()
        repo.release.set()  # no parking: the guard must not break the path
        svc = CredentialService(repo, key_path=self.dir / "k.key")
        await svc.store("alpha", "tok")
        self.assertEqual(svc._memory.get("alpha"), "tok")
        self.assertEqual(await svc.resolve("alpha"), "tok")


# -------------------------------------------------------- parse_env fixes
class TestParseEnvComments(unittest.TestCase):
    def test_empty_value_with_trailing_comment(self) -> None:
        self.assertEqual(
            Credentials.parse_env("DASHBOARD_TOKEN= # disabled\n"), {"DASHBOARD_TOKEN": ""}
        )

    def test_comment_forms(self) -> None:
        env = Credentials.parse_env('A=1 # c\nB=ab#cd\nC="x # y"\nD=\t# tabbed\nE= #disabled\n')
        self.assertEqual(env, {"A": "1", "B": "ab#cd", "C": "x # y", "D": "", "E": ""})


class TestFromFileSymlink(unittest.TestCase):
    def test_symlinked_env_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-link-") as tmp:
            real = Path(tmp) / "real.env"
            real.write_text("A=1\n")
            link = Path(tmp) / "link.env"
            link.symlink_to(real)
            with self.assertRaises(CredentialsError):
                Credentials.from_file(link)

    def test_real_env_still_loads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-link-") as tmp:
            real = Path(tmp) / ".env"
            real.write_text("A=1\n")
            self.assertEqual(Credentials.from_file(real).get("A"), "1")


class TestPrimaryBotIdTruncation(unittest.TestCase):
    def test_malformed_value_truncated_in_error(self) -> None:
        creds = Credentials({"PRIMARY_BOT_ID": "x" * 100})
        with self.assertRaises(CredentialsError) as ctx:
            _ = creds.primary_bot_id
        self.assertIn("...", str(ctx.exception))
        self.assertNotIn("x" * 41, str(ctx.exception))


# ------------------------------------------- access-manager concurrency
class SlowKv:
    """kv double that YIELDS on every await so tasks really interleave."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        await asyncio.sleep(0)
        return self.rows.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        await asyncio.sleep(0)
        self.rows[key] = value

    async def delete(self, key: str) -> bool:
        await asyncio.sleep(0)
        return self.rows.pop(key, None) is not None

    async def prefix(self, p: str) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {k: v for k, v in self.rows.items() if k.startswith(p)}


class TestAccessManagerConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_admin_add_remove_pairs(self) -> None:
        kv: Any = SlowKv()  # duck-typed KvRepository double (see its seam)
        am = AccessManager(kv)
        await am.set_admin_ids(list(range(0, 40, 2)))  # evens preloaded
        tasks = []
        for i in range(20):
            tasks.append(asyncio.create_task(am.add_admin(i * 2 + 1)))
            tasks.append(asyncio.create_task(am.remove_admin(i * 2)))
        await asyncio.gather(*tasks)
        self.assertEqual(await am.admin_ids(), list(range(1, 40, 2)))

    async def test_concurrent_allowed_add_remove_pairs(self) -> None:
        kv: Any = SlowKv()  # duck-typed KvRepository double (see its seam)
        am = AccessManager(kv)
        await am.set_allowed_user_ids("alpha", list(range(0, 40, 2)))
        tasks = []
        for i in range(20):
            tasks.append(asyncio.create_task(am.add_allowed("alpha", i * 2 + 1)))
            tasks.append(asyncio.create_task(am.remove_allowed("alpha", i * 2)))
        await asyncio.gather(*tasks)
        self.assertEqual(await am.allowed_user_ids("alpha"), list(range(1, 40, 2)))


# -------------------------------------------- malformed rate_limits spec
class TestSeedConfigDefaultsMalformed(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_interval_skips_without_raising(self) -> None:
        limiter = RateLimitManager(EventBus(), ServiceLogger(LogService(), "rl"), None)

        class _Policies:
            async def list(self) -> list[Any]:
                return []

        class _Db:
            policies = _Policies()

        config = ProjectConfig(
            policies={
                "global": {"message": {"interval": "fast"}},
                "accounts": {"alpha": {"dm": {"interval": [1]}}},
            }
        )
        await _seed_config_defaults(limiter, config, _Db())  # must not raise

    async def test_below_floor_default_is_clamped_not_skipped(self) -> None:
        limiter = RateLimitManager(EventBus(), ServiceLogger(LogService(), "rl"), None)
        limiter.set_floor(5.0)

        class _Policies:
            async def list(self) -> list[Any]:
                return []

        class _Db:
            policies = _Policies()

        config = ProjectConfig(policies={"global": {"message": {"interval": 2.6}}})
        await _seed_config_defaults(limiter, config, _Db())
        stored = [p for p in limiter.policies() if p["action"] == "message"]
        self.assertEqual([p["interval"] for p in stored], [5.0])


# ------------------------------------------------ cooldown set/clear race
class SlowCooldownRepo:
    """Cooldown repo double whose upsert() PARKS after the repo write."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def upsert(self, **kw: Any) -> None:
        key = (kw["account"], kw["plugin"], kw.get("profile", ""), kw["cooldown_key"])
        self.rows[key] = kw
        self.parked.set()
        await self.release.wait()

    async def get(
        self,
        account: str,
        plugin: str,
        cooldown_key: str,
        now: float | None = None,
        profile: str = "",
    ) -> dict[str, Any] | None:
        row = self.rows.get((account, plugin, profile, cooldown_key))
        if row is None or (now is not None and row["expires_at"] <= now):
            return None
        return dict(row)

    async def clear(self, account: str, plugin: str, cooldown_key: str, profile: str = "") -> bool:
        return self.rows.pop((account, plugin, profile, cooldown_key), None) is not None


class TestCooldownSetClearRace(_TmpCase):
    async def test_clear_during_set_upsert_await_does_not_resurrect(self) -> None:
        repo = SlowCooldownRepo()
        svc = CooldownService(repo)
        task = asyncio.create_task(svc.set("alpha", "p", "k", 60))
        await asyncio.wait_for(repo.parked.wait(), timeout=2)
        await svc.clear("alpha", "p", "k")  # racing clear completes mid-set
        repo.release.set()  # set() resumes after the upsert
        await task
        self.assertFalse(await svc.is_active("alpha", "p", "k"))

    async def test_set_without_race_mirrors(self) -> None:
        repo = SlowCooldownRepo()
        repo.release.set()
        svc = CooldownService(repo)
        await svc.set("alpha", "p", "k", 60)
        self.assertTrue(await svc.is_active("alpha", "p", "k"))

    async def test_zero_duration_replaces_previous_cooldown(self) -> None:
        from database.db import Database

        db = Database(":memory:")
        await db.connect()
        try:
            svc = CooldownService(db.cooldowns)
            await svc.set("alpha", "p", "k", 100)
            self.assertTrue(await svc.is_active("alpha", "p", "k"))
            await svc.set("alpha", "p", "k", 0)  # shorten to "now"
            self.assertFalse(await svc.is_active("alpha", "p", "k"))
        finally:
            await db.close()

    async def test_active_for_prefers_extended_db_row(self) -> None:
        from database.db import Database

        now = [1000.0]
        db = Database(":memory:")
        await db.connect()
        try:
            svc = CooldownService(db.cooldowns, time_fn=lambda: now[0])
            await svc.set("alpha", "p", "k", 10)
            # another process extends the same key in the database
            await db.cooldowns.upsert(
                account="alpha",
                plugin="p",
                cooldown_key="k",
                started_at=now[0],
                expires_at=now[0] + 100,
                source="x",
                reason="",
                metadata=None,
            )
            rows = await svc.active_for("alpha")
            self.assertEqual([r["expires_at"] for r in rows], [1100.0])
        finally:
            await db.close()


if __name__ == "__main__":
    unittest.main()


class StderrClaimWiringTests(unittest.IsolatedAsyncioTestCase):
    """Review pass-4 #1/#2: a BORROWING build must release its claim, and
    an early boot failure must not leak one (the claim is registered
    before any fallible step)."""

    def setUp(self) -> None:
        import logging
        import tempfile
        from pathlib import Path

        from core import logging_service as m

        self._m = m
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-claims-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # snapshot the process-global state
        self.users = m._STDERR_USERS
        self.listener = m._STDERR_LISTENER
        self.configured = m._STDERR_CONFIGURED
        lg = logging.getLogger("libzent")
        self.handlers = list(lg.handlers)
        self.propagate = lg.propagate

    def _restore(self) -> None:
        import logging

        # re-imported (not via self._m): the attribute loses the specific
        # module type, and mypy then rejects the private-name writes below
        from core import logging_service as m

        m._STDERR_USERS = self.users
        m._STDERR_LISTENER = self.listener
        m._STDERR_CONFIGURED = self.configured
        lg = logging.getLogger("libzent")
        lg.handlers[:] = self.handlers
        lg.propagate = self.propagate

    async def _build(
        self, *, env_file: Path | None = None, config_dir: Path | None = None
    ) -> Framework:
        from core import bootstrap

        env = str(env_file or self.root / "absent.env")
        cfg = str(config_dir or self.root / "config")
        return await bootstrap.build(
            env_file=env,
            config_dir=cfg,
            db_path=str(self.root / "claims.db"),
            plugin_dir="tests/fixtures/plugins",
            quiet=False,
        )

    async def test_borrowing_build_releases_its_claim(self) -> None:
        try:
            m = self._m
            m.stop_stderr_pipeline()  # known idle floor
            fw1 = await self._build()  # CREATOR
            self.assertTrue(m.stderr_pipeline_active())
            fw2 = await self._build()  # BORROWER (own db below)
            self.assertTrue(m.stderr_pipeline_active())
            await fw1.shutdown()  # owner gone first
            self.assertTrue(
                m.stderr_pipeline_active(), "the borrowing framework keeps the pipeline alive"
            )
            await fw2.shutdown()
            self.assertFalse(m.stderr_pipeline_active(), "the last claim stops the listener")
        finally:
            self._restore()

    async def test_early_failure_releases_the_claim(self) -> None:
        m = self._m
        m.stop_stderr_pipeline()
        # a symlinked .env is hard-rejected by Credentials.from_file —
        # a failure that lands AFTER the claim but BEFORE _assemble
        real_env = self.root / "real.env"
        real_env.write_text("DASHBOARD_TOKEN=x\n", encoding="utf-8")
        linked = self.root / "linked.env"
        linked.symlink_to(real_env)
        fw = None
        try:
            fw = await self._build(env_file=linked)
            self.fail("a symlinked env must fail the build")
        except Exception:
            pass
        finally:
            if fw is not None:  # never returned, but stay safe
                await fw.shutdown()
            self._restore()
        self.assertEqual(m._STDERR_USERS, 0, "the failed build released its claim")
        self.assertFalse(m.stderr_pipeline_active())


class PluginDataTeardownTests(unittest.IsolatedAsyncioTestCase):
    """PHASE 10 teardown: the plugin-data store is a SECOND aiosqlite
    connection beside the main database. A shutdown that leaves it open
    leaks a non-daemon worker thread, which hangs interpreter exit after
    the test summary (the CI run-110 hang)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-pd-shutdown-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    async def _build(self) -> Framework:
        from core import bootstrap

        return await bootstrap.build(
            env_file=str(self.root / "absent.env"),
            config_dir=str(self.root / "config"),
            db_path=str(self.root / "state" / "libzent.db"),
            plugin_dir="tests/fixtures/plugins",
            quiet=True,
        )

    async def test_shutdown_closes_the_plugin_data_store(self) -> None:
        fw = await self._build()
        store = fw.plugin_data
        self.assertIsNotNone(store)
        # the store is live through the shared facade before teardown
        await fw.api.accounts_add({"name": "alpha", "plugins": ["probe"]})
        await fw.api.accounts_data_put("alpha", "probe", "0", "0", {"v": 1})
        docs = await fw.api.accounts_data_list("alpha")
        self.assertEqual(len(docs), 1)
        await fw.shutdown()
        self.assertIsNone(
            store._conn,
            "shutdown must close the plugin-data store — an open "
            "aiosqlite connection keeps a non-daemon worker thread "
            "alive and hangs interpreter exit",
        )
