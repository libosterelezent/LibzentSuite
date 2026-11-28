"""Profile-scoped cooldowns — P1's runtime ownership dimension
(docs/design/data-management-adoption.md).

The invariant: a cooldown's identity is (account, plugin, profile,
cooldown_key). One profile's cooldown NEVER blocks, clears, or masks
another profile's — and the profile-less ('') scope is exactly the
pre-P1 single-profile behavior, which the existing suites still prove.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from libzentsuite.core.cooldowns import CooldownService
from libzentsuite.database.db import Database


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "c.db")
        await self.db.connect()
        self.clock = _Clock()
        self.svc = CooldownService(self.db.cooldowns, time_fn=self.clock)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()


class ProfileScoping(_Base):
    async def test_same_key_different_profiles_are_independent(self) -> None:
        a = await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        b = await self.svc.set("alpha", "exporter", "roll", 120.0, profile="secondary")
        self.assertEqual(a["profile"], "main")
        self.assertEqual(b["profile"], "secondary")
        rem_main = await self.svc.remaining("alpha", "exporter", "roll", profile="main")
        rem_sec = await self.svc.remaining("alpha", "exporter", "roll", profile="secondary")
        self.assertAlmostEqual(rem_main, 60.0, delta=1.0)
        self.assertAlmostEqual(rem_sec, 120.0, delta=1.0)

    async def test_one_profile_cooldown_never_blocks_another(self) -> None:
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        self.assertFalse(await self.svc.is_active("alpha", "exporter", "roll", profile="alt"))
        self.assertTrue(await self.svc.is_active("alpha", "exporter", "roll", profile="main"))
        self.assertFalse(await self.svc.is_active("alpha", "exporter", "roll"))  # '' scope

    async def test_clear_is_profile_scoped(self) -> None:
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="secondary")
        self.assertTrue(await self.svc.clear("alpha", "exporter", "roll", profile="main"))
        self.assertTrue(await self.svc.is_active("alpha", "exporter", "roll", profile="secondary"))
        self.assertFalse(await self.svc.is_active("alpha", "exporter", "roll", profile="main"))

    async def test_profile_less_scope_keeps_pre_p1_identity(self) -> None:
        await self.svc.set("alpha", "exporter", "roll", 60.0)  # no profile arg
        row = await self.svc.get("alpha", "exporter", "roll")
        assert row is not None
        self.assertEqual(row["profile"], "")
        self.assertTrue(await self.svc.is_active("alpha", "exporter", "roll"))
        # a profile-scoped set does NOT touch the '' row
        await self.svc.set("alpha", "exporter", "roll", 5.0, profile="main")
        rem = await self.svc.remaining("alpha", "exporter", "roll")
        self.assertAlmostEqual(rem, 60.0, delta=1.0)

    async def test_active_for_filters_by_profile(self) -> None:
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="alt")
        await self.svc.set("alpha", "exporter", "roll", 60.0)
        rows = await self.svc.active_for("alpha", "exporter", profile="main")
        self.assertEqual([r["profile"] for r in rows], ["main"])
        all_rows = await self.svc.active_for("alpha", "exporter")
        self.assertEqual(len(all_rows), 3)


class Persistence(_Base):
    async def test_restore_recovers_every_profile_dimension(self) -> None:
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        await self.svc.set("alpha", "exporter", "roll", 90.0, profile="secondary")
        await self.svc.set("alpha", "exporter", "roll", 30.0)
        fresh = CooldownService(self.db.cooldowns, time_fn=self.clock)
        report = await fresh.restore()
        self.assertEqual(report["restored"], 3)
        self.assertAlmostEqual(
            await fresh.remaining("alpha", "exporter", "roll", profile="main"), 60.0, delta=1.0
        )
        self.assertAlmostEqual(
            await fresh.remaining("alpha", "exporter", "roll", profile="secondary"), 90.0, delta=1.0
        )
        self.assertAlmostEqual(await fresh.remaining("alpha", "exporter", "roll"), 30.0, delta=1.0)

    async def test_restart_preserves_rows_across_database_reopen(self) -> None:
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        await self.db.close()
        self.db = Database(Path(self._tmp.name) / "c.db")
        await self.db.connect()
        svc2 = CooldownService(self.db.cooldowns, time_fn=self.clock)
        await svc2.restore()
        self.assertTrue(await svc2.is_active("alpha", "exporter", "roll", profile="main"))


class RepoUniqueness(_Base):
    async def test_upsert_same_key_same_profile_replaces(self) -> None:
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        await self.svc.set("alpha", "exporter", "roll", 200.0, profile="main")
        rem = await self.svc.remaining("alpha", "exporter", "roll", profile="main")
        self.assertAlmostEqual(rem, 200.0, delta=1.0)  # replaced, not doubled


if __name__ == "__main__":
    unittest.main()


class ConcurrentIsolation(_Base):
    """P6: concurrent execution — profiles racing the same logical keys."""

    async def test_concurrent_sets_same_key_distinct_profiles_all_land(self) -> None:
        async def set_profile(profile: str, duration: float) -> None:
            await self.svc.set("alpha", "exporter", "roll", duration, profile=profile)

        await asyncio.gather(
            set_profile("main", 60.0),
            set_profile("alt", 60.0),
            set_profile("third", 60.0),
        )
        for profile in ("main", "alt", "third"):
            self.assertTrue(await self.svc.is_active("alpha", "exporter", "roll", profile=profile))

    async def test_concurrent_account_deletion_and_profile_write(self) -> None:
        """A write racing the account's deletion cascade lands or dies —
        never half-lands as an orphan row for a dead account."""
        await self.db.accounts.add("alpha")
        await self.svc.set("alpha", "exporter", "roll", 60.0, profile="main")
        await asyncio.gather(
            self.db.accounts.delete("alpha"),
            self.svc.set("alpha", "exporter", "roll", 5.0, profile="late"),
            return_exceptions=True,
        )
        # the racing write may land AFTER the cascade — an orphan row.
        # The lifecycle rule: restore with the known-accounts registry
        # sweeps it; it is never resurrected into memory.
        fresh = CooldownService(self.db.cooldowns, time_fn=self.clock)
        report = await fresh.restore(known_accounts={"beta"})
        self.assertGreaterEqual(report["orphans"], 1)
        self.assertNotIn(("alpha", "exporter", "late", "roll"), fresh._active)
        remaining = await self.db.cooldowns.active_all(now=self.clock())
        orphans = [r for r in remaining if r["account"] == "alpha"]
        self.assertEqual(orphans, [])  # swept: no zombie rows for a dead account
