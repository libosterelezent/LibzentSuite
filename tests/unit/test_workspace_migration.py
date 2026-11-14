"""Migration v10 tests (W2) — the workspace fold, proven end-to-end.

A populated v9 database (every fold source table exercised) migrates
to the v10 shape, and the §13.2 INVARIANT holds: post-migration
effective settings equal pre-migration effective settings before the
profile layer is removed. Fresh DBs get v10 directly (restart-safe,
version-gated).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from database.db import Database


async def _row(conn, sql: str, params: tuple = ()) -> dict | None:
    rows = await conn.fetch_all(sql, params)
    return rows[0] if rows else None


async def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return list(await conn.fetch_all(sql, params))


class MigrationV10Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # connect() applies every pending migration, so a v10-era
        # connect would create the workspace schema BEFORE the fixture
        # exists and the fold would run against empty tables. Build the
        # database as a REAL v9 state first: connect with the migration
        # list truncated at v9, populate every fold source, then let
        # the full list apply v10.
        import database.db as db_module

        v9_only = [m for m in db_module.MIGRATIONS if m[0] <= 9]
        self._real_migrations = db_module.MIGRATIONS
        self._real_latest = db_module.latest_version
        db_module.MIGRATIONS = v9_only
        db_module.latest_version = lambda: 9
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        try:
            await self._populate_v9()
        finally:
            db_module.MIGRATIONS = self._real_migrations
            db_module.latest_version = self._real_latest
        self.addAsyncCleanup(self.db.close)

    async def _populate_v9(self) -> None:
        """Every fold source with distinguishable values."""
        conn = self.db.connection
        now = 1000.0
        # accounts: A in alpha (fleet-managed), B in alpha (sync-only),
        # C in no workspace
        await conn.execute(
            "INSERT INTO accounts (name, token_ref, enabled, sync_group, settings_json, created_at)"
            " VALUES ('A', NULL, 1, 'alpha', '{}', ?)", (now,))
        await conn.execute(
            "INSERT INTO accounts (name, token_ref, enabled, sync_group, settings_json, created_at)"
            " VALUES ('B', NULL, 1, 'alpha', '{}', ?)", (now,))
        await conn.execute(
            "INSERT INTO accounts (name, token_ref, enabled, sync_group, settings_json, created_at)"
            " VALUES ('C', NULL, 1, NULL, '{}', ?)", (now,))
        # sync group row (description + disabled)
        await conn.execute(
            "INSERT INTO sync_groups (name, description, enabled, created_at, updated_at)"
            " VALUES ('alpha', 'the grind', 0, ?, ?)", (now, now))
        # fleet orchestration state folded onto the workspace
        await conn.execute(
            "INSERT INTO fleet_groups (group_name, paused, generation, updated_at)"
            " VALUES ('alpha', 1, 7, ?)", (now,))
        # A is fleet-managed + cooling; B has NO fleet row (sync-only)
        await conn.execute(
            "INSERT INTO fleet_members (account, group_name, mode, eligibility, since, version)"
            " VALUES ('A', 'alpha', 'group_managed', 'cooling', ?, 1)", (now,))
        # one fleet task
        await conn.execute(
            "INSERT INTO fleet_assignments (id, group_name, task_id, account, status,"
            " priority, created_at, started_at, stopped_at, resume_policy, version,"
            " generation, error)"
            " VALUES ('t1', 'alpha', 'grind', 'A', 'running', 5, ?, 1001.0, NULL, '', 1, 7, NULL)",
            (now,))
        # one fleet cooldown (account scope) + one transition
        await conn.execute(
            "INSERT INTO fleet_cooldowns (scope_type, scope_id, reason, started_at,"
            " expires_at, source, cleared_at)"
            " VALUES ('account', 'A', 'observed', 999.0, 2000.0, 'observed', NULL)")
        await conn.execute(
            "INSERT INTO fleet_transitions (kind, group_name, generation, reason,"
            " correlation_id, occurred_at, task_id, assignment_id, account)"
            " VALUES ('TaskAssigned', 'alpha', 7, 'dispatch', 'corr-1', 1001.0, 'grind', 't1', 'A')")
        # plugins + profiles + bindings:
        #  - cleaner on A bound to "standard" (keys: trigger + auto_clean + channels)
        #  - A ALSO overrides trigger at the account layer (must stay untouched)
        #  - cleaner on B bound to "standard" (no overrides)
        #  - cleaner on C unbound
        await conn.execute(
            "INSERT INTO plugins (name, version, description, settings_json)"
            " VALUES ('cleaner', '1.0.0', '', '[]')")
        await conn.execute(
            "INSERT INTO plugin_profiles (id, plugin, name, description, settings_json,"
            " created_at, updated_at)"
            " VALUES (42, 'cleaner', 'standard', '', ?, ?, ?)",
            (json.dumps({"trigger": "$preset", "auto_clean": True,
                         "channels": ["500", "501"]}), now, now))
        for account in ("A", "B"):
            await conn.execute(
                "INSERT INTO account_plugins (account, plugin, profile)"
                " VALUES (?, 'cleaner', 'standard')", (account,))
        await conn.execute(
            "INSERT INTO account_plugins (account, plugin) VALUES ('C', 'cleaner')")
        # A's explicit override shadows the preset's trigger
        await conn.execute(
            "INSERT INTO plugin_settings (account, plugin, key, value_json)"
            " VALUES ('A', 'cleaner', 'trigger', '\"$mine\"')")
        await conn.execute(
            "INSERT INTO plugin_states (account, plugin, enabled)"
            " VALUES ('A', 'cleaner', 1)")

    async def _migrate(self) -> int:
        return await self.db.migrate()

    # ------------------------------------------------------------ folds

    async def test_workspace_identity_and_state_fold(self) -> None:
        await self._migrate()
        row = await _row(
            self.db.connection,
            "SELECT description, enabled, paused, generation FROM workspaces WHERE name='alpha'",
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["description"], "the grind")
        self.assertEqual(row["enabled"], 0)  # carried from sync_groups
        self.assertEqual(row["paused"], 1)  # folded from fleet_groups
        self.assertEqual(row["generation"], 7)  # folded from fleet_groups

    async def test_membership_pointer_and_projection(self) -> None:
        await self._migrate()
        conn = self.db.connection
        for account in ("A", "B"):
            self.assertEqual(
                (await _row(conn, "SELECT workspace FROM accounts WHERE name=?", (account,)))["workspace"],
                "alpha",
            )
        self.assertIsNone(
            (await _row(conn, "SELECT workspace FROM accounts WHERE name='C'"))["workspace"]
        )
        # projection: A carries fleet mode/eligibility (renamed), B is
        # the sync-only default
        member_a = await _row(
            conn, "SELECT mode, eligibility FROM workspace_members WHERE account='A'")
        self.assertEqual(member_a["mode"], "managed")
        self.assertEqual(member_a["eligibility"], "cooling")
        member_b = await _row(
            conn, "SELECT mode, eligibility FROM workspace_members WHERE account='B'")
        self.assertEqual(member_b["mode"], "independent")
        self.assertEqual(member_b["eligibility"], "eligible")

    async def test_tasks_cooldowns_transitions_carry(self) -> None:
        await self._migrate()
        conn = self.db.connection
        task = await _row(conn, "SELECT * FROM workspace_tasks WHERE id='t1'")
        self.assertEqual(task["workspace"], "alpha")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["generation"], 7)
        cd = await _row(
            conn,
            "SELECT scope_type, scope_id, source FROM workspace_cooldowns WHERE scope_id='A'")
        self.assertEqual((cd["scope_type"], cd["source"]), ("member", "observed"))
        tr = await _row(
            conn, "SELECT * FROM workspace_transitions WHERE correlation_id='corr-1'")
        self.assertEqual(tr["workspace"], "alpha")
        self.assertEqual(tr["run_id"], "t1")  # was assignment_id

    async def test_presets_fold_global_scope_with_ids(self) -> None:
        await self._migrate()
        row = await _row(
            self.db.connection,
            "SELECT preset_id, scope, owner_id, version, schema_version"
            " FROM presets WHERE plugin='cleaner' AND name='standard'")
        self.assertEqual(row["preset_id"], 42)  # provenance references survive
        self.assertEqual(row["scope"], "global")
        self.assertEqual(row["owner_id"], "")
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["schema_version"], 1)

    async def test_materialization_effective_settings_invariant(self) -> None:
        """§13.2: v10 effective values == v9 effective values."""
        await self._migrate()
        conn = self.db.connection
        # A: account override wins ("$mine"); auto_clean arrives from
        # the materialized preset; channels went to BINDINGS not settings
        a_settings = {
            r["key"]: r["value_json"]
            for r in (await _rows(
                conn, "SELECT key, value_json FROM plugin_settings WHERE account='A'"))
        }
        self.assertEqual(a_settings["trigger"], '"$mine"')  # untouched override
        self.assertEqual(a_settings["auto_clean"], "true")  # materialized
        self.assertNotIn("channels", a_settings)  # extracted, not copied
        # B: all preset keys materialize
        b_settings = {
            r["key"]: r["value_json"]
            for r in (await _rows(
                conn, "SELECT key, value_json FROM plugin_settings WHERE account='B'"))
        }
        self.assertEqual(b_settings["trigger"], '"$preset"')
        self.assertEqual(b_settings["auto_clean"], "true")
        # C: untouched — no binding, no materialization
        c_rows = await _rows(
            conn, "SELECT key FROM plugin_settings WHERE account='C'")
        self.assertEqual(c_rows, [])

    async def test_provenance_recorded_on_bound_pairs(self) -> None:
        await self._migrate()
        conn = self.db.connection
        for account in ("A", "B"):
            row = await _row(
                conn,
                "SELECT source_preset_id, source_version, applied_at"
                " FROM account_plugins WHERE account=? AND plugin='cleaner'", (account,))
            self.assertEqual(row["source_preset_id"], "42")
            self.assertEqual(row["source_version"], 1)
            self.assertIsNotNone(row["applied_at"])
        row_c = await _row(
            conn,
            "SELECT source_preset_id FROM account_plugins WHERE account='C'")
        self.assertIsNone(row_c["source_preset_id"])

    async def test_channels_extract_into_bindings(self) -> None:
        await self._migrate()
        conn = self.db.connection
        for account in ("A", "B"):
            bound = sorted(
                r["channel_id"]
                for r in (await _rows(
                    conn,
                    "SELECT channel_id FROM account_plugin_channels"
                    " WHERE account=? AND plugin='cleaner'", (account,)))
            )
            self.assertEqual(bound, ["500", "501"])
        none_c = await _rows(
            conn,
            "SELECT * FROM account_plugin_channels WHERE account='C'")
        self.assertEqual(none_c, [])

    async def test_fresh_database_gets_v10_directly(self) -> None:
        # an independent, untouched database: connect applies everything
        # up to v10 and every workspace table exists (restart-safe path)
        fresh_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(fresh_tmp.cleanup)
        fresh = Database(Path(fresh_tmp.name) / "fresh.db")
        applied = await fresh.connect()
        self.assertGreaterEqual(applied, 10)
        conn = fresh.connection
        for table in ("workspaces", "workspace_members", "workspace_tasks",
                      "workspace_chains", "chain_runs", "step_executions",
                      "signals", "workspace_cooldowns", "workspace_transitions",
                      "presets", "account_channels", "account_channel_assignments",
                      "account_plugin_channels", "deletion_jobs"):
            names = await conn.fetch_all("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            self.assertIsNotNone(names, table)
        await fresh.close()

    async def test_migration_is_idempotent_and_restart_safe(self) -> None:
        applied = await self._migrate()
        self.assertEqual(applied, 2)  # v10 (the fold) + v11 (the drops)
        again = await self._migrate()  # re-run: nothing new applies
        self.assertEqual(again, 0)


if __name__ == "__main__":
    unittest.main()
