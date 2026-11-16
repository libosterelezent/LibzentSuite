"""The legacy v4→v11 migration chain test (W5 keeps ONLY this from
the sync-groups suite: the manager is deleted, the migrations remain
as history the fold builds on)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from database.db import Database
from database.schema import MIGRATIONS


class LegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    """An existing v4 database with accounts.sync_group values converts
    to ONE group per distinct name (never one per account), and the
    v10 fold carries that membership into the workspace shape before
    v11 drops the v9 tables."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-legacy-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "legacy.db"

    async def test_legacy_values_backfill_then_fold_to_workspaces(self) -> None:
        with mock.patch("database.db.MIGRATIONS", MIGRATIONS[:4]):
            db = Database(self.path)
            await db.connect()
            # the v4-era writes use the v4-era column directly (the
            # repository's surface has moved on with the schema)
            conn = db.connection
            for name, group in (("a", "Alpha"), ("b", "Alpha"),
                                ("c", "Beta"), ("d", None)):
                await conn.execute(
                    "INSERT INTO accounts (name, token_ref, enabled,"
                    " sync_group, settings_json, created_at)"
                    " VALUES (?, NULL, 1, ?, '{}', 1000.0)",
                    (name, group),
                )
            await db.close()

        db = Database(self.path)
        applied = await db.connect()
        # migrations 5..14: backfill, assignments, profiles, legacy
        # drop, fleet state, the v10 fold, the v11 drops, the two-level
        # model's v12 store + v13 cooldown profile + v14 active pointer
        self.assertEqual(applied, 12)
        # the fold carried the pointer + projection
        self.assertEqual((await db.accounts.get("a")).workspace, "Alpha")
        self.assertIsNone((await db.accounts.get("d")).workspace)
        members = await db.workspaces.members("Alpha")
        self.assertEqual([m.account for m in members], ["a", "b"])
        beta = await db.workspaces.members("Beta")
        self.assertEqual([m.account for m in beta], ["c"])
        # v11 dropped the v9 tables AND the v9 column
        gone = await db.connection.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
            " ('sync_groups','fleet_groups','fleet_members','fleet_assignments',"
            " 'fleet_cooldowns','fleet_transitions')"
        )
        self.assertEqual(gone, [])
        columns = await db.connection.fetch_all(
            "SELECT name FROM pragma_table_info('accounts')")
        self.assertNotIn("sync_group", [c["name"] for c in columns])
        await db.close()


if __name__ == "__main__":
    unittest.main()
