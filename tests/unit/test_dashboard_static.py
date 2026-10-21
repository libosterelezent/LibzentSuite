"""Dashboard v2 SPA regression tests: startup safety, Groups integration
with the real /api/groups backend, the direct account→plugin capability
model, API compatibility with the real server, and static validation.

The dashboard is a build-free static SPA split across index.html (shell),
styles/*.css and js/*.js (no bundler — plain script tags in dependency
order). Its contract with the backend is verifiable statically:

* every ``/api/...`` endpoint the SPA calls must exist in the route table
  of interfaces/dashboard/server.py (startup can never 404),
* capabilities are DIRECT account→plugin assignments: the Phase-2
  plugin-profile model adds a Profiles tab and a per-plugin profile
  library over /api/plugins/profiles*, /api/accounts/plugin-profile[s],
  /api/accounts/channels and /api/accounts/effective-settings — while
  the LEGACY global profile system (/api/profiles) stays server-retained
  and is NEVER called by the SPA, and the plugin flows drive the real
  /api/accounts/plugins* endpoints,
* the Groups UI drives the REAL SyncGroupManager backend
  (``/api/groups/*``): the obsolete read-only-era assumptions must stay
  gone, group moves must use the group endpoints — and NEVER fake groups
  by destroying/recreating accounts or assigning ``__dummy__`` profiles
  (the v2 redesign once did both — forbidden forever),
* the script sources must be structurally balanced and reference only
  element ids that exist in the document,
* the token lives in Tools → Advanced (never the header), secrets use
  sessionStorage only, popups all carry close buttons, and per-render
  event-listener leaks stay impossible.

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
STATIC_DIR = ROOT / "interfaces" / "dashboard" / "static"
HTML_PATH = STATIC_DIR / "index.html"
SERVER_PATH = ROOT / "interfaces" / "dashboard" / "server.py"
TOKEN = "static-test-token"

#: JS modules in the exact dependency order index.html loads them.
JS_FILES = ["state.js", "api.js", "ui.js", "accounts.js", "groups.js",
            "views.js", "plugins-ui.js", "admin-ui.js", "plugin-ui.js",
            "dashboard-ui.js", "framework-ui.js", "app.js", "shell.js"]
#: every stylesheet index.html links.
CSS_FILES = ["base.css", "layout.css", "components.css", "responsive.css"]


def _spa() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _js() -> str:
    """All SPA JavaScript concatenated in load order (one logical script)."""
    return "\n".join(
        (STATIC_DIR / "js" / name).read_text(encoding="utf-8")
        for name in JS_FILES)


def _js_file(name: str) -> str:
    return (STATIC_DIR / "js" / name).read_text(encoding="utf-8")


def _css() -> str:
    return "\n".join(
        (STATIC_DIR / "styles" / name).read_text(encoding="utf-8")
        for name in CSS_FILES)


def _server_routes() -> set:
    """(method, path) routes as written in server.py's _route dispatcher."""
    src = SERVER_PATH.read_text(encoding="utf-8")
    return set(re.findall(r'\("(GET|POST)", "(/api/[A-Za-z0-9_/-]+)"\)', src))


def _spa_endpoints() -> set:
    """Every /api/... path string literal used by the SPA (query stripped)."""
    html = _spa()
    return set(re.findall(r'["`](/api/[A-Za-z0-9_/-]+)', html)) | set(
        re.findall(r'["`](/api/[A-Za-z0-9_/-]+)', _js()))


def _spa_sources() -> str:
    """index.html + all JS — the complete static surface element ids and
    structural checks must scan."""
    return _spa() + "\n" + _js()


class TestGroupsIntegration(unittest.TestCase):
    """The Groups UI must drive the real /api/groups/* backend."""

    def test_every_group_endpoint_is_called(self):
        js = _js()
        for endpoint in ("groups/create", "groups/update", "groups/delete",
                         "groups/add-member", "groups/remove-member",
                         "groups/set-account", "groups/clear-account"):
            self.assertIn(endpoint, js,
                          f"the SPA must exercise POST /api/{endpoint}")

    def test_group_endpoints_exist_on_server(self):
        routes = _server_routes()
        get_only = {"groups", "groups/info"}
        for ep in ("groups", "groups/info", "groups/create", "groups/update",
                   "groups/delete", "groups/add-member",
                   "groups/remove-member", "groups/set-account",
                   "groups/clear-account"):
            method = "GET" if ep in get_only else "POST"
            self.assertIn((method, "/api/" + ep), routes,
                          f"server must implement {method} /api/{ep}")

    def test_obsolete_readonly_assumptions_gone(self):
        js = _js()
        self.assertNotIn("readonly", js,
                         "groups tab must not fall back to read-only mode")
        self.assertNotIn("manageGroup(", js, "stub group manager must stay gone")

    def test_group_controls_functional(self):
        js = _js()
        self.assertIn("openGroupDetails", js)
        self.assertIn("openGroupEdit", js)
        self.assertIn("openMemberPicker", js)
        self.assertIn("openGroupPicker", js)
        self.assertIn("deleteGroupFlow", js)

    def test_no_dummy_profile_and_no_fake_group_ui(self):
        js = _js()
        self.assertNotIn("__dummy__", js,
                         "fake profile assignment is forbidden forever")
        self.assertNotIn("fakeGroup", js)

    def test_group_moves_use_group_endpoints_only(self):
        """Moving an account between groups must NEVER delete/recreate the
        account or tamper with profiles — only the membership endpoints."""
        js = _js()
        move = re.search(r"async function moveAccountGroup.*?\n}", js, re.S)
        self.assertIsNotNone(move, "moveAccountGroup missing")
        body = move.group(0)
        self.assertIn("/api/groups/set-account", body)
        self.assertIn("/api/groups/clear-account", body)
        self.assertNotIn("/api/accounts/remove", body,
                         "group moves must not delete accounts")
        self.assertNotIn("/api/profiles", body,
                         "group moves must not touch profiles")

    def test_no_group_startall_fleet_wide(self):
        """Start/Stop buttons on a group card act on THAT group's members
        only (the v1 fleet-wide lane is gone by design)."""
        js = _js()
        group_actions = re.search(
            r"root\.querySelectorAll\(\"\[data-gact\]\"\).*?\n  \}\);", js, re.S)
        self.assertIsNotNone(group_actions)
        self.assertNotIn("groups/fleet", group_actions.group(0))
        self.assertIn("members.map", group_actions.group(0),
                      "group start/stop iterate the group's own members")

    def test_groups_loaded_over_rest_not_derived(self):
        """GET /api/groups is the authoritative list (empty groups must be
        discoverable) — not the account-derived groupMap()."""
        js = _js()
        self.assertIn('await api("/api/groups")', js,
                      "loadGroups must fetch the persisted group list")
        self.assertIn("groupsList", js,
                      "the SPA must keep a persisted-groups state variable")

    def test_group_mutation_renders_once_after_both_fetches(self):
        """afterGroupMutation must fetch groups AND accounts, then render
        exactly once — loadGroups' own render is suppressed, otherwise a
        mutation paints fresh groups with stale account chips (and does a
        duplicate full DOM rebuild) before the accounts land."""
        js = _js()
        body = js.split("async function afterGroupMutation")[1].split("\n}")[0]
        self.assertIn("loadGroups(false)", body,
                      "mutation path must suppress loadGroups' own render")
        self.assertIn("refreshAccountsNow()", body,
                      "mutation path must also refresh account records")
        self.assertEqual(body.count("render()"), 1,
                         "exactly one render per group mutation")


