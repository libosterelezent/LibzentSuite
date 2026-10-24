"""Sync group subsystem tests: repository, manager, membership lifecycle,
RateLimitManager integration, restart restore, legacy DB migration and
concurrency.

Pacing behavior itself (shared clock, wire gaps, stamp cleanup) is covered
by test_rate_groups.py — this file only verifies the SyncGroupManager
drives the EXISTING engine correctly through its public API.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.errors import SyncGroupError
from core.event_bus import EventBus
from core.rate_limit_manager import RateLimitManager
from core.rate_limit_manager.engine import ManualClock
from core.sync_group_manager import SyncGroupManager

from database.db import Database
from database.schema import MIGRATIONS

from tests.unit.test_rate_engine import SleepAdvanceManager, StubLogger

LOG = logging.getLogger("test.sync_groups")
ACTION = "message"


class SyncGroupTestBase(unittest.IsolatedAsyncioTestCase):
    """One real (temp-file) Database + real repos + real limiter."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-groups-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "groups.db"

    async def asyncSetUp(self) -> None:
        self.db = Database(self.path)
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.limiter = SleepAdvanceManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0),
            jitter_seed=7,
        )
        self.manager = SyncGroupManager(
            self.db.sync_groups, LOG, accounts_repo=self.db.accounts,
            limiter=self.limiter,
        )
        await self.manager.load()
        for name in ("alpha", "beta", "gamma"):
            await self.db.accounts.add(name)

    def runtime_groups(self) -> dict:
        return self.limiter.account_groups()


# ------------------------------------------------------------------ groups
class GroupPersistenceTests(SyncGroupTestBase):
    async def test_create_get_list(self) -> None:
        out = await self.manager.create_group("grinders", "the grind")
        self.assertEqual(out["name"], "grinders")
        self.assertTrue(out["enabled"])
        self.assertEqual(out["description"], "the grind")
        by_id = await self.manager.get_group(out["id"])
        by_name = await self.manager.get_group_by_name("grinders")
        self.assertEqual(by_id, out)
        self.assertEqual(by_name, out)
        await self.manager.create_group("solo")
        rows = await self.manager.list_groups()
        self.assertEqual([r["name"] for r in rows], ["grinders", "solo"])
        self.assertEqual(rows[0]["member_count"], 0)  # groups may be empty

    async def test_duplicate_name_rejected(self) -> None:
        await self.manager.create_group("alpha-team")
        with self.assertRaises(SyncGroupError) as ctx:
            await self.manager.create_group("alpha-team")
        self.assertIn("already exists", str(ctx.exception))

    async def test_invalid_names_rejected(self) -> None:
        for bad in ("", "UPPER", "has space", "a" * 33, "grp:x", "a/b", None, 5):
            with self.assertRaises(SyncGroupError):
                await self.manager.create_group(bad)

    async def test_update_fields(self) -> None:
        group = await self.manager.create_group("grinders")
        out = await self.manager.update_group(
            group["id"], description="renamed desc", enabled=False)
        self.assertEqual(out["description"], "renamed desc")
        self.assertFalse(out["enabled"])
        self.assertEqual(out["name"], "grinders")
        self.assertGreaterEqual(out["updated_at"], group["updated_at"])

    async def test_update_unknown_group(self) -> None:
        await self.manager.create_group("grinders")
        with self.assertRaises(SyncGroupError):
            await self.manager.update_group(999, description="x")
        with self.assertRaises(SyncGroupError):
            await self.manager.update_group("ghost", description="x")

    async def test_rename_to_existing_name_rejected(self) -> None:
        await self.manager.create_group("one")
        await self.manager.create_group("two")
        with self.assertRaises(SyncGroupError):
            await self.manager.update_group("one", name="two")

    async def test_delete_empty_group(self) -> None:
        await self.manager.create_group("empty")
        out = await self.manager.delete_group("empty")
        self.assertTrue(out["existed"])
        self.assertEqual(await self.manager.list_groups(), [])

    async def test_delete_unknown_group_is_idempotent_shaped(self) -> None:
        with self.assertRaises(SyncGroupError):
            await self.manager.delete_group("ghost")

    async def test_delete_group_with_members_refused_then_detached(self) -> None:
        await self.manager.create_group("grinders")
        await self.manager.add_account_to_group("grinders", "alpha")
        with self.assertRaises(SyncGroupError) as ctx:
            await self.manager.delete_group("grinders")
        self.assertIn("alpha", str(ctx.exception))
        # the account itself was NOT touched
        record = await self.db.accounts.get("alpha")
        self.assertEqual(record.sync_group, "grinders")
        out = await self.manager.delete_group("grinders", detach_members=True)
        self.assertEqual(out["detached"], ["alpha"])
        record = await self.db.accounts.get("alpha")
        self.assertIsNone(record.sync_group)
        self.assertEqual(self.runtime_groups(), {})
        # deleting a group never deletes accounts
        self.assertIsNotNone(await self.db.accounts.get("alpha"))


