"""Round-2 audit pins: the shared fakes in helpers.py track the REAL repos.

The round-1 finding was that FakeProfilesRepo.delete did not emulate the
real ProfilesRepository cascade (account assignments survive a profile
delete in every test wired to the fake, so green tests lied). These pins
hold the fixed fake to the real repository's observable contract:

  * delete cascades assignments (database/repositories/profiles.py)
  * upsert rejects empty/non-string names like the real repository
  * the make_manager harness wires the cascade by default
"""
from __future__ import annotations

import unittest

from core.errors import DatabaseError
from tests.unit.helpers import FakeAccountsRepo, FakeProfilesRepo, make_manager


class TestFakeProfilesRepoCascade(unittest.IsolatedAsyncioTestCase):
    async def test_delete_cascades_account_assignments(self) -> None:
        accounts = FakeAccountsRepo()
        profiles = FakeProfilesRepo(accounts=accounts)
        await accounts.add("alpha")
        await accounts.add("beta")
        await profiles.upsert("gone", ["soulcopy"])
        await profiles.upsert("kept", ["roll"])
        await accounts.assign_profile("alpha", "gone")
        await accounts.assign_profile("alpha", "kept")
        await accounts.assign_profile("beta", "gone")

        self.assertTrue(await profiles.delete("gone"))

        self.assertEqual(await accounts.profiles_of("alpha"), ["kept"])
        self.assertEqual(await accounts.profiles_of("beta"), [])
        self.assertIsNone(await profiles.get("gone"))
        self.assertIsNotNone(await profiles.get("kept"))

    async def test_delete_unknown_profile_is_false_and_harmless(self) -> None:
        accounts = FakeAccountsRepo()
        profiles = FakeProfilesRepo(accounts=accounts)
        await accounts.add("alpha")
        await accounts.assign_profile("alpha", "ghost")
        self.assertFalse(await profiles.delete("ghost"))
        self.assertEqual(await accounts.profiles_of("alpha"), ["ghost"])

    async def test_upsert_rejects_bad_names_like_real_repo(self) -> None:
        profiles = FakeProfilesRepo()
        for bad in ("", "   ", None, 7):
            with self.assertRaises(DatabaseError):
                await profiles.upsert(bad, ["soulcopy"])
        self.assertEqual(await profiles.list(), [])


class TestHarnessWiring(unittest.IsolatedAsyncioTestCase):
    async def test_make_manager_profiles_repo_cascades(self) -> None:
        harness = make_manager()
        await harness.add_profile("gone", "soulcopy")
        await harness.add_profile("kept", "roll")
        await harness.manager.add_account("alpha", profiles=["gone", "kept"])
        self.assertEqual(
            await harness.repo.profiles_of("alpha"), ["gone", "kept"])

        out = await harness.profile_manager.delete_profile("gone")
        self.assertTrue(out["existed"])

        self.assertEqual(await harness.repo.profiles_of("alpha"), ["kept"])
        self.assertEqual(
            await harness.profile_manager.assigned("alpha"), ["kept"])
        self.assertEqual(
            harness.profile_manager.allowed_plugins("alpha"), {"roll"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