class TestAccountPluginsModel(unittest.TestCase):
    """Capabilities are DIRECT account→plugin assignments (the legacy
    profile-mediated model is gone from the UI). Phase 2 re-added a
    Profiles tab over the NEW per-plugin profile model — so the LEGACY
    global-profile UI (prof-grid cards, create dialog, global pickers,
    effectiveCaps) must stay gone, but a "profiles" nav entry now exists
    by design (see TestPluginProfilesUI for the new model's pins). The
    plugin flows must drive the real /api/accounts/plugins* backend."""

    def test_legacy_global_profile_ui_gone_new_tab_present(self):
        html = _spa()
        js = _js()
        for gone in ('id="prof-grid"', 'id="btn-create-prof"',
                     'id="dlg-create-prof"', 'id="prof-list"',
                     'id="aa-profiles"', 'id="cp-plugins"', 'id="cp-name"'):
            self.assertNotIn(gone, html, f"legacy {gone} must stay removed")
        for gone in ("openProfilePicker", "effectiveCaps",
                     "Effective Capabilities"):
            self.assertNotIn(gone, js, f"legacy {gone} must stay removed")
        # the NEW tab: nav is DATA now — the sidebar is built from the
        # server's navigation API, so the order contract lives in
        # core.framework_ui.FRAMEWORK_VIEWS and app.js must CONSUME it
        from core.framework_ui import FRAMEWORK_VIEWS
        views = list(FRAMEWORK_VIEWS)
        self.assertEqual(views.count("profiles"), 1,
                         "exactly one profiles page")
        self.assertEqual(views.index("profiles"), views.index("plugins") + 1,
                         "Profiles sits between Plugins and Activity")
        self.assertRegex(js, r"let NAV = \[\];",
                         "app.js must consume the dynamic NAV (no hardcoded "
                         "page list — that is the breaking-hardcode class)")
        # TITLES is data too: labels come from the server's page titles,
        # so assert the REGISTRY carries them (attach count == views)
        from core.framework_ui import attach_framework_ui
        from core.dashboard_ui.registry import DashboardUIRegistry
        reg = DashboardUIRegistry(offload_sync=False)
        count = attach_framework_ui(reg, None)
        self.assertEqual(count, len(views))
        titles = [p["title"] for p in (
            reg.get_layout("$framework").get("children") or [])]
        self.assertIn("Profiles", titles)
        self.assertIn("Design", titles)

    def test_no_profiles_state_or_loader(self):
        js = _js()
        for token in ("profilesList", "profilesFailed", "loadProfiles"):
            self.assertNotIn(token, js,
                             f"profile state '{token}' must be removed")

    def test_spa_never_calls_profiles_api(self):
        """The /api/profiles routes stay on the server (backend-retained)
        but the SPA must not call them anymore — explicitly absent from
        the endpoint set the existence test whitelists."""
        self.assertNotIn("/api/profiles", _spa())
        self.assertNotIn("/api/profiles", _js())
        self.assertNotIn("/api/profiles", _spa_endpoints())

    def test_account_menu_assign_plugins(self):
        js = _js_file("accounts.js")
        self.assertIn('data-act="plugins"', js,
                      "account menu needs the Assign Plugins action")
        self.assertIn("Assign Plugins…", js)
        self.assertNotIn('data-act="profile"', js)
        self.assertNotIn("Assign profile", js)
        self.assertIn("function openPluginPicker", js)
        # tile and list share the SAME wiring (no duplicated flows)
        for view in ("acctTile", "acctRow"):
            body = re.search(rf"function {view}\(.*?\n}}", js, re.S)
            self.assertIsNotNone(body, f"{view} missing")
            self.assertIn("wireAccountActions", body.group(0))

    def test_plugin_picker_from_manifests_desired_state_save(self):
        """The picker's checkbox choices come from the discovered
        manifests (no hardcoded plugin list), current assignments are
        pre-checked, and Save is the desired-state whole-set POST — an
        unknown name 400s and keeps the dialog open with the error."""
        js = _js_file("accounts.js")
        picker = re.search(r"async function openPluginPicker.*?\n}", js, re.S)
        self.assertIsNotNone(picker, "openPluginPicker missing")
        body = picker.group(0)
        # checkbox list built from the discovered manifests via the
        # shared builder (never a hardcoded plugin list)
        self.assertIn("fillPluginChecks(list, current)", body)
        self.assertIn("assignedPlugins(a)", body,
                      "current assignments pre-checked from the account's"
                      " assigned_plugins field (via the helper)")
        self.assertIn("/api/accounts/plugins?account=", body,
                      "picker loads the authoritative assignment list")
        self.assertIn('post("/api/accounts/plugins/set"', body,
                      "Save must POST the desired state")
        self.assertIn('setFb(list, "error", e.message)', body,
                      "server-side rejection keeps the dialog open + inline")
        checks = re.search(r"function fillPluginChecks.*?\n}", js, re.S)
        self.assertIsNotNone(checks, "fillPluginChecks missing")
        self.assertIn("checkbox", checks.group(0))
        self.assertIn("manifests", checks.group(0))
        self.assertIn("current.has(m.name)", checks.group(0),
                      "current assignments must pre-check the boxes")
        helper = re.search(r"function assignedPlugins\(.*?\n}", js, re.S)
        self.assertIsNotNone(helper, "assignedPlugins helper missing")
        self.assertIn("assigned_plugins", helper.group(0),
                      "the helper must read the direct assignment field")

    def test_add_account_sends_plugins_checkboxes(self):
        """The add-account dialog must send plugins:[checked] from the
        /api/plugins manifests — the old 'profiles' text input 400s on
        the new backend."""
        html = _spa()
        js = _js_file("app.js")
        self.assertIn('id="aa-plugins"', html,
                      "add-account dialog needs the plugins checkbox host")
        self.assertNotIn("aa-profiles", _js())
        self.assertIn('fillPluginChecks($("aa-plugins"), null)', js,
                      "dialog open must rebuild the checkbox list")
        submit = re.search(r"\$\(\"aa-submit\"\)\.onclick.*?\n  };", js, re.S)
        self.assertIsNotNone(submit, "aa-submit wiring missing")
        body = submit.group(0)
        self.assertIn("body.plugins", body,
                      "payload must carry the checked plugin names")
        self.assertNotIn("profiles", body)
        # the shared builder iterates manifests (never a hardcoded list)
        checks = re.search(r"function fillPluginChecks.*?\n}", _js(), re.S)
        self.assertIsNotNone(checks, "fillPluginChecks missing")
        self.assertIn("manifests", checks.group(0))

    def test_cards_and_details_use_assigned_plugins(self):
        js = _js_file("accounts.js")
        self.assertIn("assigned_plugins", js,
                      "cards/details read the direct assignment field")
        self.assertIn("Assigned Plugins", js,
                      "details popup needs an Assigned Plugins card")
        self.assertNotIn("a.profiles", js)
        self.assertNotIn("chip profile", js)

    def test_search_covers_accounts_groups_plugins_only(self):
        js = _js_file("app.js")
        search = re.search(r"\$\(\"search\"\)\.oninput.*?\n  };", js, re.S)
        self.assertIsNotNone(search, "search wiring missing")
        body = search.group(0)
        for scope in ("accounts()", "groupsList", "manifests"):
            self.assertIn(scope, body, f"search must cover {scope}")
        self.assertNotIn("profilesList", body)

    def test_plugin_endpoint_routes_exist_on_server(self):
        routes = _server_routes()
        for method, path in (
            ("GET", "/api/accounts/plugins"),
            ("POST", "/api/accounts/plugins/assign"),
            ("POST", "/api/accounts/plugins/unassign"),
            ("POST", "/api/accounts/plugins/set"),
        ):
            self.assertIn((method, path), routes,
                          f"server must implement {method} {path}")

    def test_plugin_endpoints_used_by_spa(self):
        endpoints = _spa_endpoints()
        for ep in ("/api/accounts/plugins", "/api/accounts/plugins/set",
                   "/api/accounts/plugins/unassign"):
            self.assertIn(ep, endpoints, f"the SPA must exercise {ep}")


