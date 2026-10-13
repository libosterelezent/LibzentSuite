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
from typing import Any, Dict, List, Optional
from unittest import mock

from core.access_manager import AccessManager
from core.bootstrap import _seed_config_defaults
from core.config import ProjectConfig
from core.cooldowns import CooldownService
from core.credential_service import CredentialService
from core.credentials import Credentials, CredentialsError
from core.event_bus import EventBus
from core.logging_service import LogService, ServiceLogger
from core.rate_limit_manager import RateLimitManager


class _TmpCase(unittest.IsolatedAsyncioTestCase):
    """Temp dir + a guaranteed-absent SM_MASTER_KEY (repo style)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-fixes-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SM_MASTER_KEY", None)
        self.addCleanup(self._env.stop)


# ------------------------------------------------------ store/delete race
class RaceRepo:
    """Credential repo double whose store() PARKS after the repo write."""

    def __init__(self) -> None:
        self.rows: Dict[str, bytes] = {}
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def accounts_with_credential(self) -> List[str]:
        return list(self.rows)

    async def store(self, account: str, ciphertext: bytes) -> None:
        self.rows[account] = ciphertext
        self.parked.set()
        await self.release.wait()

    async def load(self, account: str) -> Optional[bytes]:
        return self.rows.get(account)

    async def delete(self, account: str) -> bool:
        existed = account in self.rows
        self.rows.pop(account, None)
        return existed


class TestStoreDeleteRace(_TmpCase):
    async def test_delete_during_store_repo_await_leaves_memory_clean(self):
        repo = RaceRepo()
        svc = CredentialService(repo, key_path=self.dir / "k.key")
        task = asyncio.create_task(svc.store("alpha", "tok"))
        await asyncio.wait_for(repo.parked.wait(), timeout=2)
        await svc.delete("alpha")     # racing delete completes mid-store
        repo.release.set()            # store() resumes after the repo write
        await task
        self.assertNotIn("alpha", svc._memory)
        self.assertNotIn("alpha", svc._runtime_sources)
        self.assertNotIn("alpha", svc._db_accounts)
        self.assertIsNone(await svc.resolve("alpha"))

    async def test_store_without_race_caches_normally(self):
        repo = RaceRepo()
        repo.release.set()  # no parking: the guard must not break the path
        svc = CredentialService(repo, key_path=self.dir / "k.key")
        await svc.store("alpha", "tok")
        self.assertEqual(svc._memory.get("alpha"), "tok")
        self.assertEqual(await svc.resolve("alpha"), "tok")


# -------------------------------------------------------- parse_env fixes
class TestParseEnvComments(unittest.TestCase):
    def test_empty_value_with_trailing_comment(self):
        self.assertEqual(Credentials.parse_env("DASHBOARD_TOKEN= # disabled\n"),
                         {"DASHBOARD_TOKEN": ""})

    def test_comment_forms(self):
        env = Credentials.parse_env(
            "A=1 # c\nB=ab#cd\nC=\"x # y\"\nD=\t# tabbed\nE= #disabled\n")
        self.assertEqual(env, {"A": "1", "B": "ab#cd", "C": "x # y",
                               "D": "", "E": ""})


class TestFromFileSymlink(unittest.TestCase):
    def test_symlinked_env_rejected(self):
        with tempfile.TemporaryDirectory(prefix="sm-link-") as tmp:
            real = Path(tmp) / "real.env"
            real.write_text("A=1\n")
            link = Path(tmp) / "link.env"
            link.symlink_to(real)
            with self.assertRaises(CredentialsError):
                Credentials.from_file(link)

    def test_real_env_still_loads(self):
        with tempfile.TemporaryDirectory(prefix="sm-link-") as tmp:
            real = Path(tmp) / ".env"
            real.write_text("A=1\n")
            self.assertEqual(Credentials.from_file(real).get("A"), "1")


class TestMudaeBotIdTruncation(unittest.TestCase):
    def test_malformed_value_truncated_in_error(self):
        creds = Credentials({"MUDAE_BOT_ID": "x" * 100})
        with self.assertRaises(CredentialsError) as ctx:
            _ = creds.mudae_bot_id
        self.assertIn("...", str(ctx.exception))
        self.assertNotIn("x" * 41, str(ctx.exception))


# --------------------------------------------------- REMOTE_CONTROL matrix
class TestRemoteControlMatrix(unittest.TestCase):
    def test_absent_key_defaults_on(self):
        self.assertIs(Credentials({}).remote_control_enabled, True)

    def test_matrix(self):
        cases = {
            "0": False, "disable": False, "": False,
            "false": False, "OFF": False,
            "1": True, "on": True, "yes": True,
        }
        for raw, expected in cases.items():
            creds = Credentials({"REMOTE_CONTROL": raw})
            self.assertIs(creds.remote_control_enabled, expected,
                          f"REMOTE_CONTROL={raw!r}")


# ------------------------------------------- access-manager concurrency
class SlowKv:
    """kv double that YIELDS on every await so tasks really interleave."""

    def __init__(self) -> None:
        self.rows: Dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        await asyncio.sleep(0)
        return self.rows.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        await asyncio.sleep(0)
        self.rows[key] = value

    async def delete(self, key: str) -> bool:
        await asyncio.sleep(0)
        return self.rows.pop(key, None) is not None

    async def prefix(self, p: str) -> Dict[str, Any]:
        await asyncio.sleep(0)
        return {k: v for k, v in self.rows.items() if k.startswith(p)}


class TestAccessManagerConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_admin_add_remove_pairs(self):
        am = AccessManager(SlowKv())
        await am.set_admin_ids(list(range(0, 40, 2)))  # evens preloaded
        tasks = []
        for i in range(20):
            tasks.append(asyncio.create_task(am.add_admin(i * 2 + 1)))
            tasks.append(asyncio.create_task(am.remove_admin(i * 2)))
        await asyncio.gather(*tasks)
        self.assertEqual(await am.admin_ids(), list(range(1, 40, 2)))

    async def test_concurrent_allowed_add_remove_pairs(self):
        am = AccessManager(SlowKv())
        await am.set_allowed_user_ids("alpha", list(range(0, 40, 2)))
        tasks = []
        for i in range(20):
            tasks.append(asyncio.create_task(
                am.add_allowed("alpha", i * 2 + 1)))
            tasks.append(asyncio.create_task(
                am.remove_allowed("alpha", i * 2)))
        await asyncio.gather(*tasks)
        self.assertEqual(await am.allowed_user_ids("alpha"),
                         list(range(1, 40, 2)))


# -------------------------------------------- malformed rate_limits spec
class TestSeedConfigDefaultsMalformed(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_interval_skips_without_raising(self):
        limiter = RateLimitManager(
            EventBus(), ServiceLogger(LogService(), "rl"), None)

        class _Policies:
            async def list(self):
                return []

        class _Db:
            policies = _Policies()

        config = ProjectConfig(policies={
            "global": {"message": {"interval": "fast"}},
            "accounts": {"alpha": {"dm": {"interval": [1]}}},
        })
        await _seed_config_defaults(limiter, config, _Db())  # must not raise


# ------------------------------------------------ cooldown set/clear race
class SlowCooldownRepo:
    """Cooldown repo double whose upsert() PARKS after the repo write."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, Dict[str, Any]] = {}
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def upsert(self, **kw: Any) -> None:
        key = (kw["account"], kw["plugin"], kw["cooldown_key"])
        self.rows[key] = kw
        self.parked.set()
        await self.release.wait()

    async def get(self, account: str, plugin: str, cooldown_key: str,
                  now: float = None) -> Optional[Dict[str, Any]]:
        row = self.rows.get((account, plugin, cooldown_key))
        if row is None or (now is not None and row["expires_at"] <= now):
            return None
        return dict(row)

    async def clear(self, account: str, plugin: str,
                    cooldown_key: str) -> bool:
        return self.rows.pop((account, plugin, cooldown_key), None) is not None


class TestCooldownSetClearRace(_TmpCase):
    async def test_clear_during_set_upsert_await_does_not_resurrect(self):
        repo = SlowCooldownRepo()
        svc = CooldownService(repo)
        task = asyncio.create_task(svc.set("alpha", "p", "k", 60))
        await asyncio.wait_for(repo.parked.wait(), timeout=2)
        await svc.clear("alpha", "p", "k")  # racing clear completes mid-set
        repo.release.set()                  # set() resumes after the upsert
        await task
        self.assertFalse(await svc.is_active("alpha", "p", "k"))

    async def test_set_without_race_mirrors(self):
        repo = SlowCooldownRepo()
        repo.release.set()
        svc = CooldownService(repo)
        await svc.set("alpha", "p", "k", 60)
        self.assertTrue(await svc.is_active("alpha", "p", "k"))


if __name__ == "__main__":
    unittest.main()
