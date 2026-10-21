"""Dashboard v3 SPA contract tests: the Carbon/hash-routed rebuild.

The dashboard is a build-free static SPA split across index.html (shell
skeleton), styles/*.css and js/*.js + js/views/*.js (no bundler — plain
script tags in dependency order). Its backend contract is verifiable
statically:

* every ``/api/...`` endpoint the SPA calls must exist in the route table
  of interfaces/dashboard/server.py (startup can never 404),
* the script/stylesheets index.html references must resolve, load in
  dependency order (store → api → ui → router → render → views → shell →
  theme), and contain no inline script/style,
* token hygiene: the dashboard token lives in sessionStorage ONLY, is
  adopted from ``?token=`` and stripped from the URL; the header never
  renders it,
* group flows drive the REAL /api/groups backend (no fake groups, no
  __dummy__ profiles, moves never delete/recreate accounts),
* capabilities are DIRECT account→plugin assignments over
  /api/accounts/plugins* — the LEGACY /api/profiles surface is never
  called by the SPA,
* the design system defines the canonical mockup classes (hero, section,
  ability, preset, pacing) and every token the theme engine rewrites,
* the UI contract: no window.confirm/alert/prompt, every view registers
  a route, esc() exists for interpolation safety.

A live-server test additionally proves every startup data source serves
against a real DashboardServer with zero accounts.
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
STATIC_DIR = ROOT / "interfaces" / "dashboard" / "static"
HTML_PATH = STATIC_DIR / "index.html"
SERVER_PATH = ROOT / "interfaces" / "dashboard" / "server.py"
TOKEN = "static-test-token"

#: canonical class names the design system must define (mockup contract)
CANONICAL_CLASSES = (
    ".hero", ".section", ".ability", ".toggle", ".preset",
    ".pacing-grid", ".pacing-item", ".ms-pill", ".kv", ".empty",
    ".btn", ".badge", ".card", ".tbl", ".tabbar", ".field",
    ".dot", ".toast", ".dlg", ".spinner", ".skel", ".icon-btn",
)
#: tokens the theme engine rewrites at runtime (base.css must define all)
CANONICAL_TOKENS = (
    "--bg", "--surface", "--surface2", "--surface3", "--border",
    "--border2", "--text", "--dim", "--faint", "--accent",
    "--accent-rgb", "--ok", "--warn", "--err", "--font", "--mono",
    "--r", "--r-sm", "--r-lg", "--density", "--sp-4", "--fs-md",
)


def _spa() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _script_srcs() -> list:
    """Script srcs in index.html load order."""
    return re.findall(r'<script src="([^"]+)"></script>', _spa())


def _style_srcs() -> list:
    return re.findall(r'<link rel="stylesheet" href="([^"]+)">', _spa())


def _js_files() -> list:
    return [src.removeprefix("js/") for src in _script_srcs()
            if src.startswith("js/") and "/" not in src.removeprefix("js/")
            or src.startswith("js/views/")]


def _js() -> str:
    """All SPA JavaScript concatenated in load order."""
    return "\n".join(
        (STATIC_DIR / src).read_text(encoding="utf-8")
        for src in _script_srcs())


def _js_path(src: str) -> Path:
    return STATIC_DIR / src


def _css() -> str:
    return "\n".join(
        (STATIC_DIR / src).read_text(encoding="utf-8")
        for src in _style_srcs())


def _server_routes() -> set:
    """(method, path) routes as written in server.py's _route dispatcher."""
    src = SERVER_PATH.read_text(encoding="utf-8")
    return set(re.findall(r'\("(GET|POST)", "(/api/[A-Za-z0-9_/-]+)"\)', src))


def _spa_endpoints() -> set:
    """Every /api/... path string literal used by the SPA (query stripped)."""
    html = _spa()
    return set(re.findall(r'["`](/api/[A-Za-z0-9_/-]+)', html)) | set(
        re.findall(r'["`](/api/[A-Za-z0-9_/-]+)', _js()))


