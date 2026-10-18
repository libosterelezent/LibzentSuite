"""PluginProfilesRepository + profile-binding tests (migration v7).

Persistence contract only: profile CRUD (settings stored as JSON,
NEVER schema-validated here), duplicate refusal, delete cascading the
account_plugins.profile references to NULL, binding set/clear/no-op,
bindings()/all_bindings() shapes, restart persistence, and migration
idempotency via reopen.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.errors import DatabaseError
from database.db import Database


class PluginProfilesTestBase(unittest.IsolatedAsyncioTestCase):
    """One real (temp-file) Database per test (fresh schema incl. v7)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-plugin-profiles-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "profiles.db"

    async def asyncSetUp(self) -> None:
        self.db = Database(self.path)
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.repo = self.db.plugin_profiles
        self.bindings = self.db.account_plugins


# ------------------------------------------------------------------ CRUD
class CreateGetTests(PluginProfilesTestBase):
    async def test_fresh_db_applies_all_migrations(self) -> None:
        # 1..7 on an empty file (v7 adds plugin_profiles + profile column)
        self.db2 = Database(self.path)
        applied = await self.db2.connect()  # idempotency: nothing reapplies
        self.addAsyncCleanup(self.db2.close)
        self.assertEqual(applied, 0)

    async def test_create_returns_record_with_defaults(self) -> None:
        record = await self.repo.create("roll", "grind")
        self.assertEqual(record.plugin, "roll")
        self.assertEqual(record.name, "grind")
        self.assertEqual(record.description, "")
        self.assertEqual(record.settings, {})
        self.assertAlmostEqual(record.created_at, record.updated_at)

    async def test_create_stores_settings_and_description(self) -> None:
        record = await self.repo.create(
            "roll", "grind", "fast rolls",
            {"trigger": "go", "channels": {"mode": "allow", "rules": []}},
        )
        fetched = await self.repo.get("roll", "grind")
        self.assertEqual(fetched.description, "fast rolls")
        # settings ride through verbatim (validation is the API's job)
        self.assertEqual(fetched.settings["trigger"], "go")
        self.assertEqual(fetched.settings["channels"]["mode"], "allow")

    async def test_get_unknown_is_none(self) -> None:
        self.assertIsNone(await self.repo.get("roll", "ghost"))
        self.assertIsNone(await self.repo.get("ghost", "grind"))

    async def test_duplicate_name_refused_per_plugin(self) -> None:
        await self.repo.create("roll", "grind")
        with self.assertRaises(DatabaseError):  # same (plugin, name)
            await self.repo.create("roll", "grind")
        # the SAME name under a DIFFERENT plugin is a different profile
        await self.repo.create("snipe", "grind")
        self.assertEqual(
            sorted(r.name for r in await self.repo.list()),
            ["grind", "grind"])

    async def test_empty_names_refused(self) -> None:
        with self.assertRaises(DatabaseError):
            await self.repo.create("", "grind")
        with self.assertRaises(DatabaseError):
            await self.repo.create("roll", "  ")


class ListTests(PluginProfilesTestBase):
    async def test_list_all_sorted_by_plugin_then_name(self) -> None:
        await self.repo.create("snipe", "b")
        await self.repo.create("roll", "z")
        await self.repo.create("roll", "a")
        self.assertEqual(
            [(r.plugin, r.name) for r in await self.repo.list()],
            [("roll", "a"), ("roll", "z"), ("snipe", "b")],
        )

    async def test_list_scopes_to_one_plugin(self) -> None:
        await self.repo.create("roll", "a")
        await self.repo.create("snipe", "a")
        rows = await self.repo.list("roll")
        self.assertEqual([r.plugin for r in rows], ["roll"])

    async def test_list_empty_db(self) -> None:
        self.assertEqual(await self.repo.list(), [])
        self.assertEqual(await self.repo.list("roll"), [])


class UpdateTests(PluginProfilesTestBase):
    async def test_update_description_only(self) -> None:
        await self.repo.create("roll", "grind", "old")
        updated = await self.repo.update("roll", "grind", description="new")
        self.assertEqual(updated.description, "new")
        self.assertEqual(updated.name, "grind")  # untouched
        self.assertGreaterEqual(updated.updated_at, updated.created_at)

    async def test_update_settings_replaces_wholesale(self) -> None:
        await self.repo.create("roll", "grind", settings={"a": 1})
        updated = await self.repo.update("roll", "grind", settings={"b": 2})
        self.assertEqual(updated.settings, {"b": 2})

    async def test_update_settings_none_keeps_current(self) -> None:
        await self.repo.create("roll", "grind", settings={"a": 1})
        updated = await self.repo.update("roll", "grind", description="d")
        self.assertEqual(updated.settings, {"a": 1})

    async def test_rename_refuses_duplicate(self) -> None:
        await self.repo.create("roll", "a")
        await self.repo.create("roll", "b")
        with self.assertRaises(DatabaseError):
            await self.repo.update("roll", "a", name_new="b")

    async def test_rename_rewrites_bindings_in_same_tx(self) -> None:
        await self.repo.create("roll", "grind")
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.set_profile("alpha", "roll", "grind")
        updated = await self.repo.update("roll", "grind", name_new="farm")
        self.assertEqual(updated.name, "farm")
        self.assertIsNone(await self.repo.get("roll", "grind"))
        # the binding followed the rename, no dangling pointer
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": "farm"})

    async def test_update_unknown_profile_is_none(self) -> None:
        self.assertIsNone(
            await self.repo.update("ghost", "g", description="d"))