# -------------------------------------------------------------- membership
class MembershipTests(SyncGroupTestBase):
    async def test_none_to_group(self) -> None:
        await self.manager.create_group("alpha")
        await self.manager.add_account_to_group("alpha", "alpha")
        self.assertEqual(
            (await self.db.accounts.get("alpha")).sync_group, "alpha")
        self.assertEqual(self.runtime_groups(), {"alpha": "alpha"})

    async def test_group_to_group(self) -> None:
        await self.manager.create_group("one")
        await self.manager.create_group("two")
        await self.manager.set_account_group("alpha", "one")
        out = await self.manager.set_account_group("alpha", "two")
        self.assertEqual(out["previous"], "one")
        self.assertEqual(self.runtime_groups(), {"alpha": "two"})
        self.assertEqual(
            (await self.db.accounts.get("alpha")).sync_group, "two")

    async def test_group_to_none(self) -> None:
        await self.manager.create_group("one")
        await self.manager.set_account_group("alpha", "one")
        out = await self.manager.clear_account_group("alpha")
        self.assertEqual(out["previous"], "one")
        self.assertEqual(self.runtime_groups(), {})
        self.assertIsNone(
            (await self.db.accounts.get("alpha")).sync_group)

    async def test_same_group_is_idempotent(self) -> None:
        await self.manager.create_group("one")
        await self.manager.add_account_to_group("one", "alpha")
        await self.manager.add_account_to_group("one", "alpha")
        await self.manager.set_account_group("alpha", "one")
        self.assertEqual(self.runtime_groups(), {"alpha": "one"})
        self.assertEqual(await self.manager.get_group_members("one"), ["alpha"])

    async def test_remove_member(self) -> None:
        await self.manager.create_group("one")
        await self.manager.add_account_to_group("one", "alpha")
        out = await self.manager.remove_account_from_group("one", "alpha")
        self.assertTrue(out["existed"])
        again = await self.manager.remove_account_from_group("one", "alpha")
        self.assertFalse(again["existed"])
        self.assertEqual(self.runtime_groups(), {})

    async def test_remove_from_wrong_group_is_lenient(self) -> None:
        await self.manager.create_group("one")
        await self.manager.create_group("two")
        await self.manager.set_account_group("alpha", "one")
        out = await self.manager.remove_account_from_group("two", "alpha")
        self.assertFalse(out["existed"])
        # real membership untouched
        self.assertEqual(self.runtime_groups(), {"alpha": "one"})

    async def test_unknown_account_and_group_errors(self) -> None:
        await self.manager.create_group("one")
        with self.assertRaises(SyncGroupError) as ctx:
            await self.manager.add_account_to_group("one", "ghost")
        self.assertIn("alpha", str(ctx.exception))  # known accounts listed
        with self.assertRaises(SyncGroupError):
            await self.manager.set_account_group("alpha", "ghost")
        with self.assertRaises(SyncGroupError):
            await self.manager.add_account_to_group("ghost", "alpha")

    async def test_get_account_group(self) -> None:
        await self.manager.create_group("one")
        self.assertIsNone(await self.manager.get_account_group("alpha"))
        await self.manager.set_account_group("alpha", "one")
        self.assertEqual(await self.manager.get_account_group("alpha"), "one")

