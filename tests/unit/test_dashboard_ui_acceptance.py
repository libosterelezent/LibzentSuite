"""Stage-E final acceptance: the COMPLETE dashboard UI path.

Three layers prove the feature end to end:

* ``TestCompleteUIPath`` — builder → layout → attach → serialize →
  navigation → page → action dispatch → result, plus the auxiliary
  contracts the spec pins: a denying visibility check blocks dispatch,
  form submissions surface ``field_errors``, data providers return
  rows, and a disabled button is UI STATE (the action still
  dispatches server-side — disabled is not authorization).
* ``TestRenderedVisibilityPruning`` — visibility applies to the
  RENDERED page tree as well: denied subtrees are pruned before the
  serialized page crosses the wire (own check or any ancestor's,
  fail-closed, checks memoized per fetch).
* ``TestConfigDemoExample`` — the working example plugin
  (``tests/fixtures/plugins/config_demo/``) through the REAL plugin
  lifecycle: discovery → activation → ``initialize_dashboard_ui`` →
  registry attachment → serialization → dispatch → data providers →
  visibility check → deactivation cleanup.
"""
from __future__ import annotations

import logging
import unittest
from pathlib import Path

from core.dashboard_ui import (
    ActionContext,
    ActionResult,
    CheckContext,
    DashboardUIRegistry,
    DashboardUIToolset,
    UIError,
)
from core.event_bus import EventBus
from core.plugin_manager import PluginManager
from core.settings_manager import SettingsManager

LOG = logging.getLogger("t")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"


class FakeServices:
    """Minimal PluginServices stand-in for activation (same shape the
    lifecycle/http suites use)."""

    def __init__(self, account="alpha") -> None:
        self.account = account

    async def send_message(self, channel_id, content, *, priority=1):
        return None

    async def send_dm(self, user_id, content, *, priority=1):
        return None

    @property
    def settings(self):
        return {}


def _find_comp(tree: dict, type_name: str) -> dict | None:
    """First component of `type_name` in the serialized tree (BFS)."""
    queue = [tree]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            if node.get("type") == type_name:
                return node
            queue.extend(node.get("children") or [])
    return None


def _all_of_type(tree: dict, type_name: str) -> list:
    """Every component of `type_name` in the serialized tree (BFS)."""
    found, queue = [], [tree]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            if node.get("type") == type_name:
                found.append(node)
            queue.extend(node.get("children") or [])
    return found


