"""Theme engine tests — state validation, push-race design, drawer contract.

GPT audit tasks 9+10. The theme engine spans two worlds:

* PYTHON SIDE — core/theme_store.py (the durable mirror behind
  GET/POST /api/theme). The store deliberately validates only the
  envelope (JSON object, serializable, size-capped) and NEVER individual
  fields — field-level normalization is the FRONTEND's job
  (themeNormalizeState in theme.js). These tests pin that the store and
  its route stay graceful with legacy/malformed/corrupt/oversized
  payloads, and that the atomic write + size cap are intact.

* JS SIDE — interfaces/dashboard/static/js/theme.js. There is no JS test
  runner in this repo (and none may be invented), so the JS contract is
  verified the same way tests/unit/test_dashboard_static.py verifies the
  rest of the SPA: structural assertions over the source (normalization
  rules, the trailing-edge push re-arm, dialog/tablist a11y, import
  hygiene). The race logic itself is documented inline in theme.js
  (themePushFlush) and verified live in a browser against the running
  dashboard.
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from core import theme_store

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "interfaces" / "dashboard" / "static"
THEME_JS = STATIC / "js" / "theme.js"
COMPONENTS_CSS = STATIC / "styles" / "components.css"
TOKEN = "theme-engine-test-token"

#: marker starting the appended theme-drawer block in components.css
DRAWER_BLOCK_MARKER = "THEME DRAWER (theme.js panel)"


def _js() -> str:
    return THEME_JS.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────── theme store
class TestThemeStore(unittest.TestCase):
    """core/theme_store.py: envelope-only validation, atomic writes, and
    graceful handling of legacy / malformed / corrupt saved states."""

    def setUp(self) -> None:
        self.tmp = Path(self.__class__.__name__ + "-theme.json")
        if self.tmp.exists():
            self.tmp.unlink()
        self._old_path = theme_store.PATH
        theme_store.PATH = self.tmp

    def tearDown(self) -> None:
        theme_store.PATH = self._old_path
        for leftover in (self.tmp, self.tmp.with_suffix(".json.tmp")):
            if leftover.exists():
                leftover.unlink()

    # ── load: corrupt / legacy / missing ──
    def test_missing_file_reads_empty(self):
        self.assertEqual(theme_store.load(), {})

    def test_corrupt_json_reads_empty(self):
        self.tmp.write_text('{"scope": "global", "global": {', encoding="utf-8")
        self.assertEqual(theme_store.load(), {})

    def test_non_dict_json_reads_empty(self):
        for raw in ("[]", '"nope"', "3", "null", "true"):
            self.tmp.write_text(raw, encoding="utf-8")
            self.assertEqual(theme_store.load(), {}, raw)

    def test_unreadable_file_reads_empty(self):
        # OSError path (e.g. permissions) must never raise
        theme_store.PATH = Path("Z:/definitely/not/here/theme.json")
        self.assertEqual(theme_store.load(), {})

    def test_legacy_payload_with_unknown_fields_round_trips_verbatim(self):
        """Forward compatibility: the store does not understand (or drop)
        fields a newer engine may have written — they survive a
        save/load cycle untouched so an older dashboard never destroys a
        newer browser's state."""
        legacy = {
            "scope": "global", "updated_at": 42,
            "global": {"accent": "#ec4899", "fontUi": "Inter",
                       "futureField": {"nested": [1, 2]}},
            "pages": {"/accounts": {"--accent": "#10b981"}},
            "pageCss": {"/accounts": ".hero { padding: 0; }"},
            "futureTopLevel": "keep me",
        }
        theme_store.save(legacy)
        self.assertEqual(theme_store.load(), legacy)

    def test_wrong_inner_types_are_stored_and_returned_as_is(self):
        """Field-level normalization is the FRONTEND's job — the store
        must not reject (or "fix") wrong inner types: a malformed value
        round-trips and the JS normalizer falls back at load time."""
        weird = {"updated_at": "not-a-number", "global": {"radius": [3]},
                 "pages": [], "pageCss": {"x": 9}, "scope": None}
        theme_store.save(weird)
        self.assertEqual(theme_store.load(), weird)

    # ── save: envelope validation ──
    def test_save_rejects_non_dict(self):
        for bad in ([], "nope", 3, None):
            with self.assertRaises(ValueError):
                theme_store.save(bad)

    def test_save_rejects_unserializable(self):
        with self.assertRaises(ValueError):
            theme_store.save({"global": {"css": {"a set"}}})

    def test_size_cap_intact(self):
        """The 256KB cap is a hard floor — oversized states are refused,
        never silently truncated."""
        big = {"global": {"css": "x" * (theme_store.MAX_BYTES - 64)}}
        theme_store.save(big)  # just under the cap: fine
        over = {"global": {"css": "x" * (theme_store.MAX_BYTES + 1)}}
        with self.assertRaises(ValueError) as ctx:
            theme_store.save(over)
        self.assertIn("too large", str(ctx.exception))

    def test_save_is_atomic_no_tmp_left_behind(self):
        theme_store.save({"updated_at": 1})
        self.assertFalse(self.tmp.with_suffix(".json.tmp").exists())
        self.assertEqual(theme_store.load()["updated_at"], 1)
        theme_store.save({"updated_at": 2})  # replace over an existing file
        self.assertFalse(self.tmp.with_suffix(".json.tmp").exists())
        self.assertEqual(theme_store.load()["updated_at"], 2)

    def test_save_creates_parent_dirs(self):
        nested = self.tmp.parent / "tz-subdir" / "theme.json"
        old = theme_store.PATH
        theme_store.PATH = nested
        try:
            theme_store.save({"updated_at": 7})
            self.assertTrue(nested.is_file())
        finally:
            theme_store.PATH = old
            if nested.exists():
                nested.unlink()
            nested.parent.rmdir()