class RenameTests(SyncGroupTestBase):
    async def test_rename_moves_runtime_membership(self) -> None:
        """Alpha -> rename -> Raiders must not leave any account paced under
        BOTH group:Alpha and group:Raiders."""
        await self.manager.create_group("alpha")
        await self.manager.add_account_to_group("alpha", "alpha")
        await self.manager.add_account_to_group("alpha", "beta")
        self.limiter.set_policy_values(ACTION, 2.0)
        await self.limiter.acquire(ACTION, account="alpha")  # stamps group:alpha
        self.assertIn(("group:alpha", ACTION), self.limiter._last_fire)

        await self.manager.update_group("alpha", name="raiders")
        self.assertEqual(
            self.runtime_groups(), {"alpha": "raiders", "beta": "raiders"})
        # no stale runtime membership under the old name
        self.assertNotIn("alpha", self.runtime_groups().values())
        # the old shared stamps are dropped with the last member that left
        self.assertNotIn(("group:alpha", ACTION), self.limiter._last_fire)
        # persisted pointers follow the rename
        self.assertEqual(await self.db.sync_groups.members("raiders"),
                         ["alpha", "beta"])
        self.assertEqual(await self.db.sync_groups.members("alpha"), [])
        # new group id stays the stable identity
        snapshot = await self.manager.snapshot("raiders")
        self.assertEqual(snapshot["pacing_key"], "group:raiders")


# ----------------------------------------------------- pacing integration
class PacingIntegrationTests(SyncGroupTestBase):
    """SyncGroupManager drives the EXISTING engine: same group -> shared
    pacing, different groups -> independent, cleared -> per-account."""

    async def test_same_group_shares_one_clock(self) -> None:
        await self.manager.create_group("grinders")
        await self.manager.add_account_to_group("grinders", "alpha")
        await self.manager.add_account_to_group("grinders", "beta")
        self.limiter.set_policy_values(ACTION, 2.0)
        await self.limiter.acquire(ACTION, account="alpha")   # t=0
        await self.limiter.acquire(ACTION, account="beta")    # group clock -> t=2
        self.assertEqual(self.limiter._clock.now(), 2.0)

    async def test_different_groups_independent(self) -> None:
        await self.manager.create_group("one")
        await self.manager.create_group("two")
        await self.manager.set_account_group("alpha", "one")
        await self.manager.set_account_group("beta", "two")
        self.limiter.set_policy_values(ACTION, 2.0)
        await self.limiter.acquire(ACTION, account="alpha")   # t=0
        await self.limiter.acquire(ACTION, account="beta")    # own clock -> t=0
        self.assertEqual(self.limiter._clock.now(), 0.0)

    async def test_clearing_group_returns_to_account_pacing(self) -> None:
        await self.manager.create_group("one")
        await self.manager.set_account_group("alpha", "one")
        await self.manager.set_account_group("beta", "one")
        self.limiter.set_policy_values(ACTION, 2.0)
        await self.limiter.acquire(ACTION, account="alpha")   # stamps group
        await self.manager.clear_account_group("beta")
        await self.limiter.acquire(ACTION, account="beta")    # no group wait
        self.assertEqual(self.limiter._clock.now(), 0.0)

    async def test_moving_groups_paces_on_the_new_clock(self) -> None:
        await self.manager.create_group("one")
        await self.manager.create_group("two")
        await self.manager.set_account_group("alpha", "one")
        await self.manager.set_account_group("beta", "two")
        self.limiter.set_policy_values(ACTION, 2.0)
        await self.limiter.acquire(ACTION, account="alpha")   # stamps group:one
        await self.limiter.acquire(ACTION, account="beta")    # stamps group:two
        # beta moves into group one: it must now respect one's clock
        await self.manager.set_account_group("beta", "one")
        await self.limiter.acquire(ACTION, account="beta")
        self.assertEqual(self.limiter._clock.now(), 2.0)