class TestPluginProfilesUI(unittest.TestCase):
    """Phase 2: the Plugins/Profiles tabs drive ONLY the plugin-profile
    backend — library CRUD (/api/plugins/profiles*), bindings
    (/api/accounts/plugin-profile[s]) and the channels/effective-settings
    reads — never the retained legacy /api/profiles system. The editor is
    schema-driven off the plugin manifests and carries the reserved
    channel-scope section (nested guild→channel picker + manual fallback).
    """

    PHASE1_EIGHT = {
        "/api/plugins/profiles", "/api/plugins/profiles/create",
        "/api/plugins/profiles/update", "/api/plugins/profiles/delete",
        "/api/accounts/plugin-profiles", "/api/accounts/plugin-profile",
        "/api/accounts/effective-settings", "/api/accounts/channels",
        # plugin lifecycle (load/unload added later)
        "/api/plugins/reload", "/api/plugins/unload",
    }

    def _fn(self, name):
        body = re.search(rf"function {name}\(.*?\n}}", _js_file("plugins-ui.js"), re.S)
        self.assertIsNotNone(body, f"{name} missing from plugins-ui.js")
        return body.group(0)

    def test_module_registered_and_owns_both_renderers(self):
        self.assertIn("plugins-ui.js", JS_FILES)
        html = _spa()
        # script tag loads it between views.js and app.js (exact order is
        # separately enforced by test_every_script_tag_resolves;
        # dashboard-ui.js — the Stage-D generic renderer — loads between
        # plugin-ui.js and app.js)
        self.assertIn('<script src="js/views.js"></script>\n'
                      '<script src="js/plugins-ui.js"></script>\n'
                      '<script src="js/admin-ui.js"></script>\n'
                      '<script src="js/plugin-ui.js"></script>\n'
                      '<script src="js/dashboard-ui.js"></script>\n'
                      '<script src="js/framework-ui.js"></script>\n'
                      '<script src="js/app.js"></script>', html)
        js = _js()
        self.assertEqual(js.count("function renderPlugins"), 1)
        self.assertEqual(js.count("function renderProfiles"), 1)
        self.assertIn("function renderPlugins", _js_file("plugins-ui.js"))
        self.assertIn("function renderProfiles", _js_file("plugins-ui.js"))
        self.assertNotIn("function renderPlugins", _js_file("views.js"),
                         "the Plugins tab renderer lives in plugins-ui.js")
        # shell + dispatch + first-visit loader
        self.assertIn('id="panel-profiles"', html)
        self.assertIn('id="pp-acct"', html)
        self.assertIn('id="pp-host"', html)
        appjs = _js_file("app.js")
        # every main tab (incl. fleet + profiles) is a framework page now
        self.assertNotIn('renderProfiles();', appjs.split("function wire")[0])
        # every main tab is a framework page — no legacy loaders remain
        self.assertNotIn("LOADERS.profiles =", appjs)
        self.assertNotIn("LOADERS.fleet =", appjs)
        # the loader swallows its own failure (fire-and-forget from LOADERS)
        self.assertIn("catch", self._fn("loadPluginProfiles"))

    def test_endpoint_set_is_exactly_the_phase1_eight(self):
        used = set(re.findall(r'["`](/api/[A-Za-z0-9_/-]+)',
                              _js_file("plugins-ui.js")))
        self.assertEqual(used, self.PHASE1_EIGHT,
                         "plugins-ui.js may call ONLY the Phase-1 endpoints")
        endpoints = _spa_endpoints()
        for ep in self.PHASE1_EIGHT:
            self.assertIn(ep, endpoints, f"the SPA must exercise {ep}")
        routes = _server_routes()
        for method, path in (
                ("GET", "/api/plugins/profiles"),
                ("POST", "/api/plugins/profiles/create"),
                ("POST", "/api/plugins/profiles/update"),
                ("POST", "/api/plugins/profiles/delete"),
                ("GET", "/api/accounts/plugin-profiles"),
                ("POST", "/api/accounts/plugin-profile"),
                ("GET", "/api/accounts/effective-settings"),
                ("GET", "/api/accounts/channels")):
            self.assertIn((method, path), routes,
                          f"server must implement {method} {path}")
        self.assertNotIn("/api/profiles", endpoints)

    def test_plugins_tab_library_and_quick_bind(self):
        render = self._fn("renderPlugins")
        # profile library rows: name, channel-mode badge, edit + delete
        self.assertIn("profilesOf(m.name)", render)
        self.assertIn('data-prof=', render)
        self.assertIn("channelModeOf(p)", render)
        self.assertIn('data-pact="edit"', render)
        self.assertIn('data-pact="del"', render)
        self.assertIn('data-pact="new"', render)
        # quick binds exist ONLY for accounts the plugin is assigned to
        self.assertIn("assignedPlugins(a).includes(m.name)", render)
        self.assertIn('data-qb-sel', render)
        wire = self._fn("wirePluginCard")
        self.assertIn('post("/api/plugins/profiles/delete"', wire)
        self.assertIn("confirmAction", wire)
        self.assertIn('post("/api/accounts/plugin-profile"', wire)
        self.assertIn("sel.value || null", wire,
                      "empty selection posts profile:null (defaults)")
        self.assertIn("setFb(qb,", wire,
                      "quick-bind errors render inline next to the row")

    def test_profiles_tab_binding_flows(self):
        render = self._fn("renderProfiles")
        self.assertIn('$("pp-acct")', render)
        self.assertIn("assignedPlugins(a)", render,
                      "one section per ASSIGNED plugin")
        self.assertIn("a.plugin_profiles", render,
                      "bindings read the account status dict")
        self.assertIn('data-bb="unbind"', render)
        self.assertIn('data-bb="bind"', render)
        self.assertIn('data-bb="edit"', render)
        # unbind posts profile:null; errors stay inline in the card
        self.assertIn("profile:null", render)
        self.assertIn('setFb(card,"error",e.message)', render)
        # no bound profile yet: Edit offers the picker (pick or create)
        self.assertIn("openBindPicker(ppAccount, pname)", render)
        picker = self._fn("openBindPicker")
        self.assertIn('post("/api/accounts/plugin-profile"', picker)
        self.assertIn("selOpt.dataset.val || null", picker,
                      "picker binds a profile STRING or null for default")
        self.assertIn("openProfileEditor(plugin, null, {account, bind:true})",
                      picker, "create-new path binds after creating")

    def test_editor_is_schema_driven(self):
        editor = self._fn("openProfileEditor")
        self.assertIn("m.settings", editor,
                      "settings form comes from the manifest schema")
        self.assertIn('data-pf="extras"', editor,
                      "unknown-to-schema keys ride as a raw JSON textarea")
        self.assertIn('input[name="ppmode"]', editor)
        self.assertIn('"/api/accounts/channels?account="', editor)
        self.assertIn('post("/api/plugins/profiles/create"', editor)
        self.assertIn('post("/api/plugins/profiles/update"', editor)
        self.assertIn("payload.name_new", editor,
                      "rename sent only when the name changed")
        # channels is NOT a schema input: scope rides the reserved key
        self.assertIn('settingsOut.channels = scope.mode==="all"', editor)
        field = self._fn("schemaFieldHTML")
        for t in ("bool", "int", "float"):
            self.assertIn(f's.type==="{t}"', field,
                          f"schema field builder must dispatch {t}")
        # str is the fallback branch: input tagged data-t="str"
        self.assertIn('data-t="str"', field)
        self.assertIn('type="checkbox"', field)
        self.assertIn('type="number"', field)
        self.assertIn("s.default", field, "unset keys prefill the default")
        collect = self._fn("collectProfileSettings")
        self.assertIn("JSON.parse", collect)

    def test_channel_picker_nested_guild_channel_tree(self):
        tree = self._fn("renderChannelTree")
        self.assertIn("pp-guild", tree, "guild groups render")
        self.assertIn("pp-ghead", tree, "collapsible guild headers")
        self.assertIn('classList.toggle("open")', tree)
        self.assertIn('type="checkbox"', tree, "one checkbox per channel")
        self.assertIn('data-cid=', tree)
        self.assertIn("scope.rules.find", tree,
                      "current selections pre-check from channels.rules")
        self.assertIn("toggleScopeChannel", tree,
                      "toggling updates the local scope model")

    def test_channel_picker_manual_fallback_and_preview(self):
        box = self._fn("renderScopeBox")
        # channels==[] (offline account) -> notice + manual textarea,
        # always-available manual add alongside the tree
        self.assertIn("enter channel IDs manually", box)
        self.assertIn('data-pf="mtext"', box)
        self.assertIn("Add manually", box)
        self.assertIn("parseScopeText", box)
        self.assertIn('data-pf="preview"', box)
        self.assertIn("scopeCounts(scope)", box,
                      "live N/M preview derives from the scope model")
        parse = self._fn("parseScopeText")
        self.assertIn('indexOf(":")', parse,
                      "manual lines are guild_id:channel_id pairs")
        scope = self._fn("renderScopeBox")
        self.assertIn("guilds.length", scope,
                      "offline branch keyed on the empty channels list")