# ────────────────────────────────────────────────────────── /api/theme route
class _ThemeOnlyAPI:
    """The /api/theme routes never touch ControlAPI — a bare stub is
    enough (start() probes audit_recorder/task-bus defensively)."""

    audit_recorder = None  # probed via getattr during start()


class TestThemeRouteContract(unittest.IsolatedAsyncioTestCase):
    """The HTTP contract the JS engine talks to: legacy/malformed
    payloads are accepted (200) and round-trip, oversized ones are an
    honest 400, and the empty store reads as {}. The contract itself is
    UNCHANGED by the audit fix (no defect was demonstrated server-side)."""

    async def asyncSetUp(self) -> None:
        from interfaces.dashboard.server import DashboardServer
        self.srv = DashboardServer(_ThemeOnlyAPI(), token=TOKEN, port=0)
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.srv.bound_port}"
        self._old_path = theme_store.PATH
        theme_store.PATH = Path(self.__class__.__name__ + "-theme.json")
        if theme_store.PATH.exists():
            theme_store.PATH.unlink()

    async def asyncTearDown(self) -> None:
        self.srv.stop()
        if theme_store.PATH.exists():
            theme_store.PATH.unlink()
        theme_store.PATH = self._old_path

    # MUST hop threads: this test's event loop IS the server's main loop.
    def _send(self, method: str, path: str, payload=None) -> tuple:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}", method=method, data=data,
            headers={"Content-Type": "application/json",
                     "X-Dashboard-Token": TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    async def test_get_empty_store_is_ok_and_empty(self):
        status, body = await asyncio.to_thread(self._send, "GET", "/api/theme")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"], {})

    async def test_legacy_payload_accepted_and_round_trips(self):
        legacy = {
            "scope": "global", "updated_at": 99,
            "global": {"preset": "oled", "accent": "#06b6d4",
                       "futureField": True, "radius": "8"},
            "pages": {"/accounts": {"--accent": "not a color"}},
            "pageCss": {"/accounts": 12},
            "futureTopLevel": [1, 2, 3],
        }
        status, body = await asyncio.to_thread(
            self._send, "POST", "/api/theme", {"theme": legacy})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = await asyncio.to_thread(self._send, "GET", "/api/theme")
        self.assertEqual(body["data"], legacy)

    async def test_oversized_theme_is_400_with_message(self):
        big = {"theme": {"global": {
            "css": "y" * (theme_store.MAX_BYTES + 10)}}}
        status, body = await asyncio.to_thread(
            self._send, "POST", "/api/theme", big)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("too large", body["error"])

    async def test_missing_or_non_dict_theme_is_400(self):
        for payload in ({"nope": 1}, {"theme": "nope"}, {"theme": [1]}):
            status, body = await asyncio.to_thread(
                self._send, "POST", "/api/theme", payload)
            self.assertEqual(status, 400, payload)
            self.assertFalse(body["ok"])

    async def test_unauthenticated_write_is_401(self):
        def raw():
            req = urllib.request.Request(
                f"{self.base}/api/theme", method="POST",
                data=json.dumps({"theme": {}}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code
        self.assertEqual(await asyncio.to_thread(raw), 401)


# ────────────────────────────────────────────────── JS: state normalization
class TestJsNormalization(unittest.TestCase):
    """theme.js validates every persisted value before use (localStorage
    AND the remote mirror), falling back on malformed values and keeping
    unknown fields in storage without applying them."""

    def setUp(self):
        self.js = _js()

    def test_normalize_entry_points_exist(self):
        for fn in ("themeNormalizeState", "themeNormalizeGlobal",
                   "themeNormalizePages", "themeNormalizePageCss",
                   "themeAdopt"):
            self.assertIn(f"function {fn}(", self.js, fn)
        # both persisted-state consumers funnel through the normalizer
        self.assertRegex(
            self.js,
            r'localStorage\.getItem\("lz_theme"\);\s*\n\s*if \(raw\) themeAdopt\(JSON\.parse\(raw\)\)')
        self.assertRegex(self.js, r"remoteTs > localTs[\s\S]{0,300}themeAdopt\(remote\)")

    def test_known_enums_and_ranges_are_validated(self):
        self.assertIn("THEME_LAYOUT_ENUMS", self.js)
        self.assertIn("THEME_PAGE_TOKENS", self.js)
        self.assertIn("_HEX_RE", self.js)
        # catalog membership checks for presets and fonts
        self.assertRegex(self.js, r"_hasOwn\(THEME_PRESETS, src\.preset\)")
        self.assertRegex(self.js, r"_hasOwn\(THEME_FONTS\.ui, src\.fontUi\)")
        self.assertRegex(self.js, r"_hasOwn\(THEME_FONTS\.mono, src\.fontMono\)")
        # radius clamped to the slider range, density clamped to a band
        self.assertRegex(self.js, r"Math\.min\(12, Math\.max\(0, n\)\)")
        self.assertRegex(self.js, r"Math\.min\(2, Math\.max\(0\.5, Math\.round")
        # accentRgb is DERIVED (the pair can never drift)
        self.assertRegex(self.js, r'put\("accentRgb", themeHexToRgb\(accent\)\)')

    def test_unknown_fields_surve_in_storage_but_are_never_applied(self):
        """Forward compatibility: unknown fields ride along verbatim..."""
        self.assertIn("THEME_GLOBAL_KEYS", self.js)
        self.assertIn("THEME_STATE_KEYS", self.js)
        # ...copied without tripping __proto__ setters...
        self.assertIn('k === "__proto__"', self.js)
        # ...and apply() reads only via the page-token allowlist
        self.assertRegex(self.js, r"THEME_PAGE_TOKENS\.has\(k\)")

    def test_custom_css_stays_a_plain_string_feature(self):
        # clamped string-or-empty, never anything else
        self.assertRegex(
            self.js,
            r'put\("css", typeof src\.css === "string" \? src\.css\.slice\(0, THEME_CSS_MAX\) : ""\)')
        # applied ONLY as a style element's textContent
        self.assertRegex(self.js, r"cssTag\.textContent = ")
        # never evaluated
        self.assertNotIn("eval(", self.js)
        self.assertNotIn("new Function(", self.js)

    def test_theme_apply_never_trusts_state_blindly(self):
        # defense in depth at the application layer
        self.assertRegex(self.js, r"_HEX_RE\.test\(g\.accent\)")
        self.assertRegex(self.js, r"THEME_LAYOUT_ENUMS\.detail\.includes\(L\.detail\)")


# ─────────────────────────────────────────────────────── JS: push-race fix
class TestJsPushRace(unittest.TestCase):
    """The trailing-edge debounce must not drop the newest snapshot when
    its timer fires while a POST is in flight. Design (documented on
    themePushFlush): an in-flight flush marks _pushQueued; the finally
    block re-arms the debounce; pushes are serialized and snapshot at
    flush start so a stale response can never overwrite newer state.
    Python cannot execute this logic (no JS runtime in CI) — these
    assertions pin the structure, and the flow is verified live in a
    browser against the running dashboard."""

    def setUp(self):
        self.js = _js()

    def test_flush_marks_queued_when_busy_instead_of_returning(self):
        js = _js()
        self.assertIn("if (_pushBusy) { _pushQueued = true; return; }", js)

    def test_finally_re_arms_the_debounce(self):
        js = _js()
        self.assertRegex(
            js, r"finally\s*\{\s*_pushBusy = false;\s*"
                r"if \(_pushQueued\) \{ _pushQueued = false; themeRemotePush\(\); \}")

    def test_push_body_is_snapshotted_at_flush_start(self):
        js = _js()
        self.assertIn('const snapshot = JSON.stringify({theme: themeState});',
                      js)
        self.assertIn('body: snapshot', js)

    def test_consecutive_identical_errors_are_deduped_not_latched(self):
        """The old code latched _pushWarned forever (one failure silenced
        every later one). The fix dedupes consecutive IDENTICAL messages
        and resets on success. Comments are stripped first — theme.js
        documents the old latch by name."""
        stripped = re.sub(r"/\*.*?\*/", "", self.js, flags=re.S)
        stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
        self.assertNotIn("_pushWarned", stripped)
        self.assertIn("_pushLastErr", stripped)
        self.assertRegex(self.js, r"if \(msg !== _pushLastErr\)")

    def test_race_design_is_documented(self):
        js = _js()
        self.assertIn("lost-update race", js)
        self.assertIn("trailing-edge debounce", js)

    def test_local_first_behavior_kept(self):
        js = _js()
        # localStorage write happens synchronously in themeSave, before
        # the debounced remote push
        m = re.search(r"function themeSave\(\)[\s\S]*?\n\}", js)
        self.assertTrue(m, "themeSave not found")
        body = m.group(0)
        self.assertLess(body.index("localStorage.setItem"),
                        body.index("themeRemotePush()"))


# ───────────────────────────────────────────────── JS: drawer a11y/styling
class TestJsDrawerA11y(unittest.TestCase):
    """The drawer implements the dialog + tabs ARIA patterns, manages
    focus, and keeps persisted values out of markup."""

    def setUp(self):
        self.js = _js()

    def test_dialog_semantics(self):
        self.assertIn('role: "dialog"', self.js)
        self.assertIn('"aria-modal": "true"', self.js)
        self.assertIn('"aria-labelledby": "lz-theme-title"', self.js)

    def test_focus_moves_in_on_open_and_returns_to_opener(self):
        self.assertIn("_themeOpener", self.js)
        self.assertRegex(self.js, r"themeOpen\(\)[\s\S]{0,800}tab\.focus\(\)")
        self.assertRegex(
            self.js, r"opener\.focus\(\)")

    def test_escape_closes_and_background_is_inert(self):
        self.assertIn('e.key === "Escape"', self.js)
        self.assertIn('setAttribute("inert", "")', self.js)
        self.assertIn('removeAttribute("inert")', self.js)
        self.assertIn("lz-theme-scrim", self.js)

    def test_focus_trap_keeps_tab_inside_the_dialog(self):
        self.assertRegex(self.js, r'e\.key (!==|===) "Tab"')
        self.assertIn("THEME_FOCUSABLE", self.js)

    def test_tablist_pattern_complete(self):
        js = self.js
        self.assertIn('role: "tablist"', js)
        self.assertIn('role: "tab"', js)
        self.assertIn('role: "tabpanel"', js)
        self.assertIn("aria-selected", js)
        for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
            self.assertIn(key, js)
        # roving tabindex
        self.assertRegex(js, r'tabindex: i === 0 \? "0" : "-1"')

    def test_drawer_built_with_el_not_innerhtml(self):
        """No HTML-string construction at all: persisted values can never
        escape into markup or attributes (the old code interpolated
        g.radius / scope / the CSS sheet into templates)."""
        self.assertNotIn(".innerHTML", self.js)
        self.assertNotIn("html:", self.js)  # el()'s trusted-markup escape hatch
        self.assertNotIn('onclick="', self.js)  # no inline handlers
        # the CSS sheet reaches the textarea as a VALUE, not markup
        self.assertRegex(self.js, r"ta\.value = sheet;")

    def test_static_contract_markers_preserved(self):
        """markers tests/unit/test_dashboard_static.py greps theme.js for."""
        js = self.js
        self.assertIn('"use strict"', js)
        self.assertRegex(js, r"function themeOpen\(")
        self.assertIn(">Layout<", js)
        self.assertIn("lz-css-import", js)
        self.assertIn("detailLayout", js)
        self.assertIn("themeSetScope", js)  # router.js bridge

    def test_import_export_hygiene(self):
        js = self.js
        # same-file re-import works: the input resets before the read
        self.assertRegex(js, r"importFile\.value = \"\";")
        # file-read failures surface visibly (catch + alert row)
        self.assertRegex(js, r"f\.text\(\)\.then\([\s\S]*?\.catch\(")
        self.assertIn('role: "alert"', js)
        # object URLs are revoked after the export download starts
        self.assertIn("URL.revokeObjectURL(url)", js)

    def test_scope_label_reflects_the_route_scope(self):
        js = _js()
        self.assertIn("Custom CSS scope — currently", js)
        self.assertIn("This page (", js)
        # route changes re-render the CSS tab so the label cannot lie
        self.assertRegex(
            js, r"function themeSetScope\(scope\)[\s\S]*?themeRenderTab\(2\)")

    def test_functionality_preserved(self):
        """Appearance/Fonts/CSS/Layout tabs, presets, accent, radius,
        density, fonts, global+page CSS editing, Apply/Clear/Import/
        Export — all still present after the rebuild."""
        js = self.js
        for marker in ("THEME_PRESETS", "THEME_ACCENTS", "lz-radius",
                       "density",  # density segmented control
                       "THEME_FONTS", "lz-css-apply", "lz-css-clear",
                       "lz-css-import", "lz-css-export", "lz-scope"):
            self.assertIn(marker, js, marker)


# ─────────────────────────────────────────────────── components.css drawer
class TestDrawerCss(unittest.TestCase):
    """The appended theme-drawer block: tokens only, appended at the END
    of components.css, responsive down to 320px."""

    @classmethod
    def setUpClass(cls):
        css = COMPONENTS_CSS.read_text(encoding="utf-8")
        idx = css.find(DRAWER_BLOCK_MARKER)
        if idx == -1:
            raise AssertionError("theme drawer block missing from components.css")
        # the block starts at the opening /* of the comment carrying the marker
        start = css.rfind("/*", 0, idx)
        cls.css = css
        cls.block = css[start:]
        cls.head = css[:start]

    def test_block_is_appended_at_the_end(self):
        self.assertLess(self.css.find(DRAWER_BLOCK_MARKER),
                        self.css.find(".lz-theme-scrim"))
        self.assertTrue(self.css.rstrip().endswith(
            "overflow-wrap: anywhere; }"),
            "drawer block must be the last thing in the file")

    def test_drawer_classes_defined(self):
        for cls in (".lz-theme-panel", ".lz-theme-scrim", ".lz-tp-tabs",
                    ".lz-tp-tab", ".lz-tp-body", ".lz-preset", ".lz-accent",
                    ".lz-seg", ".lz-font", ".lz-css", ".lz-tp-css-err"):
            self.assertIn(cls, self.block, cls)

    def test_uses_shared_tokens_only_no_hex_colors(self):
        for token in ("var(--sp-", "var(--fs-", "var(--surface",
                      "var(--border", "var(--accent)", "var(--r"):
            self.assertIn(token, self.block, token)
        # same hex-purity rule the static suite enforces on the file
        stripped = re.sub(r"url\([^)]*\)", "url()", self.block)
        hexes = [h for h in re.findall(r"#[0-9A-Za-z]{3,8}\b", stripped)
                 if any(c.isdigit() for c in h[1:])]
        self.assertEqual(hexes, [], f"drawer block hardcodes colors: {hexes}")

    def test_responsive_to_small_viewports(self):
        self.assertRegex(self.block, r"@media \(max-width: 430px\)")
        self.assertRegex(self.block, r"@media \(max-width: 360px\)")
        self.assertIn("width: min(400px, 100vw)", self.block)
        self.assertIn("max-width: 100vw", self.block)

    def test_focus_visible_and_reduced_motion(self):
        self.assertIn(":focus-visible", self.block)
        # motion rides tokens + var(--ease); the global
        # prefers-reduced-motion rule in base.css collapses the durations
        self.assertIn("var(--ease)", self.block)

    def test_existing_rules_untouched(self):
        """APPEND-ONLY: the pre-block content still ends with the same
        last rule it had before (the home pill row), byte-for-byte."""
        self.assertTrue(
            self.head.rstrip().endswith(
                ".pill-row .ms-pill .k { text-transform: uppercase;"
                " letter-spacing: .06em; }"),
            "existing components.css rules must not be modified")


if __name__ == "__main__":
    unittest.main()
