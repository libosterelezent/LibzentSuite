"""Dashboard v2 SPA regression tests: startup safety, Groups safety,
API compatibility with the real server, and static validation.

The dashboard is a single-file static SPA (no build step), so its contract
with the backend is verifiable statically:

* every ``/api/...`` endpoint the SPA calls must exist in the route table
  of interfaces/dashboard/server.py (startup can never 404),
* the Groups UI must never mutate accounts or fake profiles (the v2
  redesign once "created" groups by assigning a ``__dummy__`` profile and
  REMOVING every member account — forbidden forever),
* the script block must be structurally balanced and reference only
  element ids that exist in the document.

A small live-server test additionally proves every startup data source
serves against a real DashboardServer with zero accounts.
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from interfaces.dashboard.server import DashboardServer

ROOT = Path(__file__).resolve().parents[2]
HTML_PATH = ROOT / "interfaces" / "dashboard" / "static" / "index.html"
SERVER_PATH = ROOT / "interfaces" / "dashboard" / "server.py"
TOKEN = "static-test-token"


def _spa() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _script(html: str) -> str:
    match = re.search(r"<script>(.*)</script>", html, re.S)
    assert match, "SPA script block not found"
    return match.group(1)


def _server_routes() -> set:
    """(method, path) routes as written in server.py's _route dispatcher."""
    src = SERVER_PATH.read_text(encoding="utf-8")
    return set(re.findall(r'\("(GET|POST)", "(/api/[A-Za-z0-9_/-]+)"\)', src))


def _spa_endpoints() -> set:
    """Every /api/... path string literal used by the SPA (query stripped)."""
    html = _spa()
    return set(re.findall(r'["`](/api/[A-Za-z0-9_/-]+)', html))


# --------------------------------------------------------------------- static
class TestGroupsSafety(unittest.TestCase):
    """The Groups UI must be read-only: no fake profiles, no account
    destruction, no calls to a group backend that does not exist."""

    def test_no_group_backend_api_assumptions(self):
        html = _spa()
        for forbidden in ("/api/groups", "group_create", "group_members",
                          "group_delete", "group_add", "group_remove"):
            self.assertNotIn(forbidden, html, f"SPA references {forbidden!r}")

    def test_no_dummy_profile_and_no_create_group_ui(self):
        html = _spa()
        for forbidden in ("__dummy__", "cg-submit", "cg-name", "cg-members",
                          "btn-create-group", "dlg-create-group"):
            self.assertNotIn(forbidden, html, f"dangerous group UI remnant {forbidden!r}")

    def test_readonly_notice_present(self):
        html = _spa()
        self.assertIn(
            "Group management backend is not available yet", html,
            "the read-only explanation message must be shown")

    def test_group_moves_rendered_disabled(self):
        html = _spa()
        # both group-move entry points must be disabled with an explanation
        menu = re.search(r'<button data-act="group"[^>]*>', html)
        self.assertIsNotNone(menu, "group menu item must exist (disabled)")
        self.assertIn("disabled", menu.group(0))
        bulk = re.search(r'<button[^>]*id="bulk-group"[^>]*>', html)
        self.assertIsNotNone(bulk, "bulk group button must exist (disabled)")
        self.assertIn("disabled", bulk.group(0))

    def test_accounts_remove_only_in_explicit_menu(self):
        html = _spa()
        self.assertEqual(
            html.count('"/api/accounts/remove"'), 1,
            "accounts/remove may only appear in the explicit Remove Account "
            "confirmation — never in a group flow")
        self.assertEqual(
            html.count('"/api/profiles/assign"'), 1,
            "profiles/assign may only appear in the real profile picker")

    def test_no_group_startall_fleet_wide(self):
        js = _script(_spa())
        # the fleet-wide pattern from the redesign ({account:null} from a
        # GROUP-scoped button) must be gone; group actions scope to members
        for path in ("/api/accounts/start", "/api/accounts/stop"):
            self.assertNotIn(f'post("{path}",{{account:null}})', js,
                             f"group card must not send {path} fleet-wide")
        self.assertIn("members.map(m=>post(", js,
                      "group Start/Stop must iterate the group's members")


