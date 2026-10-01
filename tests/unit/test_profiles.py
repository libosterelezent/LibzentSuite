"""Unit tests for core.profile_manager: CRUD, assignment, allowed_plugins."""
from __future__ import annotations

import logging
import unittest

from core.errors import ProfileError
from core.profile_manager import ProfileManager

from tests.unit.helpers import FakeAccountsRepo, FakeProfilesRepo

LOG = logging.getLogger("test.profiles")


class ProfileManagerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles_repo = FakeProfilesRepo()
        self.accounts_repo = FakeAccountsRepo()
        self.manager = ProfileManager(self.profiles_repo, LOG, self.accounts_repo)
        self.accounts_repo.add("alpha")
        self.accounts_repo.add("beta")


class SetProfileTests(ProfileManagerTestBase):
    def test_create_persists_and_returns_shape(self) -> None:
        out = self.manager.set_profile("basic", ["soulcopy", "roll"], "core flows")
        self.assertEqual(out, {
            "name": "basic", "description": "core flows",
            "plugins": ["roll", "soulcopy"],
        })
        self.assertIsNotNone(self.profiles_repo.get("basic"))
        self.assertEqual(self.manager.profiles(), [{
            "name": "basic", "description": "core flows",
            "plugins": ["roll", "soulcopy"],
        }])

    def test_plugin_list_is_deduped_and_stripped(self) -> None:
        self.manager.set_profile("dupe", ["a", " a ", "b", "a"])
        record = self.profiles_repo.get("dupe")
        self.assertEqual(record.plugins, ["a", "b"])

    def test_invalid_names_rejected(self) -> None:
        for bad in ("", "UPPER", "1st?", "-lead", "a" * 33, "has space", "ünicode", None):
            with self.assertRaises(ProfileError):
                self.manager.set_profile(bad, ["soulcopy"])

    def test_digit_leading_name_rejected(self) -> None:
        # profile names must START with a lowercase letter (pinned regex);
        # only ACCOUNT names may start with a digit
        with self.assertRaises(ProfileError):
            self.manager.set_profile("2fast", ["roll"])

    def test_empty_plugin_list_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            self.manager.set_profile("empty", [])
        with self.assertRaises(ProfileError):
            self.manager.set_profile("blank", ["  ", ""])

    def test_non_list_plugins_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            self.manager.set_profile("str", "soulcopy")
        with self.assertRaises(ProfileError):
            self.manager.set_profile("int", 5)

    def test_update_replaces_plugin_list(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        self.manager.set_profile("basic", ["roll"], "changed")
        record = self.profiles_repo.get("basic")
        self.assertEqual(record.plugins, ["roll"])
        self.assertEqual(record.description, "changed")

    def test_works_without_accounts_repo(self) -> None:
        solo = ProfileManager(self.profiles_repo, LOG)
        solo.set_profile("solo", ["roll"])
        self.assertEqual(solo.allowed_plugins("whoever"), None)


class DeleteProfileTests(ProfileManagerTestBase):
    def test_delete_existing(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        out = self.manager.delete_profile("basic")
        self.assertEqual(out, {"deleted": "basic", "existed": True})
        self.assertEqual(self.manager.profiles(), [])

    def test_delete_missing_is_idempotent(self) -> None:
        out = self.manager.delete_profile("ghost")
        self.assertEqual(out, {"deleted": "ghost", "existed": False})


class AssignTests(ProfileManagerTestBase):
    def test_assign_persists_and_returns_profiles(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        out = self.manager.assign("alpha", "basic")
        self.assertEqual(out["account"], "alpha")
        self.assertEqual(out["profile"], "basic")
        self.assertEqual(out["profiles"], ["basic"])
        self.assertEqual(self.accounts_repo.profiles_of("alpha"), ["basic"])

    def test_assign_unknown_profile_lists_known(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        with self.assertRaises(ProfileError) as ctx:
            self.manager.assign("alpha", "nope")
        self.assertIn("basic", str(ctx.exception))
        self.assertIn("nope", str(ctx.exception))

    def test_assign_unknown_account_lists_known(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        with self.assertRaises(ProfileError) as ctx:
            self.manager.assign("ghost", "basic")
        self.assertIn("alpha", str(ctx.exception))
        self.assertIn("ghost", str(ctx.exception))

    def test_assign_is_idempotent(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        self.manager.assign("alpha", "basic")
        self.manager.assign("alpha", "basic")
        self.assertEqual(self.accounts_repo.profiles_of("alpha"), ["basic"])

    def test_unassign_removes_and_reports(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        self.manager.assign("alpha", "basic")
        out = self.manager.unassign("alpha", "basic")
        self.assertTrue(out["existed"])
        self.assertEqual(out["profiles"], [])
        again = self.manager.unassign("alpha", "basic")
        self.assertFalse(again["existed"])

    def test_unassign_unknown_account_rejected(self) -> None:
        self.manager.set_profile("basic", ["soulcopy"])
        with self.assertRaises(ProfileError):
            self.manager.unassign("ghost", "basic")

    def test_unassign_unknown_profile_is_lenient(self) -> None:
        out = self.manager.unassign("alpha", "never-existed")
        self.assertFalse(out["existed"])
        self.assertEqual(out["profiles"], [])

    def test_assigned_sorted(self) -> None:
        self.manager.set_profile("zeta", ["roll"])
        self.manager.set_profile("alpha", ["soulcopy"])
        self.manager.assign("alpha", "zeta")
        self.manager.assign("alpha", "alpha")
        self.assertEqual(self.manager.assigned("alpha"), ["alpha", "zeta"])
        self.assertEqual(self.manager.assigned("beta"), [])


class AllowedPluginsTests(ProfileManagerTestBase):
    def test_union_across_profiles(self) -> None:
        self.manager.set_profile("p1", ["soulcopy", "shared"])
        self.manager.set_profile("p2", ["roll", "shared"])
        self.manager.assign("alpha", "p1")
        self.manager.assign("alpha", "p2")
        self.assertEqual(self.manager.allowed_plugins("alpha"),
                         {"soulcopy", "shared", "roll"})

    def test_none_when_unassigned(self) -> None:
        self.manager.set_profile("p1", ["soulcopy"])
        self.assertIsNone(self.manager.allowed_plugins("alpha"))

    def test_none_when_only_unknown_profiles(self) -> None:
        self.accounts_repo.assign_profile("alpha", "ghost-profile")
        with self.assertLogs("test.profiles", level="WARNING") as logs:
            self.assertIsNone(self.manager.allowed_plugins("alpha"))
        self.assertTrue(any("ghost-profile" in line for line in logs.output))

    def test_empty_plugin_profile_restricts_everything(self) -> None:
        # crafted directly in the repo: repo layer permits empty lists
        self.profiles_repo.upsert("none", [], "empty")
        self.accounts_repo.assign_profile("alpha", "none")
        self.assertEqual(self.manager.allowed_plugins("alpha"), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