class TestStartupRobustness(unittest.TestCase):
    """One uncaught init error must not be possible from static causes."""

    def test_script_block_balanced(self):
        js = _js()
        for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(js.count(a), js.count(b),
                             f"unbalanced {a}{b} in SPA javascript")

    def test_every_script_tag_resolves(self):
        html = _spa()
        tags = re.findall(r'<script src="(js/[A-Za-z0-9_.-]+)"></script>', html)
        self.assertEqual(tags, [f"js/{n}" for n in JS_FILES],
                         "script tags must load the known modules in order")
        for tag in tags:
            self.assertTrue((STATIC_DIR / tag).is_file(),
                            f"{tag} referenced but missing")

    def test_every_stylesheet_resolves(self):
        html = _spa()
        tags = re.findall(r'<link rel="stylesheet" href="(styles/[A-Za-z0-9_.-]+)">', html)
        self.assertEqual(tags, [f"styles/{n}" for n in CSS_FILES],
                         "stylesheet links must match the known files")
        for tag in tags:
            self.assertTrue((STATIC_DIR / tag).is_file(),
                            f"{tag} referenced but missing")

    def test_no_inline_script_or_style(self):
        """The shell must stay a shell: behaviour in js/, looks in styles/."""
        html = _spa()
        self.assertIsNone(re.search(r"<script>(?!.*src=)", html, re.S),
                          "inline <script> blocks belong in js/")
        self.assertNotIn("<style>", html, "inline <style> belongs in styles/")

    def test_referenced_element_ids_exist(self):
        html = _spa()
        js = _js()
        defined = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        referenced = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
        dynamic = {"nav-badge-",  # built by buildNav(), guarded at use
                   "admin-id-",    # admin-ui builds these via innerHTML
                   "mudae-id-"}
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
        js = _js()
        for loader in ("loadManifests", "loadGroups", "loadScenarios"):
            # every loader definition contains a try/catch -> showErr path
            body = re.search(
                rf"async function {loader}\([^)]*\) {{(.*?)\n}}", js, re.S)
            self.assertIsNotNone(body, f"{loader} missing")
            self.assertIn("catch", body.group(1),
                          f"{loader} must not reject (blocks Promise.all)")

    def test_init_reports_errors_to_banner(self):
        self.assertIn("dashboard init failed", _js(),
                      "init failures must be reported visibly")

    def test_boot_activates_initial_view(self):
        """Regression: .panel{display:none} — the boot sequence must call
        UI.go(...) once, otherwise the first paint is a blank main area
        until the user clicks a nav item."""
        js = _js()
        boot = js.split("loadDashboardUINav().then(() => { buildNav(); render(); });")[1].split(
            "Promise.all")[0]
        self.assertIn("UI.go(", boot,
                      "boot must activate the initial panel view")

    def test_render_activates_current_view_panel(self):
        """Regression: when the first WS stats arrive AFTER boot, render()
        must move the dashboard off the onboarding panel onto the current
        view — renderHome() filling hidden metrics left the onboarding
        screen up forever."""
        js = _js()
        self.assertIn('showPanel("panel-"+currentView)', js,
                      "render must activate the current view's panel")
        self.assertIn('showPanel("panel-onboard")', js,
                      "empty-state render must activate onboarding")

    def test_zero_accounts_do_not_trap_navigation(self):
        """Regression: with zero accounts, the onboarding welcome pinned
        every view but Tools — 'Get Started' (and any nav click) snapped
        straight back to onboarding, making the first account impossible
        to add. The guard must cover Home only."""
        js = _js()
        guard = re.search(
            r"if \(!hasAccts && ([^)]+)\) \{ showPanel\(\"panel-onboard\"\)", js)
        self.assertIsNotNone(guard, "zero-account onboarding guard missing")
        self.assertEqual(guard.group(1).strip(), 'currentView==="home"',
                         "onboarding must pin Home only, not all views")
        self.assertIn('id="onboard-cta"', _spa(),
                      "Get Started button must exist in the shell")
        self.assertIn('$("onboard-cta").onclick', js,
                      "Get Started must be wired in wire()")

    def test_formatter_calls_have_definitions(self):
        """Regression: fmtLatency() was called at two render sites but the
        function was defined as fmtLat — openAcctDetails() threw
        ReferenceError on EVERY open, and account cards with measured
        latency broke the fleet view."""
        js = _js()
        called = set(re.findall(r"\b(fmt[A-Z]\w*)\s*\(", js))
        defined = set(re.findall(r"function (fmt[A-Z]\w*)\s*\(", js))
        self.assertEqual(
            called - defined, set(),
            "fmt helpers called but not defined: %s" % (called - defined))

    def test_element_helpers_not_interpolated(self):
        """Regression: btn()/btnG() return DOM elements — interpolating
        them into an innerHTML template literal stringifies them to
        [object HTMLButtonElement] (account cards lost Start/Stop/Details
        and profile cards lost Delete, showing raw placeholder text)."""
        js = _js()
        for helper in ("btn(", "btnG("):
            self.assertNotIn("${" + helper, js,
                             f"${{{helper}...}} interpolates an element into "
                             "innerHTML — use raw HTML + post-parse wiring")

    def test_document_listeners_only_in_wire(self):
        """Regression: registering document/window listeners from a render
        path accumulated one listener per card per WS tick and froze the
        page. Global listeners may ONLY be attached inside wire()."""
        for name in JS_FILES:
            js = _js_file(name)
            for m in re.finditer(r"document\.addEventListener", js):
                start = js.rfind("function ", 0, m.start())
                fn = js[start:].split("(", 1)[0].replace("function ", "")
                self.assertEqual(
                    fn, "wire",
                    f"{name}: document.addEventListener inside '{fn}' — "
                    "global listeners belong in wire() only")
        self.assertIn("document.addEventListener", _js_file("app.js"),
                      "wire() must own the global listeners")


