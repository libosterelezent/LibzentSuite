"""Stage-C unit tests: DashboardUIRegistry (core/dashboard_ui/registry.py).

Pins the registry contract: attachment (validate -> freeze -> snapshot),
action/check/provider extraction, idempotent detach, replace-on-reattach
(failed replacement preserves the old layout), visibility dispatch
(ancestor walk, fail-closed, sync + async checks), action dispatch
(normalized ActionResult), JSON-safe bounded data providers, and the
plugin-wide scope rules shared with the plugin manager (dedup across
accounts, ref-counted detach, isolated init failures).
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from core.dashboard_ui import (
    ActionContext,
    ActionResult,
    Button,
    CheckContext,
    DashboardUIRegistry,
    DashboardUIToolset,
    Layout,
    ListView,
    Page,
    Section,
    Table,
    Text,
    TreeValidationError,
    UIError,
)
from core.dashboard_ui.registry import MAX_DATA_ITEMS
from core.event_bus import EventBus
from core.plugin_manager import PluginManager
from core.settings_manager import SettingsManager

LOG = logging.getLogger("t")

PLUGIN = "myplug"


def _page_with_button(*, page_id="page1", section_id="sec1", button_id="btn1",
                      action=None, check=None, section_check=None):
    """Minimal valid tree: Layout > Page > Section > Button."""
    button = Button(component_id=button_id).set_label("Go")
    if action is not None:
        button.set_action(action)
    if check is not None:
        button.set_check(check)
    section = Section(component_id=section_id).add_component(button)
    if section_check is not None:
        section.set_check(section_check)
    page = Page(component_id=page_id).set_title("Main").add_section(section)
    return Layout().add_page(page)


class _FakeServices:
    """Minimal services stand-in (identity only — dispatch passes it through)."""

    def __init__(self) -> None:
        self.marker = "services-facade"

    async def send_message(self, channel_id, content, *, priority=1):
        return None


# ------------------------------------------------------------ attach/detach
class AttachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = DashboardUIRegistry()

    def test_attach_valid_layout_registers_everything(self) -> None:
        seen = []
        layout = _page_with_button(
            action=lambda ctx, payload: seen.append(payload),
            check=lambda ctx: True)
        table = Table(component_id="tbl").set_data_source(lambda ctx: [])
        layout.pages()[0].get_child("sec1").add_component(table)
        snapshot = self.registry.attach(PLUGIN, layout)

        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["type"], "layout")
        self.assertEqual(self.registry.list_plugins(), [PLUGIN])
        self.assertEqual(self.registry.get_layout(PLUGIN), snapshot)
        # actions / checks / providers extracted from the frozen walk
        self.assertIn("btn1", self.registry._actions[PLUGIN])
        self.assertIn("btn1", self.registry._checks[PLUGIN])
        self.assertIn("tbl", self.registry._providers[PLUGIN])
        # tree is frozen now
        with self.assertRaises(UIError):
            layout.add_page(Page(component_id="p9"))

    def test_attach_invalid_layout_raises_and_stores_nothing(self) -> None:
        # duplicate ids across pages pass per-parent checks but fail the
        # tree-wide validation
        layout = Layout().add_pages(
            Page(component_id="p1").add_section(Section(component_id="dup")),
            Page(component_id="p2").add_section(Section(component_id="dup")),
        )
        with self.assertRaises(TreeValidationError):
            self.registry.attach(PLUGIN, layout)
        self.assertEqual(self.registry.list_plugins(), [])
        self.assertIsNone(self.registry.get_layout(PLUGIN))
        # nothing was frozen — the tree is still fixable
        layout.remove_page("p2")

    def test_attach_rejects_non_layout_and_bad_names(self) -> None:
        with self.assertRaises(UIError):
            self.registry.attach(PLUGIN, "not-a-layout")
        with self.assertRaises(UIError):
            self.registry.attach("", Layout())
        with self.assertRaises(UIError):
            self.registry.attach(None, Layout())

    def test_attach_rejects_already_frozen_tree(self) -> None:
        layout = _page_with_button()
        self.registry.attach(PLUGIN, layout)
        with self.assertRaises(UIError):
            self.registry.attach("other", layout)  # same live object twice

    def test_detach_clears_everything_and_is_idempotent(self) -> None:
        layout = _page_with_button(action=lambda ctx, payload: None)
        self.registry.attach(PLUGIN, layout)
        self.assertTrue(self.registry.detach(PLUGIN))
        self.assertEqual(self.registry.list_plugins(), [])
        self.assertNotIn(PLUGIN, self.registry._actions)
        self.assertNotIn(PLUGIN, self.registry._checks)
        self.assertNotIn(PLUGIN, self.registry._providers)
        self.assertNotIn(PLUGIN, self.registry._nodes)
        self.assertNotIn(PLUGIN, self.registry._builders)
        # the tree is unfrozen again (reusable)
        self.assertFalse(layout._frozen)
        # idempotent
        self.assertFalse(self.registry.detach(PLUGIN))
        self.assertFalse(self.registry.detach("never-attached"))

    def test_reattach_replaces_old_registrations(self) -> None:
        layout1 = _page_with_button(button_id="btn1",
                                    action=lambda ctx, payload: "one")
        self.registry.attach(PLUGIN, layout1)
        layout2 = _page_with_button(button_id="btn2",
                                    page_id="page2",
                                    action=lambda ctx, payload: "two")
        snapshot2 = self.registry.attach(PLUGIN, layout2)
        self.assertEqual(self.registry.list_plugins(), [PLUGIN])
        self.assertEqual(self.registry.get_layout(PLUGIN), snapshot2)
        self.assertNotIn("btn1", self.registry._actions[PLUGIN])
        self.assertIn("btn2", self.registry._actions[PLUGIN])
        # the replaced tree was released
        self.assertFalse(layout1._frozen)
        self.assertTrue(layout2._frozen)

    def test_failed_replacement_preserves_old_layout(self) -> None:
        layout1 = _page_with_button(button_id="btn1",
                                    action=lambda ctx, payload: "one")
        old_snapshot = self.registry.attach(PLUGIN, layout1)
        bad = Layout().add_pages(
            Page(component_id="p1").add_section(Section(component_id="dup")),
            Page(component_id="p2").add_section(Section(component_id="dup")),
        )
        with self.assertRaises(TreeValidationError):
            self.registry.attach(PLUGIN, bad)
        # the OLD layout stays: snapshot + actions intact, still frozen
        self.assertEqual(self.registry.get_layout(PLUGIN), old_snapshot)
        self.assertIn("btn1", self.registry._actions[PLUGIN])
        self.assertTrue(layout1._frozen)

    def test_get_layout_returns_a_copy(self) -> None:
        self.registry.attach(PLUGIN, _page_with_button())
        snapshot = self.registry.get_layout(PLUGIN)
        snapshot["title"] = "mutated"
        self.assertNotIn("title", self.registry.get_layout(PLUGIN))

    def test_two_plugins_same_component_ids_do_not_collide(self) -> None:
        self.registry.attach("plug-a",
                             _page_with_button(action=lambda c, p: "a"))
        self.registry.attach("plug-b",
                             _page_with_button(action=lambda c, p: "b"))
        self.assertEqual(self.registry.list_plugins(), ["plug-a", "plug-b"])
        self.assertEqual(set(self.registry._actions["plug-a"]),
                         set(self.registry._actions["plug-b"]))
        self.assertIsNot(self.registry._actions["plug-a"]["btn1"],
                         self.registry._actions["plug-b"]["btn1"])


# ---------------------------------------------------------- action dispatch
class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = DashboardUIRegistry()

    async def test_dispatch_calls_callback_with_context_and_payload(self) -> None:
        seen = []

        def cb(ctx, payload):
            seen.append((ctx, payload))
            return ActionResult(success=True, message="done")

        self.registry.attach(PLUGIN, _page_with_button(action=cb))
        services = _FakeServices()
        result = await self.registry.dispatch_action(
            PLUGIN, "btn1", {"value": 7},
            ActionContext(plugin=PLUGIN, component_id="btn1",
                          account="alpha", services=services))
        self.assertEqual(result.success, True)
        self.assertEqual(result.message, "done")
        ctx, payload = seen[0]
        self.assertIsInstance(ctx, ActionContext)
        self.assertEqual(ctx.plugin, PLUGIN)
        self.assertEqual(ctx.component_id, "btn1")
        self.assertEqual(ctx.account, "alpha")
        self.assertIs(ctx.services, services)
        self.assertEqual(payload, {"value": 7})

    async def test_dispatch_result_normalization(self) -> None:
        cases = [
            (None, ActionResult(success=True)),
            ("saved", ActionResult(success=True, message="saved")),
            ({"success": False, "message": "nope",
              "field_errors": {"name": "too long"}},
             ActionResult(success=False, message="nope",
                          field_errors={"name": "too long"})),
            ({"rows": [1, 2]}, ActionResult(success=True,
                                           data={"rows": [1, 2]})),
            (False, ActionResult(success=False)),
            (3, ActionResult(success=True, data=3)),
        ]
        for raw, expected in cases:
            layout = _page_with_button(button_id="b", action=lambda c, p: raw)
            self.registry.attach(PLUGIN, layout)
            result = await self.registry.dispatch_action(
                PLUGIN, "b", {},
                ActionContext(plugin=PLUGIN, component_id="b"))
            self.assertEqual(result, expected)
            self.registry.detach(PLUGIN)

    async def test_dispatch_async_callback(self) -> None:
        async def cb(ctx, payload):
            return "async-done"

        self.registry.attach(PLUGIN, _page_with_button(action=cb))
        result = await self.registry.dispatch_action(
            PLUGIN, "btn1", {}, ActionContext(plugin=PLUGIN,
                                              component_id="btn1"))
        self.assertEqual((result.success, result.message),
                         (True, "async-done"))

    async def test_dispatch_on_hidden_component_denied(self) -> None:
        called = []
        self.registry.attach(PLUGIN, _page_with_button(
            action=lambda ctx, payload: called.append(1),
            check=lambda ctx: False))
        with self.assertRaises(UIError):
            await self.registry.dispatch_action(
                PLUGIN, "btn1", {},
                ActionContext(plugin=PLUGIN, component_id="btn1"))
        self.assertEqual(called, [])  # never invoked

    async def test_dispatch_on_ancestor_hidden_denied(self) -> None:
        called = []
        self.registry.attach(PLUGIN, _page_with_button(
            action=lambda ctx, payload: called.append(1),
            section_check=lambda ctx: False))
        with self.assertRaises(UIError):
            await self.registry.dispatch_action(
                PLUGIN, "btn1", {},
                ActionContext(plugin=PLUGIN, component_id="btn1"))
        self.assertEqual(called, [])

    async def test_dispatch_unknown_plugin_or_component(self) -> None:
        self.registry.attach(PLUGIN, _page_with_button(action=lambda c, p: None))
        ctx = ActionContext(plugin=PLUGIN, component_id="btn1")
        with self.assertRaises(UIError):
            await self.registry.dispatch_action("ghost", "btn1", {}, ctx)
        with self.assertRaises(UIError):
            await self.registry.dispatch_action(PLUGIN, "nope", {}, ctx)

    async def test_dispatch_component_without_action(self) -> None:
        layout = _page_with_button()
        layout.pages()[0].get_child("sec1").add_component(
            Text(component_id="txt").set_value("hi"))
        self.registry.attach(PLUGIN, layout)
        with self.assertRaises(UIError):
            await self.registry.dispatch_action(
                PLUGIN, "txt", {},
                ActionContext(plugin=PLUGIN, component_id="txt"))

    async def test_dispatch_rejects_non_dict_payload(self) -> None:
        self.registry.attach(PLUGIN, _page_with_button(action=lambda c, p: None))
        with self.assertRaises(UIError):
            await self.registry.dispatch_action(
                PLUGIN, "btn1", ["not", "a", "dict"],
                ActionContext(plugin=PLUGIN, component_id="btn1"))

    async def test_dispatch_callback_failure_raises_uierror(self) -> None:
        def cb(ctx, payload):
            raise RuntimeError("callback exploded")

        self.registry.attach(PLUGIN, _page_with_button(action=cb))
        with self.assertRaises(UIError) as caught:
            await self.registry.dispatch_action(
                PLUGIN, "btn1", {},
                ActionContext(plugin=PLUGIN, component_id="btn1"))
        self.assertIn("callback exploded", str(caught.exception))

    async def test_dispatch_form_submit_action(self) -> None:
        from core.dashboard_ui import Form, TextInput

        seen = []

        def submit(ctx, payload):
            seen.append(payload)
            return "submitted"

        form = (Form(component_id="frm")
                .set_submit_action(submit)
                .add_field(TextInput(component_id="name").set_label("Name")))
        layout = Layout().add_page(
            Page(component_id="page1").add_section(
                Section(component_id="sec1").add_component(form)))
        self.registry.attach(PLUGIN, layout)
        result = await self.registry.dispatch_action(
            PLUGIN, "frm", {"name": "aka"},
            ActionContext(plugin=PLUGIN, component_id="frm"))
        self.assertEqual((result.success, result.message),
                         (True, "submitted"))
        self.assertEqual(seen, [{"name": "aka"}])


# ------------------------------------------------------------- visibility
class VisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = DashboardUIRegistry()

    async def test_no_check_defaults_to_allow(self) -> None:
        self.registry.attach(PLUGIN, _page_with_button())
        ctx = CheckContext(plugin=PLUGIN, component_id="btn1",
                           component_type="button", account="alpha")
        self.assertTrue(await self.registry.check_visibility(
            PLUGIN, "btn1", ctx))

    async def test_own_check_false_hides(self) -> None:
        self.registry.attach(PLUGIN, _page_with_button(check=lambda ctx: False))
        ctx = CheckContext(plugin=PLUGIN, component_id="btn1",
                           component_type="button")
        self.assertFalse(await self.registry.check_visibility(
            PLUGIN, "btn1", ctx))

    async def test_parent_check_denies_child(self) -> None:
        self.registry.attach(PLUGIN, _page_with_button(
            section_check=lambda ctx: False))
        # the button itself has NO check — the ancestor's verdict hides it
        ctx = CheckContext(plugin=PLUGIN, component_id="btn1",
                           component_type="button")
        self.assertFalse(await self.registry.check_visibility(
            PLUGIN, "btn1", ctx))
        # the denial scopes DOWNWARD only: the page (an ANCESTOR of the
        # checked section) keeps its own verdict — no check, allowed
        page_ctx = CheckContext(plugin=PLUGIN, component_id="page1",
                                component_type="page")
        self.assertTrue(await self.registry.check_visibility(
            PLUGIN, "page1", page_ctx))

    async def test_check_exception_fails_closed(self) -> None:
        def boom(ctx):
            raise RuntimeError("check exploded")

        self.registry.attach(PLUGIN, _page_with_button(check=boom))
        ctx = CheckContext(plugin=PLUGIN, component_id="btn1",
                           component_type="button")
        self.assertFalse(await self.registry.check_visibility(
            PLUGIN, "btn1", ctx))

    async def test_async_check_supported(self) -> None:
        async def allow(ctx):
            return ctx.account == "alpha"

        self.registry.attach(PLUGIN, _page_with_button(check=allow))
        yes = CheckContext(plugin=PLUGIN, component_id="btn1",
                           component_type="button", account="alpha")
        no = CheckContext(plugin=PLUGIN, component_id="btn1",
                          component_type="button", account="beta")
        self.assertTrue(await self.registry.check_visibility(PLUGIN, "btn1", yes))
        self.assertFalse(await self.registry.check_visibility(PLUGIN, "btn1", no))

    async def test_unknown_plugin_or_component_is_false(self) -> None:
        self.registry.attach(PLUGIN, _page_with_button())
        ctx = CheckContext(plugin=PLUGIN, component_id="btn1",
                           component_type="button")
        self.assertFalse(await self.registry.check_visibility("ghost", "btn1", ctx))
        self.assertFalse(await self.registry.check_visibility(PLUGIN, "nope", ctx))

    async def test_check_receives_the_node_it_guards(self) -> None:
        seen = []

        def button_check(ctx):
            seen.append((ctx.component_id, ctx.component_type, ctx.plugin))
            return True

        def section_check(ctx):
            seen.append((ctx.component_id, ctx.component_type, ctx.plugin))
            return True

        self.registry.attach(PLUGIN, _page_with_button(
            check=button_check, section_check=section_check))
        ctx = CheckContext(plugin=PLUGIN, component_id="btn1",
                           component_type="button", account="alpha")
        self.assertTrue(await self.registry.check_visibility(PLUGIN, "btn1", ctx))
        self.assertEqual(seen, [("sec1", "section", PLUGIN),
                                ("btn1", "button", PLUGIN)])


# ------------------------------------------------------------ data providers
class DataProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = DashboardUIRegistry()

    def _layout_with_provider(self, source, check=None) -> None:
        view = ListView(component_id="list1").set_data_source(source)
        if check is not None:
            view.set_check(check)
        section = Section(component_id="sec1").add_component(view)
        self.registry.attach(
            PLUGIN,
            Layout().add_page(
                Page(component_id="page1").add_section(section)))

    async def test_get_data_returns_json_safe_result(self) -> None:
        seen = []

        def source(ctx):
            seen.append(ctx)
            return {"items": [{"name": "a", "n": 1}, {"name": "b", "n": 2}]}

        self._layout_with_provider(source)
        ctx = CheckContext(plugin=PLUGIN, component_id="list1",
                           component_type="list-view", account="alpha")
        data = await self.registry.get_data(PLUGIN, "list1", ctx)
        self.assertEqual(data, {"items": [{"name": "a", "n": 1},
                                          {"name": "b", "n": 2}]})
        # the provider got a context pinned to its own component
        (received,) = seen
        self.assertEqual((received.plugin, received.component_id,
                          received.component_type, received.account),
                         (PLUGIN, "list1", "list-view", "alpha"))

    async def test_get_data_async_provider(self) -> None:
        async def source(ctx):
            return ["x"]

        self._layout_with_provider(source)
        ctx = CheckContext(plugin=PLUGIN, component_id="list1",
                           component_type="list-view")
        self.assertEqual(await self.registry.get_data(PLUGIN, "list1", ctx), ["x"])

    async def test_get_data_hidden_denied(self) -> None:
        called = []
        self._layout_with_provider(lambda ctx: called.append(1) or [],
                                   check=lambda ctx: False)
        ctx = CheckContext(plugin=PLUGIN, component_id="list1",
                           component_type="list-view")
        with self.assertRaises(UIError):
            await self.registry.get_data(PLUGIN, "list1", ctx)
        self.assertEqual(called, [])

    async def test_get_data_unknown_or_providerless(self) -> None:
        self._layout_with_provider(lambda ctx: [])
        ctx = CheckContext(plugin=PLUGIN, component_id="list1",
                           component_type="list-view")
        with self.assertRaises(UIError):
            await self.registry.get_data("ghost", "list1", ctx)
        with self.assertRaises(UIError):
            await self.registry.get_data(PLUGIN, "nope", ctx)
        # a Text component has no provider
        layout = _page_with_button()
        layout.pages()[0].get_child("sec1").add_component(
            Text(component_id="txt"))
        self.registry.attach(PLUGIN, layout)
        txt_ctx = CheckContext(plugin=PLUGIN, component_id="txt",
                               component_type="text")
        with self.assertRaises(UIError):
            await self.registry.get_data(PLUGIN, "txt", txt_ctx)

    async def test_get_data_rejects_non_json_safe(self) -> None:
        class Opaque:
            pass

        self._layout_with_provider(lambda ctx: {"bad": Opaque()})
        ctx = CheckContext(plugin=PLUGIN, component_id="list1",
                           component_type="list-view")
        with self.assertRaises(UIError):
            await self.registry.get_data(PLUGIN, "list1", ctx)
        # non-str dict keys are not JSON-safe either
        self.registry.detach(PLUGIN)
        self._layout_with_provider(lambda ctx: {1: "int key"})
        with self.assertRaises(UIError):
            await self.registry.get_data(PLUGIN, "list1", ctx)

    async def test_get_data_bounds_list_results(self) -> None:
        self._layout_with_provider(lambda ctx: list(range(600)))
        ctx = CheckContext(plugin=PLUGIN, component_id="list1",
                           component_type="list-view")
        data = await self.registry.get_data(PLUGIN, "list1", ctx)
        self.assertEqual(len(data), MAX_DATA_ITEMS)
        self.assertEqual(data[:3], [0, 1, 2])

    async def test_get_data_provider_failure_raises_uierror(self) -> None:
        def source(ctx):
            raise RuntimeError("provider exploded")

        self._layout_with_provider(source)
        ctx = CheckContext(plugin=PLUGIN, component_id="list1",
                           component_type="list-view")
        with self.assertRaises(UIError) as caught:
            await self.registry.get_data(PLUGIN, "list1", ctx)
        self.assertIn("provider exploded", str(caught.exception))


# ------------------------------------------------- plugin scope (manager side)
def _plugin_src(body: str) -> str:
    return (
        "from core.plugin_base import PluginBase\n"
        "\n"
        "class P(PluginBase):\n"
        + "\n".join(f"    {line}" for line in body.splitlines())
        + "\n"
    )


class PluginScopeTests(unittest.IsolatedAsyncioTestCase):
    """Registry x PluginManager: plugin-wide dedup, ref-counted detach,
    isolated init failures (task items 16-18)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-ui-reg-")
        self.addCleanup(self.tmp.cleanup)
        self.plugin_dir = Path(self.tmp.name)
        self.registry = DashboardUIRegistry()

    def _write(self, name: str, body: str) -> None:
        (self.plugin_dir / f"{name}.py").write_text(_plugin_src(body))

    def _manager(self) -> PluginManager:
        bus = EventBus()
        return PluginManager(
            bus, LOG, SettingsManager(bus), plugin_dir=str(self.plugin_dir),
            ui_registry=self.registry)

    @staticmethod
    def _services(account: str) -> _FakeServices:
        return _FakeServices()

    def _write_withui(self) -> None:
        self._write("withui", "\n".join([
            "name = 'withui'",
            "version = '1.0.0'",
            "def __init__(self):",
            "    self.init_calls = []",
            "def initialize_dashboard_ui(self, ui):",
            "    self.init_calls.append(type(ui).__name__)",
            "    button = ui.Button(component_id='btn').set_label('Go')",
            "    button.set_action(lambda ctx, payload: 'clicked')",
            "    page = ui.Page(component_id='page')",
            "    page.add_section(ui.Section(component_id='sec')",
            "                     .add_component(button))",
            "    self.attach_dashboard_ui(ui.Layout().add_page(page))",
        ]))

    async def test_same_plugin_two_accounts_one_registration(self) -> None:
        self._write_withui()
        manager = self._manager()
        await manager.attach_account("alpha", {"withui"})
        await manager.attach_account("beta", {"withui"})
        await manager.activate_account("alpha", self._services("alpha"))
        await manager.activate_account("beta", self._services("beta"))

        self.assertEqual(self.registry.list_plugins(), ["withui"])
        alpha_calls = manager._accounts["alpha"]["withui"].instance.init_calls
        beta_calls = manager._accounts["beta"]["withui"].instance.init_calls
        self.assertEqual(alpha_calls, ["DashboardUIToolset"])
        self.assertEqual(beta_calls, [])  # dedup: second account skips

    async def test_detach_kept_while_another_account_is_active(self) -> None:
        self._write_withui()
        manager = self._manager()
        await manager.attach_account("alpha", {"withui"})
        await manager.attach_account("beta", {"withui"})
        await manager.activate_account("alpha", self._services("alpha"))
        await manager.activate_account("beta", self._services("beta"))

        await manager.deactivate_account("alpha")
        self.assertEqual(self.registry.list_plugins(), ["withui"])
        # the registered actions still dispatch (beta owns them now)
        result = await self.registry.dispatch_action(
            "withui", "btn", {},
            ActionContext(plugin="withui", component_id="btn",
                          account="beta"))
        self.assertEqual(result.message, "clicked")

        await manager.deactivate_account("beta")
        self.assertEqual(self.registry.list_plugins(), [])  # last one out

    async def test_ui_init_failure_isolated_and_partial_registration_cleaned(self) -> None:
        self._write_withui()
        # crashes BEFORE attaching anything
        self._write("uiboom", "\n".join([
            "name = 'uiboom'",
            "version = '1.0.0'",
            "def initialize_dashboard_ui(self, ui):",
            "    raise RuntimeError('ui init explodes')",
        ]))
        # attaches a VALID layout, then crashes — partial registration
        # must be cleaned up by the isolation handler
        self._write("uipartial", "\n".join([
            "name = 'uipartial'",
            "version = '1.0.0'",
            "def initialize_dashboard_ui(self, ui):",
            "    page = ui.Page(component_id='page')",
            "    page.add_section(ui.Section(component_id='sec'))",
            "    self.attach_dashboard_ui(ui.Layout().add_page(page))",
            "    raise RuntimeError('boom after attach')",
        ]))
        manager = self._manager()
        await manager.attach_account(
            "alpha", {"withui", "uiboom", "uipartial"})
        await manager.activate_account("alpha", self._services("alpha"))

        # activation continued for every plugin
        for name in ("withui", "uiboom", "uipartial"):
            runtime = manager._accounts["alpha"][name]
            self.assertTrue(runtime.active, name)
            self.assertTrue(runtime.enabled, name)
        # the healthy UI registered; the broken ones left nothing behind
        self.assertEqual(self.registry.list_plugins(), ["withui"])
        # failures were recorded on the runtimes (visible in states())
        boom = manager._accounts["alpha"]["uiboom"]
        self.assertEqual(boom.error_count, 1)
        self.assertIn("ui_init", boom.last_error)
        self.assertIn("ui init explodes", boom.last_error)
        partial = manager._accounts["alpha"]["uipartial"]
        self.assertEqual(partial.error_count, 1)