class TestCompleteUIPath(unittest.IsolatedAsyncioTestCase):
    """Final acceptance: builder → layout → attach → serialize →
    navigation → page → action dispatch → result."""

    async def test_full_path(self):
        # 1. Build a layout with the toolset
        ui = DashboardUIToolset()
        layout = ui.Layout()
        page = ui.Page().set_id("test").set_title("Test Page")
        section = ui.Section().set_title("Controls")
        card = ui.Card().set_title("Actions")

        button = ui.Button().set_label("Execute").set_action(self._execute)
        card.add_component(button)

        metric = ui.Metric().set_label("Status").set_value("idle")
        card.add_component(metric)

        section.add_component(card)
        page.add_section(section)
        layout.add_page(page)

        # 2. Attach to a registry
        registry = DashboardUIRegistry()
        snapshot = registry.attach("demo", layout)
        self.assertIn("children", snapshot)

        # 3. Verify navigation data
        nav_page = snapshot["children"][0]
        self.assertEqual(nav_page["title"], "Test Page")

        # 4. Verify the component tree
        section_data = nav_page["children"][0]
        card_data = section_data["children"][0]
        button_data = [c for c in card_data["children"]
                       if c["type"] == "button"][0]
        self.assertEqual(button_data["label"], "Execute")
        self.assertTrue(button_data.get("action"))  # action is registered

        # 5. Dispatch an action
        context = ActionContext(plugin="demo", component_id=button.get_id())
        result = await registry.dispatch_action(
            "demo", button.get_id(), {"value": True}, context)
        self.assertTrue(result.success)
        self.assertEqual(result.message, "executed")

        # 6. Verify visibility (no check declared → default allow)
        visible = await registry.check_visibility(
            "demo", button.get_id(),
            CheckContext(plugin="demo", component_id=button.get_id(),
                         component_type="button"))
        self.assertTrue(visible)

        # 7. Detach and verify cleanup
        registry.detach("demo")
        self.assertEqual(registry.list_plugins(), [])

    async def _execute(self, context, payload):
        return ActionResult(success=True, message="executed")

    # ---------------------------------------------------------- denial
    async def test_denying_visibility_check_blocks_everything(self):
        """A check returning False hides the component AND fails every
        server-side request for it (fail-closed, not cosmetic)."""
        ui = DashboardUIToolset()
        button = (ui.Button(component_id="deny-btn")
                  .set_label("Nope")
                  .set_action(self._execute)
                  .set_check(lambda ctx: False))
        page = (ui.Page(component_id="p")
                .add_section(ui.Section().add_component(button)))
        registry = DashboardUIRegistry()
        registry.attach("gated", ui.Layout().add_page(page))

        ctx = CheckContext(plugin="gated", component_id="deny-btn",
                           component_type="button")
        self.assertFalse(await registry.check_visibility("gated", "deny-btn", ctx))
        with self.assertRaises(UIError):
            await registry.dispatch_action(
                "gated", "deny-btn", {},
                ActionContext(plugin="gated", component_id="deny-btn"))
        # a RAISING check fails closed too (the error is swallowed)
        boom = (ui.Button(component_id="boom-btn")
                .set_label("Boom")
                .set_action(self._execute)
                .set_check(lambda ctx: 1 / 0))
        registry.attach("crashy", ui.Layout().add_page(
            ui.Page(component_id="p2")
            .add_section(ui.Section().add_component(boom))))
        self.assertFalse(await registry.check_visibility(
            "crashy", "boom-btn",
            CheckContext(plugin="crashy", component_id="boom-btn",
                         component_type="button")))

    # ------------------------------------------------------------ forms
    async def test_form_submission_with_field_errors(self):
        """A form's submit action reports per-field validation errors;
        field_errors keys are the FORM FIELD component ids."""
        ui = DashboardUIToolset()

        async def _submit(context, payload):
            errors = {}
            if not str(payload.get("demo-name") or "").strip():
                errors["demo-name"] = "name is required"
            return (ActionResult(success=False, message="fix the fields",
                                 field_errors=errors)
                    if errors else ActionResult(success=True, message="saved"))

        form = (ui.Form(component_id="demo-form")
                .set_submit_label("Save")
                .set_submit_action(_submit))
        form.add_field(ui.TextInput(component_id="demo-name").set_required(True))
        form.add_field(ui.NumberInput(component_id="demo-count")
                       .set_min_value(0))

        registry = DashboardUIRegistry()
        registry.attach("forms", ui.Layout().add_page(
            ui.Page(component_id="fp").add_section(
                ui.Section().add_component(form))))

        # empty required field → structured field_errors, keyed by field id
        result = await registry.dispatch_action(
            "forms", "demo-form",
            {"demo-name": "", "demo-count": 3},
            ActionContext(plugin="forms", component_id="demo-form"))
        self.assertFalse(result.success)
        self.assertEqual(result.message, "fix the fields")
        self.assertEqual(result.field_errors,
                         {"demo-name": "name is required"})
        # a valid submission clears them
        ok = await registry.dispatch_action(
            "forms", "demo-form", {"demo-name": "abc", "demo-count": 3},
            ActionContext(plugin="forms", component_id="demo-form"))
        self.assertTrue(ok.success)
        self.assertEqual(ok.field_errors, {})

    # ------------------------------------------------------ data provider
    async def test_data_provider_returns_rows(self):
        """set_data_source(callable) feeds tables/list-views; the
        registry returns the provider's JSON-safe rows verbatim."""
        rows = [{"kind": "claim", "count": 3},
                {"kind": "roll", "count": 12}]

        def provider(ctx: CheckContext) -> list:
            # providers receive a CheckContext pinned to the component
            self.assertEqual(ctx.component_id, "rows-table")
            return rows

        ui = DashboardUIToolset()
        table = (ui.Table(component_id="rows-table")
                 .set_columns(["kind", "count"])
                 .set_data_source(provider))
        registry = DashboardUIRegistry()
        registry.attach("datademo", ui.Layout().add_page(
            ui.Page(component_id="dp").add_section(
                ui.Section().add_component(table))))

        data = await registry.get_data(
            "datademo", "rows-table",
            CheckContext(plugin="datademo", component_id="rows-table",
                         component_type="table"))
        self.assertEqual(data, rows)

    # --------------------------------------------------- disabled button
    async def test_disabled_button_is_ui_state_only(self):
        """set_disabled(True) renders the control inert in the frontend
        but is NOT authorization: the server still dispatches."""
        ui = DashboardUIToolset()
        button = (ui.Button(component_id="locked")
                  .set_label("Locked")
                  .set_disabled(True)
                  .set_action(self._execute))
        registry = DashboardUIRegistry()
        snapshot = registry.attach("locked", ui.Layout().add_page(
            ui.Page(component_id="lp").add_section(
                ui.Section().add_component(button))))

        serialized = _find_comp(snapshot, "button")
        self.assertTrue(serialized.get("disabled"))
        # still visible (no check) and still dispatchable
        self.assertTrue(await registry.check_visibility(
            "locked", "locked",
            CheckContext(plugin="locked", component_id="locked",
                         component_type="button")))
        result = await registry.dispatch_action(
            "locked", "locked", {},
            ActionContext(plugin="locked", component_id="locked"))
        self.assertTrue(result.success)
        self.assertEqual(result.message, "executed")