class TestUiStructure(unittest.TestCase):
    """Popup, view-mode, token and feedback structure rules."""

    def test_every_dialog_has_upper_right_close_button(self):
        html = _spa()
        dialogs = re.findall(r"<dialog[^>]*id=\"([a-z-]+)\">", html)
        self.assertTrue(dialogs, "no dialogs found")
        for did in dialogs:
            block = re.search(
                rf'<dialog id="{did}">(.*?)</dialog>', html, re.S).group(1)
            hdr = re.search(r'<div class="dlg-hdr">(.*?)</div>', block, re.S)
            self.assertIsNotNone(hdr, f"dlg {did} has no header")
            self.assertRegex(
                hdr.group(1), r'class="dlg-close"[^>]*aria-label="Close"',
                f"dlg {did} lacks the standard close button")

    def test_dialogs_are_viewport_centered(self):
        css = _css()
        dialog_rule = re.search(r"dialog \{(.*?)\}", css, re.S).group(1)
        for prop in ("position: fixed", "inset: 0", "margin: auto"):
            self.assertIn(prop, dialog_rule,
                          f"dialog centering needs {prop}")
        self.assertRegex(dialog_rule, r"max-height:",
                         "dialogs must be viewport-height constrained")
        self.assertIn("dialog .dlg-body {", css)
        self.assertIn("overflow-y: auto", css, "dialog body must scroll")

    def test_view_modes_exist_and_persist(self):
        js = _js()
        html = _spa()
        for sel, mode in (("vm-acct", "acctViewMode"), ("vm-group", "groupViewMode")):
            self.assertIn(f'id="{sel}"', html, f"{sel} selector missing")
            self.assertIn(f'data-mode="tiles"', html)
            self.assertIn(f'data-mode="list"', html)
            self.assertIn(mode, js)
        self.assertIn('store.setItem("pref_acct_view"', js,
                      "account view mode must persist")
        self.assertIn('store.setItem("pref_group_view"', js,
                      "group view mode must persist")

    def test_tile_and_list_share_wiring(self):
        """List/tile renderers must not duplicate business logic: both call
        the same wire*Actions function."""
        js = _js()
        for view, wire in (("acctTile", "wireAccountActions"),
                           ("acctRow", "wireAccountActions"),
                           ("groupTile", "wireGroupActions"),
                           ("groupRow", "wireGroupActions")):
            body = re.search(rf"function {view}\(.*?\n}}", js, re.S)
            self.assertIsNotNone(body, f"{view} missing")
            self.assertIn(wire, body.group(0),
                          f"{view} must delegate actions to {wire}")

    def test_token_not_in_header(self):
        """The dashboard token input must live in Tools → Advanced, never
        in the always-visible header."""
        html = _spa()
        hdr = re.search(r'<header id="hdr">(.*?)</header>', html, re.S)
        self.assertIsNotNone(hdr)
        self.assertNotIn("token", hdr.group(1).lower(),
                         "header must not render the token input")
        adv = re.search(r'<div id="ttab-adv".*?</div>\s*</div>\s*</div>\s*</div>', html, re.S)
        self.assertIn('id="adv-token"', html, "Tools → Advanced must hold the token field")

    def test_token_uses_session_storage_only(self):
        """Credentials must never persist in localStorage."""
        js = _js_file("state.js")
        self.assertIn('secretStore.getItem("dash_token")', js)
        token_sets = re.findall(r'secretStore\.setItem\("dash_token"', _js())
        self.assertTrue(token_sets, "token save must use secretStore")
        # the ONLY tolerated localStorage access is the one-time migration
        # that adopts the legacy value and scrubs it — never a write
        self.assertNotIn('store.setItem("dash_token"', _js(),
                         "writing the token to localStorage is forbidden")
        self.assertIn('store.removeItem("dash_token")', _js(),
                      "boot must scrub any legacy localStorage token")
        for name in JS_FILES:
            src = _js_file(name)
            for m in re.finditer(r"localStorage", src):
                ctx = src[max(0, m.start()-200):m.start()+200]
                if "dash_token" in ctx and "removeItem" not in ctx and "kept the token in localStorage" not in ctx:
                    self.fail(f"{name}: dash_token must not touch localStorage")

    def test_no_global_error_strip(self):
        """The old #errbox strip between header and body must stay gone;
        scoped [data-fb] slots + #toasts replace it."""
        html = _spa()
        self.assertNotIn('id="errbox"', html,
                         "global error strip removed — use inline feedback")
        self.assertIn('id="toasts"', html, "toast host missing")
        js = _js()
        self.assertIn("function setFb(", js, "inline feedback helper missing")

    def test_policy_rows_are_editable(self):
        """Each stored policy row must offer an edit button that prefills
        the shared dialog — editing reuses POST /api/ratelimit/set (the
        engine keys by (level, owner, action): setting an existing key
        UPDATES it, memory-first) and loadPolicies() re-renders the table
        instantly. No new endpoint, no page reload."""
        js = _js()
        html = _spa()
        self.assertIn("data-rledit=", js, "policy rows need edit buttons")
        self.assertEqual(
            js.count("data-rledit="), 1,
            "edit buttons are wired once in renderPolicyTable")
        self.assertIn("function openPolicyEditor", js)
        # the editor carries every field the row holds
        for ref in ("rp-action", "rp-interval", "rp-jmin", "rp-jmax",
                    "rp-level", "rp-owner"):
            self.assertIn(f'"{ref}")', js.split("function openPolicyEditor")[1].split("\n}")[0],
                          f"openPolicyEditor must prefill {ref}")
        self.assertIn('id="rp-title"', html,
                      "dialog title needs an id for Add/Edit switching")
        # edit must go through the SAME set endpoint as add
        self.assertEqual(js.count('post("/api/ratelimit/set"'), 1)

    def test_picker_rows_are_structured(self):
        """Regression: picker rows (plugin/member/group) were inline
        flex+text-node lists — a long description squeezed the checkbox and
        the name. All three builders must use the .pick-row grid (fixed
        checkbox column, non-shrinking .pl-name, ellipsized .pl-desc)."""
        js = _js()
        css = _css()
        for builder in ("fillPluginChecks", "openMemberPicker", "openGroupPicker"):
            body = re.search(rf"function {builder}\(.*?\n}}", js, re.S)
            self.assertIsNotNone(body, f"{builder} missing")
            self.assertIn("pick-row", body.group(0),
                          f"{builder} must use the structured .pick-row grid")
        self.assertIn("pl-name", js)
        self.assertIn("pl-desc", js)
        self.assertRegex(
            css, r"\.pick-row \{[^}]*grid-template-columns: 18px auto minmax\(0, 1fr\)",
            "pick-row must fix the checkbox column and let only the "
            "description absorb overflow")
        self.assertRegex(
            css, r"\.pick-row input\[type=\"checkbox\"\][^{]*\{[^}]*width: 15px",
            "picker checkboxes must have a fixed size")

    def test_feedback_placement_guards(self):
        """Regression: inline errors are appended as the last child of
        their scope, so the scope's layout decides their box — inside a
        list-row GRID they landed in the 18px checkbox column as an
        unreadable sliver, and inside the bulk bar's flex ROW they were a
        squeezed content-width item. Guards must force full placement, and
        gapped parents (.field/.form) must not double-space the message."""
        css = _css()
        self.assertRegex(css, r"\.lrow \.fb \{[^}]*grid-column: 1 / -1",
                         "list-row errors must span every grid column")
        self.assertRegex(css, r"#bulkbar \.fb[^{]*\{[^}]*flex: 1 1 100%",
                         "bulk-bar errors must take a full-width line")
        self.assertRegex(css, r"\.field \.fb, \.form \.fb \{[^}]*margin-top: 0",
                         "gapped parents position the message themselves")

    def test_custom_scrollbar_theme(self):
        css = _css()
        self.assertIn("::-webkit-scrollbar", css,
                      "themed scrollbar missing")
        self.assertIn("scrollbar-width: thin", css)
        self.assertIn("scrollbar-color:", css)

    def test_old_positioning_hack_gone(self):
        """dialog must not carry ad-hoc margins that broke centering."""
        css = _css()
        dialog_rule = re.search(r"dialog \{(.*?)\}", css, re.S).group(1)
        self.assertNotIn("margin: 8px auto", dialog_rule)


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
        css = _css()
        self.assertRegex(css, r"--border2: #[0-9a-fA-F]{6}",
                         "--border2 must be a well-formed hex color")
        # hex color candidates: #tokens containing at least one digit (CSS
        # id selectors in this sheet contain none), all chars must be hex
        tokens = re.findall(r"#[0-9A-Za-z]{3,8}\b", css)
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
        # loop — a blocking urlopen on it starves the route coroutine and
        # every request would 504.
        return await asyncio.to_thread(_get, f"{self.base}{path}", as_json)

    async def test_index_served(self) -> None:
        status, body = await self._get("/", as_json=False)
        self.assertEqual(status, 200)
        self.assertIn("LibzentSuite", body)

    async def test_static_assets_served_without_auth(self) -> None:
        """The split frontend's CSS/JS must be reachable BEFORE the token
        exists (same rule as the index) — and traversal must be blocked."""
        def raw(path):
            try:
                with urllib.request.urlopen(f"{self.base}/{path}", timeout=10) as resp:
                    return resp.status, resp.read().decode("utf-8"), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                return exc.code, "", ""
        for path, marker in (("styles/base.css", "--border2"),
                             ("styles/components.css", "acct-card"),
                             ("js/state.js", "dash_token"),
                             ("js/app.js", "connectWS")):
            status, body, ctype = await asyncio.to_thread(raw, path)
            self.assertEqual(status, 200, path)
            self.assertIn(marker, body, path)
            self.assertIn("text/css" if "css" in path else "javascript", ctype, path)
        # any traversal shape must resolve outside STATIC_DIR → 404
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
        """The onboarding path depends on status.accounts being a list."""
        _, body = await self._get("/api/status")
        self.assertIsInstance(body["data"]["accounts"], list)
        self.assertEqual(body["data"]["accounts"], [])


