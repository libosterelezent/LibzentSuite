"""ProfileManager: built-ins, custom persistence, resolution."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.database import Database
from core.profiles import BUILTIN_PROFILES, ProfileError, ProfileManager


class TestProfileManager(unittest.TestCase):
    def test_builtins_present_and_conservative(self) -> None:
        manager = ProfileManager()
        names = [p.name for p in manager.all()]
        self.assertIn("conservative", names)
        self.assertIn("balanced", names)
        conservative = manager.get("conservative")
        self.assertEqual(conservative.rules, {"message": "3/12s", "dm": "3/12s"})
        self.assertEqual(conservative.delays["message"], [1.2, 3.0])
        self.assertEqual(conservative.prefixrate, 2.0)
        self.assertTrue(conservative.builtin)

    def test_custom_profiles_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            manager = ProfileManager(db)
            manager.set_profile("my-profile", rules={"message": "4/12s"},
                                delays={"message": [1.0, 2.0]}, prefixrate=1.8,
                                description="mine")
            reloaded = ProfileManager(Database(Path(tmp) / "s.db"))
            profile = reloaded.get("my-profile")
            self.assertIsNotNone(profile)
            self.assertEqual(profile.rules, {"message": "4/12s"})
            self.assertEqual(profile.prefixrate, 1.8)

    def test_builtin_cannot_be_overwritten_or_deleted(self) -> None:
        manager = ProfileManager()
        with self.assertRaises(ProfileError):
            manager.set_profile("balanced", rules={"message": "1/2s"})
        with self.assertRaises(ProfileError):
            manager.delete_profile("balanced")

    def test_unknown_profile_resolution(self) -> None:
        with self.assertRaises(ProfileError) as ctx:
            ProfileManager().resolve("ghost")
        self.assertIn("defined", str(ctx.exception))

    def test_delete_custom_profile(self) -> None:
        manager = ProfileManager()
        manager.set_profile("temp", rules={"message": "3/12s"})
        self.assertTrue(manager.delete_profile("temp"))
        self.assertFalse(manager.delete_profile("temp"))


if __name__ == "__main__":
    unittest.main()