class TestRenderedVisibilityPruning(unittest.IsolatedAsyncioTestCase):
    """Criterion 8, rendered half: visibility applies to the SERIALIZED
    page tree too — denied subtrees never cross the wire, matching the
    server-side verdicts dispatch/data already enforce."""

    def _registry_with(self, button_check=None, section_check=None,
                       page_check=None):
        ui = DashboardUIToolset()
        button = (ui.Button(component_id="btn")
                  .set_label("Go")
                  .set_action(self._noop))
        if button_check is not None:
            button.set_check(button_check)
        section = ui.Section(component_id="sec")
        if section_check is not None:
            section.set_check(section_check)
        section.add_component(button)
        page = ui.Page(component_id="pg")
        if page_check is not None:
            page.set_check(page_check)
        page.add_section(section)
        registry = DashboardUIRegistry()
        registry.attach("prune", ui.Layout().add_page(page))
        return registry

    @staticmethod
    async def _noop(context, payload):
        return ActionResult(success=True)

    async def _page(self, registry) -> dict | None:
        from core.api import ControlAPI
        api = ControlAPI(accounts=object(),
                         plugins=type("P", (), {
                             "ui_registry": registry})(),
                         profiles=object(), settings=object(),
                         limiter=object(), scheduler=object(), logs=object())
        return await api.dashboard_ui_page("prune", "pg")

    async def test_no_checks_keep_the_whole_tree(self):
        registry = self._registry_with()
        page = await self._page(registry)
        button = _find_comp(page, "button")
        self.assertIsNotNone(button)

    async def test_denied_button_pruned_from_rendered_tree(self):
        registry = self._registry_with(button_check=lambda ctx: False)
        page = await self._page(registry)
        self.assertIsNone(_find_comp(page, "button"))
        self.assertIsNotNone(_find_comp(page, "section"))

    async def test_denied_ancestor_prunes_its_whole_subtree(self):
        registry = self._registry_with(section_check=lambda ctx: False)
        page = await self._page(registry)
        self.assertIsNone(_find_comp(page, "section"))
        self.assertIsNone(_find_comp(page, "button"))

    async def test_denied_page_yields_nothing(self):
        registry = self._registry_with(page_check=lambda ctx: False)
        self.assertIsNone(await self._page(registry))

    async def test_raising_check_prunes_fail_closed(self):
        def boom(ctx):
            raise RuntimeError("check exploded")
        registry = self._registry_with(button_check=boom)
        page = await self._page(registry)
        self.assertIsNone(_find_comp(page, "button"))

    async def test_async_checks_and_memoization(self):
        calls = []

        async def allow(ctx):
            calls.append(ctx.component_id)
            return True

        registry = self._registry_with(section_check=allow)
        page = await self._page(registry)
        self.assertIsNotNone(_find_comp(page, "button"))
        # the shared cache evaluates each check at most once per fetch
        self.assertEqual(calls, ["sec"])

    async def test_snapshot_untouched_by_pruning(self):
        registry = self._registry_with(button_check=lambda ctx: False)
        stored = registry.get_layout("prune")
        await self._page(registry)          # prunes its own copy only
        # the stored snapshot (and every later copy) still has the button
        self.assertIsNotNone(_find_comp(stored, "button"))
        self.assertIsNotNone(
            _find_comp(registry.get_layout("prune"), "button"))