class TestRowActionWiring(unittest.TestCase):
    """Row-action buttons render INSIDE table bodies, which load
    asynchronously AFTER wireDashboardUI ran — loadDUIData must wire
    them on every render or they are dead DOM (the bug behind
    'unresponsive buttons')."""

    def test_load_dui_data_wires_row_actions(self):
        js = _js()
        self.assertIn("wireDUIRowActions(host, pluginName);", js,
                      "loadDUIData must wire row-action buttons in the "
                      "freshly rendered table body")
        # and the shared wiring helper exists + is idempotent
        self.assertIn("function wireDUIRowActions(root, pluginName)", js)
        self.assertIn('el.dataset.duiWired = "1"', js)
        # feedback must target a host OUTSIDE the data body (an action's
        # own refresh re-renders the body and would wipe the message)
        self.assertIn('const body = el.closest("[data-dui-body]");', js)

    def test_wire_dashboard_ui_still_wires_row_actions(self):
        js = _js()
        # the static path (pre-rendered markup) keeps wiring too
        self.assertIn("wireDUIRowActions(root, pluginName);", js)


if __name__ == "__main__":
    unittest.main()


class TestWidthCollapseRegression(unittest.TestCase):
    """The width-collapse defect (content squeezed into a left strip):
    .dui-item carried max-width:480px — the wrapper of EVERY component,
    capping whole lists/tables at 480px while the viewport sat empty.
    Inside it, minmax(0,1fr) tracks computed to 0px → vertical text.
    Pinned so the cap can never silently return."""

    def test_dui_item_has_no_width_cap(self):
        css = (STATIC_DIR / "styles" / "components.css").read_text()
        import re
        m = re.search(r"\.dui-item \{[^}]*\}", css)
        self.assertIsNotNone(m, ".dui-item rule must exist")
        self.assertNotIn("max-width: 480", m.group(0),
                         ".dui-item must never carry the 480px cap")
        self.assertIn("max-width: none", m.group(0))

    def test_page_fills_panel_no_arbitrary_cap(self):
        css = (STATIC_DIR / "styles" / "components.css").read_text()
        import re
        m = re.search(r"\.dui-page \{[^}]*\}", css)
        self.assertIsNotNone(m)
        self.assertIn("width: 100%", m.group(0))
        self.assertNotIn("max-width: 1280", m.group(0))