# ------------------------------------------------------- account lifecycle
class AccountLifecycleTests(SyncGroupTestBase):
    def _wiring(self):
        from tests.unit.helpers import (
            FakePluginManager, FakeScheduler, FakeSettings, FakeAudit,
            FakeClientFactory,
        )
        from core.credentials import Credentials

        return (
            FakePluginManager(), FakeScheduler(), FakeSettings(), FakeAudit(),
            FakeClientFactory(),
            Credentials.from_values({}),
        )

    def _make_account_manager(self, *, sync_mgr="default", limiter=None):
        """Wire an AccountManager; ``sync_mgr=None`` means UNWIRED (legacy
        plumbing path), anything else overrides the default self.manager."""
        from core.account_manager import AccountManager

        (plugins, scheduler, settings, audit, factory,
         credentials) = self._wiring()
        return AccountManager(
            EventBus(), LOG, self.db.accounts,
            plugins, scheduler, settings, audit,
            factory, credentials,
            limiter=limiter if limiter is not None else self.limiter,
            sync_groups=self.manager if sync_mgr == "default" else sync_mgr,
        )

    async def test_grouped_account_removal_cleans_membership(self) -> None:
        accounts = self._make_account_manager()
        await accounts.load()
        await self.manager.create_group("grinders")
        await self.manager.add_account_to_group("grinders", "alpha")
        await self.manager.add_account_to_group("grinders", "beta")
        self.limiter.set_policy_values(ACTION, 2.0)
        await self.limiter.acquire(ACTION, account="alpha")

        out = await accounts.remove_account("alpha")
        self.assertEqual(out["removed"], "alpha")
        # runtime mapping: no stale group entry for the removed account
        self.assertEqual(self.runtime_groups(), {"beta": "grinders"})
        # persisted membership: the pointer died with the account row
        self.assertEqual(await self.db.sync_groups.members("grinders"), ["beta"])
        # the group itself survives its last-but-one member leaving
        snapshot = await self.manager.snapshot("grinders")
        self.assertEqual(snapshot["member_count"], 1)

    async def test_last_member_removal_leaves_empty_group(self) -> None:
        accounts = self._make_account_manager()
        await accounts.load()
        await self.manager.create_group("solo")
        await self.manager.add_account_to_group("solo", "gamma")
        await accounts.remove_account("gamma")
        self.assertEqual(self.runtime_groups(), {})
        rows = await self.manager.list_groups()
        self.assertEqual([r["name"] for r in rows], ["solo"])
        self.assertEqual(rows[0]["member_count"], 0)

    async def test_wired_manager_restores_runtime_on_load(self) -> None:
        """Boot: AccountManager.load() delegates to SyncGroupManager which
        restores the limiter mapping from persisted membership."""
        await self.manager.create_group("grinders")
        await self.manager.set_account_group("alpha", "grinders")
        await self.manager.set_account_group("beta", "grinders")
        # a fresh limiter + manager (process restart) over the same DB
        fresh_limiter = SleepAdvanceManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0),
            jitter_seed=7)
        fresh = SyncGroupManager(
            self.db.sync_groups, LOG, accounts_repo=self.db.accounts,
            limiter=fresh_limiter)
        await fresh.load()
        accounts = self._make_account_manager(
            sync_mgr=fresh, limiter=fresh_limiter)
        await accounts.load()
        self.assertEqual(fresh_limiter.account_groups(),
                         {"alpha": "grinders", "beta": "grinders"})

    async def test_unwired_manager_keeps_legacy_plumbing(self) -> None:
        """AccountManager without a SyncGroupManager still feeds the limiter
        per record (legacy contract preserved for unwired stacks)."""
        await self.manager.create_group("grinders")
        await self.manager.set_account_group("alpha", "grinders")
        fresh_limiter = SleepAdvanceManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0),
            jitter_seed=7)
        accounts = self._make_account_manager(sync_mgr=None,
                                              limiter=fresh_limiter)
        await accounts.load()
        self.assertEqual(fresh_limiter.account_groups(),
                         {"alpha": "grinders"})


# ---------------------------------------------------------------- restart
class RestartTests(SyncGroupTestBase):
    async def test_groups_and_membership_survive_restart(self) -> None:
        await self.manager.create_group("grinders", "the grind")
        await self.manager.set_account_group("alpha", "grinders")
        await self.manager.set_account_group("beta", "grinders")
        await self.manager.create_group("idle")  # stays empty

        # full restart: close, reopen, fresh manager + limiter
        await self.db.close()
        self.db = Database(self.path)
        await self.db.connect()
        # the base cleanup closes Database#1 (already closed) — Database#2
        # must be closed too or its worker thread holds groups.db open on
        # Windows and the temp-dir cleanup fails (PermissionError)
        self.addAsyncCleanup(self.db.close)
        limiter2 = SleepAdvanceManager(
            EventBus(), StubLogger(), None, clock=ManualClock(0.0),
            jitter_seed=7)
        manager2 = SyncGroupManager(
            self.db.sync_groups, LOG, accounts_repo=self.db.accounts,
            limiter=limiter2)
        await manager2.load()

        rows = await manager2.list_groups()
        self.assertEqual(
            [(r["name"], r["member_count"]) for r in rows],
            [("grinders", 2), ("idle", 0)]
        )
        # runtime restore: the boot pass re-registers every membership
        restored = await manager2.sync_runtime()
        self.assertEqual(restored, 2)
        self.assertEqual(limiter2.account_groups(),
                         {"alpha": "grinders", "beta": "grinders"})
        # and the restored mapping actually paces as a group
        limiter2.set_policy_values(ACTION, 2.0)
        await limiter2.acquire(ACTION, account="alpha")   # t=0
        await limiter2.acquire(ACTION, account="beta")    # shared clock -> t=2
        self.assertEqual(limiter2._clock.now(), 2.0)

    async def test_add_account_adopts_legacy_sync_group_field(self) -> None:
        """accounts with a sync_group value created OUTSIDE the group API
        (repo add / config-dir payloads) get a first-class group row + the
        runtime mapping on restore."""
        await self.db.accounts.add("newbie", sync_group="ad-hoc")
        self.assertIsNone(await self.db.sync_groups.get_by_name("ad-hoc"))
        await self.manager.sync_runtime()
        adopted = await self.manager.get_group_by_name("ad-hoc")
        self.assertIsNotNone(adopted)
        self.assertEqual(self.runtime_groups().get("newbie"), "ad-hoc")
        # adoption is one group per NAME, not per account
        await self.db.accounts.add("second", sync_group="ad-hoc")
        await self.manager.sync_membership("second", "ad-hoc")
        rows = [g for g in await self.manager.list_groups()
                if g["name"] == "ad-hoc"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["member_count"], 2)


