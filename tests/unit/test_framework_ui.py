"""Framework UI tests — the main tabs as builder pages ($framework)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "fixtures"))

from core.dashboard_ui import DashboardUIToolset, RowAction
from core.dashboard_ui.components import Table, Metric, Text, Form
from core.dashboard_ui.context import ActionContext
from core.dashboard_ui.errors import UIError
from core.dashboard_ui.registry import DashboardUIRegistry
from core.framework_ui import FRAMEWORK_PLUGIN, FRAMEWORK_VIEWS, attach_framework_ui


class StubAPI:
    """Every ControlAPI surface the framework pages touch."""

    def __init__(self):
        self.calls: list = []

    async def status(self):
        return {"accounts": [
                    {"name": "a1", "status": "running", "sync_group": "g1"},
                    {"name": "a2", "status": "invalid_credential", "sync_group": None}],
                "ratelimit": {"floor": 1.5,
                              "hits_429": {"total": 3, "accounts": {"a1": 3}},
                              "cooldowns": {"accounts": {"a1": 2}}},
                "tasks": {"running": 2, "queued": 1},
                "uptime_s": 7200}

    async def groups_list(self):
        return [{"name": "g1", "member_count": 1, "enabled": True}]

    async def accounts_list(self):
        return [{"name": "a1", "assigned_plugins": ["probe"], "sync_group": "g1"},
                {"name": "a2", "assigned_plugins": [], "sync_group": None}]

    async def plugins_manifests(self):
        return [{"name": "probe", "version": "2.0", "description": "fixture"}]

    async def plugins_state(self, account):
        return [{"name": "probe", "enabled": True,
                 "active": True, "error_count": 0}]

    async def plugins_reload(self, plugin, surface="api"):
        self.calls.append(("plugins_reload", plugin))
        return ({"discovered": ["probe"]} if plugin == "__rescan__"
                else {"restored_accounts": 1})

    async def plugins_unload(self, plugin, surface="api"):
        self.calls.append(("plugins_unload", plugin))
        return {"detached_accounts": 1}

    async def plugins_set_enabled(self, account, plugin, enabled):
        # real ControlAPI takes no surface kwarg
        self.calls.append(("plugins_set_enabled", account, plugin, enabled))
        return {}

    async def tasks_list(self):
        return [{"id": 7, "account": "a1", "plugin": "probe",
                 "status": "waiting", "run_count": 3}]

    async def tasks_cancel(self, task_id):
        self.calls.append(("tasks_cancel", task_id))
        return {"cancelled": True}

    async def cooldowns_list(self):
        return [{"account": "a1", "plugin": "probe", "action": "message",
                 "until": 4102444800, "count": 2}]

    async def cooldowns_clear(self, account, plugin, cooldown_key=None, surface="api"):
        self.calls.append(("cooldowns_clear", account, plugin))
        return {}

    async def logs_tail(self, n=100, **kw):
        return [{"ts": "t", "account": "a1", "severity": "info",
                 "message": "hi"}]

    async def audit_tail(self, n=100, **kw):
        # real audit schema: action/note (the LOG schema's 'event'/
        # 'detail' never exist here — that bug rendered '?')
        return [{"ts": "t", "account": "a1", "action": "e", "note": "d"}]

    async def ratelimit_policies(self):
        return [{"level": "global", "owner": None, "action": "message",
                 "interval": 2.6, "jitter": [0, 0.2]}]

    async def ratelimit_set(self, action, interval, jmin, jmax, level, owner):
        self.calls.append(("ratelimit_set", action, interval, level, owner))
        return {}

    async def ratelimit_clear(self, level, owner, action):
        self.calls.append(("ratelimit_clear", level, owner, action))
        return {"cleared": True}

    async def ratelimit_stats(self):
        return {"floor": 1.5}

    async def ratelimit_set_floor(self, seconds):
        self.calls.append(("ratelimit_set_floor", seconds))
        return {"floor": seconds}

    async def admin_overview(self):
        return {"admins": [1, 2], "primary_bot_id": 123,
                "accounts": {"a1": {"is_admin": False, "prefix_mode": "mention",
                                    "permissions": {"help": True}}}}

    async def admin_add_id(self, user_id, surface="api"):
        self.calls.append(("admin_add_id", user_id))
        return {}

    async def admin_remove_id(self, user_id, surface="api"):
        self.calls.append(("admin_remove_id", user_id))
        return {}

    async def admin_primary_bot_id_set(self, user_id, surface="api"):
        self.calls.append(("admin_primary_bot_id_set", user_id))
        return {}

    async def admin_prefix_set(self, account, mode, symbol, surface="api"):
        self.calls.append(("admin_prefix_set", account, mode, symbol))
        return {}

    async def admin_account_flag(self, account, is_admin, surface="api"):
        self.calls.append(("admin_account_flag", account, is_admin))
        return {}

    async def admin_forget_account(self, account, surface="api"):
        self.calls.append(("admin_forget_account", account))
        return {}

    async def simulation_list(self):
        return [{"name": "smoke"}]

    async def simulation_run(self, name):
        self.calls.append(("simulation_run", name))
        # real runner schema: passed/log_lines/summary (not 'log')
        return {"passed": True, "log_lines": ["ran " + name],
                "summary": "ok"}

    async def emergency_stop_all(self, surface="api"):
        self.calls.append(("emergency_stop_all",))
        return {}

    async def system_resume(self):
        self.calls.append(("system_resume",))
        return {}

    async def credentials_import_env(self, surface="api"):
        self.calls.append(("credentials_import_env",))
        return {}


def make_registry():
    api = StubAPI()
    reg = DashboardUIRegistry()
    count = attach_framework_ui(reg, api)
    return reg, api, count


class TestAttach(unittest.TestCase):
    def test_registers_every_main_tab_page(self):
        reg, api, count = make_registry()
        self.assertEqual(count, len(FRAMEWORK_VIEWS))
        self.assertIn(FRAMEWORK_PLUGIN, reg.list_plugins())
        layout = reg.get_layout(FRAMEWORK_PLUGIN)
        page_ids = [p["id"] for p in (layout.get("children") or [])]
        self.assertEqual(sorted(page_ids), sorted(FRAMEWORK_VIEWS))

    def test_reattach_replaces_atomically(self):
        reg, api, _ = make_registry()
        # a second attach replaces without error (validation-first)
        self.assertEqual(
            attach_framework_ui(reg, StubAPI()), len(FRAMEWORK_VIEWS))
        self.assertIn(FRAMEWORK_PLUGIN, reg.list_plugins())

    def test_reservered_name_cannot_collide_with_real_plugins(self):
        # the loader rejects '$' in plugin names — '$framework' is safe
        self.assertTrue(FRAMEWORK_PLUGIN.startswith("$"))


class TestProviders(unittest.IsolatedAsyncioTestCase):
    async def test_home_metric_computes_level(self):
        reg, api, _ = make_registry()
        data = await reg.get_data(FRAMEWORK_PLUGIN, "hm-accounts", None)
        # 1 of 2 running -> warn
        self.assertEqual(data, {"value": "1/2", "sub": "online", "level": "warn"})

    async def test_empty_fleet_is_healthy_not_error(self):
        # v1 semantics: 0/0 renders green (the onboarding panel owns the
        # zero-account experience, not a red metric)
        api = StubAPI()

        async def empty_status():
            return {"accounts": [], "ratelimit": {}, "tasks": {},
                    "uptime_s": 0}
        api.status = empty_status
        reg = DashboardUIRegistry()
        attach_framework_ui(reg, api)
        data = await reg.get_data(FRAMEWORK_PLUGIN, "hm-accounts", None)
        self.assertEqual(data["level"], "ok")

    async def test_attention_flags_credential_trouble(self):
        reg, api, _ = make_registry()
        items = await reg.get_data(FRAMEWORK_PLUGIN, "home-attn", None)
        self.assertEqual(items, [{"label": "a2", "value": "credential invalid"}])

    async def test_groups_table_shape(self):
        reg, api, _ = make_registry()
        data = await reg.get_data(FRAMEWORK_PLUGIN, "home-groups", None)
        self.assertEqual(data["columns"], ["Group", "Members", "Online", "State"])
        self.assertEqual(data["rows"][0][0], "g1")

    async def test_plugin_state_emits_composite_key_cell(self):
        reg, api, _ = make_registry()
        data = await reg.get_data(FRAMEWORK_PLUGIN, "plug-state", None)
        # the __key cell rides PAST the visible columns
        self.assertEqual(len(data["rows"][0]), len(data["columns"]) + 1)
        self.assertEqual(data["rows"][0][-1], "a1|probe")

    async def test_policies_key_encoding_roundtrips(self):
        reg, api, _ = make_registry()
        data = await reg.get_data(FRAMEWORK_PLUGIN, "pol-table", None)
        key = data["rows"][0][-1]
        level, _, rest = key.partition("\u0001")
        owner, _, action = rest.partition("\u0001")
        self.assertEqual((level, owner, action), ("global", "", "message"))

    async def test_logs_and_audit_render_lines(self):
        reg, api, _ = make_registry()
        logs = await reg.get_data(FRAMEWORK_PLUGIN, "act-logs", None)
        self.assertIn("hi", logs["value"])
        self.assertIn("INFO", logs["value"])   # severity renders, not level
        audit = await reg.get_data(FRAMEWORK_PLUGIN, "act-audit", None)
        self.assertIn("e", audit["value"])     # the action
        self.assertIn("d", audit["value"])     # the note


class TestActions(unittest.IsolatedAsyncioTestCase):
    async def test_row_action_dispatch_routes_to_callable(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="plug-manifests")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "plug-manifests",
            {"action": "reload", "row": "probe"}, ctx)
        self.assertTrue(result.success)
        self.assertIn("probe reloaded", result.message)
        self.assertIn("plug-manifests", result.refresh)
        self.assertIn(("plugins_reload", "probe"), api.calls)

    async def test_unknown_row_action_rejected(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="plug-manifests")
        with self.assertRaises(UIError):
            await reg.dispatch_action(
                FRAMEWORK_PLUGIN, "plug-manifests",
                {"action": "nope", "row": "probe"}, ctx)

    async def test_plugin_toggle_reads_state_then_flips(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="plug-state")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "plug-state",
            {"action": "toggle", "row": "a1|probe"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("plugins_set_enabled", "a1", "probe", False), api.calls)

    async def test_task_cancel_row(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="act-tasks")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "act-tasks",
            {"action": "cancel", "row": "7"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("tasks_cancel", 7), api.calls)

    async def test_cooldown_clear_splits_composite_key(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="act-cds")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "act-cds",
            {"action": "clear", "row": "a1|probe"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("cooldowns_clear", "a1", "probe"), api.calls)

    async def test_policy_form_validates_interval(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="pol-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "pol-form", {"pol-interval": 0}, ctx)
        self.assertFalse(result.success)
        self.assertIn("pol-interval", result.field_errors)
        self.assertNotIn(("ratelimit_set",), [c[:1] for c in api.calls])

    async def test_policy_form_submits_and_refreshes(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="pol-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "pol-form",
            {"pol-action": "message", "pol-interval": 2.6,
             "pol-level": "global"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("ratelimit_set", "message", 2.6, "global", None), api.calls)
        self.assertIn("pol-table", result.refresh)

    async def test_rescan_button_uses_reserved_name(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="plug-rescan")
        result = await reg.dispatch_action(FRAMEWORK_PLUGIN, "plug-rescan", {}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("plugins_reload", "__rescan__"), api.calls)

    async def test_admin_forms_validate_integers(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="adm-add-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "adm-add-form", {"adm-add-id": "abc"}, ctx)
        self.assertFalse(result.success)
        self.assertIn("adm-add-id", result.field_errors)

    async def test_admin_ids_table_lists_entries(self):
        reg, api, _ = make_registry()
        data = await reg.get_data(FRAMEWORK_PLUGIN, "adm-ids", None)
        self.assertEqual(data, {"columns": ["User id"],
                                "rows": [["1"], ["2"]]})

    async def test_admin_id_remove_is_a_row_action(self):
        # removal is a ✕ on each entry — the typed remove form is GONE
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="adm-ids")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "adm-ids",
            {"action": "remove", "row": "42"}, ctx)
        self.assertTrue(result.success)
        self.assertIn("admin 42 removed", result.message)
        self.assertIn(("admin_remove_id", 42), api.calls)
        self.assertIn("adm-ids", result.refresh)

    async def test_typed_remove_form_no_longer_exists(self):
        from core.dashboard_ui.errors import UIError
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="adm-remove-form")
        with self.assertRaises(UIError):
            await reg.dispatch_action(
                FRAMEWORK_PLUGIN, "adm-remove-form", {"adm-remove-id": "1"}, ctx)

    async def test_admin_toggle_row_flips_flag(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="adm-accounts")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "adm-accounts",
            {"action": "toggle-admin", "row": "a1"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("admin_account_flag", "a1", True), api.calls)

    async def test_token_form_has_no_server_action(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="token-form")
        with self.assertRaises(UIError):
            await reg.dispatch_action(
                FRAMEWORK_PLUGIN, "token-form", {"token-value": "x"}, ctx)

    async def test_refresh_buttons_report_and_refresh(self):
        # regression: these used to return a bare {"refresh": [...]} dict
        # which the registry swallowed — the buttons looked dead
        reg, api, _ = make_registry()
        for cid, target in (("act-tasks-refresh", "act-tasks"),
                            ("act-cds-refresh", "act-cds"),
                            ("act-logs-refresh", "act-logs"),
                            ("act-audit-refresh", "act-audit")):
            ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id=cid)
            result = await reg.dispatch_action(FRAMEWORK_PLUGIN, cid, {}, ctx)
            self.assertTrue(result.success, cid)
            self.assertEqual(result.refresh, [target], cid)
            self.assertTrue(result.message, cid)

    async def test_simulation_run_feeds_report_provider(self):
        reg, api, _ = make_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="sim-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "sim-form", {"sim-name": "smoke"}, ctx)
        self.assertTrue(result.success)
        report = await reg.get_data(FRAMEWORK_PLUGIN, "sim-report", None)
        self.assertEqual(report["value"], "ran smoke")


class TestRowActionComponent(unittest.TestCase):
    def test_serializes_row_actions_and_key_column(self):
        table = (DashboardUIToolset().Table(component_id="t")
                 .set_columns(["Name", "Count"])
                 .set_key_column("Name")
                 .set_row_actions([
                     RowAction("go", "Go", lambda row, ctx: None),
                     RowAction("del", "Delete", lambda row, ctx: None,
                               variant="danger", confirm="really?"),
                 ]))
        props = table._props()
        self.assertEqual(props["key_column"], "Name")
        self.assertEqual([a["id"] for a in props["row_actions"]], ["go", "del"])
        self.assertEqual(props["row_actions"][1]["variant"], "danger")
        self.assertEqual(props["row_actions"][1]["confirm"], "really?")

    def test_duplicate_row_action_ids_rejected(self):
        table = DashboardUIToolset().Table(component_id="t")
        with self.assertRaises(Exception):
            table.set_row_actions([
                RowAction("go", "Go", lambda row, ctx: None),
                RowAction("go", "Go again", lambda row, ctx: None)])

    def test_row_action_dispatch_awaits_async_callables(self):
        ran = []

        async def go(row, ctx):
            ran.append(row)
            return {"success": True, "message": f"did {row}"}

        table = (Table(component_id="t")
                 .set_key_column("Name")
                 .set_row_actions([RowAction("go", "Go", go)]))

        async def run():
            return await table._dispatch_row_action(
                None, {"action": "go", "row": "x"})

        import asyncio
        result = asyncio.run(run())
        self.assertEqual(ran, ["x"])
        self.assertTrue(result["success"])

    def test_live_metric_and_text_flags(self):
        m = Metric(component_id="m").set_data_source(lambda ctx=None: {"value": "1"})
        self.assertTrue(m._props().get("live"))
        t = Text(component_id="t").set_variant("code").set_data_source(lambda ctx=None: "x")
        self.assertTrue(t._props().get("live"))

    def test_form_description_and_client_action(self):
        f = (Form(component_id="f")
             .set_description("d")
             .set_client_action("save-token"))
        props = f._props()
        self.assertEqual(props["description"], "d")
        self.assertEqual(props["client_action"], "save-token")
        with self.assertRaises(Exception):
            Form(component_id="f2").set_client_action("nope")


if __name__ == "__main__":
    unittest.main()


class FleetStubAPI(StubAPI):
    """The fleet/profiles surfaces the new pages touch."""

    def __init__(self):
        super().__init__()
        self.fleet_calls: list = []
        self._groups = [{"name": "g1", "member_count": 1, "enabled": True,
                         "description": "farm"}]
        self._snap = {"name": "g1", "enabled": True, "description": "farm",
                      "pacing_key": "group:g1", "members": [
                          {"name": "a1", "status": "running"}]}

    async def status(self):
        return {"accounts": [
                    {"name": "a1", "status": "running", "sync_group": "g1",
                     "credential": "db", "uptime_s": 100, "latency_ms": 88,
                     "assigned_plugins": ["probe"], "enabled": True},
                    {"name": "a2", "status": "stopped", "sync_group": None,
                     "credential": None, "assigned_plugins": []}],
                "ratelimit": {}, "tasks": {}, "uptime_s": 10}

    async def accounts_start(self, account, surface="api"):
        self.fleet_calls.append(("start", account)); return {}

    async def accounts_stop(self, account, surface="api"):
        self.fleet_calls.append(("stop", account)); return {}

    async def accounts_emergency_stop(self, name, surface="api"):
        self.fleet_calls.append(("estop", name)); return {}

    async def accounts_remove(self, name, surface="api"):
        self.fleet_calls.append(("remove", name)); return {}

    async def accounts_add(self, payload, surface="api"):
        self.fleet_calls.append(("add", payload)); return {}

    async def accounts_set_credential(self, name, credential, surface="api"):
        self.fleet_calls.append(("cred", name)); return {}

    async def accounts_plugins_set(self, account, plugins, surface="api"):
        self.fleet_calls.append(("plugins-set", account, tuple(plugins))); return {}

    async def accounts_plugin_unassign(self, account, plugin, surface="api"):
        self.fleet_calls.append(("unassign", account, plugin)); return {}

    async def groups_list(self):
        return list(self._groups)

    async def groups_get(self, group):
        return dict(self._snap) if group == "g1" else None

    async def groups_snapshot(self, group):
        # real snapshot shape: members/status/pacing_key ride here —
        # groups_get returns only the record (api.py split)
        return dict(self._snap) if group == "g1" else None

    async def groups_create(self, payload, surface="api"):
        self.fleet_calls.append(("g-create", payload)); return {}

    async def groups_update(self, group, payload, surface="api"):
        self.fleet_calls.append(("g-update", group, payload)); return {}

    async def groups_delete(self, group, detach_members=False, surface="api"):
        self.fleet_calls.append(("g-delete", group, detach_members)); return {}

    async def groups_add_account(self, group, account, surface="api"):
        self.fleet_calls.append(("g-add", group, account)); return {}

    async def groups_remove_account(self, group, account, surface="api"):
        self.fleet_calls.append(("g-remove", group, account)); return {}

    async def groups_set_account(self, account, group, surface="api"):
        self.fleet_calls.append(("g-set", account, group)); return {}

    async def groups_clear_account(self, account, surface="api"):
        self.fleet_calls.append(("g-clear", account)); return {}

    async def plugin_profiles_list(self, plugin=None):
        return [{"plugin": "probe", "name": "fast", "description": "d",
                 "settings": {"trigger": "go"}}]

    async def accounts_plugin_profiles(self, account):
        return {"probe": "fast"}

    async def accounts_plugin_profile(self, account, plugin, profile, surface="api"):
        self.fleet_calls.append(("bind", account, plugin, profile)); return {}

    async def plugin_profile_create(self, plugin, name, description="",
                                    settings=None, surface="api"):
        self.fleet_calls.append(("p-create", plugin, name))
        self.create_settings = settings
        return {}

    async def plugin_profile_update(self, plugin, name, *, name_new=None,
                                    description=None, settings=None,
                                    surface="api"):
        self.fleet_calls.append(("p-update", plugin, name)); return {}

    async def plugin_profile_delete(self, plugin, name):
        self.fleet_calls.append(("p-delete", plugin, name)); return {}

    async def accounts_effective_settings(self, account, plugin):
        return {"trigger": "go", "channels": {"allow": ["1"]}}

    async def accounts_channels(self, account):
        return [{"guild_id": "999", "guild_name": "Test Guild",
                 "channels": [{"id": "111", "name": "general", "type": 0},
                              {"id": "222", "name": "grind", "type": 0}]}]

    async def _plugin_schema(self, plugin):
        from core.settings_manager import Setting
        if plugin != "probe":
            return ()
        return (Setting("trigger", "str", "go", "fixture trigger word"),
                Setting("factor", "float", 2.0, "multiplier",
                        min_value=0.1, max_value=10.0))


def make_fleet_registry():
    api = FleetStubAPI()
    reg = DashboardUIRegistry()
    attach_framework_ui(reg, api)
    return reg, api


class TestFleetPage(unittest.IsolatedAsyncioTestCase):
    async def test_eight_pages_register_including_fleet(self):
        reg, api, _ = make_registry() if False else (None, None, None)
        reg, api = make_fleet_registry()
        layout = reg.get_layout(FRAMEWORK_PLUGIN)
        ids = [p["id"] for p in layout["children"]]
        self.assertEqual(sorted(ids), sorted(FRAMEWORK_VIEWS))

    async def test_account_records_provider(self):
        reg, api = make_fleet_registry()
        data = await reg.get_data(FRAMEWORK_PLUGIN, "acct-list", None)
        keys = [r["key"] for r in data["records"]]
        self.assertEqual(keys, ["a1", "a2"])
        # identity cell: strong primary + mono secondary id
        self.assertEqual(data["records"][0]["cells"][0]["primary"], "a1")
        # status cell: text + tone (+ runtime meta line)
        status = data["records"][1]["cells"][1]
        self.assertEqual(status["text"], "stopped")
        self.assertEqual(status["tone"], "warn")   # no credential
        self.assertIn("🔑 missing", status["meta"])
        # assignments: group chips + plugin chips
        self.assertEqual(data["records"][0]["cells"][2]["chips"],
                         [{"text": "g1", "kind": "group"}])

    async def test_group_records_provider(self):
        reg, api = make_fleet_registry()
        data = await reg.get_data(FRAMEWORK_PLUGIN, "group-list", None)
        self.assertEqual(data["records"][0]["key"], "g1")
        self.assertIn("1/1", data["records"][0]["cells"][1]["text"])

    async def test_details_row_action_opens_modal(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="acct-list")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "acct-list",
            {"action": "details", "row": "a1"}, ctx)
        self.assertEqual(result.modal, "acct-details-modal")
        self.assertEqual(framework_ui._fleet_state["details"], "a1")
        lines = await reg.get_data(FRAMEWORK_PLUGIN, "ad-lines", None)
        self.assertTrue(any("db" == item["value"] for item in lines))

    async def test_bulk_start_dispatches_each(self):
        reg, api = make_fleet_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="acct-list")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "acct-list",
            {"action": "start", "rows": ["a1", "a2"]}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("start", "a1"), api.fleet_calls)
        self.assertIn(("start", "a2"), api.fleet_calls)

    async def test_bulk_move_opens_move_modal(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="acct-list")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "acct-list",
            {"action": "move", "rows": ["a1"]}, ctx)
        self.assertEqual(result.modal, "move-modal")
        self.assertEqual(framework_ui._fleet_state["move"], ["a1"])

    async def test_move_form_moves_all_selected(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._fleet_state["move"] = ["a1", "a2"]
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="move-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "move-form", {"mv-group": "g1"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("g-set", "a1", "g1"), api.fleet_calls)
        self.assertIn(("g-set", "a2", "g1"), api.fleet_calls)

    async def test_group_actions_route(self):
        reg, api = make_fleet_registry()
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="group-list")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "group-list",
            {"action": "open", "row": "g1"}, ctx)
        self.assertEqual(result.modal, "group-details-modal")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "group-list",
            {"action": "start", "row": "g1"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("start", "a1"), api.fleet_calls)

    async def test_dynamic_options_providers(self):
        reg, api = make_fleet_registry()
        groups = await reg.get_data(FRAMEWORK_PLUGIN, "fa-group", None)
        self.assertEqual([o["value"] for o in groups["options"]],
                         ["__independent__", "g1"])
        plugins = await reg.get_data(FRAMEWORK_PLUGIN, "fa-plugins", None)
        self.assertEqual([o["value"] for o in plugins["options"]], ["probe"])


class TestProfilesPage(unittest.IsolatedAsyncioTestCase):
    async def test_binding_tiles_provider(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["account"] = "a1"
        data = await reg.get_data(FRAMEWORK_PLUGIN, "prof-tiles", None)
        self.assertEqual([t["key"] for t in data["tiles"]], ["probe"])
        self.assertEqual(data["tiles"][0]["status"], "bound")

    async def test_bind_action_and_form(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["account"] = "a1"
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="prof-tiles")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "prof-tiles",
            {"action": "bind", "row": "probe"}, ctx)
        self.assertEqual(result.modal, "bind-modal")
        opts = await reg.get_data(FRAMEWORK_PLUGIN, "bp-profile", None)
        values = [o["value"] for o in opts["options"]]
        self.assertIn("fast", values)
        self.assertIn("__new__", values)
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "bind-form", {"bp-profile": "fast"}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("bind", "a1", "probe", "fast"), api.fleet_calls)

    async def test_editor_creates_profile(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["editor"] = {"plugin": "probe", "name": None}
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="editor-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "editor-form",
            {"pe-name": "fast2", "pe-settings": '{"trigger": "hi"}'}, ctx)
        self.assertTrue(result.success)
        self.assertIn(("p-create", "probe", "fast2"), api.fleet_calls)

    async def test_editor_fields_come_from_schema(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["editor"] = {
            "plugin": "probe", "name": None,
            "settings": {"trigger": "custom", "factor": 3.5}}
        data = await reg.get_data(FRAMEWORK_PLUGIN, "editor-form", None)
        by_id = {f["id"]: f for f in data["fields"]}
        self.assertEqual(by_id["trigger"]["type"], "text")
        self.assertEqual(by_id["trigger"]["value"], "custom")
        self.assertEqual(by_id["factor"]["type"], "number")
        self.assertEqual(by_id["factor"]["value"], "3.5")

    async def test_editor_submit_typed_settings(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["editor"] = {"plugin": "probe", "name": None,
                                                  "settings": {}}
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="editor-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "editor-form",
            {"pe-name": "typed", "trigger": "go", "factor": 2.5}, ctx)
        self.assertTrue(result.success, result.message)
        self.assertIn(("p-create", "probe", "typed"), api.fleet_calls)

    async def test_editor_fields_include_channel_selector(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["account"] = "a1"
        framework_ui._profiles_state["editor"] = {
            "plugin": "probe", "name": None,
            "settings": {"channels": {"channels": ["111"]}}}
        data = await reg.get_data(FRAMEWORK_PLUGIN, "editor-form", None)
        by_id = {f["id"]: f for f in data["fields"]}
        tree = by_id["channels-scope"]
        self.assertEqual(tree["type"], "channel-tree")
        self.assertEqual(tree["value"], ["111"])   # pre-checked
        self.assertEqual(len(tree["groups"]), 1)
        guild = tree["groups"][0]
        self.assertEqual(guild["id"], "999")
        self.assertEqual(guild["name"], "Test Guild")
        self.assertEqual([c["id"] for c in guild["channels"]], ["111", "222"])

    async def test_editor_fields_read_legacy_scope_shape(self):
        # stored mode/rules scopes pre-select their channels in the tree
        # (the re-edit-and-re-save conversion path)
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["account"] = "a1"
        framework_ui._profiles_state["editor"] = {
            "plugin": "probe", "name": None,
            "settings": {"channels": {"mode": "allow",
                                      "rules": [{"guild_id": "999",
                                                 "channel_ids": ["111"]}]}}}
        data = await reg.get_data(FRAMEWORK_PLUGIN, "editor-form", None)
        tree = {f["id"]: f for f in data["fields"]}["channels-scope"]
        self.assertEqual(tree["type"], "channel-tree")
        self.assertEqual(tree["value"], ["111"])

    async def test_editor_submit_packs_channel_scope(self):
        from core import framework_ui
        reg, api = make_fleet_registry()
        framework_ui._profiles_state["editor"] = {"plugin": "probe", "name": None,
                                                  "settings": {}}
        ctx = ActionContext(plugin=FRAMEWORK_PLUGIN, component_id="editor-form")
        result = await reg.dispatch_action(
            FRAMEWORK_PLUGIN, "editor-form",
            {"pe-name": "scoped", "channels-scope": ["111", "222"]}, ctx)
        self.assertTrue(result.success, result.message)
        create_calls = [c for c in api.fleet_calls if c[0] == "p-create"]
        self.assertTrue(create_calls)
        self.assertEqual(api.create_settings.get("channels"),
                         {"channels": ["111", "222"]})




class ViewScopedStateTests(unittest.TestCase):
    def test_two_views_do_not_share_modal_selection(self):
        from core import framework_ui
        from core.dashboard_ui.context import _VIEW_ID
        state = framework_ui._ViewState({"cred": None})
        t = _VIEW_ID.set("tabA")
        try:
            state["cred"] = "alpha"
            _VIEW_ID.set("tabB")
            self.assertIsNone(state.get("cred"))
            state["cred"] = "beta"
            _VIEW_ID.set("tabA")
            self.assertEqual(state.get("cred"), "alpha")
        finally:
            _VIEW_ID.reset(t)

    def test_views_are_lru_bounded(self):
        from core import framework_ui
        from core.dashboard_ui.context import _VIEW_ID
        state = framework_ui._ViewState({"x": 0})
        for i in range(state.MAX_VIEWS + 10):
            t = _VIEW_ID.set(f"v{i}")
            try:
                state["x"] = i
            finally:
                _VIEW_ID.reset(t)
        self.assertLessEqual(len(state._buckets), state.MAX_VIEWS)

    def test_view_id_header_value_is_sanitised(self):
        from core.dashboard_ui.context import clean_view_id
        self.assertEqual(clean_view_id("abc-123_X"), "abc-123_X")
        self.assertIsNone(clean_view_id("a b"))
        self.assertIsNone(clean_view_id("x" * 65))
        self.assertIsNone(clean_view_id(None))