class TestListRecordsRenderer(unittest.TestCase):
    """Phase 3: accounts render as structured record rows via ListView
    records mode — the aligned list contract."""

    def test_records_branch_in_load(self):
        js = _js()
        self.assertIn("duiListRecordsHTML(comp, data, compId)", js)
        # chosen ONLY when columns are declared (kv mode stays legacy)
        self.assertIn('comp.type === "list-view" && Array.isArray(comp.columns)',
                      js)

    def test_row_grid_alignment_contract(self):
        js = _js()
        # alignment from the row grid, not per-button margins
        self.assertIn("grid-template-columns:${gridCols}", js)
        # four-zone anatomy: selection, cells, trailing actions
        self.assertIn("dui-lrow-actions", js)
        self.assertIn("dui-lrow-identity", js)
        self.assertIn("dui-lrow-sel", js)

    def test_status_never_color_alone(self):
        js = _js()
        # dot + TEXT together
        self.assertIn("dui-lrow-dot", js)
        self.assertIn("${esc(v.text)}", js)

    def test_fleet_uses_listview_records(self):
        py = (ROOT / "core" / "framework_ui.py").read_text()
        self.assertIn('ListView(component_id="acct-list")', py)
        self.assertIn('.set_columns(["Account", "Status", "Group", "Plugins"])',
                      py)
        self.assertIn('ListView(component_id="group-list")', py)
        # the tile grid is no longer used for accounts
        self.assertNotIn('TileView(component_id="acct-tiles")', py)
        self.assertNotIn('TileView(component_id="group-tiles")', py)


class TestRendererHardening(unittest.TestCase):
    """Spec 9.5: renderer guarantees pinned as static checks."""

    def test_every_component_type_has_renderer(self):
        js = _js()
        for ctype in ("page", "tab", "section", "card", "row", "column",
                      "container", "toolbar", "grid", "tile-view", "modal",
                      "alert", "text", "display-text", "separator",
                      "description", "header", "footer", "badge", "metric",
                      "progress", "button", "checkbox", "toggle",
                      "text-input", "number-input", "textarea", "select",
                      "radio-group", "multi-select", "list-view", "table",
                      "form"):
            self.assertIn(f'case "{ctype}"', js, f"no renderer for {ctype}")

    def test_tabs_are_functional_not_containers(self):
        js = _js()
        self.assertIn('role="tablist"', js)
        self.assertIn("wireDuiTabs(panel, pluginName);", js)
        # keyboard navigation + ARIA selection state
        self.assertIn('"ArrowRight"', js)
        self.assertIn('"ArrowLeft"', js)
        self.assertIn('aria-selected', js)

    def test_no_duplicate_handler_registration(self):
        js = _js()
        # every dynamic wiring path is idempotent via the wired guard
        for guard in ("el.dataset.duiWired", "cb.dataset.duiWired",
                      "btn.dataset.duiWired"):
            self.assertIn(guard, js)

    def test_disabled_and_visibility_enforced_server_side(self):
        # the frontend never decides enablement: requests carry only ids
        js = _js()
        self.assertNotIn("disabled: true, action:", js)

    def test_escaping_applied_in_attribute_contexts(self):
        js = _js()
        # icon paths and row keys are esc()'d inside attributes
        self.assertIn('d="${esc(iconPath)}"', js)
        self.assertIn('data-dui-row="${esc(key)}"', js)

    def test_stale_request_guard_present(self):
        js = _js()
        self.assertIn("seq !== duiSeq", js)
        self.assertIn("seq !== fwSeq", js)

    def test_account_scoped_pages_send_validated_account(self):
        js = _js()
        self.assertIn("duiPageAccount[pluginName]", js)
        self.assertIn("body.account = acct", js)
        self.assertIn("renderDuiAccountPicker", js)