class TestConfigDemoExample(unittest.IsolatedAsyncioTestCase):
    """The working example proves the complete path through the REAL
    plugin lifecycle (discovery → activation → initialize_dashboard_ui
    → attach → serialize → dispatch → data → deactivate → cleanup)."""

    async def asyncSetUp(self) -> None:
        self.registry = DashboardUIRegistry()
        bus = EventBus()
        self.manager = PluginManager(
            bus, LOG, SettingsManager(bus),
            plugin_dir=str(FIXTURES), ui_registry=self.registry)
        await self.manager.attach_account("alpha", {"config_demo"})
        await self.manager.activate_account("alpha", FakeServices("alpha"))

    async def asyncTearDown(self) -> None:
        await self.manager.deactivate_account("alpha")

    # ------------------------------------------------------------ layout
    async def test_example_registers_one_page_with_four_tabs(self):
        self.assertEqual(self.registry.list_plugins(), ["config_demo"])
        page = self.registry.get_layout("config_demo")["children"][0]
        self.assertEqual((page["type"], page["id"]), ("page", "config-demo"))
        self.assertEqual(page["title"], "Config Demo")
        tabs = [c for c in page["children"] if c["type"] == "tab"]
        self.assertEqual([t["title"] for t in tabs],
                         ["General", "Advanced", "Data", "Layout"])
        # every input type the API supports is on the page
        for type_name in ("checkbox", "toggle", "text-input",
                          "number-input", "textarea", "select",
                          "radio-group", "multi-select"):
            self.assertIsNotNone(
                _find_comp(page, type_name), f"missing {type_name}")
        # layout containers, data parts and display parts too
        for type_name in ("row", "column", "container", "table",
                          "list-view", "metric", "progress", "badge",
                          "text", "display-text", "form", "button"):
            self.assertIsNotNone(
                _find_comp(page, type_name), f"missing {type_name}")
        # input constraints serialize (bounds + explicit options)
        timeout = _find_comp(page, "number-input")
        self.assertEqual((timeout["min_value"], timeout["max_value"]), (5, 300))
        mode = _find_comp(page, "select")
        self.assertEqual([o["value"] for o in mode["options"]],
                         ["fast", "normal", "slow"])
        # the alignment vocabulary serializes (grow ratio row + justify)
        ratio_row = next(r for r in _all_of_type(page, "row")
                         if r["id"] == "ratio-row")
        shares = [c.get("grow") for c in ratio_row["children"]]
        self.assertEqual(shares, [1, 2, 1])
        justify_row = next(r for r in _all_of_type(page, "row")
                           if r["id"] == "justify-row")
        self.assertEqual(justify_row.get("justify"), "between")
        self.assertEqual(justify_row.get("align"), "center")
        split_value = next(d for d in _all_of_type(page, "display-text")
                           if d["id"] == "split-value")
        self.assertTrue(split_value.get("monospace"))
        self.assertEqual(split_value.get("width"), "120px")
        # the confirmation + disabled states are part of the snapshot
        purge = next(b for b in _all_of_type(page, "button")
                     if b["id"] == "purge-button")
        self.assertEqual(purge["confirm"], "Are you sure?")
        locked = next(b for b in _all_of_type(page, "button")
                      if b["id"] == "export-button")
        self.assertTrue(locked.get("disabled"))

    async def test_example_checkbox_toggle_dispatch(self):
        result = await self.registry.dispatch_action(
            "config_demo", "feature-enabled", {"value": True},
            ActionContext(plugin="config_demo",
                          component_id="feature-enabled", account="alpha"))
        self.assertTrue(result.success)
        self.assertIn("enabled", result.message)
        self.assertIn("alpha", result.message)   # account context flows
        self.assertEqual(result.refresh, ["recent-events", "status-lines"])

    async def test_example_form_validation_then_acceptance(self):
        # empty required field → structured field_errors by field id
        invalid = await self.registry.dispatch_action(
            "config_demo", "rule-form",
            {"rule-name": "", "rule-threshold": 50, "rule-note": ""},
            ActionContext(plugin="config_demo", component_id="rule-form",
                          account="alpha"))
        self.assertFalse(invalid.success)
        self.assertIn("rule-name", invalid.field_errors)
        self.assertIn("required", invalid.field_errors["rule-name"])
        # an out-of-bounds threshold reports on its own field
        bounds = await self.registry.dispatch_action(
            "config_demo", "rule-form",
            {"rule-name": "ok", "rule-threshold": 250, "rule-note": ""},
            ActionContext(plugin="config_demo", component_id="rule-form"))
        self.assertFalse(bounds.success)
        self.assertIn("rule-threshold", bounds.field_errors)
        # a valid submission succeeds and is visible through the provider
        ok = await self.registry.dispatch_action(
            "config_demo", "rule-form",
            {"rule-name": "high-claim-rate", "rule-threshold": 75,
             "rule-note": "watch it"},
            ActionContext(plugin="config_demo", component_id="rule-form"))
        self.assertTrue(ok.success)
        self.assertIn("high-claim-rate", ok.message)
        self.assertEqual(ok.data, {"rules": 1})

    async def test_example_data_providers_return_rows(self):
        # seed one dispatched action so the table has a row
        await self.registry.dispatch_action(
            "config_demo", "feature-enabled", {"value": True},
            ActionContext(plugin="config_demo",
                          component_id="feature-enabled", account="alpha"))
        rows = await self.registry.get_data(
            "config_demo", "recent-events",
            CheckContext(plugin="config_demo", component_id="recent-events",
                         component_type="table", account="alpha"))
        self.assertIsInstance(rows, list)
        self.assertTrue(rows)
        self.assertEqual(sorted(rows[0]),
                         ["account", "count", "detail", "kind"])
        # the list-view provider returns {label, value} rows
        lines = await self.registry.get_data(
            "config_demo", "status-lines",
            CheckContext(plugin="config_demo", component_id="status-lines",
                         component_type="list-view"))
        self.assertEqual([l["label"] for l in lines],
                         ["feature", "verbose logging", "rules", "event rows"])

    async def test_example_visibility_check_on_advanced_tab(self):
        """The Advanced tab declares set_check() — True here, but the
        check is evaluated server-side for the whole subtree."""
        self.assertTrue(await self.registry.check_visibility(
            "config_demo", "tab-advanced",
            CheckContext(plugin="config_demo", component_id="tab-advanced",
                         component_type="tab", account="alpha")))
        # components under the tab inherit its check (ancestor walk)
        self.assertTrue(await self.registry.check_visibility(
            "config_demo", "rule-form",
            CheckContext(plugin="config_demo", component_id="rule-form",
                         component_type="form")))

    async def test_example_confirmation_and_disabled_buttons(self):
        purge = await self.registry.dispatch_action(
            "config_demo", "purge-button", {},
            ActionContext(plugin="config_demo", component_id="purge-button"))
        self.assertTrue(purge.success)
        self.assertIn("purged", purge.message)
        # disabled is UI state only — the server still dispatches
        locked = await self.registry.dispatch_action(
            "config_demo", "export-button", {},
            ActionContext(plugin="config_demo", component_id="export-button"))
        self.assertTrue(locked.success)
        self.assertIn("UI hint", locked.message)

    async def test_example_cleanup_on_deactivation(self):
        self.assertEqual(self.registry.list_plugins(), ["config_demo"])
        await self.manager.deactivate_account("alpha")
        self.assertEqual(self.registry.list_plugins(), [])
        self.assertIsNone(self.registry.get_layout("config_demo"))
        with self.assertRaises(UIError):
            await self.registry.dispatch_action(
                "config_demo", "feature-enabled", {"value": True},
                ActionContext(plugin="config_demo",
                              component_id="feature-enabled"))


if __name__ == "__main__":
    unittest.main()
