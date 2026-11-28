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

Every pattern here mirrors the Dashboard UI API reference the file
carries (see the stand-in context types above); the dashboard interface
itself was removed, and today the fixture is exercised only through
plugin discovery (``tests/integration/test_account_plugins_e2e.py``
asserts it appears in the manifest).

SCOPE NOTE (plugin-wide UI): the dashboard UI registers ONCE per
plugin (the first account to activate it wins), so the callbacks are
bound to that first instance while ``context.account`` names the
account the dashboard had selected when the action fired. Keep the
state actions mutate small and account-agnostic, or key it off
``context.account`` like the event log below does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from libzentsuite.core.plugin_base import PluginBase, PluginContext
from libzentsuite.core.settings_manager import Setting

if TYPE_CHECKING:
    from libzentsuite.core.event_bus import Event


# ------------------------------------------------------- removed-API stand-ins
# The Dashboard UI API (core.dashboard_ui.context) was deleted with the
# dashboard interface; this discovery-only fixture keeps the reference
# callback shapes alive locally so the example below stays faithful to
# the contract it documents. Nothing dispatches these callbacks today —
# the plugin only needs to load and appear in the plugin manifest.
@dataclass(frozen=True)
class CheckContext:
    plugin: str
    component_id: str
    component_type: str
    account: str | None = None
    services: Any = None


@dataclass(frozen=True)
class ActionContext:
    plugin: str
    component_id: str
    account: str | None = None
    services: Any = None


@dataclass(frozen=True)
class ActionResult:
    success: bool
    message: str = ""
    data: Any = None
    field_errors: dict[str, str] = field(default_factory=dict)
    refresh: list[str] = field(default_factory=list)
    modal: str | None = None


class ConfigDemoPlugin(PluginBase):
    """A realistic configuration page built with the Dashboard UI API."""

    name = "config_demo"
    version = "1.0.0"
    description = "dashboard UI API reference example"

    settings = (
        Setting("page_title", "str", "Config Demo", "title of the dashboard page"),
        Setting("max_rules", "int", 10, "threshold-rule capacity", min_value=1, max_value=50),
    )

    def __init__(self) -> None:
        #: the live PluginContext of the current activation
        self._ctx: PluginContext | None = None
        #: demo state mutated by UI actions (see the module docstring's
        #: scope note: one instance owns the UI plugin-wide)
        self._feature_enabled = False
        self._verbose = False
        self._rules: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
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
    async def _on_toggle_feature(
        self, context: ActionContext, payload: dict[str, Any]
    ) -> ActionResult:
        """Checkbox action — payload carries the new bool under 'value'."""
        self._feature_enabled = bool(payload.get("value"))
        self._action_count += 1
        state = "enabled" if self._feature_enabled else "disabled"
        self._note_event(context, "feature", state)
        return ActionResult(
            success=True,
            message=f"feature {state} (account {context.account or '—'})",
            refresh=["recent-events", "status-lines"],
        )

    async def _on_toggle_verbose(
        self, context: ActionContext, payload: dict[str, Any]
    ) -> ActionResult:
        self._verbose = bool(payload.get("value"))
        self._action_count += 1
        self._note_event(context, "verbose", "on" if self._verbose else "off")
        return ActionResult(
            success=True,
            message=f"verbose logging {'on' if self._verbose else 'off'}",
            refresh=["recent-events"],
        )

    async def _on_submit_rule(
        self, context: ActionContext, payload: dict[str, Any]
    ) -> ActionResult:
        """Form submit — payload maps field id → current field value.

        Demonstrates field validation: a failed check returns
        ``success=False`` with ``field_errors`` keyed by field id; the
        form renders each message inline under its input.
        """
        self._action_count += 1
        name = str(payload.get("rule-name") or "").strip()
        note = str(payload.get("rule-note") or "").strip()
        errors: dict[str, str] = {}
        if not name:
            errors["rule-name"] = "the rule name is required"
        elif len(self._rules) >= self._max_rules():
            errors["rule-name"] = f"rule capacity is exhausted ({self._max_rules()})"
        try:
            raw_threshold: Any = payload.get("rule-threshold")
            threshold = float(raw_threshold)
        except (TypeError, ValueError):
            threshold = None
            errors["rule-threshold"] = "the threshold must be a number"
        else:
            assert threshold is not None  # the try-block assigned a float
            if not 0 <= threshold <= 100:
                errors["rule-threshold"] = "the threshold must be 0-100"
        if errors:
            return ActionResult(
                success=False,
                message="the rule has errors — fix the highlighted fields",
                field_errors=errors,
            )
        self._rules.append({"name": name, "threshold": threshold, "note": note})
        self._note_event(context, "rule", f"{name} @ {threshold:g}")
        return ActionResult(
            success=True,
            message=f"rule {name!r} added (threshold {threshold:g})",
            data={"rules": len(self._rules)},
            refresh=["recent-events", "status-lines"],
        )

    async def _on_purge(self, context: ActionContext, payload: dict[str, Any]) -> ActionResult:
        """Confirmation-gated button — runs only after the user accepts
        the frontend's "Are you sure?" dialog."""
        self._action_count += 1
        rules, events = len(self._rules), len(self._events)
        self._rules.clear()
        self._events.clear()
        self._note_event(context, "purge", f"{rules} rule(s), {events} event row(s)")
        return ActionResult(
            success=True,
            message=f"purged {rules} rule(s) and {events} event row(s)",
            refresh=["recent-events", "status-lines"],
        )

    async def _on_export(self, context: ActionContext, payload: dict[str, Any]) -> ActionResult:
        """Disabled-button action — still dispatchable server-side."""
        self._action_count += 1
        self._note_event(context, "export", "dispatched while disabled")
        return ActionResult(
            success=True,
            message="export prepared (the button's disabled state is a "
            "UI hint, not an authorization)",
            data={"rules": [dict(r) for r in self._rules]},
            refresh=["recent-events"],
        )

    # ---------------------------------------------------- data providers
    def _recent_events(self, context: CheckContext) -> list[dict[str, Any]]:
        """Feeds the Data tab's table — a list of JSON-safe row dicts."""
        return list(self._events)

    def _status_lines(self, context: CheckContext) -> list[dict[str, Any]]:
        """Feeds the list-view — {label, value} dicts render as rows."""
        return [
            {"label": "feature", "value": "enabled" if self._feature_enabled else "disabled"},
            {"label": "verbose logging", "value": "on" if self._verbose else "off"},
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
    def _note_event(self, context: ActionContext, kind: str, detail: str) -> None:
        """Append one bounded, JSON-safe row to the demo event log."""
        self._events.append(
            {
                "kind": kind,
                "detail": detail,
                "account": context.account or "-",
                "count": self._action_count,
            }
        )
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