if __name__ == "__main__":
    unittest.main()


class ResultNormalizationEdgeCases(unittest.TestCase):
    """Bare result-shaped dicts must keep their keys (regression).

    A callback returning {"refresh": [...]} with no success/message used
    to be coerced to a DATA payload — swallowing the refresh list, so the
    frontend re-fetched nothing and refresh buttons looked dead.
    """

    def test_bare_refresh_dict_keeps_refresh(self):
        from core.dashboard_ui.registry import DashboardUIRegistry
        result = DashboardUIRegistry._normalize_result(
            {"refresh": ["tbl"]}, "p", "btn")
        self.assertEqual(result.refresh, ["tbl"])
        self.assertTrue(result.success)
        self.assertIsNone(result.data)

    def test_bare_field_errors_dict_parses(self):
        from core.dashboard_ui.registry import DashboardUIRegistry
        result = DashboardUIRegistry._normalize_result(
            {"field_errors": {"f": "bad"}}, "p", "btn")
        self.assertEqual(result.field_errors, {"f": "bad"})
        self.assertTrue(result.success)

    def test_unrelated_dict_still_becomes_data(self):
        from core.dashboard_ui.registry import DashboardUIRegistry
        result = DashboardUIRegistry._normalize_result(
            {"rows": [1, 2]}, "p", "btn")
        self.assertEqual(result.data, {"rows": [1, 2]})
        self.assertEqual(result.refresh, [])
