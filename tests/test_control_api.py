"""Control API + dashboard backend: shared management surface."""
from __future__ import annotations

import asyncio
import unittest

from core.api import ControlAPI
from core.rate_limiter import RateRuleError

from .helpers import Harness


def make_api(harness: Harness) -> ControlAPI:
    return ControlAPI(
        harness.container, harness.limiter, harness.loader, harness.credentials
    )


class TestControlAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = Harness("tests/fixtures/plugins", ["alpha", "beta"])
        self.api = make_api(self.harness)

    async def test_status_shape(self) -> None:
        status = await self.api.status()
        self.assertIn("accounts", status)
        self.assertIn("limits", status)
        self.assertEqual([a["name"] for a in status["accounts"]], ["alpha", "beta"])
        self.assertIn("message", status["limits"]["actions"])

    async def test_plugins_manifest(self) -> None:
        manifests = await self.api.plugins()
        spy = next(m for m in manifests if m["name"] == "spy")
        self.assertTrue(spy["settings"])

    async def test_set_global_limit(self) -> None:
        result = await self.api.set_limit("message", "5/12s")
        self.assertIsNone(result["account"])
        stats = await self.api.limits()
        self.assertEqual(stats["actions"]["message"]["default_rule"], "5/12.0s")

    async def test_set_and_clear_account_limit(self) -> None:
        await self.api.set_limit("message", "1/30s", account="alpha")
        stats = await self.api.limits()
        self.assertEqual(stats["actions"]["message"]["overrides"], {"alpha": "1/30.0s"})
        cleared = await self.api.clear_limit("alpha", "message")
        self.assertTrue(cleared["cleared"])
        stats = await self.api.limits()
        self.assertEqual(stats["actions"]["message"]["overrides"], {})

    async def test_bad_limit_rejected(self) -> None:
        with self.assertRaises(RateRuleError):
            await self.api.set_limit("message", "banana")

    async def test_plugin_toggle_reflected_in_status(self) -> None:
        await self.harness.container.start_account("alpha")
        await self.api.set_plugin_enabled("alpha", "spy", enabled=False)
        status = await self.api.status()
        alpha = next(a for a in status["accounts"] if a["name"] == "alpha")
        self.assertFalse(alpha["plugins"]["spy"]["enabled"])
        account_plugins = await self.api.account_plugins("alpha")
        self.assertFalse(next(p for p in account_plugins if p["name"] == "spy")["enabled"])

    async def test_credentials_summary_redacted(self) -> None:
        summary = self.api.credentials_summary()
        self.assertNotIn("sim-token-alpha", " ".join(summary.values()))
        self.assertIn("MUDAE_BOT_ID", summary)

    async def test_start_stop_unknown_account(self) -> None:
        with self.assertRaises(Exception):
            await self.api.start("ghost")
        await self.api.stop()  # stop-all always succeeds


class TestDashboardRouting(unittest.IsolatedAsyncioTestCase):
    async def test_route_get_status(self) -> None:
        from dashboard.server import DashboardServer

        harness = Harness("tests/fixtures/plugins", ["alpha"])
        api = make_api(harness)
        server = DashboardServer(api, token="secret", port=0)
        server._loop = asyncio.get_running_loop()
        status, payload = await server._route("GET", "/api/status", {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["data"]["accounts"]), 1)

    async def test_route_set_limit(self) -> None:
        from dashboard.server import DashboardServer

        harness = Harness("tests/fixtures/plugins", ["alpha"])
        api = make_api(harness)
        server = DashboardServer(api, token=None, port=0)
        server._loop = asyncio.get_running_loop()
        status, payload = await server._route(
            "POST", "/api/limit", {"action": "message", "rule": "3/10s", "account": "alpha"}, {}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(harness.limiter.rule_for("message", "alpha").actions, 3)

    async def test_unknown_route_404(self) -> None:
        from dashboard.server import DashboardServer

        harness = Harness("tests/fixtures/plugins", ["alpha"])
        server = DashboardServer(make_api(harness), token=None, port=0)
        server._loop = asyncio.get_running_loop()
        status, payload = await server._route("GET", "/api/nope", {}, {})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