# ─────────────────────────────────────────────────────────────── structure
class TestStaticStructure(unittest.TestCase):
    """index.html references must resolve; load order is the dependency
    order; no inline script/style; ids unique."""

    def test_every_script_tag_resolves(self):
        for src in _script_srcs():
            self.assertTrue(_js_path(src).is_file(), f"missing script {src}")

    def test_every_stylesheet_resolves(self):
        for src in _style_srcs():
            self.assertTrue(_js_path(src).is_file(), f"missing stylesheet {src}")

    def test_load_order_is_dependency_order(self):
        """store before api (token fn), render before views (plugin pages),
        every view before shell (routes registered before attach), shell
        before theme (button wiring exists either way, keep canonical)."""
        srcs = _script_srcs()
        self.assertEqual(srcs[0], "js/store.js")
        self.assertEqual(srcs[1], "js/api.js")
        self.assertLess(srcs.index("js/ui.js"), srcs.index("js/views/home.js"))
        self.assertLess(srcs.index("js/render.js"),
                        srcs.index("js/views/plugins.js"))
        self.assertEqual(srcs[-2], "js/shell.js")
        self.assertEqual(srcs[-1], "js/theme.js")

    def test_no_inline_script_or_style(self):
        html = _spa()
        self.assertNotRegex(
            html, r"<script(?![^>]*src=)[^>]*>",
            "all JS lives in js/ files (contract: no inline handlers)")
        self.assertNotRegex(
            html, r"<style(?![^>]*>)|style=\"",
            "all styling lives in styles/ files (token purity)")

    def test_html_element_ids_unique(self):
        ids = re.findall(r'id="([A-Za-z0-9_-]+)"', _spa())
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(dupes, set(), f"duplicate ids: {dupes}")

    def test_every_view_registers_a_route(self):
        views = sorted(p.name for p in (STATIC_DIR / "js" / "views").glob("*.js"))
        self.assertTrue(views, "no view files found")
        for name in views:
            src = (STATIC_DIR / "js" / "views" / name).read_text(encoding="utf-8")
            self.assertIn("routerAdd(", src, f"{name} must register a route")
            self.assertIn('"use strict"', src, f"{name} must be strict")

    def test_strict_mode_everywhere(self):
        for src in _script_srcs():
            body = _js_path(src).read_text(encoding="utf-8")
            self.assertIn('"use strict"', body, f"{src} must be strict")


# ─────────────────────────────────────────────────────────────── api safety
class TestApiCompatibility(unittest.TestCase):
    """The SPA may only call endpoints the real server implements."""

    def test_every_spa_endpoint_exists_on_server(self):
        routes = _server_routes()
        self.assertTrue(routes, "server route table not parsed")
        endpoints = _spa_endpoints()
        self.assertTrue(endpoints, "no endpoints extracted from SPA")
        missing = sorted(
            path for path in endpoints
            if not any((m, path) in routes for m in ("GET", "POST")))
        self.assertEqual(missing, [],
                         "SPA calls endpoints the server does not implement")

    def test_css_custom_props_are_valid(self):
        css = _css()
        self.assertRegex(css, r"--border2: #[0-9a-fA-F]{6}",
                         "--border2 must be a well-formed hex color")
        tokens = re.findall(r"#[0-9A-Za-z]{3,8}\b", css)
        candidates = [t for t in tokens if any(c.isdigit() for c in t[1:])]
        bad = [t for t in candidates
               if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}"
                                   r"|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", t)]
        self.assertEqual(bad, [], f"malformed CSS hex values: {bad}")

    def test_no_hardcoded_colors_outside_base(self):
        """components/shell style with tokens only (base.css owns the
        palette). data-URIs are exempt (favicons can't read vars)."""
        for name in ("components.css", "shell.css"):
            css = (STATIC_DIR / "styles" / name).read_text(encoding="utf-8")
            # strip data-uris before scanning
            css = re.sub(r"url\([^)]*\)", "url()", css)
            hexes = [h for h in re.findall(r"#[0-9A-Za-z]{3,8}\b", css)
                     if any(c.isdigit() for c in h[1:])]
            self.assertEqual(hexes, [],
                             f"{name} hardcodes colors: {hexes[:5]}")


# ─────────────────────────────────────────────────────────────── token hygiene
class TestTokenHygiene(unittest.TestCase):
    def test_token_uses_session_storage_only(self):
        js = _js_path("js/store.js").read_text(encoding="utf-8")
        self.assertIn('secretStore.getItem("dash_token")', js)
        self.assertIn("sessionStorage", js)
        # localStorage must never hold the token
        self.assertNotIn('store.setItem("dash_token"', js)

    def test_token_not_rendered_in_chrome(self):
        html = _spa()
        self.assertNotIn("dash_token", html)

    def test_token_url_bootstrap_strips(self):
        js = _js_path("js/store.js").read_text(encoding="utf-8")
        self.assertIn("adoptTokenFromUrl", js)
        self.assertIn("replaceState", js,
                      "bootstrap must strip ?token= from the address bar")

    def test_rest_sends_header_and_ws_sends_query(self):
        api_js = _js_path("js/api.js").read_text(encoding="utf-8")
        self.assertIn("X-Dashboard-Token", api_js)
        self.assertIn("?token=", api_js)


