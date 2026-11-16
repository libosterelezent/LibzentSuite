"""P7 — the legacy fold: template profiles become account-owned instances.

The migration contract (docs/design/data-management-adoption.md):
every (account, plugin, legacy-profile-name) binding becomes ONE
account_plugin_profiles instance copied from the template's body, the
active pointer lands on it, and nothing else changes. Destructive
follow-ups (the legacy table/repo/verbs retiring to dump/) ride their
own commits AFTER this proves green.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from database.db import Database


async def _legacy_state(db: Database) -> tuple[int, int]:
    """(bound accounts' rows remaining, active pointers set)."""
    rows = await db.connection.fetch_all(
        "SELECT account, plugin FROM account_plugin_active_profile"
    )
    return len(rows), len(rows)


class TheFold(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "m.db")
        # a v7-era database: legacy profiles + bindings already applied
        await self.db.connect()
        await self._seed_legacy()

    async def _seed_legacy(self) -> None:
        conn = self.db.connection
        async with conn.write() as c:
            await c.execute(
                "INSERT INTO plugin_profiles (plugin, name, description,"
                " settings_json, created_at, updated_at)"
                " VALUES ('exporter', 'main', 'template', ?, 1.0, 1.0)",
                ('{"chunk": 1800, "channels": ["444"]}',),
            )
            await c.execute(
                "INSERT INTO accounts (name, enabled, created_at) VALUES ('alpha', 1, 1.0)"
            )
            await c.execute(
                "INSERT INTO account_plugins (account, plugin, profile)"
                " VALUES ('alpha', 'exporter', 'main')"
            )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_fold_creates_instance_and_pointer_per_binding(self) -> None:
        from database.legacy_fold import fold_legacy_profiles

        report = await fold_legacy_profiles(self.db)
        self.assertEqual(report["folded"], 1)
        record = await self.db.account_plugin_profiles.get("alpha", "exporter", "main")
        assert record is not None
        self.assertEqual(record.settings["chunk"], 1800)
        self.assertEqual(record.settings["channels"], ["444"])  # stays IN the body
        active = await self.db.account_plugin_profiles.get_active("alpha", "exporter")
        self.assertEqual(active, "main")

    async def test_fold_is_idempotent(self) -> None:
        from database.legacy_fold import fold_legacy_profiles

        first = await fold_legacy_profiles(self.db)
        second = await fold_legacy_profiles(self.db)
        self.assertEqual(first["folded"], 1)
        self.assertEqual(second["folded"], 0)  # already an instance: skipped
        self.assertEqual(second["skipped"], 1)

    async def test_unbound_templates_are_not_instanced(self) -> None:
        async with self.db.connection.write() as c:
            await c.execute(
                "INSERT INTO plugin_profiles (plugin, name, description,"
                " settings_json, created_at, updated_at)"
                " VALUES ('exporter', 'unused', '', '{}', 1.0, 1.0)"
            )
        from database.legacy_fold import fold_legacy_profiles

        report = await fold_legacy_profiles(self.db)
        self.assertEqual(report["folded"], 1)
        self.assertIsNone(await self.db.account_plugin_profiles.get("alpha", "exporter", "unused"))

    async def test_binding_without_template_row_still_gets_a_pointer(self) -> None:
        """A dangling binding (template deleted) folds to an EMPTY instance —
        the pointer must never dangle, and the identity is preserved."""
        async with self.db.connection.write() as c:
            await c.execute(
                "INSERT INTO accounts (name, enabled, created_at) VALUES ('beta', 1, 1.0)"
            )
            await c.execute(
                "INSERT INTO account_plugins (account, plugin, profile)"
                " VALUES ('beta', 'exporter', 'ghost')"
            )
        from database.legacy_fold import fold_legacy_profiles

        report = await fold_legacy_profiles(self.db)
        self.assertEqual(report["folded"], 2)
        self.assertEqual(report["dangling"], 1)
        record = await self.db.account_plugin_profiles.get("beta", "exporter", "ghost")
        assert record is not None
        self.assertEqual(record.settings, {})  # empty body, identity preserved
        self.assertEqual(
            await self.db.account_plugin_profiles.get_active("beta", "exporter"), "ghost"
        )

    async def test_fold_rides_the_boot_migration(self) -> None:
        """A v14 database carrying bindings folds them when v15 applies."""
        await self.db.close()
        # roll the stamp back to v14: the file now looks like a pre-fold
        # database whose bindings were seeded before the upgrade
        import sqlite3

        raw = sqlite3.connect(Path(self._tmp.name) / "m.db")
        raw.execute("DELETE FROM schema_migrations WHERE version >= 15")
        raw.commit()
        raw.close()
        self.db = Database(Path(self._tmp.name) / "m.db")
        applied = await self.db.connect()  # v15 applies the fold
        self.assertGreaterEqual(applied, 1)
        active = await self.db.account_plugin_profiles.get_active("alpha", "exporter")
        self.assertEqual(active, "main")


if __name__ == "__main__":
    unittest.main()