class TestPromptSystemContracts(unittest.TestCase):
    """User-directed prompt-system contracts (Oct 2026 round). The
    floating feedback chips must ALWAYS be anchored + aligned under
    their triggering control, only ONE may be visible at a time, they
    must be opaque (readable over any content), and add/remove /
    high-priority outcomes must surface the 5s header prompt. These
    static assertions pin the mechanism so the original failure mode -
    form-bottom-anchored, translucent, stacked prompts - cannot
    silently return."""

    def test_float_calls_carry_anchor_and_align(self):
        """Every setFbFloat/flashFbFloat call passes >= 3 top-level
        arguments: (container, text-or-level, ..., anchorEl, alignEl).
        A 2-argument call is the old form-bottom-anchored bug."""
        import re as _re

        def top_level_args(call_body: str):
            args, depth, cur = [], 0, []
            for ch in call_body:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if ch == "," and depth == 0:
                    args.append("".join(cur).strip())
                    cur = []
                else:
                    cur.append(ch)
            args.append("".join(cur).strip())
            return [a for a in args]

        for name in ("dashboard-ui.js", "admin-ui.js", "views.js",
                     "accounts.js", "groups.js", "app.js"):
            js = _js_file(name)
            for match in _re.finditer(
                    r"(?:set|flash)FbFloat\(([^()]*(?:\([^()]*(?:\([^()]*\))?\))?)\)",
                    js):
                call = match.group(1)
                args = top_level_args(call)
                min_args = 4 if match.group(0).startswith("setFbFloat") else 3
                self.assertGreaterEqual(
                    len(args), min_args,
                    f"{name}: unanchored float call "
                    f"{match.group(0)[:70]!r} - every float must pass "
                    f"anchorEl (+ alignEl for setFbFloat)")

    def test_single_float_purge_and_alignment_exist(self):
        ui = _js_file("ui.js")
        self.assertIn('querySelectorAll(".fb-float[data-fb]")', ui,
                      "the most-recent-wins purge (no overlapping "
                      "prompts) must stay in the float branch")
        self.assertIn("fbAnchorFor", ui)
        self.assertIn("alignEl", ui,
                      "chips must align to their triggering element")

    def test_floats_are_opaque(self):
        css = _css()
        self.assertIn(".fb-float {", css)
        self.assertIn("background: var(--surface)",
                      css.split(".fb-float {", 1)[1].split("}", 1)[0],
                      "float chips must be OPAQUE surface chips - the "
                      "translucent tint let page content bleed through")

    def test_header_prompt_surface_and_usage(self):
        ui = _js_file("ui.js")
        self.assertIn("function showHeaderPrompt", ui)
        self.assertIn("?5000", ui.replace(" ", ""),
                      "header prompt default duration must be 5s")
        for name in ("app.js", "accounts.js", "groups.js", "admin-ui.js",
                     "plugins-ui.js"):
            self.assertIn("showHeaderPrompt(", _js_file(name),
                          f"{name} must surface add/remove outcomes via "
                          f"the header prompt")
        css = _css()
        self.assertIn("#hdr-prompt", css)
        self.assertIn("position: fixed", css)

    def test_component_chip_paths_unchanged(self):
        """The component-level chips stay: field errors render inline on
        their slot; dui-act/dui-in handlers keep their anchors."""
        js = _js_file("dashboard-ui.js")
        self.assertIn('setFb(slot, "error", err)', js)
        self.assertIn('fbAnchorFor(el)', js)

class TestButtonHintNeverOverlaps(unittest.TestCase):
    """User guard: button descriptions must NEVER render as visible hint
    text — inside the fixed-height Toolbar they overlap the content
    below. They render as native tooltips (title attribute) instead,
    which cannot overlap by construction."""

    def test_button_renderer_uses_tooltip_not_hint(self):
        js = _js_file("dashboard-ui.js")
        render_btn = js.split("function renderButton", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertNotIn("duiHint(comp)", render_btn,
                         "renderButton must not emit a visible hint")
        self.assertIn('title="${esc(comp.description)}"', render_btn,
                      "button descriptions must become native tooltips")

    def test_toolbar_hint_overlay_rules_gone(self):
        """The overlay experiment was rejected - the base toolbar hint
        rules stay exactly as designed (in-flow, right-aligned) because
        toolbar buttons no longer emit hints at all."""
        css = _css()
        self.assertNotIn(
            ".dui-toolbar-actions .dui-item .hint { position: absolute",
            css)

class TestDuiTableExpansionAndKeys(unittest.TestCase):
    """User-reported Design-tab table bugs: (1) expanding via 'show all'
    collapsed on the next WS stats tick because loadDUIData re-rendered
    from the ORIGINAL page_size; (2) tables without a key column gave
    EVERY row the same empty selection key, so checking one checkbox
    marked its neighbours too. Both mechanisms are pinned here."""

    def test_expansion_survives_data_refresh(self):
        js = _js_file("dashboard-ui.js")
        self.assertIn("const duiExpanded = {};", js,
                      "per-component expansion store missing")
        self.assertIn("duiExpanded[compId] = true;", js,
                      "'show all' must record the expansion")
        table_fn = js.split("function duiTableHTML", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertIn("duiExpanded[compId] ? 0", table_fn,
                      "duiTableHTML must honour the stored expansion "
                      "over the declared page_size")

    def test_keyless_rows_get_unique_selection_keys(self):
        js = _js_file("dashboard-ui.js")
        table_fn = js.split("function duiTableHTML", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertIn('"#" + rowIndex', table_fn,
                      "keyless rows need a synthetic unique key - an "
                      "empty key made one checkbox mark all others")
        tiles_fn = js.split("function duiTilesHTML", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertIn('"#" + tileIndex', tiles_fn,
                      "TileView shares the same keyless-row hazard")

class TestSelectAllAndAutoCollapse(unittest.TestCase):
    """User request: tables get a header SELECT-ALL (with partial-
    selection indeterminate state), and expanded tables auto-collapse
    when the active view moves away."""

    def test_header_select_all_wired(self):
        js = _js_file("dashboard-ui.js")
        table_fn = js.split("function duiTableHTML", 1)[1].split(
            "\nfunction ", 1)[0]
        self.assertIn('data-dui-selall=', table_fn,
                      "selectable tables must render a header select-all")
        self.assertIn("syncDuiSelAll", js,
                      "partial selection must reflect as indeterminate")
        self.assertIn("indeterminate", js)

    def test_navigation_resets_expansion(self):
        self.assertIn("function duiResetExpansions",
                      _js_file("dashboard-ui.js"))
        app = _js()
        self.assertIn("duiResetExpansions()", app,
                      "UI.go must collapse expansions when navigating")
