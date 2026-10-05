"""Static audit of the dashboard SPA (interfaces/dashboard/static/index.html).

No browser, no JS runtime: the single-file SPA and the server routing table
are parsed (html.parser + regex) and cross-checked:

  * every REST URL the SPA calls exists as a (method, path) route in
    interfaces/dashboard/server.py — endpoint drift is caught here
  * every ``innerHTML`` concatenation wraps dynamic server values in
    ``esc()`` (and esc() itself escapes quotes, so quoted attribute
    contexts cannot be broken out of)
  * no blocking dialogs (alert/confirm/prompt), one inline <script>, no
    literal ``</script>`` smuggled inside the JS
  * every declared JS function is referenced (dead-code detector)
  * every ``LOADERS.<section>`` key has a matching ``#panel-<section>``
  * WS handler covers stats/log/audit/task/error; reconnect cap + backoff
  * per-tab empty-state copy and the live-box line caps exist
"""
from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPA_PATH = ROOT / "interfaces" / "dashboard" / "static" / "index.html"
SERVER_PATH = ROOT / "interfaces" / "dashboard" / "server.py"


class _ScriptExtractor(HTMLParser):
    """Collects the raw text of every <script> block (CDATA mode)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._in_script = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._in_script = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._buf))
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self._buf.append(data)


def _load() -> tuple[str, str]:
    html = SPA_PATH.read_text(encoding="utf-8")
    parser = _ScriptExtractor()
    parser.feed(html)
    parser.close()
    assert len(parser.scripts) == 1, "expected exactly one inline <script>"
    return html, parser.scripts[0]


HTML, JS = _load()

#: the JS with // and /* */ comments stripped (comment prose must not
#: trip the dialog/dead-code scanners)
JS_NOCOMMENT = re.sub(
    r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", JS, flags=re.S), flags=re.S,
)

#: identifiers whose ``.<field>`` accesses carry server-controlled values
DYNAMIC_VAR = re.compile(
    r"\b(?:msg|entry|report|info|stats|t|p|m|a|s|r|i|w|eff|lane|cd|rt|st)"
    r"\.[A-Za-z_]\w*"
)
#: an innerHTML statement: `.innerHTML = ...` or `.innerHTML += ...`
INNERHTML_LINE = re.compile(r"\.innerHTML\s*(\+?=)(.*)")
#: a server.py routing entry: (method, path) == ("GET", "/api/...")
ROUTE_RE = re.compile(r'\(method,\s*path\)\s*==\s*\("(GET|POST)",\s*"(/[^"]*)"\)')
#: api()/post()/fetch() called with a literal "/api/..." URL
CALL_RE = re.compile(r'\b(api|post|fetch)\(\s*"(/api/[^"?]+)(?:\?[^"]*)?"')


class TestSpaRoutes(unittest.TestCase):
    """Every URL the SPA fetches must be a real route with the right verb."""

    @classmethod
    def setUpClass(cls):
        cls.routes = set(ROUTE_RE.findall(SERVER_PATH.read_text(encoding="utf-8")))
        cls.calls = set()
        for match in CALL_RE.finditer(JS):
            method = "POST" if match.group(1) == "post" else "GET"
            cls.calls.add((method, match.group(2)))

    def test_routes_were_extracted(self):
        self.assertGreater(len(self.routes), 30, "route extraction broke")

    def test_calls_were_extracted(self):
        self.assertGreaterEqual(len(self.calls), 30, "call extraction broke")
        for expected in (
            ("GET", "/api/status"),
            ("GET", "/api/accounts"),
            ("POST", "/api/accounts/add"),
            ("POST", "/api/estop"),
            ("POST", "/api/shutdown"),
            ("POST", "/api/simulation/run"),
            ("GET", "/api/credentials/status"),
            ("POST", "/api/credentials/import-env"),
        ):
            self.assertIn(expected, self.calls)

    def test_every_spa_call_is_a_server_route(self):
        unknown = self.calls - self.routes
        self.assertEqual(
            unknown, set(),
            f"SPA calls a non-existent route (verb mismatches count): "
            f"{sorted(unknown)}",
        )

    def test_server_routes_not_called_are_known_unused(self):
        unused = self.routes - self.calls
        self.assertEqual(
            unused,
            {
                ("POST", "/api/resume"),
                ("GET", "/api/ratelimit/lane"),
                ("POST", "/api/accounts/enable"),
                ("POST", "/api/tasks/cancel-matching"),
                ("GET", "/api/plugins/settings"),
            },
            "server routes unused by the SPA changed — update this list "
            "deliberately (missing UI = dead backend surface)",
        )


class TestEscCoverage(unittest.TestCase):
    """innerHTML + server data must go through esc(); quotes escaped too."""

    def test_esc_escapes_quotes(self):
        self.assertIn("&quot;", JS)
        self.assertIn("&#39;", JS)
        self.assertRegex(JS, r"function esc\(")

    def test_every_dynamic_innerhtml_value_is_escaped(self):
        offenders = []
        for lineno, line in enumerate(JS.splitlines(), start=1):
            match = INNERHTML_LINE.search(line)
            if not match:
                continue
            for segment in re.split(r"\s*\+\s*", match.group(2)):
                refs = DYNAMIC_VAR.findall(segment)
                if not refs:
                    continue
                if all(ref.endswith(".length") for ref in refs):
                    continue
                if re.match(
                    r"^\s*(?:esc|kv|fmtDur|fmtMMSS|fmtAge)\(", segment
                ):
                    continue
                offenders.append((lineno, segment.strip()))
        self.assertEqual(
            offenders, [],
            "innerHTML segment interpolates a server value without esc()",
        )

    def test_no_attribute_context_interpolation(self):
        for pattern in (
            r"class=\\?[" + r"']?\s*\+\s*esc\(",
            r"class=[a-z-]+[" + r"']?\s*\+\s*esc\(",
        ):
            self.assertIsNone(
                re.search(pattern, JS),
                f"attribute-context interpolation found: {pattern}",
            )

    def test_status_badge_is_dom_built(self):
        self.assertNotIn("'st-' + esc(", JS)

    def test_no_literal_script_close_in_js(self):
        self.assertNotIn("</script>", JS)

    def test_no_blocking_dialogs(self):
        self.assertIsNone(
            re.search(r"\b(?:alert|confirm|prompt)\(", JS_NOCOMMENT)
        )


class TestDeadCode(unittest.TestCase):
    """Declared functions must be referenced somewhere (no dead handlers)."""

    @classmethod
    def setUpClass(cls):
        names = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", JS))
        names |= set(
            re.findall(
                r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
                JS,
            )
        )
        names |= set(
            re.findall(
                r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[\w$]+\s*=>",
                JS,
            )
        )
        cls.functions = names

    def test_functions_extracted(self):
        self.assertGreaterEqual(len(self.functions), 40)

    def test_every_function_is_referenced(self):
        dead = [
            name for name in self.functions
            if len(re.findall(
                r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", JS_NOCOMMENT
            )) < 2
        ]
        self.assertEqual(dead, [], "dead JS functions (declared, never used)")

    def test_loaders_match_panels(self):
        for section in re.findall(r"LOADERS\.([A-Za-z-]+)\s*=", JS):
            self.assertIn('id="panel-%s"' % section, HTML)

    def test_every_button_id_is_wired(self):
        for bid in re.findall(r'<button[^>]+id="([\w-]+)"', HTML):
            self.assertRegex(
                JS, re.escape('$("' + bid + '").'),
                "button #%s has no JS handler (dead button)" % bid,
            )


class TestWsHandling(unittest.TestCase):
    def test_all_message_types_handled(self):
        for mtype in ("stats", "log", "audit", "task", "error"):
            self.assertIn('msg.type === "%s"' % mtype, JS)

    def test_render_failures_are_contained(self):
        self.assertIn("failed to render live update", JS)

    def test_reconnect_cap_and_backoff(self):
        self.assertIn("wsAttempts > 10", JS)
        self.assertIn("Math.min(wsRetry * 2, 10000)", JS)
        self.assertIn("wsRetry = 1000", JS)
        self.assertIn("wsAttempts = 0", JS)

    def test_ws_token_is_encoded(self):
        self.assertIn("encodeURIComponent(token())", JS)

    def test_malformed_duration_is_guarded(self):
        self.assertIn("Number.isFinite(d)", JS)


class TestEmptyStatesAndCaps(unittest.TestCase):
    def test_tab_empty_state_copy(self):
        for text in (
            "no accounts yet — add one in Accounts",
            "no accounts — add one above",
            "no plugins discovered",
            "no plugins discovered — create nothing yet",
            "no profiles defined",
            "no stored policies",
            "no active cooldowns",
            "no history yet",
            "no simulation scenarios configured",
            "no plugins attached",
            "none assigned (unrestricted)",
            "no actions paced yet",
            "no settings",
            "no interactions recorded",
            "no failure details",
            "run a scenario to see its report",
        ):
            self.assertIn(text, HTML)

    def test_live_boxes_have_placeholders(self):
        self.assertGreaterEqual(HTML.count("data-ph="), 3)
        self.assertGreaterEqual(JS.count('querySelector("[data-ph]")'), 3)

    def test_live_box_line_caps(self):
        for cap in (r"> 120", r"> 400", r"> 300"):
            self.assertRegex(JS, r"childNodes\.length\s*" + cap)

    def test_sim_log_cap(self):
        self.assertIn("SIM_LOG_LINE_CAP", JS)
        self.assertRegex(JS, r"slice\(-SIM_LOG_LINE_CAP\)")

    def test_sim_run_locks_all_buttons(self):
        self.assertIn("simRunButtons", JS)
        self.assertRegex(JS, r"for \(const b of simRunButtons\) b\.disabled = true")

    def test_floor_display_formats_large_values(self):
        self.assertRegex(JS, r"floorS >= 60")

    def test_error_boxes_are_bounded(self):
        self.assertRegex(JS, r"childNodes\.length > 8")
        self.assertRegex(JS, r"childNodes\.length > 5")

    def test_section_err_divs_are_used(self):
        for section in (
            "accounts", "plugins", "profiles", "ratelimit", "cooldowns",
            "tasks", "simulation", "advanced",
        ):
            self.assertIn('id="panel-%s-err"' % section, HTML)
            self.assertIn('"#panel-%s"' % section, JS)


class TestRound3SpaHardening(unittest.TestCase):
    """Round-3 demonic-audit fixes pinned as static assertions."""

    # ------------------------------------------------- accounts sig (5)
    def test_render_sig_folds_latency_uptime_cooldowns(self):
        # the old sig [name,status,enabled,credential] froze steady
        # accounts: latency/uptime/cooldowns must now gate re-renders
        self.assertIn("a.name, a.status, a.enabled, a.credential,", JS)
        self.assertIn("a.latency_ms != null ? Math.round(a.latency_ms / 10) : null", JS)
        self.assertIn("Math.floor((a.uptime_s ?? 0) / 60)", JS)
        self.assertIn("a.cooldowns ?? 0]", JS)

    # --------------------------------------------- tasks re-entry (6)
    def test_activate_tasks_schedules_refresh(self):
        self.assertIn('if (section === "tasks") scheduleTaskRefresh();', JS)

    # ---------------------------------------- overview hidden skip (7)
    def test_overview_render_skipped_when_hidden(self):
        self.assertIn(
            'if (!$("panel-overview").classList.contains("active")) return;', JS)
        # ...and repainted on re-entry
        self.assertIn('if (section === "overview") renderOverview();', JS)

    # --------------------------------------------- timed_out chip (8)
    def test_timed_out_chip_present(self):
        self.assertIn('["timed_out", "timed_out", "st-timed_out"]', JS)
        self.assertIn(".st-timed_out", HTML)  # CSS existed; chip now uses it

    # ------------------------------------------- showModal guard (9)
    def test_details_showmodal_guarded(self):
        self.assertIn('if (!$("details").open) $("details").showModal();', JS)

    # ------------------------------------- double-click guards (10)
    def test_mutating_buttons_disable_in_flight(self):
        self.assertIn("function btnGuarded(", JS)
        self.assertIn('btnGuarded("start"', JS)
        self.assertIn('btnGuarded("stop"', JS)
        for bid in ("addacct", "prof-create"):
            self.assertIn('$("' + bid + '").disabled = true', JS)
            self.assertIn('$("' + bid + '").disabled = false', JS)
            self.assertIn('if ($("' + bid + '").disabled) return;', JS)

    # --------------------------------------- details 401 honest (11)
    def test_plugin_state_auth_failure_distinguished(self):
        # .catch(() => []) rendered 'no plugins' on a 401 — the catch now
        # marks null and the dialog says 'unavailable (auth?)'
        self.assertIn(
            'api("/api/plugins/state?account=" + encodeURIComponent(name)).catch(() => null)',
            JS)
        self.assertIn("plugin states unavailable (auth?)", JS)
        self.assertIn("no plugins attached", JS)  # [] keeps its own copy
        self.assertNotIn(
            'api("/api/plugins/state?account=" + encodeURIComponent(name)).catch(() => [])',
            JS)

    # ------------------------------------------- WS watchdog (12)
    def test_connect_watchdog_armed_and_cleared(self):
        self.assertIn("wsWatchdog", JS)
        self.assertIn("clearTimeout(wsWatchdog)", JS)
        self.assertIn("WS_CONNECT_TIMEOUT_MS", JS)
        self.assertIn("ws.onclose = () => handleWsClosed();", JS)

    # -------------------------------------- finite latency (13)
    def test_latency_finite_guarded(self):
        self.assertIn("function fmtLatency(", JS)
        self.assertRegex(JS, r"Number\.isFinite\(n\) \? Math\.round\(n\) \+ \"ms\"")
        # every former raw Math.round(latency_ms) site routes through it
        self.assertNotIn("Math.round(a.latency_ms)", JS)
        self.assertNotIn("Math.round(st.latency_ms)", JS)
        self.assertEqual(JS.count("fmtLatency("), 4)  # def + 3 call sites

    # -------------------------------- CSS gaps + dead branch (14)
    def test_credential_status_css_classes_exist(self):
        self.assertIn(".status.missing_credential", HTML)
        self.assertIn(".status.invalid_credential", HTML)

    def test_dead_cred_env_branch_removed(self):
        # server guarantees db|missing — 'env' UI is dead and must stay gone
        self.assertNotIn("cred-env", HTML)
        self.assertNotIn('state === "env"', JS)

    # ------------------------------------------ backfill merge (15)
    def test_backfill_merges_instead_of_wiping(self):
        self.assertIn("function buildLogLine(", JS)
        self.assertIn("function buildAuditLine(", JS)
        self.assertGreaterEqual(JS.count("insertBefore(div, anchor)"), 2)
        # the wipe must be gone from both backfill paths
        self.assertNotIn('$("logbox").innerHTML = ""', JS)
        self.assertNotIn('$("auditbox").innerHTML = ""', JS)


if __name__ == "__main__":
    unittest.main()