# ─────────────────────────────────────────────────────── group invariants
class TestGroupInvariants(unittest.TestCase):
    """Group flows drive the real /api/groups/* backend; forbidden
    patterns from the v1/v2 era stay forbidden forever."""

    def test_group_endpoints_exercised(self):
        js = _js()
        for endpoint in ("groups/create", "groups/delete",
                         "groups/add-member", "groups/remove-member",
                         "groups/set-account"):
            self.assertIn(endpoint, js,
                          f"the SPA must exercise POST /api/{endpoint}")

    def test_no_dummy_profile_and_no_fake_group_ui(self):
        js = _js()
        self.assertNotIn("__dummy__", js,
                         "fake profile assignment is forbidden forever")
        self.assertNotIn("fakeGroup", js)

    def test_group_moves_use_membership_endpoints_only(self):
        """Moving an account between groups must NEVER delete/recreate
        the account — only the membership endpoints."""
        js = _js()
        for move_ep in ("groups/set-account", "groups/clear-account"):
            self.assertIn(move_ep, js)
        # any function that moves groups must not touch accounts/remove
        for m in re.finditer(r"post\(\"/api/(groups/set-account"
                             r"|groups/clear-account)", js):
            start = max(0, m.start() - 400)
            window = js[start:m.end() + 400]
            if "moveAccount" in window or "move" in window.lower():
                self.assertNotIn("/api/accounts/remove", window,
                                 "group moves must not delete accounts")


# ─────────────────────────────────────────────── plugin capability model
class TestPluginModel(unittest.TestCase):
    """Capabilities are DIRECT account→plugin assignments; the legacy
    global-profile surface (/api/profiles) is server-retained but NEVER
    called by the SPA."""

    def test_spa_never_calls_profiles_api(self):
        js = _js()
        self.assertNotIn('"/api/profiles"', js)
        self.assertNotIn("/api/profiles/", js)

    def test_plugin_assignment_flows_use_real_endpoints(self):
        js = _js()
        # the SPA manages the capability set via the whole-set replace plus
        # per-plugin unassign (server truth: /assign is redundant when /set
        # carries the full desired set)
        for endpoint in ("accounts/plugins/set",
                         "accounts/plugins/unassign",
                         "accounts/plugins"):
            self.assertIn(endpoint, js)


# ─────────────────────────────────────────────────────────── design system
class TestDesignSystem(unittest.TestCase):
    def test_canonical_classes_defined(self):
        css = _css()
        for cls in CANONICAL_CLASSES:
            self.assertRegex(css, re.escape(cls) + r"[ ,{:.]",
                             f"design system must define {cls}")

    def test_canonical_tokens_defined(self):
        base = (STATIC_DIR / "styles" / "base.css").read_text(encoding="utf-8")
        for tok in CANONICAL_TOKENS:
            self.assertRegex(base, re.escape(tok) + r":",
                             f"base.css must define {tok}")

    def test_low_radius_tokens(self):
        base = (STATIC_DIR / "styles" / "base.css").read_text(encoding="utf-8")
        self.assertRegex(base, r"--r:\s*3px", "Carbon radius is 3px")

    def test_focus_visible_present(self):
        for name in ("components.css", "base.css"):
            css = (STATIC_DIR / "styles" / name).read_text(encoding="utf-8")
            self.assertIn(":focus-visible", css,
                          f"{name} must style keyboard focus")

    def test_reduced_motion_respected(self):
        base = (STATIC_DIR / "styles" / "base.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", base)