class DeleteTests(PluginProfilesTestBase):
    async def test_delete_removes_row(self) -> None:
        await self.repo.create("roll", "grind")
        self.assertTrue(await self.repo.delete("roll", "grind"))
        self.assertIsNone(await self.repo.get("roll", "grind"))

    async def test_delete_missing_is_false_not_error(self) -> None:
        self.assertFalse(await self.repo.delete("roll", "ghost"))

    async def test_delete_cascades_bindings_to_null(self) -> None:
        await self.repo.create("roll", "grind")
        await self.repo.create("roll", "farm")
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.assign_plugin("beta", "roll")
        await self.bindings.set_profile("alpha", "roll", "grind")
        await self.bindings.set_profile("beta", "roll", "farm")

        self.assertTrue(await self.repo.delete("roll", "grind"))
        # the deleted profile's binding NULLs; the other account's stays
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": None})
        self.assertEqual(await self.bindings.bindings("beta"),
                         {"roll": "farm"})


# -------------------------------------------------------------- bindings
class BindingTests(PluginProfilesTestBase):
    async def test_set_profile_then_bindings(self) -> None:
        await self.bindings.assign_plugin("alpha", "roll")
        self.assertTrue(
            await self.bindings.set_profile("alpha", "roll", "grind"))
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": "grind"})

    async def test_set_profile_is_idempotent(self) -> None:
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.set_profile("alpha", "roll", "grind")
        await self.bindings.set_profile("alpha", "roll", "grind")
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": "grind"})

    async def test_clear_profile_with_none(self) -> None:
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.set_profile("alpha", "roll", "grind")
        self.assertTrue(await self.bindings.set_profile("alpha", "roll", None))
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": None})

    async def test_set_profile_without_assignment_is_noop(self) -> None:
        # never creates an assignment implicitly (binding requires
        # assignment is the calling manager's rule)
        self.assertFalse(
            await self.bindings.set_profile("alpha", "roll", "grind"))
        self.assertEqual(await self.bindings.plugins_of("alpha"), [])

    async def test_bindings_marks_unbound_as_none(self) -> None:
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.assign_plugin("alpha", "snipe")
        await self.bindings.set_profile("alpha", "roll", "grind")
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": "grind", "snipe": None})

    async def test_bindings_unknown_account_is_empty(self) -> None:
        self.assertEqual(await self.bindings.bindings("ghost"), {})

    async def test_all_bindings_shape(self) -> None:
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.assign_plugin("beta", "roll")
        await self.bindings.assign_plugin("beta", "snipe")
        await self.bindings.set_profile("beta", "roll", "grind")
        self.assertEqual(
            await self.bindings.all_bindings(),
            {"alpha": {"roll": None},
             "beta": {"roll": "grind", "snipe": None}},
        )

    async def test_assignment_shapes_unchanged(self) -> None:
        """plugins_of/all_assignments keep their frozen shapes (the
        profile column must NOT leak into the legacy reads)."""
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.set_profile("alpha", "roll", "grind")
        self.assertEqual(await self.bindings.plugins_of("alpha"), ["roll"])
        self.assertEqual(await self.bindings.all_assignments(),
                         {"alpha": ["roll"]})

    async def test_set_plugins_replacement_keeps_bindings_of_kept_plugins(
            self) -> None:
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.assign_plugin("alpha", "gone")
        await self.bindings.set_profile("alpha", "roll", "grind")
        await self.bindings.set_plugins("alpha", ["roll", "new"])
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": "grind", "new": None})
        await self.bindings.set_plugins("alpha", [])
        self.assertEqual(await self.bindings.bindings("alpha"), {})


class NameNormalisationTests(PluginProfilesTestBase):
    async def test_create_with_padded_name_returns_the_stored_record(self) -> None:
        record = await self.repo.create("roll", " fast ", "d", {})
        self.assertEqual(record.name, "fast")
        self.assertIsNotNone(await self.repo.get("roll", "fast"))


# --------------------------------------------------------------- restart
class RestartTests(PluginProfilesTestBase):
    async def test_profiles_and_bindings_survive_restart(self) -> None:
        await self.repo.create("roll", "grind", "desc", {"a": 1})
        await self.bindings.assign_plugin("alpha", "roll")
        await self.bindings.set_profile("alpha", "roll", "grind")

        await self.db.close()
        self.db = Database(self.path)
        applied = await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.repo = self.db.plugin_profiles
        self.bindings = self.db.account_plugins
        self.assertEqual(applied, 0)  # migration guard: v7 never reapplies

        record = await self.repo.get("roll", "grind")
        self.assertEqual(record.description, "desc")
        self.assertEqual(record.settings, {"a": 1})
        self.assertEqual(await self.bindings.bindings("alpha"),
                         {"roll": "grind"})
        self.assertEqual(await self.bindings.all_bindings(),
                         {"alpha": {"roll": "grind"}})


if __name__ == "__main__":
    unittest.main()
