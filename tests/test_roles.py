"""Roles: registry semantics + container plugin gating."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.account import AccountConfig
from core.database import Database
from core.roles import RoleError, RoleRegistry

from .helpers import Harness


class TestRoleRegistry(unittest.TestCase):
    def test_validation(self) -> None:
        with self.assertRaises(RoleError):
            RoleRegistry().set_role("Bad_Name", ["x"])
        with self.assertRaises(RoleError):
            RoleRegistry().set_role("empty", [])

    def test_set_get_delete(self) -> None:
        registry = RoleRegistry()
        role = registry.set_role("roll", ["roll", "claim"], "duty")
        self.assertEqual(role.plugins, ["claim", "roll"])
        self.assertEqual(registry.get("roll").description, "duty")
        self.assertIn("roll", dict(registry.all()))
        self.assertTrue(registry.delete_role("roll"))
        self.assertIsNone(registry.get("roll"))

    def test_manifests_shape(self) -> None:
        registry = RoleRegistry()
        registry.set_role("snipe", ["snipe"])
        manifests = registry.manifests()
        self.assertEqual(manifests, [{"name": "snipe", "description": "", "plugins": ["snipe"]}])

    def test_allowed_plugins_union_and_unknown(self) -> None:
        registry = RoleRegistry()
        registry.set_role("roll", ["roll"])
        registry.set_role("snipe", ["snipe", "soulcopy"])
        self.assertEqual(registry.allowed_plugins(["roll", "snipe"]), {"roll", "snipe", "soulcopy"})
        # unknown roles ignored; a known one still applies
        self.assertEqual(registry.allowed_plugins(["ghost", "roll"]), {"roll"})
        # only unknown roles -> no restriction (None)
        self.assertIsNone(registry.allowed_plugins(["ghost"]))
        self.assertIsNone(registry.allowed_plugins([]))

    def test_db_backed_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            registry = RoleRegistry(db)
            registry.set_role("roll", ["roll"])
            reloaded = RoleRegistry(db)  # fresh registry reads persistence
            self.assertIsNotNone(reloaded.get("roll"))
            db.close()


class TestRoleGating(unittest.TestCase):
    def _registry(self) -> RoleRegistry:
        registry = RoleRegistry()
        registry.set_role("copier", ["spy"])
        registry.set_role("roller", ["roll"])  # 'roll' plugin does not exist (future)
        return registry

    def test_account_with_role_only_gets_allowed_plugins(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], roles=self._registry())
        harness.container.add_account(AccountConfig(name="beta", roles=["copier"]))
        beta = harness.container._accounts["beta"]
        # 'roll' plugin isn't discovered; copier grants spy only
        self.assertEqual(sorted(beta.plugins), ["spy"])

    def test_account_without_roles_keeps_everything(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], roles=self._registry())
        alpha = harness.container._accounts["alpha"]
        self.assertEqual(sorted(alpha.plugins), ["spy"])  # untouched by roles

    def test_unknown_role_is_lenient(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], roles=self._registry())
        harness.container.add_account(AccountConfig(name="gamma", roles=["does-not-exist"]))
        gamma = harness.container._accounts["gamma"]
        self.assertEqual(sorted(gamma.plugins), ["spy"])  # no known role -> no restriction


if __name__ == "__main__":
    unittest.main()
