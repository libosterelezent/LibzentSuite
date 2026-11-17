"""AccountPluginsRepository tests: direct account→plugin assignments.

Persistence contract only: idempotent assign/unassign, deterministic
sorted reads, bulk replacement, boot bulk-read, and restart persistence
(the reopen also exercises the schema_migrations guard — migration v6
must not reapply). This layer does no account/plugin existence
validation, so tests never need to create account rows.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database.db import Database


class AccountPluginsTestBase(unittest.IsolatedAsyncioTestCase):
    """One real (temp-file) Database per test; assignments reference
    account names but live independently of the accounts table."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-acct-plugins-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "plugins.db"

    async def asyncSetUp(self) -> None:
        self.db = Database(self.path)
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.repo = self.db.account_plugins


# ------------------------------------------------------------- assignment
class AssignmentTests(AccountPluginsTestBase):
    async def test_assign_then_plugins_of(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        self.assertEqual(await self.repo.plugins_of("alpha"), ["roll"])

    async def test_duplicate_assign_is_idempotent(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.assign_plugin("alpha", "roll")
        self.assertEqual(await self.repo.plugins_of("alpha"), ["roll"])

    async def test_plugins_of_unknown_account_is_empty(self) -> None:
        self.assertEqual(await self.repo.plugins_of("ghost"), [])

    async def test_unassign_removes(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.unassign_plugin("alpha", "roll")
        self.assertEqual(await self.repo.plugins_of("alpha"), [])

    async def test_unassign_missing_is_not_an_error(self) -> None:
        await self.repo.unassign_plugin("alpha", "roll")  # never assigned
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.unassign_plugin("alpha", "roll")
        await self.repo.unassign_plugin("alpha", "roll")  # double unassign
        self.assertEqual(await self.repo.plugins_of("alpha"), [])

    async def test_unassign_only_touches_its_own_row(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.assign_plugin("alpha", "snipe")
        await self.repo.assign_plugin("beta", "roll")
        await self.repo.unassign_plugin("alpha", "roll")
        self.assertEqual(await self.repo.plugins_of("alpha"), ["snipe"])
        self.assertEqual(await self.repo.plugins_of("beta"), ["roll"])

    async def test_multiple_plugins_one_account(self) -> None:
        for plugin in ("roll", "snipe", "claim"):
            await self.repo.assign_plugin("alpha", plugin)
        self.assertEqual(await self.repo.plugins_of("alpha"), ["claim", "roll", "snipe"])

    async def test_multiple_accounts_independent_sets(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.assign_plugin("beta", "snipe")
        await self.repo.assign_plugin("beta", "claim")
        self.assertEqual(await self.repo.plugins_of("alpha"), ["roll"])
        self.assertEqual(await self.repo.plugins_of("beta"), ["claim", "snipe"])


# ----------------------------------------------------------- replacement
class SetPluginsTests(AccountPluginsTestBase):
    async def test_set_plugins_replaces_entirely(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.assign_plugin("alpha", "snipe")
        stored = await self.repo.set_plugins("alpha", ["claim"])
        self.assertEqual(stored, ["claim"])
        self.assertEqual(await self.repo.plugins_of("alpha"), ["claim"])

    async def test_set_plugins_dedups_and_sorts(self) -> None:
        stored = await self.repo.set_plugins("alpha", ["b", "a", "b"])
        self.assertEqual(stored, ["a", "b"])
        self.assertEqual(await self.repo.plugins_of("alpha"), ["a", "b"])

    async def test_set_plugins_to_empty_clears_the_account(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        stored = await self.repo.set_plugins("alpha", [])
        self.assertEqual(stored, [])
        self.assertEqual(await self.repo.plugins_of("alpha"), [])

    async def test_set_plugins_scopes_to_one_account(self) -> None:
        await self.repo.assign_plugin("beta", "roll")
        await self.repo.set_plugins("alpha", ["claim"])
        self.assertEqual(await self.repo.plugins_of("beta"), ["roll"])


# -------------------------------------------------------------- bulk read
class AllAssignmentsTests(AccountPluginsTestBase):
    async def test_all_assignments_bulk_read(self) -> None:
        await self.repo.assign_plugin("beta", "snipe")
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.assign_plugin("alpha", "claim")
        self.assertEqual(
            await self.repo.all_assignments(),
            {"alpha": ["claim", "roll"], "beta": ["snipe"]},
        )

    async def test_accounts_with_zero_plugins_are_omitted(self) -> None:
        """Documented convention: a missing key MEANS "no plugins", so the
        consumer shape is ``assignments.get(account, [])``."""
        await self.db.accounts.add("alpha")  # real account, zero plugins
        await self.repo.assign_plugin("beta", "roll")
        assignments = await self.repo.all_assignments()
        self.assertNotIn("alpha", assignments)
        self.assertEqual(assignments.get("alpha", []), [])

    async def test_all_assignments_empty_db(self) -> None:
        self.assertEqual(await self.repo.all_assignments(), {})


# --------------------------------------------------------------- restart
class RestartTests(AccountPluginsTestBase):
    async def test_assignments_survive_restart(self) -> None:
        await self.repo.assign_plugin("alpha", "roll")
        await self.repo.set_plugins("beta", ["claim", "snipe"])

        # full restart: close, reopen the same file
        await self.db.close()
        self.db = Database(self.path)
        applied = await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.repo = self.db.account_plugins
        # migration guard: v6 is already recorded, nothing reapplies
        self.assertEqual(applied, 0)

        self.assertEqual(await self.repo.plugins_of("alpha"), ["roll"])
        self.assertEqual(await self.repo.plugins_of("beta"), ["claim", "snipe"])
        self.assertEqual(
            await self.repo.all_assignments(),
            {"alpha": ["roll"], "beta": ["claim", "snipe"]},
        )


if __name__ == "__main__":
    unittest.main()