# ─────────────────────────────────────────────────────────── UI contracts
class TestUiContracts(unittest.TestCase):
    def test_no_native_dialog_apis(self):
        """No window.confirm/alert/prompt CALLS — comments may name them
        (ui.js documents the ban). Strip comments before scanning."""
        js = _js()
        stripped = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
        for banned in ("window.confirm", "window.alert", "window.prompt",
                       "alert(", "confirm(", "prompt("):
            self.assertNotIn(banned, stripped,
                             f"native {banned} is forbidden (use ui.js)")

    def test_dialog_close_buttons_structural(self):
        ui_js = _js_path("js/ui.js").read_text(encoding="utf-8")
        self.assertIn("dlg-close", ui_js)
        self.assertIn("aria-label", ui_js)

    def test_escaping_discipline(self):
        """Views build DOM through el() (text nodes — uninterpolatable).
        They must NOT write innerHTML from templates; esc() stays available
        in ui.js for the few string-markup surfaces."""
        ui_js = _js_path("js/ui.js").read_text(encoding="utf-8")
        self.assertRegex(ui_js, r"function esc\(")
        for view in (STATIC_DIR / "js" / "views").glob("*.js"):
            src = view.read_text(encoding="utf-8")
            self.assertNotIn(".innerHTML", src,
                             f"{view.name} must build DOM via el(), "
                             "not innerHTML templates")

    def test_theme_engine_entrypoint(self):
        theme = _js_path("js/theme.js").read_text(encoding="utf-8")
        shell = _js_path("js/shell.js").read_text(encoding="utf-8")
        self.assertRegex(theme, r"function themeOpen\(")
        self.assertIn("themeOpen", shell,
                      "shell must wire the theme button to themeOpen()")

    def test_double_submit_guards(self):
        ui_js = _js_path("js/ui.js").read_text(encoding="utf-8")
        self.assertRegex(ui_js, r"function busy\(")

    def test_router_mount_lifecycle(self):
        router = _js_path("js/router.js").read_text(encoding="utf-8")
        self.assertIn("unmount", router,
                      "router must honor view unmount cleanup")
        self.assertIn("hashchange", router)


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

    async def accounts_plugins_list(self, account):
        return []

    async def groups_list(self):
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
    startup data source answers 200/ok — the SPA's first paint must never
    depend on state that requires accounts to exist."""

    async def asyncSetUp(self) -> None:
        self.srv = DashboardServer(_ZeroAccountAPI(), token=TOKEN, port=0)
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.bound_port}"

    async def asyncTearDown(self) -> None:
        self.srv.stop()

    async def _get(self, path: str, as_json: bool = True) -> tuple:
        # MUST hop threads: this test's event loop IS the server's main
        # loop — a blocking urlopen on it starves the route coroutine.
        return await asyncio.to_thread(_get, f"{self.base}{path}", as_json)

    async def test_index_served(self) -> None:
        status, body = await self._get("/", as_json=False)
        self.assertEqual(status, 200)
        self.assertIn("LibzentSuite", body)

    async def test_static_assets_served_without_auth(self) -> None:
        """CSS/JS must be reachable BEFORE the token exists (same rule
        as the index) — and traversal must be blocked."""
        def raw(path):
            try:
                with urllib.request.urlopen(
                        f"{self.base}/{path}", timeout=10) as resp:
                    return (resp.status, resp.read().decode("utf-8"),
                            resp.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as exc:
                return exc.code, "", ""
        for path, marker in (("styles/base.css", "--border2"),
                             ("styles/components.css", ".hero"),
                             ("styles/shell.css", "snav"),
                             ("js/store.js", "dash_token"),
                             ("js/shell.js", "bootDashboard"),
                             ("js/views/accounts.js", "routerAdd")):
            status, body, ctype = await asyncio.to_thread(raw, path)
            self.assertEqual(status, 200, path)
            self.assertIn(marker, body, path)
            self.assertIn("text/css" if "css" in path else "javascript",
                          ctype, path)
        status, _, _ = await asyncio.to_thread(
            raw, "styles/../../server.py")
        self.assertEqual(status, 404, "traversal must be rejected")

    async def test_startup_endpoints_answer(self) -> None:
        for path in ("/api/status", "/api/accounts", "/api/plugins",
                     "/api/groups",
                     "/api/simulation/list", "/api/tasks",
                     "/api/tasks/history", "/api/cooldowns",
                     "/api/logs", "/api/audit"):
            status, body = await self._get(path)
            self.assertEqual(status, 200, f"{path} -> {status}")
            self.assertTrue(body.get("ok"), f"{path} payload not ok")

    async def test_zero_account_status_shape(self) -> None:
        _, body = await self._get("/api/status")
        self.assertIsInstance(body["data"]["accounts"], list)
        self.assertEqual(body["data"]["accounts"], [])


if __name__ == "__main__":
    unittest.main()
