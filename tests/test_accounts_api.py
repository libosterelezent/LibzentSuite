"""Accounts management, details page, settings parity (CLI + API)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cli.commands import QuitRequested, run_command
from core.api import ControlAPI
from core.audit import AuditRecorder
from core.database import Database
from core.roles import RoleRegistry
from core.settings import SettingValidationError

from .helpers import Harness


class ApiSurface:
    """Harness + ControlAPI + DB-backed state, shared by API and CLI tests."""

    def __init__(self, tmp: str) -> None:
        self.database = Database(Path(tmp) / "s.db")
        self.roles = RoleRegistry(self.database)
        self.roles.set_role("copier", ["spy"])
        self.audit = AuditRecorder(self.database)
        self.harness = Harness(
            "tests/fixtures/plugins", ["alpha"], state=self.database,
            roles=self.roles, audit=self.audit,
        )
        self.api = ControlAPI(
            self.harness.container, self.harness.limiter, self.harness.loader,
            self.harness.credentials, state=self.database, roles=self.roles,
            audit=self.audit,
        )


class TestAccountsManagement(unittest.IsolatedAsyncioTestCase):
    async def test_add_and_remove_account_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.add_account({"name": "gamma", "sync_group": "grp", "roles": ["copier"]})
            names = [a["name"] for a in (await surface.api.status())["accounts"]]
            self.assertIn("gamma", names)
            # persisted: a fresh DB read knows about it
            self.assertIn("gamma", [c.name for c in surface.database.load_accounts()])
            gamma = surface.harness.container._accounts["gamma"]
            self.assertEqual(sorted(gamma.plugins), ["spy"])  # role gate applied
            await surface.api.remove_account("gamma")
            self.assertNotIn("gamma", [c.name for c in surface.database.load_accounts()])

    async def test_add_duplicate_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(Exception):
                await surface.api.add_account({"name": "alpha"})

    async def test_add_invalid_name_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(Exception):
                await surface.api.add_account({"name": "Not Valid!"})

    async def test_remove_running_account_stops_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.start("alpha")
            await surface.api.remove_account("alpha")
            status = [a["name"] for a in (await surface.api.status())["accounts"]]
            self.assertNotIn("alpha", status)

    async def test_status_includes_warnings_and_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            status = await surface.api.status()
            self.assertIn("warnings", status)
            alpha = next(a for a in status["accounts"] if a["name"] == "alpha")
            self.assertIn("user_id", alpha)


class TestAccountDetails(unittest.IsolatedAsyncioTestCase):
    async def test_details_from_cached_memory_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.start("alpha")
            details = await surface.api.account_details("alpha")
            self.assertEqual(details["status"], "running")
            self.assertEqual(details["user_id"], 2000)
            cached = details["cached"]
            self.assertTrue(cached["available"])
            self.assertEqual(cached["user"]["id"], 2000)
            channels = [c for g in cached["guilds"] for c in g["channels"]]
            self.assertTrue(any(c["id"] == 500 for c in channels))  # test channel visible
            await surface.api.stop("alpha")

    async def test_details_when_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            details = await surface.api.account_details("alpha")
            self.assertEqual(details["status"], "created")
            self.assertFalse(details["cached"]["available"])


class TestSettingsParity(unittest.IsolatedAsyncioTestCase):
    async def test_set_plugin_setting_validated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            result = surface.api.set_plugin_setting("alpha", "spy", "greeting", "yo")
            self.assertEqual(result["value"], "yo")
            runtime = surface.harness.container._accounts["alpha"]
            self.assertEqual(runtime.plugins["spy"].settings["greeting"], "yo")
            # persisted as the account layer
            self.assertEqual(
                surface.database.setting_overrides(),
                {"alpha": {"spy": {"greeting": "yo"}}},
            )

    async def test_set_plugin_setting_bad_value_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(SettingValidationError):
                surface.api.set_plugin_setting("alpha", "spy", "volume", 999)
            with self.assertRaises(Exception):
                surface.api.set_plugin_setting("alpha", "spy", "nope", 1)

    async def test_cli_set_command_shares_the_api_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            text = await run_command(surface.api, "set alpha spy greeting yo")
            self.assertIn("yo", text)
            self.assertEqual(
                surface.database.setting_overrides(),
                {"alpha": {"spy": {"greeting": "yo"}}},
            )

    async def test_cli_settings_and_details_and_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.start("alpha")
            accounts = await run_command(surface.api, "accounts")
            self.assertIn("alpha", accounts)
            self.assertIn("2000", accounts)  # id under the name
            details = await run_command(surface.api, "details alpha")
            self.assertIn("cached", details.lower())
            settings = await run_command(surface.api, "settings alpha")
            self.assertIn("greeting", settings)
            with self.assertRaises(QuitRequested):
                await run_command(surface.api, "quit")


class TestDelaysAndWarningsViaApi(unittest.IsolatedAsyncioTestCase):
    async def test_set_and_clear_delay_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.set_delay("message", 0.8, 2.4)
            await surface.api.set_delay("message", 1.0, 3.0, account="alpha")
            self.assertEqual(surface.harness.limiter.delay_for("message").minimum, 0.8)
            self.assertEqual(surface.harness.limiter.delay_for("message", "alpha").maximum, 3.0)
            self.assertEqual(
                surface.database.delays(),
                {"global": {"message": [0.8, 2.4]}, "accounts": {"alpha": {"message": [1.0, 3.0]}}},
            )
            await surface.api.clear_delay("message", "alpha")
            self.assertEqual(surface.harness.limiter.delay_for("message", "alpha").maximum, 2.4)

    async def test_set_limit_floor_enforced_through_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(Exception) as ctx:
                await surface.api.set_limit("message", "10/5s")
            self.assertIn("safety floor", str(ctx.exception))
            await surface.api.set_limit("message", "5/12s")  # fine

    async def test_roles_via_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            roles = await surface.api.roles()
            self.assertEqual([r["name"] for r in roles], ["copier"])
            await surface.api.set_role("roller", ["roll"], "rolling duty")
            result = await surface.api.assign_role("alpha", "roller", True)
            self.assertIn("roller", result["roles"])
            # persisted on the account config
            config = next(c for c in surface.database.load_accounts() if c.name == "alpha")
            self.assertIn("roller", config.roles)
            await surface.api.assign_role("alpha", "roller", False)
            config = next(c for c in surface.database.load_accounts() if c.name == "alpha")
            self.assertNotIn("roller", config.roles)

    async def test_audit_via_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.start("alpha")
            session = surface.harness.session("alpha")
            await session.send_message(500, "audited")
            entries = surface.api.audit(limit=10)
            self.assertTrue(any(e["content"] == "audited" for e in entries))
            await surface.api.stop("alpha")


if __name__ == "__main__":
    unittest.main()