# ----------------------------------------------------------- legacy DB
class LegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    """An existing v4 database with accounts.sync_group values converts to
    ONE group per distinct name (never one group per account)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-legacy-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "legacy.db"

    async def test_legacy_sync_group_values_backfill_one_group_per_name(self) -> None:
        with mock.patch("database.db.MIGRATIONS", MIGRATIONS[:4]):
            db = Database(self.path)
            await db.connect()
            await db.accounts.add("a", sync_group="Alpha")
            await db.accounts.add("b", sync_group="Alpha")
            await db.accounts.add("c", sync_group="Beta")
            await db.accounts.add("d")  # ungrouped
            await db.close()

        db = Database(self.path)
        applied = await db.connect()
        # migrations 5 (sync-group backfill) + 6 (account_plugins) +
        # 7 (plugin_profiles + binding column) + 8 (drop legacy profile
        # tables) + 9 (fleet orchestration state)
        self.assertEqual(applied, 5)
        groups = sorted(
            (g.name, g.enabled) for g in await db.sync_groups.list())
        self.assertEqual(groups, [("Alpha", True), ("Beta", True)])
        self.assertEqual(await db.sync_groups.members("Alpha"), ["a", "b"])
        self.assertEqual(await db.sync_groups.members("Beta"), ["c"])
        # the legacy column is PRESERVED (dashboard + unwired readers)
        self.assertEqual((await db.accounts.get("a")).sync_group, "Alpha")
        self.assertIsNone((await db.accounts.get("d")).sync_group)
        await db.close()

    async def test_migration_safe_on_fresh_and_current_dbs(self) -> None:
        # fresh file: everything applies, no legacy rows -> no groups
        db = Database(self.path)
        await db.connect()
        self.assertEqual(await db.sync_groups.list(), [])
        await db.close()
        # reopen the now-current file: migration is a recorded no-op
        db = Database(self.path)
        self.assertEqual(await db.connect(), 0)
        await db.close()


# ----------------------------------------------------------- concurrency
class ConcurrencyTests(SyncGroupTestBase):
    async def test_concurrent_moves_agree(self) -> None:
        """A->Alpha racing A->Beta: whichever wins LAST, the persisted
        membership and the runtime mapping agree afterwards."""
        await self.manager.create_group("alpha")
        await self.manager.create_group("beta")
        await asyncio.gather(
            self.manager.set_account_group("alpha", "beta"),
            self.manager.set_account_group("alpha", "alpha"),
        )
        persisted = (await self.db.accounts.get("alpha")).sync_group
        runtime = self.limiter.account_groups().get("alpha")
        self.assertEqual(persisted, runtime)
        self.assertIn(persisted, ("alpha", "beta"))

    async def test_concurrent_mixed_membership_moves(self) -> None:
        await self.manager.create_group("one")
        await self.manager.create_group("two")
        await self.manager.set_account_group("alpha", "one")
        await asyncio.gather(
            self.manager.set_account_group("alpha", "two"),
            self.manager.set_account_group("beta", "two"),
            self.manager.clear_account_group("alpha"),
            self.manager.set_account_group("gamma", "one"),
        )
        for name in ("alpha", "beta", "gamma"):
            persisted = (await self.db.accounts.get(name)).sync_group
            runtime = self.limiter.account_groups().get(name)
            self.assertEqual(persisted, runtime, name)


if __name__ == "__main__":
    unittest.main()
