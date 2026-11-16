"""Config Demo plugin — the Dashboard UI API reference example.

Demonstrates: builder-based layout, tabs, cards, multiple input types
(checkbox, text, number, select), a form with field validation, a
data-backed table, a progress metric, action dispatch with results,
and a visibility check. Every pattern a plugin author needs.

The page layout:

* Tab "General"  — the eight input field types across two cards
  (checkbox with an action, text, number with bounds, select with
  labeled options, toggle, radio group, multi-select, textarea);
* Tab "Advanced" — carries a ``set_check`` visibility check, a form
  whose submit action validates fields and returns ``field_errors``,
  a confirmation-gated button, a disabled button (UI state only) and
  a status badge;
* Tab "Data"     — a table fed by a data provider (list of row
  dicts), a live metric + progress bar in a row/column layout and a
  list-view fed by a second provider.

Every pattern here is exercised end-to-end by
``tests/unit/test_dashboard_ui_acceptance.py`` through the REAL
plugin lifecycle (discovery → activation → initialize_dashboard_ui →
attach → dispatch → deactivate).

SCOPE NOTE (plugin-wide UI): the dashboard UI registers ONCE per
plugin (the first account to activate it wins), so the callbacks are
bound to that first instance while ``context.account`` names the
account the dashboard had selected when the action fired. Keep the
state actions mutate small and account-agnostic, or key it off
``context.account`` like the event log below does.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List

from core.dashboard_ui import (
    ActionContext,
    ActionResult,
    CheckContext,
)
from core.event_bus import Event
from core.plugin_base import PluginBase, PluginContext
from core.settings_manager import Setting

if TYPE_CHECKING:
    from core.dashboard_ui import DashboardUIToolset


class ConfigDemoPlugin(PluginBase):
    """A realistic configuration page built with the Dashboard UI API."""

    name = "config_demo"
    version = "1.0.0"
    description = "dashboard UI API reference example"

    settings = (
        Setting("page_title", "str", "Config Demo",
                "title of the dashboard page"),
        Setting("max_rules", "int", 10, "threshold-rule capacity",
                min_value=1, max_value=50),
    )

    def __init__(self) -> None:
        #: the live PluginContext of the current activation
        self._ctx: PluginContext | None = None
        #: demo state mutated by UI actions (see the module docstring's
        #: scope note: one instance owns the UI plugin-wide)
        self._feature_enabled = False
        self._verbose = False
        self._rules: List[dict] = []
        self._events: List[dict] = []
        self._action_count = 0

    # ------------------------------------------------------------ lifecycle
    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        ctx.logger.info("config_demo v%s loaded", self.version)

    async def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def on_disable(self, ctx: PluginContext) -> None:
        pass

    async def on_unload(self, ctx: PluginContext) -> None:
        self._ctx = None

    # ---------------------------------------------------------------- events
    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        return  # the demo reacts to nothing — the dashboard is its surface

    # -------------------------------------------------------- dashboard UI
    def initialize_dashboard_ui(self, ui: "DashboardUIToolset") -> None:
        """Build and attach the reference layout (SYNC by design —
        attachment is pure in-memory registration)."""
        page = (ui.Page()
                .set_id("config-demo")            # explicit id: navigation
                .set_title(self._page_title())    # targeted by ?page_id=
                .set_description(
                    "The Dashboard UI API reference example — every "
                    "builder pattern on one page.")
                .set_order(10))

        # ── Tab "General": the eight input types ──────────────────────
        basic = ui.Card().set_title("Basic")
        basic.add_component(
            # checkbox/toggle actions receive {"value": bool} payloads
            ui.Checkbox(component_id="feature-enabled")
            .set_label("Enable the demo feature")
            .set_description("dispatched server-side as {value: bool}")
            .set_value(self._feature_enabled)
            .set_action(self._on_toggle_feature))
        basic.add_component(
            ui.TextInput(component_id="display-name")
            .set_label("Display name")
            .set_placeholder("e.g. grind-bot")
            .set_max_length(32)
            .set_required(True))
        basic.add_component(
            # number values are strings (the field's initial text);
            # min/max/step are the numeric bounds
            ui.NumberInput(component_id="timeout-seconds")
            .set_label("Timeout (seconds)")
            .set_value("30")
            .set_min_value(5)
            .set_max_value(300)
            .set_step(5))
        basic.add_component(
            ui.Select(component_id="mode")
            .set_label("Mode")
            .set_options([{"value": "fast", "label": "Fast (risky)"},
                          {"value": "normal", "label": "Normal"},
                          {"value": "slow", "label": "Slow (safe)"}])
            .set_value("normal"))

        notifications = ui.Card().set_title("Notifications")
        notifications.add_component(
            ui.Toggle(component_id="verbose-logging")
            .set_label("Verbose logging")
            .set_value(self._verbose)
            .set_action(self._on_toggle_verbose))
        notifications.add_component(
            ui.RadioGroup(component_id="log-level")
            .set_label("Log level")
            .set_options(["debug", "info", "warning"])
            .set_value("info"))
        notifications.add_component(
            ui.MultiSelect(component_id="watched-events")
            .set_label("Watched events")
            .set_options(["claims", "rolls", "reactions"])
            .set_value(["claims"]))
        notifications.add_component(
            ui.Textarea(component_id="notes")
            .set_label("Free-form notes")
            .set_placeholder("anything worth remembering")
            .set_rows(3))

        general_section = (ui.Section()
                           .set_title("Core settings")
                           .set_description(
                               "Plain inputs — no actions required; add "
                               "one with set_action() to persist on change."))
        general_section.add_component(basic)
        general_section.add_component(notifications)

        # ── Tab "Advanced": form + confirmation + disabled + badge ────
        # set_check() guards the WHOLE tab: the renderer omits it AND
        # every server-side request below it fails closed when denied.
        advanced_tab = (ui.Tab()
                        .set_id("tab-advanced")
                        .set_title("Advanced")
                        .set_check(self._advanced_visible))

        operations = ui.Card().set_title("Operations")
        rule_form = (ui.Form(component_id="rule-form")
                     .set_title("Add a threshold rule")
                     .set_submit_label("Add rule")
                     .set_submit_action(self._on_submit_rule))
        rule_form.add_field(
            ui.TextInput(component_id="rule-name")
            .set_label("Rule name")
            .set_placeholder("e.g. high-claim-rate")
            .set_required(True))
        rule_form.add_field(
            ui.NumberInput(component_id="rule-threshold")
            .set_label("Threshold")
            .set_value("50")
            .set_min_value(0)
            .set_max_value(100))
        rule_form.add_field(
            ui.TextInput(component_id="rule-note")
            .set_label("Note (optional)"))
        operations.add_component(rule_form)

        operations.add_component(
            # set_confirm() makes the frontend ask before dispatching
            ui.Button(component_id="purge-button")
            .set_label("Purge collected data")
            .set_variant("danger")
            .set_confirm("Are you sure?")
            .set_action(self._on_purge))
        operations.add_component(
            # set_disabled() is UI STATE, not authorization: the frontend
            # renders the control inert, but the server still dispatches
            # if asked (the acceptance test pins this)
            ui.Button(component_id="export-button")
            .set_label("Export (read-only demo)")
            .set_description("disabled controls are a UI hint only")
            .set_disabled(True)
            .set_action(self._on_export))
        operations.add_component(
            ui.Badge(component_id="status-badge")
            .set_value("active" if self._feature_enabled else "idle")
            .set_variant("ok" if self._feature_enabled else "info"))

        advanced_section = (ui.Section()
                            .set_title("Danger zone")
                            .set_description(
                                "Forms submit every field as {field_id: "
                                "value}; the action answers with "
                                "field_errors keyed the same way."))
        advanced_section.add_component(operations)
        advanced_tab.add_section(advanced_section)

        # ── Tab "Data": providers, metric, progress, layout rows ──────
        data_section = ui.Section().set_title("Live data")
        data_section.add_component(
            # the provider runs server-side on GET /api/dashboard-ui/data;
            # it must return JSON-safe values (rows are dicts of str/int)
            ui.Table(component_id="recent-events")
            .set_columns(["kind", "detail", "account", "count"])
            .set_data_source(self._recent_events)
            .set_empty_text("no actions dispatched yet")
            .set_page_size(8))

        overview = ui.Card().set_title("Overview")
        row = ui.Row()                       # Row: horizontal, no nesting
        left, right = ui.Column(), ui.Column()
        left.add_component(
            # Metric/Progress values are a snapshot taken when the layout
            # (re-)attaches; for continuously live values use a data
            # provider (Table/ListView) and refresh targets instead
            ui.Metric(component_id="action-count")
            .set_label("Dispatched actions")
            .set_value(str(self._action_count))
            .set_sub("since last (re)registration")
            .set_level("ok"))
        right.add_component(
            ui.Progress(component_id="rule-capacity")
            .set_label("Rule capacity used")
            .set_value(min(100.0, len(self._rules) * 100.0 / self._max_rules())))
        row.add_component(left)
        row.add_component(right)
        overview.add_component(row)
        data_section.add_component(overview)

        data_section.add_component(
            ui.ListView(component_id="status-lines")
            .set_data_source(self._status_lines)
            .set_empty_text("nothing to report"))
        data_section.add_component(
            ui.Text().set_value(
                "Table and list-view bodies are fetched lazily from the "
                "plugin's data providers; actions name refresh targets "
                "by component id in ActionResult.refresh.")
            .set_variant("muted"))

        # ── assemble: sections into tabs, tabs into the page ──────────
        general_tab = (ui.Tab().set_id("tab-general").set_title("General")
                       .add_section(general_section))
        data_tab = (ui.Tab().set_id("tab-data").set_title("Data")
                    .add_section(data_section))

        # ── Tab "Layout": containers, DisplayText, side-by-side ───────
        layout_section = ui.Section().set_id("layout-section").set_title("Layout")

        # side-by-side with grow ratios: 1 : 2 : 1 splits the row
        layout_section.add_component(
            ui.Row().set_id("ratio-row")
            .add_component(
                ui.Container().set_id("ratio-left").set_grow(1)
                .add_component(ui.DisplayText().set_id("rt-left")
                               .set_value("grow 1").set_tone("dim")
                               .set_align("center")))
            .add_component(
                ui.Container().set_id("ratio-main").set_grow(2)
                .add_component(ui.DisplayText().set_id("rt-main")
                               .set_value("grow 2 — double share")
                               .set_size("lg").set_weight("semibold")
                               .set_align("center")))
            .add_component(
                ui.Container().set_id("ratio-right").set_grow(1)
                .add_component(ui.DisplayText().set_id("rt-right")
                               .set_value("grow 1").set_tone("dim")
                               .set_align("center"))))

        # justification: push the ends apart on the main axis
        layout_section.add_component(
            ui.Row().set_id("justify-row").set_justify("between").set_align("center")
            .add_component(ui.DisplayText().set_id("j-left").set_value("start"))
            .add_component(ui.DisplayText().set_id("j-mid")
                           .set_value("justify: between").set_tone("dim")
                           .set_size("sm"))
            .add_component(ui.Button().set_id("j-end").set_label("end")
                           .set_variant("ghost").set_align_self("center")))

        # fixed + flowing: a fixed-width value beside a growing container
        layout_section.add_component(
            ui.Container().set_id("split-box").set_direction("horizontal").set_gap("12px")
            .add_component(ui.DisplayText().set_id("split-value")
                           .set_value("99.2%").set_size("xl").set_weight("bold")
                           .set_tone("ok").set_monospace(True).set_width("120px"))
            .add_component(ui.Container().set_id("split-desc").set_grow(1)
                           .add_component(ui.DisplayText().set_id("split-note")
                                          .set_value(
                                              "fixed 120px value, this side "
                                              "takes the rest (grow 1)")
                                          .set_tone("dim").set_size("sm"))))

        # toolbar: title left, actions right (refresh no longer floats)
        layout_section.add_component(
            ui.Toolbar().set_id("demo-toolbar").set_title("Toolbar header")
            .add_component(ui.Button().set_id("tb-refresh")
                           .set_label("↻ Refresh").set_variant("ghost")))

        # form field grid: compact columns instead of a field tower
        layout_section.add_component(
            ui.Form().set_id("demo-grid-form").set_title("Gridded form")
            .set_columns(3).set_submit_label("Submit")
            .add_fields(ui.TextInput().set_id("gf-a").set_label("Field A"),
                        ui.TextInput().set_id("gf-b").set_label("Field B"),
                        ui.TextInput().set_id("gf-c").set_label("Field C")))

        # structured grid: equal cells that wrap as whole rows
        layout_section.add_component(
            ui.Grid().set_id("demo-grid").set_columns("auto").set_min_child("160px")
            .add_component(ui.DisplayText().set_id("g1").set_value("cell 1")
                           .set_tone("dim").set_align("center"))
            .add_component(ui.DisplayText().set_id("g2").set_value("cell 2")
                           .set_tone("dim").set_align("center"))
            .add_component(ui.DisplayText().set_id("g3").set_value("cell 3")
                           .set_tone("dim").set_align("center"))
            .add_component(ui.DisplayText().set_id("g4").set_value("cell 4")
                           .set_tone("dim").set_align("center")))

        # alerts: the designed error/notice surface
        layout_section.add_component(
            ui.Grid().set_id("alert-grid").set_columns(2)
            .add_component(ui.Alert().set_id("al-info").set_variant("info")
                           .set_title("Heads up")
                           .set_value("Notices render as part of the UI."))
            .add_component(ui.Alert().set_id("al-err").set_variant("err")
                           .set_title("Something failed")
                           .set_value("Caught errors belong in an Alert, "
                                      "never in raw text.")))

        layout_tab = (ui.Tab().set_id("tab-layout").set_title("Layout")
                      .add_section(layout_section))

        page.add_tabs(general_tab, advanced_tab, data_tab, layout_tab)

        self.attach_dashboard_ui(ui.Layout().add_page(page))

    # ------------------------------------------------------ UI callbacks
    async def _on_toggle_feature(self, context: ActionContext,
                                 payload: dict) -> ActionResult:
        """Checkbox action — payload carries the new bool under 'value'."""
        self._feature_enabled = bool(payload.get("value"))
        self._action_count += 1
        state = "enabled" if self._feature_enabled else "disabled"
        self._note_event(context, "feature", state)
        return ActionResult(
            success=True,
            message=f"feature {state} (account {context.account or '—'})",
            refresh=["recent-events", "status-lines"])

    async def _on_toggle_verbose(self, context: ActionContext,
                                 payload: dict) -> ActionResult:
        self._verbose = bool(payload.get("value"))
        self._action_count += 1
        self._note_event(context, "verbose",
                         "on" if self._verbose else "off")
        return ActionResult(
            success=True,
            message=f"verbose logging {'on' if self._verbose else 'off'}",
            refresh=["recent-events"])

    async def _on_submit_rule(self, context: ActionContext,
                              payload: dict) -> ActionResult:
        """Form submit — payload maps field id → current field value.

        Demonstrates field validation: a failed check returns
        ``success=False`` with ``field_errors`` keyed by field id; the
        form renders each message inline under its input.
        """
        self._action_count += 1
        name = str(payload.get("rule-name") or "").strip()
        note = str(payload.get("rule-note") or "").strip()
        errors: dict = {}
        if not name:
            errors["rule-name"] = "the rule name is required"
        elif len(self._rules) >= self._max_rules():
            errors["rule-name"] = (
                f"rule capacity is exhausted ({self._max_rules()})")
        try:
            threshold = float(payload.get("rule-threshold"))
        except (TypeError, ValueError):
            threshold = None
            errors["rule-threshold"] = "the threshold must be a number"
        else:
            if not 0 <= threshold <= 100:
                errors["rule-threshold"] = "the threshold must be 0-100"
        if errors:
            return ActionResult(
                success=False,
                message="the rule has errors — fix the highlighted fields",
                field_errors=errors)
        self._rules.append(
            {"name": name, "threshold": threshold, "note": note})
        self._note_event(context, "rule", f"{name} @ {threshold:g}")
        return ActionResult(
            success=True,
            message=f"rule {name!r} added (threshold {threshold:g})",
            data={"rules": len(self._rules)},
            refresh=["recent-events", "status-lines"])

    async def _on_purge(self, context: ActionContext,
                        payload: dict) -> ActionResult:
        """Confirmation-gated button — runs only after the user accepts
        the frontend's "Are you sure?" dialog."""
        self._action_count += 1
        rules, events = len(self._rules), len(self._events)
        self._rules.clear()
        self._events.clear()
        self._note_event(context, "purge",
                         f"{rules} rule(s), {events} event row(s)")
        return ActionResult(
            success=True,
            message=f"purged {rules} rule(s) and {events} event row(s)",
            refresh=["recent-events", "status-lines"])

    async def _on_export(self, context: ActionContext,
                         payload: dict) -> ActionResult:
        """Disabled-button action — still dispatchable server-side."""
        self._action_count += 1
        self._note_event(context, "export", "dispatched while disabled")
        return ActionResult(
            success=True,
            message="export prepared (the button's disabled state is a "
                    "UI hint, not an authorization)",
            data={"rules": [dict(r) for r in self._rules]},
            refresh=["recent-events"])

    # ---------------------------------------------------- data providers
    def _recent_events(self, context: CheckContext) -> list:
        """Feeds the Data tab's table — a list of JSON-safe row dicts."""
        return list(self._events)

    def _status_lines(self, context: CheckContext) -> list:
        """Feeds the list-view — {label, value} dicts render as rows."""
        return [
            {"label": "feature",
             "value": "enabled" if self._feature_enabled else "disabled"},
            {"label": "verbose logging",
             "value": "on" if self._verbose else "off"},
            {"label": "rules", "value": len(self._rules)},
            {"label": "event rows", "value": len(self._events)},
        ]

    # ---------------------------------------------------- visibility check
    def _advanced_visible(self, context: CheckContext) -> bool:
        """The Advanced tab's visibility check — always True here (the
        demo has no gate), proving the API. Fail-closed semantics: a
        falsey return OR a raised error hides the tab in the rendered
        page AND fails every server-side request beneath it."""
        return True

    # --------------------------------------------------------- state utils
    def _note_event(self, context: ActionContext, kind: str,
                    detail: str) -> None:
        """Append one bounded, JSON-safe row to the demo event log."""
        self._events.append({
            "kind": kind,
            "detail": detail,
            "account": context.account or "-",
            "count": self._action_count,
        })
        del self._events[:-25]  # keep the demo table bounded

    def _page_title(self) -> str:
        settings = getattr(self._ctx, "settings", None) or {}
        return str(settings.get("page_title", "Config Demo"))

    def _max_rules(self) -> int:
        settings = getattr(self._ctx, "settings", None) or {}
        try:
            return max(1, int(settings.get("max_rules", 10)))
        except (TypeError, ValueError):
            return 10


__all__ = ["ConfigDemoPlugin"]