class TestStartupRobustness(unittest.TestCase):
    """One uncaught init error must not be possible from static causes."""

    def test_script_block_balanced(self):
        js = _script(_spa())
        for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(js.count(a), js.count(b),
                             f"unbalanced {a}{b} in script block")

    def test_referenced_element_ids_exist(self):
        html = _spa()
        js = _script(html)
        defined = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        referenced = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
        dynamic = {"nav-badge-"}  # built by buildNav(), guarded at use
        missing = sorted(
            rid for rid in referenced - defined
            if not any(rid.startswith(p) for p in dynamic))
        self.assertEqual(missing, [], "wire()/render() reference missing ids")

    def test_html_element_ids_unique(self):
        html = _spa()
        ids = re.findall(r'\sid="([A-Za-z0-9_-]+)"', html)
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(dupes, set(), "duplicate element ids in document")

    def test_startup_loaders_catch_own_failures(self):
        js = _script(_spa())
        for loader in ("loadManifests", "loadProfiles", "loadScenarios"):
            # every loader definition contains a try/catch -> showErr path
            body = re.search(
                rf"async function {loader}\(\) {{(.*?)\n}}", js, re.S)
            self.assertIsNotNone(body, f"{loader} missing")
            self.assertIn("catch", body.group(1),
                          f"{loader} must not reject (blocks Promise.all)")

    def test_init_reports_errors_to_banner(self):
        js = _script(_spa())
        self.assertIn("dashboard init failed", js,
                      "init failures must be reported via the error banner")


class TestApiCompatibility(unittest.TestCase):
    """The SPA may only call endpoints the real server implements."""

    def test_every_spa_endpoint_exists_on_server(self):
        routes = _server_routes()
        self.assertTrue(routes, "server route table not parsed")
        endpoints = _spa_endpoints()
        self.assertTrue(endpoints, "no endpoints extracted from SPA")
        methods = {"GET", "POST"}
        # every endpoint must be served under at least one method the SPA
        # uses it with; static check pairs each path with both methods the
        # server defines (the SPA's api()/post() split is method-correct)
        missing = sorted(
            path for path in endpoints
            if not any((m, path) in routes for m in methods))
        self.assertEqual(missing, [],
                         "SPA calls endpoints the server does not implement")

    def test_css_custom_props_are_valid(self):
        html = _spa()
        self.assertIn("--border2: #364160", html,
                      "the --border2 hex typo (#36416o) must stay fixed")
        style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
        # hex color candidates: #tokens containing at least one digit (CSS
        # id selectors in this sheet contain none), all chars must be hex
        tokens = re.findall(r"#[0-9A-Za-z]{3,8}\b", style)
        candidates = [t for t in tokens if any(c.isdigit() for c in t[1:])]
        bad = [t for t in candidates
               if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}"
                                   r"|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", t)]
        self.assertEqual(bad, [], f"malformed CSS hex values: {bad}")


# ----------------------------------------------------------------------- live
class _ZeroAccountAPI:
    """Minimal duck-typed ControlAPI: healthy endpoints, zero accounts."""

    async def status(self):
        return {"accounts": [], "ratelimit": {"floor": 1.5,
                                               "cooldowns": {"global": 0.0,
                                                             "accounts": {}}},
                "tasks": {"queued": 0, "running": 0, "completed": 0,
                          "failed": 0, "cancelled": 0},
                "warnings": [], "uptime_s": 0.5}

    async def accounts_list(self):
        return []

    async def plugins_manifests(self):
        return []

    async def profiles_list(self):
        return []

    async def simulation_list(self):
        return []

    async def tasks_list(self, status=None, account=None):
        return []

    async def tasks_history(self, limit=50, account=None):
        return []

    async def cooldowns_list(self, account=None):
        return []

    async def logs_tail(self, n=100, account=None, severity=None):
        return []

    async def audit_tail(self, n=100, account=None):
        return []


def _get(url: str, as_json: bool = True) -> tuple:
    req = urllib.request.Request(url, headers={"X-Dashboard-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if as_json else raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if as_json else raw)


class TestStartupEndpointsLive(unittest.IsolatedAsyncioTestCase):
    """Boot a real DashboardServer with zero accounts and confirm every
    SPA startup data source answers (SPA loads, no 404s, empty lists OK)."""

    async def asyncSetUp(self):
        self.server = DashboardServer(_ZeroAccountAPI(), token=TOKEN, port=0)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    async def asyncTearDown(self):
        self.server.stop()

    async def _get(self, path: str, as_json: bool = True) -> tuple:
        return await asyncio.to_thread(_get, f"{self.base}{path}", as_json)

    async def test_index_served(self):
        status, body = await self._get("/", as_json=False)
        self.assertEqual(status, 200)
        self.assertIn("SlaughterMudae", body)

    async def test_startup_endpoints_answer(self):
        for path in ("/api/status", "/api/accounts", "/api/plugins",
                     "/api/profiles", "/api/simulation/list", "/api/tasks",
                     "/api/tasks/history", "/api/cooldowns",
                     "/api/logs", "/api/audit"):
            status, body = await self._get(path)
            self.assertEqual(status, 200, f"{path} -> {status}")
            self.assertTrue(body.get("ok"), f"{path} payload not ok")

    async def test_zero_account_status_shape(self):
        """The onboarding path depends on status.accounts being a list."""
        _, body = await self._get("/api/status")
        self.assertIsInstance(body["data"]["accounts"], list)
        self.assertEqual(body["data"]["accounts"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
