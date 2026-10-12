"""Unit tests for core.profile_manager: CRUD, assignment, allowed_plugins.

All ProfileManager methods that touch the repositories are async now
(aiosqlite); ``allowed_plugins`` stays sync-memory over the cache kept
fresh by load()/assign/unassign/set_profile.
"""
from __future__ import annotations

import asyncio
import logging
import unittest

from core.errors import ProfileError
from core.profile_manager import ProfileManager

from tests.unit.helpers import FakeAccountsRepo, FakeProfilesRepo

LOG = logging.getLogger("test.profiles")


class ProfileManagerTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.profiles_repo = FakeProfilesRepo()
        self.accounts_repo = FakeAccountsRepo()
        self.manager = ProfileManager(self.profiles_repo, LOG, self.accounts_repo)
        await self.accounts_repo.add("alpha")
        await self.accounts_repo.add("beta")


class SetProfileTests(ProfileManagerTestBase):
    async def test_create_persists_and_returns_shape(self) -> None:
        out = await self.manager.set_profile("basic", ["soulcopy", "roll"], "core flows")
        self.assertEqual(out, {
            "name": "basic", "description": "core flows",
            "plugins": ["roll", "soulcopy"],
        })
        self.assertIsNotNone(await self.profiles_repo.get("basic"))
        self.assertEqual(await self.manager.profiles(), [{
            "name": "basic", "description": "core flows",
            "plugins": ["roll", "soulcopy"],
        }])

    async def test_plugin_list_is_deduped_and_stripped(self) -> None:
        await self.manager.set_profile("dupe", ["a", " a ", "b", "a"])
        record = await self.profiles_repo.get("dupe")
        self.assertEqual(record.plugins, ["a", "b"])

    async def test_invalid_names_rejected(self) -> None:
        for bad in ("", "UPPER", "1st?", "-lead", "a" * 33, "has space", "ünicode", None):
            with self.assertRaises(ProfileError):
                await self.manager.set_profile(bad, ["soulcopy"])

    async def test_separator_and_newline_names_rejected(self) -> None:
        """Round-2: ``$``-anchored regex let a trailing newline through;
        ':'/'.' separators must never enter the profile key-space."""
        for bad in ("basic\n", "basic\n ", "group:xyz", "a.b", ".hidden"):
            with self.assertRaises(ProfileError):
                await self.manager.set_profile(bad, ["soulcopy"])
        self.assertIsNone(self.profiles_repo.data.get("basic\n"))

    async def test_boundary_name_lengths(self) -> None:
        await self.manager.set_profile("a" * 32, ["soulcopy"])  # exactly max
        with self.assertRaises(ProfileError):
            await self.manager.set_profile("a" * 33, ["soulcopy"])

    async def test_digit_leading_name_rejected(self) -> None:
        # profile names must START with a lowercase letter (pinned regex);
        # only ACCOUNT names may start with a digit
        with self.assertRaises(ProfileError):
            await self.manager.set_profile("2fast", ["roll"])

    async def test_empty_plugin_list_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            await self.manager.set_profile("empty", [])
        with self.assertRaises(ProfileError):
            await self.manager.set_profile("blank", ["  ", ""])

    async def test_non_list_plugins_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            await self.manager.set_profile("str", "soulcopy")
        with self.assertRaises(ProfileError):
            await self.manager.set_profile("int", 5)

    async def test_update_replaces_plugin_list(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        await self.manager.set_profile("basic", ["roll"], "changed")
        record = await self.profiles_repo.get("basic")
        self.assertEqual(record.plugins, ["roll"])
        self.assertEqual(record.description, "changed")

    async def test_works_without_accounts_repo(self) -> None:
        solo = ProfileManager(self.profiles_repo, LOG)
        await solo.set_profile("solo", ["roll"])
        self.assertEqual(solo.allowed_plugins("whoever"), None)


class DeleteProfileTests(ProfileManagerTestBase):
    async def test_delete_existing(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        out = await self.manager.delete_profile("basic")
        self.assertEqual(out, {"deleted": "basic", "existed": True})
        self.assertEqual(await self.manager.profiles(), [])

    async def test_delete_missing_is_idempotent(self) -> None:
        out = await self.manager.delete_profile("ghost")
        self.assertEqual(out, {"deleted": "ghost", "existed": False})


class AssignTests(ProfileManagerTestBase):
    async def test_assign_persists_and_returns_profiles(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        out = await self.manager.assign("alpha", "basic")
        self.assertEqual(out["account"], "alpha")
        self.assertEqual(out["profile"], "basic")
        self.assertEqual(out["profiles"], ["basic"])
        self.assertEqual(await self.accounts_repo.profiles_of("alpha"), ["basic"])

    async def test_assign_unknown_profile_lists_known(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        with self.assertRaises(ProfileError) as ctx:
            await self.manager.assign("alpha", "nope")
        self.assertIn("basic", str(ctx.exception))
        self.assertIn("nope", str(ctx.exception))

    async def test_assign_unknown_account_lists_known(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        with self.assertRaises(ProfileError) as ctx:
            await self.manager.assign("ghost", "basic")
        self.assertIn("alpha", str(ctx.exception))
        self.assertIn("ghost", str(ctx.exception))

    async def test_assign_is_idempotent(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        await self.manager.assign("alpha", "basic")
        await self.manager.assign("alpha", "basic")
        self.assertEqual(await self.accounts_repo.profiles_of("alpha"), ["basic"])

    async def test_unassign_removes_and_reports(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        await self.manager.assign("alpha", "basic")
        out = await self.manager.unassign("alpha", "basic")
        self.assertTrue(out["existed"])
        self.assertEqual(out["profiles"], [])
        again = await self.manager.unassign("alpha", "basic")
        self.assertFalse(again["existed"])

    async def test_unassign_unknown_account_rejected(self) -> None:
        await self.manager.set_profile("basic", ["soulcopy"])
        with self.assertRaises(ProfileError):
            await self.manager.unassign("ghost", "basic")

    async def test_unassign_unknown_profile_is_lenient(self) -> None:
        out = await self.manager.unassign("alpha", "never-existed")
        self.assertFalse(out["existed"])
        self.assertEqual(out["profiles"], [])

    async def test_assigned_sorted(self) -> None:
        await self.manager.set_profile("zeta", ["roll"])
        await self.manager.set_profile("alpha", ["soulcopy"])
        await self.manager.assign("alpha", "zeta")
        await self.manager.assign("alpha", "alpha")
        self.assertEqual(await self.manager.assigned("alpha"), ["alpha", "zeta"])
        self.assertEqual(await self.manager.assigned("beta"), [])


class AllowedPluginsTests(ProfileManagerTestBase):
    async def test_union_across_profiles(self) -> None:
        await self.manager.set_profile("p1", ["soulcopy", "shared"])
        await self.manager.set_profile("p2", ["roll", "shared"])
        await self.manager.assign("alpha", "p1")
        await self.manager.assign("alpha", "p2")
        self.assertEqual(self.manager.allowed_plugins("alpha"),
                         {"soulcopy", "shared", "roll"})

    async def test_none_when_unassigned(self) -> None:
        await self.manager.set_profile("p1", ["soulcopy"])
        self.assertIsNone(self.manager.allowed_plugins("alpha"))

    async def test_all_unknown_profiles_allow_nothing(self) -> None:
        """Assignments that ALL reference unknown profiles (typo'd or
        deleted out-of-band) must DENY (empty set), never widen the
        grant to the unrestricted None — the warning names them."""
        await self.accounts_repo.assign_profile("alpha", "ghost-profile")
        await self.manager.load()  # sync cache view picks the repo write up
        with self.assertLogs("test.profiles", level="WARNING") as logs:
            self.assertEqual(self.manager.allowed_plugins("alpha"), set())
        self.assertTrue(any("ghost-profile" in line for line in logs.output))
        self.assertTrue(any("NONE" in line or "allowing NOTHING" in line
                            for line in logs.output))

    async def test_mixed_unknown_and_known_keeps_known_union(self) -> None:
        await self.manager.set_profile("p1", ["soulcopy"])
        await self.accounts_repo.assign_profile("alpha", "ghost-profile")
        await self.accounts_repo.assign_profile("alpha", "p1")
        await self.manager.load()
        with self.assertLogs("test.profiles", level="WARNING"):
            self.assertEqual(self.manager.allowed_plugins("alpha"),
                             {"soulcopy"})

    async def test_empty_plugin_profile_restricts_everything(self) -> None:
        # crafted directly in the repo: repo layer permits empty lists
        await self.profiles_repo.upsert("none", [], "empty")
        await self.accounts_repo.assign_profile("alpha", "none")
        # sync cache view needs a load() to see repo-direct writes
        await self.manager.load()
        self.assertEqual(self.manager.allowed_plugins("alpha"), set())

    async def test_load_populates_the_sync_cache(self) -> None:
        """Boot path: rows written by OTHER paths (CLI/dashboard/SQL) become
        visible to the sync allowed_plugins() after load()."""
        await self.profiles_repo.upsert("p1", ["soulcopy"])
        await self.accounts_repo.assign_profile("beta", "p1")
        self.assertIsNone(self.manager.allowed_plugins("beta"))  # cache empty
        await self.manager.load()
        self.assertEqual(self.manager.allowed_plugins("beta"), {"soulcopy"})

    async def test_delete_profile_updates_cache(self) -> None:
        await self.manager.set_profile("p1", ["soulcopy"])
        await self.manager.assign("alpha", "p1")
        self.assertEqual(self.manager.allowed_plugins("alpha"), {"soulcopy"})
        await self.manager.delete_profile("p1")
        self.assertIsNone(self.manager.allowed_plugins("alpha"))

    async def test_delete_profile_cascades_every_holder(self) -> None:
        """Round-2: two accounts hold the deleted profile — BOTH assignment
        caches are cleaned; a remaining profile still grants its plugins.

        Uses a locally cascading profiles fake because the shared
        FakeProfilesRepo in helpers.py does not emulate the real
        repository's assignment cascade (reported as a finding).
        """

        class CascadingProfilesRepo(FakeProfilesRepo):
            async def delete(self, name):
                existed = await super().delete(name)
                for acct in list(self.accounts_repo.records):
                    await self.accounts_repo.unassign_profile(acct, name)
                return existed

        repo = CascadingProfilesRepo()
        repo.accounts_repo = self.accounts_repo
        manager = ProfileManager(repo, LOG, self.accounts_repo)
        await manager.set_profile("gone", ["soulcopy"])
        await manager.set_profile("kept", ["roll"])
        await manager.assign("alpha", "gone")
        await manager.assign("alpha", "kept")
        await manager.assign("beta", "gone")
        await manager.delete_profile("gone")
        self.assertEqual(await self.accounts_repo.profiles_of("alpha"), ["kept"])
        self.assertEqual(await self.accounts_repo.profiles_of("beta"), [])
        self.assertEqual(manager.allowed_plugins("alpha"), {"roll"})
        # beta now holds NO known profile -> unrestricted (documented
        # ported semantics; see manager docstring)
        self.assertIsNone(manager.allowed_plugins("beta"))

    async def test_unknown_plugin_names_allowed_in_profiles(self) -> None:
        """Profiles may reference not-yet-installed plugins; the plugin
        manager's attach filter drops the unknown names (verified in
        test_plugin_manager)."""
        out = await self.manager.set_profile("future", ["soulcopy", "not-yet"])
        self.assertEqual(out["plugins"], ["not-yet", "soulcopy"])
        self.assertEqual(self.manager.allowed_plugins("alpha"), None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
