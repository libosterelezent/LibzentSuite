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

    async def plugins_state(self):
        return [{"account": "a1", "plugin": "probe", "enabled": True,
                 "active": True, "error_count": 0}]

    async def plugins_reload(self, plugin, surface="api"):
        self.calls.append(("plugins_reload", plugin))
        return ({"discovered": ["probe"]} if plugin == "__rescan__"
                else {"restored_accounts": 1})

    async def plugins_unload(self, plugin, surface="api"):
        self.calls.append(("plugins_unload", plugin))
        return {"detached_accounts": 1}

    async def plugins_set_enabled(self, account, plugin, enabled, surface="api"):
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
        return [{"ts": "t", "account": "a1", "level": "info", "message": "hi"}]

    async def audit_tail(self, n=100, **kw):
        return [{"ts": "t", "account": "a1", "event": "e", "detail": "d"}]

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
        return {"admins": [1, 2], "mudae_bot_id": 123,
                "accounts": {"a1": {"is_admin": False, "prefix_mode": "mention",
                                    "permissions": {"help": True}}}}

    async def admin_add_id(self, user_id, surface="api"):
        self.calls.append(("admin_add_id", user_id))
        return {}

    async def admin_remove_id(self, user_id, surface="api"):
        self.calls.append(("admin_remove_id", user_id))
        return {}

    async def admin_mudae_id_set(self, user_id, surface="api"):
        self.calls.append(("admin_mudae_id_set", user_id))
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
        return {"log": "ran " + name}

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
    def test_registers_six_main_tab_pages(self):
        reg, api, count = make_registry()
        self.assertEqual(count, 6)
        self.assertIn(FRAMEWORK_PLUGIN, reg.list_plugins())
        layout = reg.get_layout(FRAMEWORK_PLUGIN)
        page_ids = [p["id"] for p in (layout.get("children") or [])]
        self.assertEqual(sorted(page_ids), sorted(FRAMEWORK_VIEWS))

    def test_reattach_replaces_atomically(self):
        reg, api, _ = make_registry()
        # a second attach replaces without error (validation-first)
        self.assertEqual(attach_framework_ui(reg, StubAPI()), 6)
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
        audit = await reg.get_data(FRAMEWORK_PLUGIN, "act-audit", None)
        self.assertIn("e — d", audit["value"])


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
