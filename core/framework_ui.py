"""The framework's OWN dashboard pages, built with the plugin UI API.

The main dashboard tabs (Home, Plugins, Activity, Policies, Admin, Tools)
are defined here through the SAME builder toolset plugins use
(:mod:`core.dashboard_ui`) and registered in the
:class:`~core.dashboard_ui.registry.DashboardUIRegistry` under the
reserved plugin name ``$framework``. The SPA renders them with the same
generic renderer as plugin pages — the dashboard dogfoods its own UI API.

Design rules:
  * pages are BUILT ONCE at boot (frozen snapshots); anything dynamic is
    a data provider (tables, list-views, live metrics/texts) or an
    action closure over the ControlAPI — never a rebuilt layout;
  * actions call ControlAPI methods only (audit + validation included)
    and return ``refresh`` lists so the renderer re-fetches exactly the
    data components the mutation touched;
  * the reserved ``$framework`` plugin name cannot collide with a real
    plugin (the loader rejects ``$`` in plugin names), and the generic
    sidebar nav filters it out — these pages ARE the main tabs.

Row keys: tables whose rows need composite or display-cleaned keys emit
an extra ``__key`` cell past the visible columns; the renderer resolves
``key_column`` against the columns first, then the ``__key`` cell.

Accounts & Groups and Profiles stay on the legacy panels until the
builder API grows selection/modals; everything here is fully covered by
components that exist today.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Dict, List

from core.dashboard_ui import DashboardUIToolset, RowAction, BulkAction
from core.dashboard_ui.context import current_view_id
from core.dashboard_ui.registry import DashboardUIRegistry

#: reserved registry plugin name for the framework's own pages
FRAMEWORK_PLUGIN = "$framework"

#: main-tab views the SPA renders through the framework pages
FRAMEWORK_VIEWS = ("home", "fleet", "plugins", "profiles", "activity",
                   "policies", "admin", "tools", "design")

class _ViewState:
    """Dict-like modal context, scoped to the requesting dashboard VIEW.

    Which row a modal is looking at was one process-wide dict, so a
    second open tab (or a phone) overwrote the first tab's selection and
    a pasted token could land on the wrong account. Every read/write now
    resolves to the bucket of the current request's view id
    (:func:`current_view_id`, set by the dashboard server); requests with
    no view id share one legacy bucket (CLI/tests/old clients). Buckets
    are LRU-bounded so abandoned tabs cannot grow memory.
    """

    MAX_VIEWS = 64

    def __init__(self, defaults: Dict[str, Any]) -> None:
        self._defaults = dict(defaults)
        self._buckets: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def _bucket(self) -> Dict[str, Any]:
        key = current_view_id() or ""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = dict(self._defaults)
            while len(self._buckets) > self.MAX_VIEWS:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        return bucket

    def get(self, key: str, default: Any = None) -> Any:
        return self._bucket().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._bucket()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._bucket()[key] = value


#: context-modal state: which row each modal is looking at (set by the
#: opening action, read by the modal's data providers) — per dashboard view
_fleet_state = _ViewState({"details": None, "cred": None, "move": None,
                           "assign": None, "group_details": None,
                           "group_edit": None, "group_members": None})
_profiles_state = _ViewState({"account": None, "bind": None,
                              "editor": None, "effective": None})


def attach_framework_ui(registry: DashboardUIRegistry, api: Any) -> int:
    """Build and register the framework's main-tab pages.

    Called once from the bootstrap once the ControlAPI exists. Returns
    the number of pages registered. Re-attaching replaces the previous
    layout atomically — attach() validates the new tree before dropping
    the old one.
    """
    ui = DashboardUIToolset()
    layout = ui.Layout(component_id="framework-root")
    for build in (_build_home, _build_fleet, _build_plugins,
                  _build_profiles, _build_activity, _build_policies,
                  _build_admin, _build_tools, _build_design):
        layout.add_page(build(ui, api))
    registry.attach(FRAMEWORK_PLUGIN, layout)
    return len(layout.pages())


# ─────────────────────────────────────────────────────────────── helpers
def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 129600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


async def _schema_for(api: Any, plugin: str) -> tuple:
    """Plugin setting schema — sync or async duck-typed, failure-tolerant."""
    try:
        raw = api._plugin_schema(plugin)
        if hasattr(raw, "__await__"):
            raw = await raw
        return tuple(raw or ())
    except Exception:
        return ()


async def _status(api: Any) -> Dict[str, Any]:
    """status() with a hard fallback — pages must render even when the
    managers are mid-reconcile."""
    try:
        return await api.status()
    except Exception:
        return {}


# ────────────────────────────────────────────────────────────────── home
def _build_home(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="home")
            .set_title("Home")
            .set_icon("M3 12l9-9 9 9v9a2 2 0 0 1-2 2h-4v-7h-6v7H5a2 2 0 0 1-2-2z")
            .set_description(
                "Fleet overview — live metrics refresh with the stats stream."))

    async def accounts_metric(ctx=None):
        st = await _status(api)
        rows = st.get("accounts") or []
        running = sum(1 for a in rows if a.get("status") == "running")
        total = len(rows)
        level = ("ok" if running == total   # 0/0 is healthy (v1 semantics)
                 else ("err" if running == 0 else "warn"))
        return {"value": f"{running}/{total}", "sub": "online", "level": level}

    async def groups_metric(ctx=None):
        try:
            groups = await api.groups_list()
        except Exception:
            groups = []
        return {"value": str(len(groups or [])), "sub": "sync groups"}

    async def tasks_metric(ctx=None):
        st = await _status(api)
        tk = st.get("tasks") or {}
        return {"value": str(tk.get("running", 0)),
                "sub": f"{tk.get('queued', 0)} queued"}

    async def cooldown_metric(ctx=None):
        st = await _status(api)
        rl = st.get("ratelimit") or {}
        cds = ((rl.get("cooldowns") or {}).get("accounts")) or {}
        return {"value": str(len(cds)), "sub": "active"}

    async def uptime_metric(ctx=None):
        st = await _status(api)
        return {"value": _fmt_uptime(st.get("uptime_s") or 0),
                "sub": "framework uptime"}

    (page
     .add_section(ui.Section(component_id="home-metrics-sec")
        .add_component(ui.Grid(component_id="home-metrics")
           .set_columns("auto").set_min_child("150px")
           .add_component(ui.Metric(component_id="hm-accounts").set_label("Accounts")
                          .set_value("0/0").set_level("warn")
                          .set_data_source(accounts_metric))
           .add_component(ui.Metric(component_id="hm-groups").set_label("Groups")
                          .set_value("0").set_data_source(groups_metric))
           .add_component(ui.Metric(component_id="hm-tasks").set_label("Tasks")
                          .set_value("0").set_data_source(tasks_metric))
           .add_component(ui.Metric(component_id="hm-cooldowns").set_label("Cooldowns")
                          .set_value("0").set_data_source(cooldown_metric))
           .add_component(ui.Metric(component_id="hm-uptime").set_label("Uptime")
                          .set_value("0s").set_data_source(uptime_metric))))
     .add_section(ui.Section(component_id="home-attn-sec", title="Attention")
            .set_icon("M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z")
        .add_component(ui.ListView(component_id="home-attn")
                       .set_empty_text("All accounts healthy — no alerts.")
                       .set_data_source(_attention_data(api))))
     .add_section(ui.Section(component_id="home-groups-sec")
        .add_component(ui.Toolbar(component_id="home-groups-bar")
                       .set_title("Sync Groups")
                       .set_icon("M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"))
        .add_component(ui.Table(component_id="home-groups")
                       .set_columns(["Group", "Members", "Online", "State"])
                       .set_key_column("Group")
                       .set_empty_text("no groups yet — create one in Accounts & Groups")
                       .set_data_source(_groups_data(api)))
        .add_component(ui.Footer(component_id="home-foot")
                       .set_value("Group management lives in Accounts & Groups; "
                                  "plugin pages appear in the sidebar."))))
    return page


def _attention_data(api: Any):
    async def provide(ctx):
        st = await _status(api)
        items: List[Dict[str, Any]] = []
        for a in (st.get("accounts") or []):
            status = a.get("status") or ""
            if status in ("missing_credential", "invalid_credential"):
                items.append({
                    "label": a.get("name", "?"),
                    "value": "credential " + (
                        "invalid" if status == "invalid_credential" else "missing")})
            elif status == "failed":
                items.append({"label": a.get("name", "?"), "value": "account failed"})
        return items
    return provide


def _groups_data(api: Any):
    async def provide(ctx):
        try:
            groups = await api.groups_list()
        except Exception:
            groups = []
        st = await _status(api)
        rows = st.get("accounts") or []
        out = []
        for g in (groups or []):
            name = g.get("name", "?")
            members = [a for a in rows if a.get("sync_group") == name]
            online = sum(1 for m in members if m.get("status") == "running")
            count = g.get("member_count", len(members))
            out.append([name, str(count), f"{online}/{count}",
                        "disabled" if g.get("enabled") is False else "enabled"])
        return {"columns": ["Group", "Members", "Online", "State"], "rows": out}
    return provide


# ──────────────────────────────────────────────────────────────── plugins
def _build_plugins(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="plugins")
            .set_title("Plugins")
            .set_icon("M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z")
            .set_description(
                "Discovered plugin packages. Reload re-imports the module and "
                "re-attaches every account (assignments preserved); unload "
                "detaches everywhere — reload restores. Profile libraries and "
                "per-account bindings live in the Profiles tab."))

    async def rescan(ctx=None, payload=None):
        out = await api.plugins_reload("__rescan__", surface="dashboard")
        found = out.get("discovered") or []
        return {"success": True,
                "message": f"rescan complete — {len(found)} plugin(s) discovered",
                "refresh": ["plug-manifests", "plug-state"]}

    async def reload_row(row, ctx):
        out = await api.plugins_reload(row, surface="dashboard")
        return {"success": True,
                "message": f"{row} reloaded "
                           f"({out.get('restored_accounts', 0)} account(s) restored)",
                "refresh": ["plug-manifests", "plug-state"]}

    async def unload_row(row, ctx):
        out = await api.plugins_unload(row, surface="dashboard")
        return {"success": True,
                "message": f"{row} unloaded from {out.get('detached_accounts', 0)} account(s)",
                "refresh": ["plug-manifests", "plug-state"]}

    async def toggle_row(row, ctx):
        account, _, plugin = row.partition("|")
        current = None
        try:
            state = await api.plugins_state(account)
        except Exception:
            state = []
        for entry in (state or []):
            if entry.get("name") == plugin:
                current = entry.get("enabled")
                break
        if current is None:
            return {"success": False,
                    "message": f"{plugin!r} is not attached to {account!r}"}
        await api.plugins_set_enabled(account, plugin, not current)
        return {"success": True,
                "message": f"{plugin} {'enabled' if not current else 'disabled'} on {account}",
                "refresh": ["plug-state"]}

    (page
     .add_section(ui.Section(component_id="plug-man-sec")
        .add_component(ui.Toolbar(component_id="plug-man-bar")
                       .set_title("Discovered plugins")
                       .set_icon("M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4")
                       .add_component(ui.Button(component_id="plug-rescan")
                                      .set_label("↻ Rescan")
                                      .set_description(
                                          "Re-scan plugins/ for new or externally-changed packages.")
                                      .set_action(rescan)))
        .add_component(ui.Table(component_id="plug-manifests")
                       .set_columns(["Plugin", "Version", "Accounts", "Description"])
                       .set_key_column("Plugin")
                       .set_empty_text("No plugins discovered — place a package in "
                                       "plugins/ and rescan.")
                       .set_data_source(_manifests_data(api))
                       .set_row_actions([
                           RowAction("reload", "reload", reload_row),
                           RowAction("unload", "unload", unload_row, variant="danger",
                                     confirm="Unload this plugin from every account? "
                                             "Assignments are preserved — reload restores them."),
                       ])))
     .add_section(ui.Section(component_id="plug-state-sec")
        .add_component(ui.Toolbar(component_id="plug-state-bar")
                       .set_title("Per-account state")
                       .set_icon("M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2"))
        .add_component(ui.Table(component_id="plug-state")
                       .set_columns(["Account", "Plugin", "Enabled", "Active", "Errors"])
                       .set_key_column("__key")
                       .set_empty_text("no plugin attached to any account yet")
                       .set_data_source(_plugin_state_data(api))
                       .set_row_actions([
                           RowAction("toggle", "enable/disable", toggle_row),
                       ]))))
    return page


def _manifests_data(api: Any):
    async def provide(ctx):
        try:
            manifests = await api.plugins_manifests()
            accounts = await api.accounts_list()
        except Exception:
            manifests, accounts = [], []
        by_plugin: Dict[str, int] = {}
        for a in (accounts or []):
            for p in (a.get("assigned_plugins") or []):
                by_plugin[p] = by_plugin.get(p, 0) + 1
        rows = []
        for m in (manifests or []):
            name = m.get("name", "?")
            rows.append([name, str(m.get("version", "?")),
                         str(by_plugin.get(name, 0)),
                         (m.get("description") or "")[:80]])
        return {"columns": ["Plugin", "Version", "Accounts", "Description"],
                "rows": rows}
    return provide


def _plugin_state_data(api: Any):
    async def provide(ctx):
        rows = []
        try:
            accounts = await api.accounts_list()
        except Exception:
            accounts = []
        for acct in accounts:
            try:
                state = await api.plugins_state(acct.get("name", ""))
            except Exception:
                continue
            for e in (state or []):
                # __key cell past the visible columns: "account|plugin"
                rows.append([acct.get("name", "?"), e.get("name", "?"),
                             "yes" if e.get("enabled") else "no",
                             "yes" if e.get("active") else "no",
                             str(e.get("error_count", 0)),
                             f"{acct.get('name', '?')}|{e.get('name', '?')}"])
        return {"columns": ["Account", "Plugin", "Enabled", "Active", "Errors"],
                "rows": rows}
    return provide


# ─────────────────────────────────────────────────────────────── activity
def _build_activity(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="activity")
            .set_title("Activity")
            .set_icon("M22 12h-4l-3 9L9 3l-3 9H2")
            .set_description(
                "Scheduler tasks, persistent cooldowns and the log/audit tails. "
                "Data refreshes with the stats stream; the refresh buttons pull "
                "immediately."))

    async def cancel_row(row, ctx):
        await api.tasks_cancel(int(row))
        return {"success": True, "message": f"cancel requested for task #{row}",
                "refresh": ["act-tasks"]}

    async def clear_cd_row(row, ctx):
        account, _, plugin = row.partition("|")
        await api.cooldowns_clear(account, plugin, surface="dashboard")
        return {"success": True,
                "message": f"cooldowns cleared for {plugin} on {account}",
                "refresh": ["act-cds"]}

    (page
     .add_section(ui.Section(component_id="act-tasks-sec")
        .add_component(ui.Toolbar(component_id="act-tasks-bar").set_title("Tasks")
                       .set_icon("M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11")
                       .add_component(ui.Button(component_id="act-tasks-refresh")
                                      .set_label("↻ Refresh").set_variant("ghost")
                                      .set_action(lambda ctx=None, payload=None: {"success": True, "message": "tasks refreshed", "refresh": ["act-tasks"]})))
        .add_component(ui.Table(component_id="act-tasks")
                       .set_columns(["ID", "Account", "Plugin", "Status", "Runs"])
                       .set_key_column("ID")
                       .set_empty_text("no tasks scheduled")
                       .set_page_size(50)
                       .set_data_source(_tasks_data(api))
                       .set_row_actions([
                           RowAction("cancel", "cancel", cancel_row, variant="danger",
                                     confirm="Request cancellation for this task?"),
                       ])))
     .add_section(ui.Section(component_id="act-cds-sec")
        .add_component(ui.Toolbar(component_id="act-cds-bar").set_title("Cooldowns")
                       .set_icon("M12 8v4l3 3m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0z")
                       .add_component(ui.Button(component_id="act-cds-refresh")
                                      .set_label("↻ Refresh").set_variant("ghost")
                                      .set_action(lambda ctx=None, payload=None: {"success": True, "message": "cooldowns refreshed", "refresh": ["act-cds"]})))
        .add_component(ui.Table(component_id="act-cds")
                       .set_columns(["Account", "Plugin", "Action", "Remaining", "Total"])
                       .set_key_column("__key")
                       .set_empty_text("no active cooldowns")
                       .set_data_source(_cooldowns_data(api))
                       .set_row_actions([
                           RowAction("clear", "clear plugin", clear_cd_row, variant="danger",
                                     confirm="Clear EVERY cooldown this account holds "
                                             "for this plugin?"),
                       ])))
     .add_section(ui.Section(component_id="act-logs-sec")
        .add_component(ui.Toolbar(component_id="act-logs-bar")
                       .set_title("Recent logs")
                       .set_icon("M4 6h16M4 12h16M4 18h10")
                       .add_component(ui.Button(component_id="act-logs-refresh")
                                      .set_label("↻ Refresh").set_variant("ghost")
                                      .set_action(lambda ctx=None, payload=None: {"success": True, "message": "logs refreshed", "refresh": ["act-logs"]})))
        .add_component(ui.Text(component_id="act-logs").set_variant("code")
                       .set_value("— refresh to load —")
                       .set_data_source(_logs_data(api))))
     .add_section(ui.Section(component_id="act-audit-sec")
        .add_component(ui.Toolbar(component_id="act-audit-bar").set_title("Audit trail")
                       .set_icon("M9 12l2 2 4-4m6 2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z")
                       .add_component(ui.Button(component_id="act-audit-refresh")
                                      .set_label("↻ Refresh").set_variant("ghost")
                                      .set_action(lambda ctx=None, payload=None: {"success": True, "message": "audit refreshed", "refresh": ["act-audit"]})))
        .add_component(ui.Text(component_id="act-audit").set_variant("code")
                       .set_value("— refresh to load —")
                       .set_data_source(_audit_data(api)))))
    return page


def _tasks_data(api: Any):
    async def provide(ctx):
        try:
            tasks = await api.tasks_list()
        except Exception:
            tasks = []
        rows = []
        for k in (tasks or []):
            rows.append([str(k.get("id", "?")), k.get("account") or "—",
                         k.get("plugin") or "—", k.get("status", "?"),
                         str(k.get("run_count", k.get("runs", "?")))])
        return {"columns": ["ID", "Account", "Plugin", "Status", "Runs"],
                "rows": rows}
    return provide


def _cooldowns_data(api: Any):
    async def provide(ctx):
        try:
            cds = await api.cooldowns_list()
        except Exception:
            cds = []
        rows = []
        now = time.time()
        for c in (cds or []):
            until = c.get("until") or c.get("until_s") or c.get("expires_at")
            remain = "?" if until is None else f"{max(0, int(float(until) - now))}s"
            account = c.get("account", "?")
            plugin = c.get("plugin") or "—"
            rows.append([account, plugin, c.get("action", "?"), remain,
                         str(c.get("count", c.get("total", "?"))),
                         f"{account}|{plugin}"])
        return {"columns": ["Account", "Plugin", "Action", "Remaining", "Total"],
                "rows": rows}
    return provide


def _logs_data(api: Any):
    async def provide(ctx):
        try:
            entries = await api.logs_tail(60)
        except Exception:
            entries = []
        lines = []
        for e in (entries or []):
            lines.append(f"[{e.get('ts', e.get('time', ''))}] "
                         f"{str(e.get('account') or 'system'):12} "
                         f"{str(e.get('level') or 'info'):5} "
                         f"{e.get('message', e.get('msg', ''))}")
        return {"value": "\n".join(lines) or "— no log entries —"}
    return provide


def _audit_data(api: Any):
    async def provide(ctx):
        try:
            entries = await api.audit_tail(60)
        except Exception:
            entries = []
        lines = []
        for e in (entries or []):
            lines.append(f"[{e.get('ts', e.get('time', ''))}] "
                         f"{str(e.get('account') or 'system'):12} "
                         f"{e.get('event', '?')}"
                         + (f" — {e.get('detail')}" if e.get("detail") else ""))
        return {"value": "\n".join(lines) or "— no audit entries —"}
    return provide


# ─────────────────────────────────────────────────────────────── policies
def _build_policies(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="policies")
            .set_title("Policies")
            .set_icon("M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z")
            .set_description(
                "Discord bucket pacing is the primary constraint — stored "
                "policies are explicit overrides. Setting an existing (level, "
                "owner, action) key UPDATES it in place."))

    async def floor_metric(ctx=None):
        try:
            stats = await api.ratelimit_stats()
        except Exception:
            stats = {}
        floor = (stats or {}).get("floor")
        return {"value": f"{floor}s" if floor is not None else "—",
                "sub": "hard minimum"}

    async def add_policy(ctx, payload):
        try:
            interval = float(payload.get("pol-interval") or 0)
        except (TypeError, ValueError):
            interval = 0.0
        if interval <= 0:
            return {"success": False, "field_errors": {
                "pol-interval": "Interval must be a positive number of seconds."}}
        action = str(payload.get("pol-action") or "message")
        level = str(payload.get("pol-level") or "global")
        owner = str(payload.get("pol-owner") or "").strip() or None
        try:
            jmin = float(payload.get("pol-jmin") or 0)
            jmax = float(payload.get("pol-jmax") or 0)
        except (TypeError, ValueError):
            jmin = jmax = 0.0
        await api.ratelimit_set(action, interval, jmin, jmax, level, owner)
        return {"success": True,
                "message": f"{level} policy for {action} set to {interval}s",
                "refresh": ["pol-table"]}

    async def clear_row(row, ctx):
        level, _, rest = row.partition("\u0001")
        owner, _, action = rest.partition("\u0001")
        await api.ratelimit_clear(level, owner or None, action)
        return {"success": True,
                "message": f"cleared {level} policy for {action}",
                "refresh": ["pol-table"]}

    async def set_floor(ctx, payload):
        try:
            seconds = float(payload.get("floor-seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds <= 0:
            return {"success": False, "field_errors": {
                "floor-seconds": "Floor must be a positive number of seconds."}}
        out = await api.ratelimit_set_floor(seconds)
        return {"success": True,
                "message": f"safety floor set to {out.get('floor')}s",
                "refresh": ["pol-floor"]}

    form = (ui.Form(component_id="pol-form")
            .set_title("Add / update policy")
            .set_description("Policies key on (level, owner, action): setting "
                             "an existing key updates it.")
            .set_columns(3)
            .set_submit_action(add_policy)
            .set_submit_label("Save policy")
            .add_fields(
                ui.Select(component_id="pol-action").set_label("Action")
                .set_options(["message", "dm", "reaction", "button"]),
                ui.NumberInput(component_id="pol-interval").set_label("Interval (s)")
                .set_min_value(1.5).set_step(0.1).set_required(True)
                .set_description("Hard floor 1.5s — anything lower is rejected."),
                ui.NumberInput(component_id="pol-jmin").set_label("Jitter min (s)")
                .set_min_value(0).set_step(0.1),
                ui.NumberInput(component_id="pol-jmax").set_label("Jitter max (s)")
                .set_min_value(0).set_step(0.1),
                ui.Select(component_id="pol-level").set_label("Level")
                .set_options(["global", "account", "plugin"]),
                ui.TextInput(component_id="pol-owner").set_label("Owner")
                .set_placeholder("account or plugin name (account/plugin levels)")
                .set_max_length(64)))

    floor_form = (ui.Form(component_id="floor-form")
                  .set_title("Safety floor")
                  .set_description("The pacing floor applies to every policy — "
                                   "Discord bucket limits stay primary.")
                  .set_submit_action(set_floor)
                  .set_submit_label("Set floor")
                  .add_fields(ui.NumberInput(component_id="floor-seconds")
                              .set_label("Seconds").set_min_value(1.5).set_step(0.1)))

    (page
     .add_section(ui.Section(component_id="pol-top-sec")
        .add_component(ui.Grid(component_id="pol-top")
           .set_columns(2)
           .add_component(ui.Metric(component_id="pol-floor")
                          .set_label("Safety floor").set_value("—")
                          .set_data_source(floor_metric))
           .add_component(floor_form)))
     .add_section(ui.Section(component_id="pol-form-sec")
        .add_component(form))
     .add_section(ui.Section(component_id="pol-table-sec")
        .add_component(ui.Toolbar(component_id="pol-table-bar")
                       .set_title("Stored policies")
                       .set_icon("M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"))
        .add_component(ui.Table(component_id="pol-table")
                       .set_columns(["Level", "Owner", "Action", "Interval", "Jitter"])
                       .set_key_column("__key")
                       .set_empty_text("No stored policies — Discord bucket pacing "
                                       "is active by default.")
                       .set_data_source(_policies_data(api))
                       .set_row_actions([
                           RowAction("clear", "clear", clear_row, variant="danger",
                                     confirm="Remove this stored policy?"),
                       ])))
     .add_section(ui.Section(component_id="pol-429-sec", title="Throttles (429)")
        .add_component(ui.Text(component_id="pol-429").set_variant("code")
                       .set_value("—").set_data_source(_throttle_data(api)))))
    return page


def _policies_data(api: Any):
    async def provide(ctx):
        try:
            policies = await api.ratelimit_policies()
        except Exception:
            policies = []
        rows = []
        for p in (policies or []):
            key = "\u0001".join([str(p.get("level", "global")),
                                 str(p.get("owner") or ""),
                                 str(p.get("action", "?"))])
            jitter = p.get("jitter") or []
            jtxt = (f"{jitter[0]}–{jitter[1]}s"
                    if len(jitter) == 2 and any(jitter) else "—")
            rows.append([str(p.get("level", "global")), str(p.get("owner") or "—"),
                         str(p.get("action", "?")), f"{p.get('interval', '?')}s",
                         jtxt, key])
        return {"columns": ["Level", "Owner", "Action", "Interval", "Jitter"],
                "rows": rows}
    return provide


def _throttle_data(api: Any):
    async def provide(ctx):
        st = await _status(api)
        rl = st.get("ratelimit") or {}
        hits = rl.get("hits_429") or {}
        accounts = hits.get("accounts") or {}
        lines = [f"fleet total: {hits.get('total', 0)}"]
        for name, n in sorted(accounts.items()):
            lines.append(f"  {name}: {n}")
        return {"value": "\n".join(lines)}
    return provide


# ─────────────────────────────────────────────────────────────────── admin
def _build_admin(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="admin")
            .set_title("Admin")
            .set_icon("M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z")
            .set_description(
                "DB-backed access control: the admin list, per-account admin "
                "flags, prefixes and the Mudae bot id. Stale rows (accounts "
                "removed before the cleanup cascade existed) carry a remove "
                "action."))

    async def add_admin(ctx, payload):
        try:
            user_id = int(str(payload.get("adm-add-id")).strip())
        except (TypeError, ValueError):
            return {"success": False, "field_errors": {
                "adm-add-id": "Admin id must be an integer Discord user id."}}
        await api.admin_add_id(user_id, surface="dashboard")
        return {"success": True, "message": f"admin {user_id} added",
                "refresh": ["adm-ids", "adm-accounts"]}

    async def remove_id_row(row, ctx):
        """✕ on each admin id entry — no typed-input remove form."""
        try:
            user_id = int(row)
        except (TypeError, ValueError):
            return {"success": False,
                    "message": f"{row!r} is not an integer user id"}
        await api.admin_remove_id(user_id, surface="dashboard")
        return {"success": True, "message": f"admin {user_id} removed",
                "refresh": ["adm-ids", "adm-accounts"]}

    async def set_mudae(ctx, payload):
        raw = payload.get("adm-mudae-id")
        if raw is None or str(raw).strip() == "":
            await api.admin_mudae_id_set(None, surface="dashboard")
            return {"success": True, "message": "mudae bot id cleared"}
        await api.admin_mudae_id_set(str(raw).strip(), surface="dashboard")
        return {"success": True, "message": "mudae bot id saved"}

    async def set_prefix(ctx, payload):
        account = str(payload.get("pr-account") or "").strip()
        if not account:
            return {"success": False, "field_errors": {
                "pr-account": "Account name is required."}}
        mode = str(payload.get("pr-mode") or "mention")
        symbol = str(payload.get("pr-symbol") or "").strip()
        await api.admin_prefix_set(account, mode, symbol, surface="dashboard")
        return {"success": True,
                "message": f"prefix for {account}: {mode}"
                           + (f" {symbol!r}" if symbol else ""),
                "refresh": ["adm-accounts"]}

    async def toggle_admin_row(row, ctx):
        try:
            ov = await api.admin_overview()
        except Exception as exc:
            # NEVER default the flag on a read failure — an empty overview
            # made is_admin False and a "toggle" silently GRANTED admin
            return {"success": False,
                    "message": f"account overview unavailable ({exc})"}
        row_data = ((ov.get("accounts") or {}).get(row)) or {}
        if not row_data:
            return {"success": False,
                    "message": f"account {row!r} not found in overview"}
        is_admin = bool(row_data.get("is_admin"))
        await api.admin_account_flag(row, not is_admin, surface="dashboard")
        return {"success": True,
                "message": f"{row}: admin {'removed' if is_admin else 'granted'}",
                "refresh": ["adm-accounts"]}

    async def forget_row(row, ctx):
        await api.admin_forget_account(row, surface="dashboard")
        return {"success": True,
                "message": f"{row} removed from the admin list",
                "refresh": ["adm-accounts"]}

    # ── box 1: admin user ids — a LIST with a ✕ on every entry ──────
    ids_box = (ui.Container(component_id="adm-ids-box").set_gap("10px")
               .add_component(ui.Header(component_id="adm-ids-h")
                              .set_title("Admin user ids")
                              .set_subtitle("Discord users allowed to run "
                                            "admin commands"))
               .add_component(ui.Table(component_id="adm-ids")
                              .set_columns(["User id"])
                              .set_key_column("User id")
                              .set_empty_text("no admin ids yet")
                              .set_data_source(_admin_ids_data(api))
                              .set_row_actions([
                                  RowAction("remove", "✕", remove_id_row,
                                            variant="danger",
                                            confirm="Remove this admin id?"),
                              ]))
               .add_component(ui.Form(component_id="adm-add-form")
                              .set_title("Add admin id")
                              .set_submit_action(add_admin)
                              .set_submit_label("Add")
                              .add_fields(ui.TextInput(component_id="adm-add-id")
                                          .set_label("User id")
                                          .set_placeholder("Discord user id")
                                          .set_max_length(32))))

    # ── box 2: the Mudae bot id ─────────────────────────────────────
    mudae_box = (ui.Container(component_id="adm-mudae-box").set_gap("10px")
                 .add_component(ui.Header(component_id="adm-mudae-h")
                                .set_title("Mudae bot id")
                                .set_subtitle("The bot every Mudae flow "
                                              "listens to"))
                 .add_component(ui.Form(component_id="adm-mudae-form")
                                .set_submit_action(set_mudae)
                                .set_submit_label("Save")
                                .add_fields(ui.TextInput(component_id="adm-mudae-id")
                                            .set_label("Bot user id")
                                            .set_placeholder("integer, or empty to clear")
                                            .set_max_length(32))))

    # ── prefix form: full-width, three fields in a row ──────────────
    prefix_form = (ui.Form(component_id="pr-form", title="Set account prefix")
                   .set_columns(3)
                   .set_submit_action(set_prefix).set_submit_label("Apply")
                   .add_fields(ui.TextInput(component_id="pr-account")
                               .set_label("Account").set_max_length(64),
                               ui.Select(component_id="pr-mode")
                               .set_label("Mode")
                               .set_options(["mention", "symbol", "both"]),
                               ui.TextInput(component_id="pr-symbol")
                               .set_label("Symbol")
                               .set_placeholder("e.g. $ (symbol/both modes)")
                               .set_max_length(8)))

    accounts = (ui.Table(component_id="adm-accounts")
                .set_columns(["Account", "Admin", "Prefix", "Commands"])
                .set_key_column("__key")
                .set_empty_text("no access rows — they appear once accounts exist")
                .set_data_source(_admin_rows_data(api))
                .set_row_actions([
                    RowAction("toggle-admin", "admin ⇄", toggle_admin_row),
                    RowAction("forget", "remove", forget_row, variant="danger",
                              confirm="Remove this account's access row "
                                      "(admin flag, permissions, prefix)?"),
                ]))

    page.add_sections(
        ui.Section(component_id="adm-top-sec")
        .add_component(ui.Grid(component_id="adm-top-grid").set_columns(2)
                       .add_component(ids_box)
                       .add_component(mudae_box)),
        ui.Section(component_id="adm-prefix-sec")
        .add_component(prefix_form),
        ui.Section(component_id="adm-accounts-sec")
        .add_component(ui.Toolbar(component_id="adm-accounts-bar")
                       .set_title("Account access rows")
                       .set_icon("M17 20h5v-2a4 4 0 0 0-3-3.87M9 20H4v-2a4 4 0 0 1 3-3.87m6-1.13a4 4 0 1 0 0-8 4 4 0 0 0 0 8z"))
        .add_component(accounts))
    return page


def _admin_ids_data(api: Any):
    async def provide(ctx):
        try:
            ov = await api.admin_overview()
        except Exception:
            ov = {}
        return {"columns": ["User id"],
                "rows": [[str(i)] for i in (ov.get("admins") or [])]}
    return provide


def _admin_rows_data(api: Any):
    async def provide(ctx):
        try:
            ov = await api.admin_overview()
            accounts = await api.accounts_list()
        except Exception:
            ov, accounts = {}, []
        live = {a.get("name") for a in (accounts or [])}
        rows = []
        for name, row in sorted((ov.get("accounts") or {}).items()):
            display = name if name in live else f"{name}  (stale)"
            perms = row.get("permissions") or {}
            granted = sum(1 for v in perms.values() if v)
            rows.append([display,
                         "yes" if row.get("is_admin") else "no",
                         str(row.get("prefix_mode", "mention"))
                         + (f" {row.get('prefix_symbol')}"
                            if row.get("prefix_symbol") else ""),
                         f"{granted}/{len(perms)}" if perms else "fail-closed",
                         name])
        return {"columns": ["Account", "Admin", "Prefix", "Commands"],
                "rows": rows}
    return provide


# ─────────────────────────────────────────────────────────────────── tools
# ─────────────────────────────────────────────────────── design gallery
def _dg_demo_button(ui, label, cid):
    """A gallery button that DEMONSTRATES the action contract instead of
    erroring with 'declares no action' (a decorative button without an
    action is a dead end for the user)."""
    def _demo(ctx, payload):
        return {"success": True,
                "message": f"{label}: demo action — real buttons wire "
                           f"set_action(callable)"}
    return ui.Button(component_id=cid).set_label(label).set_action(_demo)


def _build_design(ui: DashboardUIToolset, api: Any):
    """The component gallery: every builder component, every state and
    tone, on one page — the sandbox for judging design changes.

    Everything is static (no accounts, no live providers) except the
    interactive demos (row/bulk actions, the modal trigger, the form's
    validation), which run against fixed sample data.
    """
    page = (ui.Page(component_id="design")
            .set_title("Design")
            .set_icon("M12 2l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 14.5l-4.8 2.4.9-5.4L4.2 7.7l5.4-.8z")
            .set_description(
                "Component gallery — every component, state and tone in "
                "one place. The sandbox for design iterations."))

    # ───────────────────────────────────────────── structure tab
    structure = ui.Tab(component_id="dg-structure").set_title("Structure")

    structure.add_section(
        ui.Section(component_id="dg-sec-containers").set_title("Containers")
        .set_description("Section, Card (every tone), Header, Footer, Separator")
        .add_component(ui.Header(component_id="dg-hdr")
                       .set_title("Header")
                       .set_subtitle("Header.set_subtitle — page-level title block"))
        .add_component(ui.Grid(component_id="dg-grid-cards").set_columns(4).set_gap("8px")
            .add_component(ui.Card(component_id="dg-card-plain").set_title("Card")
                .add_component(ui.Text(component_id="dg-t-plain").set_value("plain")))
            .add_component(ui.Card(component_id="dg-card-info").set_title("info").set_tone("info")
                .add_component(ui.Text(component_id="dg-t-info").set_value("tone=info")))
            .add_component(ui.Card(component_id="dg-card-ok").set_title("ok").set_tone("ok")
                .add_component(ui.Text(component_id="dg-t-ok").set_value("tone=ok")))
            .add_component(ui.Card(component_id="dg-card-warn").set_title("warn").set_tone("warn")
                .add_component(ui.Text(component_id="dg-t-warn").set_value("tone=warn")))
            .add_component(ui.Card(component_id="dg-card-err").set_title("err").set_tone("err")
                .add_component(ui.Text(component_id="dg-t-err").set_value("tone=err"))))
        .add_component(ui.Card(component_id="dg-card-layout")
            .set_title("Rows, Columns, Grids")
            .add_component(ui.Row(component_id="dg-row1")
                .add_component(_dg_demo_button(ui, "Row item 1", "dg-b1"))
                .add_component(_dg_demo_button(ui, "Row item 2", "dg-b2"))
                .add_component(_dg_demo_button(ui, "Row item 3", "dg-b3")))
            .add_component(ui.Row(component_id="dg-row2").set_gap("16px")
                .add_component(ui.Text(component_id="dg-t-gap").set_value("gap=16px row"))
                .add_component(ui.Text(component_id="dg-t-gap2").set_value("second cell")))
            .add_component(ui.Grid(component_id="dg-grid-auto").set_columns("auto").set_min_child("180px")
                .add_component(ui.Card(component_id="dg-gc1").add_component(ui.Text(component_id="dg-gt1").set_value("auto grid cell")))
                .add_component(ui.Card(component_id="dg-gc2").add_component(ui.Text(component_id="dg-gt2").set_value("auto grid cell")))
                .add_component(ui.Card(component_id="dg-gc3").add_component(ui.Text(component_id="dg-gt3").set_value("auto grid cell"))))
            .add_component(ui.Container(component_id="dg-cont-v")
                .add_component(ui.Text(component_id="dg-t-cv").set_value("Container (vertical)"))
                .add_component(ui.Text(component_id="dg-t-cv2").set_value("stacked children")))
            .add_component(ui.Container(component_id="dg-cont-h").set_direction("horizontal")
                .add_component(_dg_demo_button(ui, "Horizontal", "dg-b-h1"))
                .add_component(_dg_demo_button(ui, "container", "dg-b-h2"))
                .add_component(_dg_demo_button(ui, "children", "dg-b-h3"))))
        .add_component(ui.Separator(component_id="dg-sep"))
        .add_component(ui.Separator(component_id="dg-sep-dash").set_variant("dashed"))
        .add_component(ui.Description(component_id="dg-desc")
                       .set_title("Description")
                       .set_value("Description — a term/definition pair for metadata lists."))
        .add_component(ui.Footer(component_id="dg-foot")
                       .set_value("Footer — page-bottom fine print")))

    structure.add_section(
        ui.Section(component_id="dg-sec-toolbar").set_title("Toolbar & Buttons")
        .set_description("Toolbar, Button variants, disabled state")
        .add_component(ui.Toolbar(component_id="dg-toolbar").set_title("Toolbar")
            .add_component(_dg_demo_button(ui, "↻ Refresh", "dg-tb-refresh"))
            .add_component(_dg_demo_button(ui, "Delete", "dg-tb-danger").set_variant("danger")))
        .add_component(ui.Row(component_id="dg-row-btns")
            .add_component(_dg_demo_button(ui, "Plain", "dg-btn-plain"))
            .add_component(_dg_demo_button(ui, "Primary", "dg-btn-primary").set_variant("primary"))
            .add_component(_dg_demo_button(ui, "Danger", "dg-btn-danger").set_variant("danger"))
            .add_component(ui.Button(component_id="dg-btn-disabled").set_label("Disabled").set_disabled(True))
            .add_component(ui.Button(component_id="dg-btn-modal").set_label("Open modal…").set_open_modal("dg-modal"))))

    page.add_modal(ui.Modal(component_id="dg-modal").set_title("Modal")
                   .set_description("Modal — viewport-centered dialog with form chrome.")
                   .add_component(ui.Text(component_id="dg-modal-text")
                                  .set_value("Modal body: any component nests here."))
                   .add_component(ui.Button(component_id="dg-modal-close").set_label("Close")
                                  .set_action(lambda ctx, payload: {"success": True,
                                                                    "message": "modal closed"})))

    page.add_tab(structure)

    # ───────────────────────────────────────────── display tab
    display = ui.Tab(component_id="dg-display").set_title("Display")

    display.add_section(
        ui.Section(component_id="dg-sec-text").set_title("Text & Code")
        .set_description("Text variants, DisplayText sizes × tones, Code")
        .add_component(ui.Text(component_id="dg-tx-plain").set_value("Text — plain body copy"))
        .add_component(ui.Text(component_id="dg-tx-dim").set_value("Text variant=muted").set_variant("muted"))
        .add_component(ui.Text(component_id="dg-tx-code").set_value("Text variant=code").set_variant("code"))
        .add_component(ui.Separator(component_id="dg-sep2"))
        .add_component(ui.Grid(component_id="dg-grid-dt").set_columns(3).set_gap("8px")
            .add_component(ui.DisplayText(component_id="dg-dt-xs").set_value("xs dim").set_size("xs").set_tone("dim"))
            .add_component(ui.DisplayText(component_id="dg-dt-sm").set_value("sm dim").set_size("sm").set_tone("dim"))
            .add_component(ui.DisplayText(component_id="dg-dt-md").set_value("md text").set_size("md"))
            .add_component(ui.DisplayText(component_id="dg-dt-lg").set_value("lg ok").set_size("lg").set_tone("ok"))
            .add_component(ui.DisplayText(component_id="dg-dt-xl").set_value("xl warn").set_size("xl").set_tone("warn"))
            .add_component(ui.DisplayText(component_id="dg-dt-xl-err").set_value("xl err").set_size("xl").set_tone("err")))
        .add_component(ui.Text(component_id="dg-code-block").set_variant("code")
                       .set_value("Code block — bounded, scrollable pane:\nline two\nline three")))

    display.add_section(
        ui.Section(component_id="dg-sec-badges").set_title("Badges & Metrics")
        .set_description("Badge variants, Metric levels, Progress")
        .add_component(ui.Row(component_id="dg-row-badges")
            .add_component(ui.Badge(component_id="dg-bdg-info").set_value("info").set_variant("info"))
            .add_component(ui.Badge(component_id="dg-bdg-ok").set_value("ok").set_variant("ok"))
            .add_component(ui.Badge(component_id="dg-bdg-warn").set_value("warn").set_variant("warn"))
            .add_component(ui.Badge(component_id="dg-bdg-err").set_value("err").set_variant("err")))
        .add_component(ui.Grid(component_id="dg-grid-metrics").set_columns(4).set_gap("8px")
            .add_component(ui.Metric(component_id="dg-m-plain").set_label("accounts").set_value("12"))
            .add_component(ui.Metric(component_id="dg-m-ok").set_label("healthy").set_value("9").set_level("ok"))
            .add_component(ui.Metric(component_id="dg-m-warn").set_label("degraded").set_value("2").set_level("warn"))
            .add_component(ui.Metric(component_id="dg-m-err").set_label("failed").set_value("1").set_level("err")))
        .add_component(ui.Metric(component_id="dg-m-sub").set_label("with sub")
                       .set_value("3.2k").set_sub("≈ 42/min sustained").set_level("ok"))
        .add_component(ui.Progress(component_id="dg-prog").set_value(62).set_label("progress 62%"))
        .add_component(ui.Progress(component_id="dg-prog-0").set_value(0).set_label("progress 0%"))
        .add_component(ui.Progress(component_id="dg-prog-100").set_value(100).set_label("progress 100%")))

    display.add_section(
        ui.Section(component_id="dg-sec-alerts").set_title("Alerts")
        .set_description("Every alert variant")
        .add_component(ui.Alert(component_id="dg-al-info").set_title("Info").set_variant("info")
                       .set_value("Something worth knowing."))
        .add_component(ui.Alert(component_id="dg-al-ok").set_title("Success").set_variant("ok")
                       .set_value("The operation completed."))
        .add_component(ui.Alert(component_id="dg-al-warn").set_title("Warning").set_variant("warn")
                       .set_value("Attention may be required."))
        .add_component(ui.Alert(component_id="dg-al-err").set_title("Error").set_variant("err")
                       .set_value("The operation failed.")))

    page.add_tab(display)

    # ───────────────────────────────────────────── inputs tab
    inputs = ui.Tab(component_id="dg-inputs").set_title("Inputs")

    form = (ui.Form(component_id="dg-form").set_submit_label("Submit demo form")
            .set_submit_action(_dg_form_submit))
    form.add_field(ui.TextInput(component_id="dg-in-text")
                       .set_label("TextInput").set_placeholder("type here…")
                       .set_required(True).set_max_length(40))
    form.add_field(ui.TextInput(component_id="dg-in-disabled")
                       .set_label("TextInput (disabled)").set_value("not editable")
                       .set_disabled(True))
    form.add_field(ui.NumberInput(component_id="dg-in-num")
                       .set_label("NumberInput").set_value("7")
                       .set_min_value(0).set_max_value(60).set_step(1))
    form.add_field(ui.Textarea(component_id="dg-in-area")
                       .set_label("Textarea").set_placeholder("multi-line…").set_rows(3))
    form.add_field(ui.Select(component_id="dg-in-select").set_label("Select")
                       .set_placeholder("choose…")
                       .set_options([{"value": "a", "label": "Option A"},
                                     {"value": "b", "label": "Option B"},
                                     {"value": "c", "label": "Option C"}]))
    form.add_field(ui.MultiSelect(component_id="dg-in-multi").set_label("MultiSelect")
                       .set_options([{"value": "x", "label": "Choice X"},
                                     {"value": "y", "label": "Choice Y"},
                                     {"value": "z", "label": "Choice Z"}])
                       .set_value(["x"]))
    form.add_field(ui.Checkbox(component_id="dg-in-check").set_label("Checkbox")
                       .set_description("a boolean choice").set_value(True))
    form.add_field(ui.Toggle(component_id="dg-in-toggle").set_label("Toggle")
                       .set_description("the switch-skinned boolean"))
    form.add_field(ui.RadioGroup(component_id="dg-in-radio").set_label("RadioGroup")
                       .set_options([{"value": "one", "label": "One"},
                                     {"value": "two", "label": "Two"}])
                       .set_value("one"))

    inputs.add_section(
        ui.Section(component_id="dg-sec-form").set_title("Form — every input")
        .set_description("Required validation fires on submit; disabled states shown")
        .add_component(form))

    page.add_tab(inputs)

    # ───────────────────────────────────────────── data tab
    data = ui.Tab(component_id="dg-data").set_title("Data")

    async def _rows(ctx=None):
        return {"columns": ["Name", "Status", "Seen"],
                "rows": [["alpha", "online", "2m"], ["beta", "offline", "1h"],
                         ["gamma", "cooldown", "9m"], ["delta", "online", "now"]]}

    async def _records(ctx=None):
        tones = {"online": "ok", "offline": "muted", "cooldown": "warn"}
        return {"records": [
            {"key": name, "cells": [
                {"primary": name, "secondary": seen},
                {"text": status, "tone": tones.get(status, "muted")},
                {"chips": ["probe"]}, "—"]}
            for name, status, seen in [
                ("alpha", "online", "2m"), ("beta", "offline", "1h"),
                ("gamma", "cooldown", "9m")]]}

    async def _no_records(ctx=None):
        return {"records": []}

    async def _tiles(ctx=None):
        return [{"title": "libo", "status": "online", "meta": "12 plugins",
                 "chips": ["probe", "soulcopy"], "lines": [["uptime", "3h 12m"]]},
                {"title": "libalt30", "status": "cooldown", "meta": "8 plugins",
                 "chips": ["probe"], "lines": [["uptime", "41m"]], "tone": "warn"},
                {"title": "libalt21", "status": "offline", "meta": "—",
                 "chips": [], "lines": [["uptime", "—"]], "tone": "err"}]

    def _poke(row, ctx):
        return {"success": True, "message": f"poked {row}"}

    data.add_section(
        ui.Section(component_id="dg-sec-table").set_title("Table")
        .set_description("Row actions + bulk actions + pagination (page size 2)")
        .add_component(ui.Table(component_id="dg-table").set_columns(["Name", "Status", "Seen"])
                       .set_data_source(_rows).set_page_size(2)
                       .set_row_actions([ui.RowAction("poke", "Poke", _poke)])
                       .set_bulk_actions([ui.BulkAction("poke-all", "Poke all",
                                                        lambda rows, ctx: {"success": True,
                                                                           "message": f"poked {len(rows)}"})])))

    data.add_section(
        ui.Section(component_id="dg-sec-tiles").set_title("TileView")
        .set_description("Status pills, meta rows, chips, tone borders")
        .add_component(ui.TileView(component_id="dg-tiles").set_data_source(_tiles)))

    data.add_section(
        ui.Section(component_id="dg-sec-records").set_title("ListView — records mode")
        .set_description("The record-list contract: identity | status | meta | actions")
        .add_component(ui.ListView(component_id="dg-records")
                       .set_columns(["Account", "Status", "Plugins", "Group"])
                       .set_key_column("Account")
                       .set_data_source(_records)
                       .set_empty_text("no records")))

    data.add_section(
        ui.Section(component_id="dg-sec-empty").set_title("Empty states")
        .set_description("Every data component's empty_text")
        .add_component(ui.Table(component_id="dg-table-empty").set_columns(["A", "B"])
                       .set_data_source(lambda ctx=None: {"columns": ["A", "B"], "rows": []})
                       .set_empty_text("nothing here yet"))
        .add_component(ui.ListView(component_id="dg-records-empty")
                       .set_columns(["A", "B"]).set_key_column("A")
                       .set_data_source(_no_records)
                       .set_empty_text("no records match")))

    page.add_tab(data)
    return page


async def _dg_form_submit(ctx, payload):
    """The gallery form's validation demo: a missing required field
    surfaces as a field error; anything else succeeds."""
    if not str(payload.get("dg-in-text") or "").strip():
        return {"success": False, "field_errors": {
            "dg-in-text": "This field is required (validation demo)."}}
    return {"success": True,
            "message": f"submitted: {sorted(payload)[:3]}…",
            "refresh": []}


def _build_tools(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="tools")
            .set_title("Tools")
            .set_icon("M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z")
            .set_description(
                "Scenario simulation, fleet-wide controls and the dashboard "
                "session token."))

    last_report: Dict[str, Any] = {"log": ""}

    async def scenarios_text(ctx=None):
        try:
            scenarios = await api.simulation_list()
        except Exception:
            scenarios = []
        names = [s.get("name", "?") if isinstance(s, dict) else str(s)
                 for s in (scenarios or [])]
        return {"value": "\n".join(names) or "— no scenarios discovered —"}

    async def run_scenario(ctx, payload):
        name = str(payload.get("sim-name") or "").strip()
        if not name:
            return {"success": False, "field_errors": {
                "sim-name": "Type a scenario name from the list."}}
        report = await api.simulation_run(name)
        last_report["log"] = ((report or {}).get("log")
                              or "\n".join(str(x) for x in
                                           ((report or {}).get("steps") or []))
                              or "completed")
        return {"success": True, "message": f"scenario {name} finished",
                "refresh": ["sim-report"]}

    async def estop(ctx=None, payload=None):
        await api.emergency_stop_all(surface="dashboard")
        return {"success": True,
                "message": "emergency stop requested for the fleet"}

    async def resume(ctx=None, payload=None):
        await api.system_resume()
        return {"success": True, "message": "resume requested"}

    async def import_env(ctx=None, payload=None):
        await api.credentials_import_env(surface="dashboard")
        return {"success": True, "message": "env import requested"}

    (page
     .add_section(ui.Section(component_id="sim-sec", title="Simulation")
        .add_component(ui.Grid(component_id="sim-row").set_columns(2)
           .add_component(ui.Form(component_id="sim-form")
                          .set_description("Run a scenario against the simulated "
                                           "stack (no live accounts).")
                          .set_submit_action(run_scenario)
                          .set_submit_label("Run scenario")
                          .add_fields(ui.TextInput(component_id="sim-name")
                                      .set_label("Scenario name")
                                      .set_placeholder("see the list →")
                                      .set_max_length(64)))
           .add_component(ui.Text(component_id="sim-list").set_variant("code")
                          .set_value("…").set_data_source(scenarios_text)))
        .add_component(ui.Text(component_id="sim-report").set_variant("code")
                       .set_value("— no scenario run yet —")
                       .set_data_source(lambda ctx: {"value": last_report["log"]
                                                     or "— no scenario run yet —"})))
     .add_section(ui.Section(component_id="adv-sec")
        .add_component(ui.Toolbar(component_id="adv-bar")
                       .set_title("Fleet controls")
                       .set_icon("M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"))
        .add_component(ui.Grid(component_id="adv-row")
                       .set_columns("auto").set_min_child("200px")
           .add_component(ui.Button(component_id="adv-estop")
                          .set_label("■ EMERGENCY STOP").set_variant("danger")
                          .set_confirm("GLOBAL EMERGENCY STOP — this affects ALL "
                                       "running accounts and tasks.")
                          .set_action(estop))
           .add_component(ui.Button(component_id="adv-resume")
                          .set_label("▶ Resume").set_variant("secondary")
                          .set_action(resume))
           .add_component(ui.Button(component_id="adv-import")
                          .set_label("Import env credentials").set_variant("ghost")
                          .set_description("One-time import of legacy .env tokens "
                                           "into the encrypted store.")
                          .set_action(import_env))))
     .add_section(ui.Section(component_id="theme-sec", title="Appearance")
        .add_component(ui.Form(component_id="theme-form")
                       .set_description("Decorative only — accent hue and "
                                        "density, applied instantly in this "
                                        "browser. Layout never changes.")
                       .set_columns(2)
                       .set_client_action("save-theme")
                       .set_submit_label("Apply")
                       .add_fields(ui.Select(component_id="theme-accent")
                                   .set_label("Accent")
                                   .set_options(["blue", "violet", "green",
                                                 "amber", "rose", "cyan"]),
                                   ui.Select(component_id="theme-density")
                                   .set_label("Density")
                                   .set_options(["comfortable", "compact"]))))
     .add_section(ui.Section(component_id="token-sec", title="Dashboard session token")
        .add_component(ui.Form(component_id="token-form")
                       .set_description("Stored only in this browser's sessionStorage "
                                        "— never sent anywhere except the auth header.")
                       .set_client_action("save-token")
                       .set_submit_label("Save & reconnect")
                       .add_fields(ui.TextInput(component_id="token-value")
                                   .set_label("Token")
                                   .set_placeholder("paste the dashboard token")))))
    return page


# ──────────────────────────────────────────────────────────────────── fleet
def _build_fleet(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="fleet")
            .set_title("Accounts & Groups")
            .set_icon("M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75")
            .set_description(
                "The fleet: every Discord account with runtime state, and "
                "the sync groups that coordinate their pacing. Bulk-select "
                "accounts for fleet-wide actions."))

    async def _accts():
        st = await _status(api)
        return st.get("accounts") or []

    async def _groups():
        try:
            return await api.groups_list() or []
        except Exception:
            return []

    # ── account actions ────────────────────────────────────────────
    async def toggle_run(row, ctx):
        st = await _status(api)
        acct = next((a for a in st.get("accounts") or []
                     if a.get("name") == row), None)
        running = acct and acct.get("status") == "running"
        await api.accounts_start(row, surface="dashboard") if not running \
            else await api.accounts_stop(row, surface="dashboard")
        return {"success": True,
                "message": f"{row}: {'stop' if running else 'start'} requested"}

    async def open_details(row, ctx):
        _fleet_state["details"] = row
        return {"open_modal": "acct-details-modal",
                "refresh": ["ad-status", "ad-lines", "ad-plugins"]}

    async def open_cred(row, ctx):
        _fleet_state["cred"] = row
        return {"open_modal": "acct-cred-modal"}

    async def open_move(row, ctx):
        _fleet_state["move"] = [row]
        return {"open_modal": "move-modal", "refresh": ["mv-group"]}

    async def open_assign(row, ctx):
        _fleet_state["assign"] = row
        return {"open_modal": "assign-modal", "refresh": ["assign-plugins"]}

    async def estop_row(row, ctx):
        await api.accounts_emergency_stop(row, surface="dashboard")
        return {"success": True, "message": f"emergency stop requested for {row}"}

    async def remove_row(row, ctx):
        await api.accounts_remove(row, surface="dashboard")
        return {"success": True, "message": f"{row} removed",
                "refresh": ["acct-list", "group-list"]}

    async def bulk_run(rows, ctx, start=True):
        calls = [(api.accounts_start if start else api.accounts_stop)
                 (n, surface="dashboard") for n in rows]
        results = await _run_all_static(calls)
        failed = [str(e) for e in results if isinstance(e, Exception)]
        if failed:
            return {"success": False,
                    "message": f"{len(results)-len(failed)}/{len(results)} succeeded — {'; '.join(failed[:3])}"}
        return {"success": True,
                "message": f"{len(results)} request{'s' if len(results) != 1 else ''} sent"}

    async def _run_all_static(calls):
        import asyncio
        return await asyncio.gather(*calls, return_exceptions=True)

    async def bulk_start(rows, ctx):
        return await bulk_run(rows, ctx, start=True)

    async def bulk_stop(rows, ctx):
        return await bulk_run(rows, ctx, start=False)

    async def bulk_move(rows, ctx):
        _fleet_state["move"] = list(rows)
        return {"open_modal": "move-modal", "refresh": ["mv-group"]}

    # ── form submits ───────────────────────────────────────────────
    async def submit_add(ctx, payload):
        name = str(payload.get("fa-name") or "").strip()
        if not name:
            return {"success": False, "field_errors": {
                "fa-name": "Account name is required."}}
        body: Dict[str, Any] = {"name": name}
        group = str(payload.get("fa-group") or "").strip()
        if group and group != "__independent__":
            body["sync_group"] = group
        plugins = payload.get("fa-plugins") or []
        if isinstance(plugins, list):
            body["plugins"] = plugins
        cred = str(payload.get("fa-cred") or "").strip()
        if cred:
            body["credential"] = cred
        await api.accounts_add(body, surface="dashboard")
        return {"success": True, "message": f"account {name} created",
                "refresh": ["acct-list", "group-list"]}

    async def submit_cred(ctx, payload):
        name = _fleet_state.get("cred") or ""
        cred = str(payload.get("fc-cred") or "").strip()
        if not name:
            return {"success": False, "message": "no account selected"}
        if not cred:
            return {"success": False, "field_errors": {
                "fc-cred": "Paste the Discord token."}}
        # one-shot: the selection is consumed, so a later submit from a
        # modal that was never re-opened cannot silently overwrite this
        # account's credential again
        _fleet_state["cred"] = None
        await api.accounts_set_credential(name, cred, surface="dashboard")
        return {"success": True, "message": f"credential stored for {name}",
                "refresh": ["acct-list", "ad-lines"]}

    async def submit_move(ctx, payload):
        names = _fleet_state.get("move") or []
        group = str(payload.get("mv-group") or "").strip()
        if not names:
            return {"success": False, "message": "nothing to move"}
        results = await _run_all_static([
            (api.groups_clear_account(n, surface="dashboard")
             if group in ("", "__independent__")
             else api.groups_set_account(n, group, surface="dashboard"))
            for n in names])
        failed = [str(e) for e in results if isinstance(e, Exception)]
        if failed:
            return {"success": False,
                    "message": f"{len(results)-len(failed)}/{len(results)} moved — {'; '.join(failed[:3])}"}
        target = "independent" if group in ("", "__independent__") else group
        return {"success": True,
                "message": f"{len(names)} moved to {target}",
                "refresh": ["acct-list", "group-list"]}

    async def submit_assign(ctx, payload):
        name = _fleet_state.get("assign") or ""
        if not name:
            return {"success": False, "message": "no account selected"}
        plugins = payload.get("as-plugins") or []
        if not isinstance(plugins, list):
            plugins = []
        await api.accounts_plugins_set(name, plugins, surface="dashboard")
        return {"success": True,
                "message": f"{name}: {len(plugins)} plugin(s) assigned",
                "refresh": ["acct-list", "ad-plugins", "plug-state"]}

    # ── providers ──────────────────────────────────────────────────
    def _acct_records(api):
        """The account LIST: one aligned record row per account.

        Columns follow the list-row contract: identity (name + id),
        status (badge + runtime meta), group, plugins. Actions ride the
        trailing zone with a deliberate hierarchy: run = primary,
        management = ghost, destructive = danger.
        """
        async def provide(ctx):
            rows = await _accts()
            records = []
            for a in rows:
                status = a.get("status") or "unknown"
                tone = {"running": "ok", "failed": "err",
                        "invalid_credential": "err",
                        "missing_credential": "warn"}.get(status, "info")
                if not a.get("credential") and tone not in ("err", "warn"):
                    tone = "warn"   # missing credential always flags
                meta_bits = [f"🔑 {a.get('credential') or 'missing'}"]
                if a.get("uptime_s"):
                    meta_bits.append(f"⏱ {_fmt_uptime(a['uptime_s'])}")
                if a.get("latency_ms") is not None:
                    meta_bits.append(f"⚡ {a['latency_ms']:.0f}ms")
                if a.get("cooldowns"):
                    meta_bits.append(f"⏳ {a['cooldowns']}")
                records.append({
                    "key": a.get("name", "?"),
                    "cells": [
                        {"primary": a.get("name", "?"),
                         "secondary": str(a.get("user_id") or a.get("discord_id") or "")},
                        {"text": status, "tone": tone,
                         "meta": " · ".join(meta_bits)},
                        {"chips": ([{"text": a["sync_group"], "kind": "group"}]
                                   if a.get("sync_group") else [])},
                        {"chips": [{"text": p, "kind": "plugin"}
                                   for p in (a.get("assigned_plugins") or [])]},
                    ]})
            return {"records": records}
        return provide

    def _details_status(api):
        async def provide(ctx):
            name = _fleet_state.get("details")
            if not name:
                return {"value": "no account selected"}
            rows = await _accts()
            a = next((x for x in rows if x.get("name") == name), None)
            if not a:
                return {"value": f"{name} — not found (removed?)"}
            return {"value": f"{a.get('status', '?')} · id {a.get('user_id', '?')} · "
                            f"{'enabled' if a.get('enabled', True) else 'disabled'}",
                    "level": "ok" if a.get("status") == "running" else "err"}
        return provide

    def _details_lines(api):
        async def provide(ctx):
            name = _fleet_state.get("details")
            rows = await _accts()
            a = next((x for x in rows if x.get("name") == name), None)
            if not a:
                return []
            out = []
            if a.get("latency_ms") is not None:
                out.append({"label": "Latency", "value": f"{a['latency_ms']:.0f}ms"})
            out.append({"label": "Uptime", "value": _fmt_uptime(a.get("uptime_s") or 0)})
            out.append({"label": "Credential",
                        "value": a.get("credential") or "missing"})
            out.append({"label": "Sync group", "value": a.get("sync_group") or "independent"})
            return out
        return provide

    async def unassign_plugin(row, ctx):
        # row arrives as "account|plugin" via the __key cell
        account, _, plugin = row.partition("|")
        await api.accounts_plugin_unassign(account, plugin, surface="dashboard")
        return {"success": True, "message": f"{plugin} unassigned from {account}",
                "refresh": ["ad-plugins", "acct-list"]}

    def _details_plugins_rows(api):
        async def provide(ctx):
            name = _fleet_state.get("details")
            rows = await _accts()
            a = next((x for x in rows if x.get("name") == name), None)
            if not a:
                return {"columns": ["Plugin"], "rows": []}
            return {"columns": ["Plugin", "Key"],
                    "rows": [[p, f"{name}|{p}"]
                             for p in (a.get("assigned_plugins") or [])]}
        return provide

    def _group_options(api):
        async def provide(ctx):
            groups = await _groups()
            return {"options": [{"value": "__independent__",
                                 "label": "Independent / no group"}] +
                               [{"value": g["name"], "label": g["name"]}
                                for g in groups]}
        return provide

    def _plugin_options(api):
        async def provide(ctx):
            try:
                manifests = await api.plugins_manifests()
            except Exception:
                manifests = []
            return {"options": [{"value": m.get("name", "?"),
                                 "label": m.get("name", "?")}
                                for m in (manifests or [])]}
        return provide

    def _assign_plugins(api):
        async def provide(ctx):
            name = _fleet_state.get("assign")
            rows = await _accts()
            a = next((x for x in rows if x.get("name") == name), None)
            current = set((a or {}).get("assigned_plugins") or [])
            try:
                manifests = await api.plugins_manifests()
            except Exception:
                manifests = []
            return {"options": [{"value": m.get("name", "?"),
                                 "label": m.get("name", "?")}
                                for m in (manifests or [])],
                    "value": sorted(current)}
        return provide

    # ── group actions ──────────────────────────────────────────────
    async def group_details(row, ctx):
        _fleet_state["group_details"] = row
        return {"open_modal": "group-details-modal",
                "refresh": ["gd-lines", "gd-members"]}

    async def group_edit(row, ctx):
        _fleet_state["group_edit"] = row
        return {"open_modal": "group-edit-modal"}

    async def group_addmember(row, ctx):
        _fleet_state["group_members"] = row
        return {"open_modal": "group-member-modal",
                "refresh": ["gm-accounts"]}

    async def group_delete(row, ctx):
        try:
            groups = await _groups()
        except Exception:
            groups = []
        g = next((x for x in groups if x.get("name") == row), None)
        count = (g or {}).get("member_count", 0) or 0
        await api.groups_delete(row, detach_members=bool(count),
                                surface="dashboard")
        return {"success": True, "message": f"group {row} deleted",
                "refresh": ["group-list", "acct-list"]}

    async def group_bulk_run(row, ctx, start=True):
        try:
            snap = await api.groups_get(row)
        except Exception:
            return {"success": False, "message": f"unknown group {row}"}
        members = [m.get("name") for m in (snap.get("members") or [])]
        if not members:
            return {"success": False, "message": "group is empty"}
        return await bulk_run(members, ctx, start=start)

    async def group_start(row, ctx):
        return await group_bulk_run(row, ctx, start=True)

    async def group_stop(row, ctx):
        return await group_bulk_run(row, ctx, start=False)

    async def submit_group_create(ctx, payload):
        name = str(payload.get("gc-name") or "").strip()
        if not name:
            return {"success": False, "field_errors": {
                "gc-name": "Group name is required."}}
        await api.groups_create(
            {"name": name,
             "description": str(payload.get("gc-desc") or ""),
             "enabled": payload.get("gc-enabled") is not False},
            surface="dashboard")
        return {"success": True, "message": f"group {name} created",
                "refresh": ["group-list"]}

    async def submit_group_edit(ctx, payload):
        group = _fleet_state.get("group_edit") or ""
        if not group:
            return {"success": False, "message": "no group selected"}
        body: Dict[str, Any] = {}
        nn = str(payload.get("ge-name") or "").strip()
        if nn and nn != group:
            body["name"] = nn
        desc = str(payload.get("ge-desc") or "")
        if desc:
            body["description"] = desc
        body["enabled"] = payload.get("ge-enabled") is not False
        await api.groups_update(group, body, surface="dashboard")
        return {"success": True, "message": f"group {group} updated",
                "refresh": ["group-list", "gd-lines"]}

    async def submit_group_member(ctx, payload):
        group = _fleet_state.get("group_members") or ""
        chosen = payload.get("gm-accounts") or []
        if not group:
            return {"success": False, "message": "no group selected"}
        if not chosen:
            return {"success": False, "field_errors": {
                "gm-accounts": "Select at least one account."}}
        results = await _run_all_static([
            api.groups_add_account(group, n, surface="dashboard") for n in chosen])
        failed = [str(e) for e in results if isinstance(e, Exception)]
        if failed:
            return {"success": False,
                    "message": f"{len(results)-len(failed)}/{len(results)} added — {'; '.join(failed[:3])}"}
        return {"success": True, "message": f"{len(chosen)} member(s) added to {group}",
                "refresh": ["group-list", "gd-members", "acct-list"]}

    async def remove_member(row, ctx):
        group, _, account = row.partition("|")
        await api.groups_remove_account(group, account, surface="dashboard")
        return {"success": True, "message": f"{account} removed from {group}",
                "refresh": ["group-list", "gd-members", "acct-list"]}

    def _group_records(api):
        """Sync groups as aligned record rows (same list contract)."""
        async def provide(ctx):
            groups = await _groups()
            rows = await _accts()
            records = []
            for g in groups:
                name = g.get("name", "?")
                members = [a for a in rows if a.get("sync_group") == name]
                online = sum(1 for m in members if m.get("status") == "running")
                count = g.get("member_count", len(members)) or len(members)
                tone = ("info" if g.get("enabled") is False
                        else ("ok" if members and online == len(members)
                              else ("err" if online == 0 and members else "warn")))
                member_chips = [{"text": m["name"], "kind": "plugin"}
                                for m in members[:6]]
                if len(members) > 6:
                    member_chips.append({"text": f"+{len(members)-6} more"})
                status_txt = (f"{online}/{count} online" if count else "empty")
                if g.get("enabled") is False:
                    status_txt += " · disabled"
                records.append({
                    "key": name,
                    "cells": [
                        {"primary": name,
                         "secondary": g.get("description") or ""},
                        {"text": status_txt, "tone": tone,
                         "meta": f"{count} member{'' if count == 1 else 's'}"},
                        {"chips": member_chips},
                    ]})
            return {"records": records}
        return provide

    def _gd_lines(api):
        async def provide(ctx):
            name = _fleet_state.get("group_details")
            if not name:
                return []
            try:
                snap = await api.groups_get(name)
            except Exception:
                return [{"label": name, "value": "not found"}]
            out = [{"label": "State",
                    "value": "enabled" if snap.get("enabled", True) else "disabled"}]
            if snap.get("description"):
                out.append({"label": "Description", "value": snap["description"]})
            if snap.get("pacing_key"):
                out.append({"label": "Pacing", "value": snap["pacing_key"]})
            members = snap.get("members") or []
            online = sum(1 for m in members if m.get("status") == "running")
            out.append({"label": "Online", "value": f"{online}/{len(members)}"})
            return out
        return provide

    def _gd_members(api):
        async def provide(ctx):
            name = _fleet_state.get("group_details")
            if not name:
                return {"columns": ["Member", "Status", "Key"], "rows": []}
            try:
                snap = await api.groups_get(name)
            except Exception:
                return {"columns": ["Member", "Status", "Key"], "rows": []}
            rows = []
            for m in (snap.get("members") or []):
                rows.append([m.get("name", "?"), m.get("status", "?"),
                             f"{name}|{m.get('name', '?')}"])
            return {"columns": ["Member", "Status", "Key"], "rows": rows}
        return provide

    def _gm_accounts(api):
        async def provide(ctx):
            group = _fleet_state.get("group_members") or ""
            rows = await _accts()
            cands = [a for a in rows if a.get("sync_group") != group]
            return {"options": [{"value": a["name"],
                                 "label": f"{a['name']} ({a.get('sync_group') or 'independent'})"}
                                for a in cands]}
        return provide

    # ── assemble ───────────────────────────────────────────────────
    (page
     .add_section(ui.Section(component_id="fleet-accts-sec")
        .add_component(ui.Toolbar(component_id="fleet-accts-bar")
                       .set_title("Accounts")
                       .set_icon("M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75")
                       .add_component(ui.Button(component_id="fleet-add")
                                      .set_label("+ Add Account")
                                      .set_variant("primary")
                                      .set_open_modal("fleet-add-modal")))
        .add_component(ui.ListView(component_id="acct-list")
                       .set_columns(["Account", "Status", "Group", "Plugins"])
                       .set_key_column("key")
                       .set_empty_text("No accounts yet — add your first "
                                       "Discord account.")
                       .set_data_source(_acct_records(api))
                       .set_row_actions([
                           RowAction("run", "Start/Stop", toggle_run,
                                     variant="primary"),
                           RowAction("details", "Details", open_details,
                                     variant="ghost"),
                           RowAction("cred", "Credential", open_cred,
                                     variant="ghost"),
                           RowAction("group", "Group", open_move,
                                     variant="ghost"),
                           RowAction("plugins", "Plugins", open_assign,
                                     variant="ghost"),
                           RowAction("estop", "E-stop", estop_row, variant="danger",
                                     confirm="Emergency stop this account?"),
                           RowAction("remove", "Remove", remove_row, variant="danger",
                                     confirm="Remove this account? Deletes its "
                                             "credential, cooldowns and state."),
                       ])
                       .set_bulk_actions([
                           BulkAction("start", "Start", bulk_start),
                           BulkAction("stop", "Stop", bulk_stop),
                           BulkAction("move", "Move to group…", bulk_move),
                       ]))
        )
     .add_section(ui.Section(component_id="fleet-groups-sec")
        .add_component(ui.Toolbar(component_id="fleet-groups-bar")
                       .set_title("Sync Groups")
                       .set_icon("M17 20h5v-2a4 4 0 0 0-3-3.87M9 20H4v-2a4 4 0 0 1 3-3.87m6-1.13a4 4 0 1 0 0-8 4 4 0 0 0 0 8z")
                       .add_component(ui.Button(component_id="fleet-group-add")
                                      .set_label("+ Create Group")
                                      .set_variant("primary")
                                      .set_open_modal("group-create-modal")))
        .add_component(ui.ListView(component_id="group-list")
                       .set_columns(["Group", "Status", "Members"])
                       .set_key_column("key")
                       .set_empty_text("No sync groups — create one to let "
                                       "accounts coordinate their pacing.")
                       .set_data_source(_group_records(api))
                       .set_row_actions([
                           RowAction("open", "Open", group_details,
                                     variant="primary"),
                           RowAction("edit", "Edit", group_edit, variant="ghost"),
                           RowAction("addmember", "+ Member", group_addmember,
                                     variant="ghost"),
                           RowAction("start", "Start all", group_start,
                                     variant="ghost"),
                           RowAction("stop", "Stop all", group_stop,
                                     variant="ghost"),
                           RowAction("delete", "Delete", group_delete, variant="danger",
                                     confirm="Delete this group? Accounts are never "
                                             "deleted — members return to independent "
                                             "pacing."),
                       ])))
     # ── modals ──
     .add_modal(ui.Modal(component_id="fleet-add-modal").set_title("Add Account")
        .add_component(ui.Form(component_id="fleet-add-form")
                       .set_description("The token is stored encrypted — never "
                                        "displayed back.")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_add).set_submit_label("Create Account")
                       .add_fields(ui.TextInput(component_id="fa-name")
                                   .set_label("Account name").set_max_length(32)
                                   .set_required(True),
                                   ui.Select(component_id="fa-group").set_label("Sync group")
                                   .set_options_provider(_group_options(api)),
                                   ui.MultiSelect(component_id="fa-plugins")
                                   .set_label("Plugins")
                                   .set_options_provider(_plugin_options(api)),
                                   ui.TextInput(component_id="fa-cred").set_label("Token")
                                   .set_placeholder("Discord token (optional now)"))))
     .add_modal(ui.Modal(component_id="acct-details-modal").set_title("Account")
        .add_component(ui.Metric(component_id="ad-status").set_label("State")
                       .set_value("—").set_data_source(_details_status(api)))
        .add_component(ui.ListView(component_id="ad-lines")
                       .set_data_source(_details_lines(api)))
        .add_component(ui.Toolbar(component_id="ad-plugins-bar")
                       .set_title("Assigned plugins"))
        .add_component(ui.Table(component_id="ad-plugins")
                       .set_columns(["Plugin"]).set_key_column("__key")
                       .set_empty_text("none assigned")
                       .set_data_source(_details_plugins_rows(api))
                       .set_row_actions([
                           RowAction("unassign", "✕", unassign_plugin, variant="danger",
                                     confirm="Unassign this plugin?")])))
     .add_modal(ui.Modal(component_id="acct-cred-modal").set_title("Set Credential")
        .add_component(ui.Form(component_id="acct-cred-form")
                       .set_description("Encrypted at rest; never displayed back.")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_cred).set_submit_label("Save")
                       .add_fields(ui.TextInput(component_id="fc-cred")
                                   .set_label("Discord token").set_required(True))))
     .add_modal(ui.Modal(component_id="move-modal").set_title("Move to group")
        .add_component(ui.Form(component_id="move-form")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_move).set_submit_label("Move")
                       .add_fields(ui.Select(component_id="mv-group").set_label("Group")
                                   .set_options_provider(_group_options(api)))))
     .add_modal(ui.Modal(component_id="assign-modal").set_title("Assign Plugins")
        .add_component(ui.Form(component_id="assign-form")
                       .set_description("Empty selection is valid — the account "
                                        "runs nothing.")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_assign).set_submit_label("Save")
                       .add_fields(ui.MultiSelect(component_id="as-plugins")
                                   .set_label("Plugins")
                                   .set_options_provider(_assign_plugins(api)))))
     .add_modal(ui.Modal(component_id="group-create-modal").set_title("Create Group")
        .add_component(ui.Form(component_id="group-create-form")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_group_create)
                       .set_submit_label("Create Group")
                       .add_fields(ui.TextInput(component_id="gc-name")
                                   .set_label("Group name").set_max_length(32)
                                   .set_required(True),
                                   ui.TextInput(component_id="gc-desc")
                                   .set_label("Description").set_max_length(200),
                                   ui.Toggle(component_id="gc-enabled")
                                   .set_label("Group is active").set_value(True))))
     .add_modal(ui.Modal(component_id="group-edit-modal").set_title("Edit Group")
        .add_component(ui.Form(component_id="group-edit-form")
                       .set_description("Renaming moves every member and their "
                                        "shared pacing to the new name.")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_group_edit).set_submit_label("Save")
                       .add_fields(ui.TextInput(component_id="ge-name")
                                   .set_label("Name").set_max_length(32),
                                   ui.TextInput(component_id="ge-desc")
                                   .set_label("Description").set_max_length(200),
                                   ui.Toggle(component_id="ge-enabled")
                                   .set_label("Group is active").set_value(True))))
     .add_modal(ui.Modal(component_id="group-details-modal").set_title("Group")
        .add_component(ui.ListView(component_id="gd-lines")
                       .set_data_source(_gd_lines(api)))
        .add_component(ui.Toolbar(component_id="gd-members-bar")
                       .set_title("Members"))
        .add_component(ui.Table(component_id="gd-members")
                       .set_columns(["Member", "Status"]).set_key_column("__key")
                       .set_empty_text("empty group")
                       .set_data_source(_gd_members(api))
                       .set_row_actions([
                           RowAction("remove", "remove", remove_member, variant="danger"),
                       ])))
     .add_modal(ui.Modal(component_id="group-member-modal").set_title("Add Member")
        .add_component(ui.Form(component_id="group-member-form")
                       .set_description("Adding an account REPLACES its previous "
                                        "group membership.")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_group_member).set_submit_label("Add")
                       .add_fields(ui.MultiSelect(component_id="gm-accounts")
                                   .set_label("Accounts")
                                   .set_options_provider(_gm_accounts(api))))))
    return page


# ──────────────────────────────────────────────────────────────── profiles
def _build_profiles(ui: DashboardUIToolset, api: Any):
    page = (ui.Page(component_id="profiles")
            .set_title("Profiles")
            .set_icon("M12 2L2 7l10 5 10-5-10-5zM2 12l10 5 10-5M2 17l10 5 10-5")
            .set_description(
                "Per-plugin profiles: shared settings bundles with channel "
                "scopes, bound per account. Pick an account to manage its "
                "bindings."))

    async def _accts():
        st = await _status(api)
        return st.get("accounts") or []

    def _account_options(api):
        async def provide(ctx):
            rows = await _accts()
            if not _profiles_state.get("account") and rows:
                _profiles_state["account"] = rows[0].get("name")
            return {"options": [{"value": a["name"], "label": a["name"]}
                                for a in rows]}
        return provide

    async def select_account(ctx, payload):
        _profiles_state["account"] = str(payload.get("value") or "").strip() or None
        return {"success": True,
                "message": f"showing {_profiles_state['account'] or 'no account'}",
                "refresh": ["prof-tiles"]}

    def _bound_map():
        async def fetch():
            account = _profiles_state.get("account")
            if not account:
                return {}
            try:
                return await api.accounts_plugin_profiles(account) or {}
            except Exception:
                return {}
        return fetch

    def _prof_tiles(api):
        async def provide(ctx):
            account = _profiles_state.get("account")
            if not account:
                return {"tiles": []}
            rows = await _accts()
            a = next((x for x in rows if x.get("name") == account), None)
            assigned = (a or {}).get("assigned_plugins") or []
            try:
                bindings = await api.accounts_plugin_profiles(account) or {}
            except Exception:
                bindings = {}
            try:
                profiles = await api.plugin_profiles_list() or []
            except Exception:
                profiles = []
            counts: Dict[str, int] = {}
            for p in profiles:
                counts[p.get("plugin", "?")] = counts.get(p.get("plugin", "?"), 0) + 1
            tiles = []
            for pname in assigned:
                bound = bindings.get(pname)
                try:
                    manifests = await api.plugins_manifests()
                except Exception:
                    manifests = []
                m = next((x for x in manifests if x.get("name") == pname), None)
                tiles.append({
                    "key": pname, "title": pname,
                    "status": "bound" if bound else "default",
                    "tone": "ok" if bound else "info",
                    "meta": [["🏷", f"v{(m or {}).get('version', '?')}"],
                             ["📚", f"{counts.get(pname, 0)} profile(s) in library"]],
                    "chips": [{"text": bound, "kind": "plugin"}] if bound else []})
            return {"tiles": tiles}
        return provide

    async def open_bind(row, ctx):
        _profiles_state["bind"] = row
        return {"open_modal": "bind-modal", "refresh": ["bp-profile"]}

    async def open_editor(row, ctx):
        _profiles_state["editor"] = {"plugin": row, "name": None, "settings": {}}
        return {"open_modal": "editor-modal", "refresh": ["editor-form"]}

    async def edit_profile(row, ctx):
        plugin, _, name = row.partition("|")
        settings: Dict[str, Any] = {}
        if name:
            try:
                profiles = await api.plugin_profiles_list(plugin) or []
                prof = next((x for x in profiles if x.get("name") == name), None)
                settings = dict((prof or {}).get("settings") or {})
            except Exception:
                settings = {}
        _profiles_state["editor"] = {"plugin": plugin, "name": name,
                                     "settings": settings}
        return {"open_modal": "editor-modal", "refresh": ["editor-form"]}

    async def unbind(row, ctx):
        account = _profiles_state.get("account")
        if not account:
            return {"success": False, "message": "select an account first"}
        await api.accounts_plugin_profile(account, row, None, surface="dashboard")
        return {"success": True, "message": f"{row} unbound — defaults apply",
                "refresh": ["prof-tiles"]}

    async def open_effective(row, ctx):
        _profiles_state["effective"] = row
        return {"open_modal": "effective-modal", "refresh": ["eff-lines"]}

    def _bind_options(api):
        async def provide(ctx):
            plugin = _profiles_state.get("bind")
            try:
                profiles = await api.plugin_profiles_list(plugin) or []
            except Exception:
                profiles = []
            opts = [{"value": "", "label": "Default (no profile)"}]
            opts += [{"value": p.get("name", "?"),
                      "label": f"{p.get('name', '?')} — {p.get('description', '')[:40]}"}
                     for p in profiles]
            opts.append({"value": "__new__", "label": "New profile…"})
            return {"options": opts}
        return provide

    async def submit_bind(ctx, payload):
        account = _profiles_state.get("account")
        plugin = _profiles_state.get("bind")
        choice = str(payload.get("bp-profile") or "")
        if not account or not plugin:
            return {"success": False, "message": "select an account and plugin first"}
        if choice == "__new__":
            _profiles_state["editor"] = {"plugin": plugin, "name": None}
            return {"open_modal": "editor-modal"}
        await api.accounts_plugin_profile(account, plugin, choice or None,
                                           surface="dashboard")
        return {"success": True,
                "message": f"{plugin}: {choice or 'default'} bound to {account}",
                "refresh": ["prof-tiles"]}

    def _editor_fields(api):
        async def provide(ctx):
            ed = _profiles_state.get("editor") or {}
            plugin = ed.get("plugin")
            if not plugin:
                return {"fields": []}
            schema = await _schema_for(api, plugin)
            current = dict(ed.get("settings") or {})
            fields = []
            # ── channel selector: per-guild multiselects from the live
            #    account cache (the component-built successor of the old
            #    scope boxes); pre-checked from the profile's rules
            account = _profiles_state.get("account")
            if account:
                try:
                    guilds = await api.accounts_channels(account) or []
                except Exception:
                    guilds = []
                scope = (current.get("channels") or {})
                rules = {str(r.get("guild_id")): [str(c) for c in (r.get("channel_ids") or [])]
                         for r in (scope.get("rules") or [])}
                for g in guilds:
                    gid = str(g.get("guild_id", ""))
                    chans = g.get("channels") or []
                    if not gid or not chans:
                        continue
                    fields.append({
                        "id": f"ch:{gid}",
                        "type": "multiselect",
                        "label": g.get("guild_name") or gid,
                        "description": "checked = in the rule list",
                        "options": [{"value": str(c.get("id", "")),
                                     "label": f"#{c.get('name', '?')}"}
                                    for c in chans],
                        "value": rules.get(gid, []),
                    })
            for s in schema or ():
                name = getattr(s, "name", None)
                stype = getattr(s, "type", "str")
                if not name:
                    continue
                value = current.get(name, getattr(s, "default", None))
                f: Dict[str, Any] = {"id": name, "type": "text",
                                     "label": name,
                                     "description": getattr(s, "description", "") or ""}
                if stype in ("int", "float"):
                    f["type"] = "number"
                    f["step"] = 1 if stype == "int" else 0.1
                    if getattr(s, "min_value", None) is not None:
                        f["min"] = s.min_value
                    if getattr(s, "max_value", None) is not None:
                        f["max"] = s.max_value
                    if value is not None:
                        f["value"] = str(value)
                elif stype == "bool":
                    f["type"] = "checkbox"
                    f["value"] = bool(value)
                else:
                    if value is not None:
                        f["value"] = str(value)
                fields.append(f)
            return {"fields": fields}
        return provide

    async def submit_editor(ctx, payload):
        ed = _profiles_state.get("editor") or {}
        plugin = ed.get("plugin")
        if not plugin:
            return {"success": False, "message": "no plugin selected"}
        name = str(payload.get("pe-name") or "").strip()
        if not name:
            return {"success": False, "field_errors": {
                "pe-name": "Profile name is required."}}
        # schema-driven fields arrive typed under their setting names;
        # the only raw JSON left is the channel scope
        schema = await _schema_for(api, plugin)
        settings: Dict[str, Any] = {}
        field_errors: Dict[str, str] = {}
        for s in schema or ():
            sname = getattr(s, "name", None)
            if not sname or sname not in payload:
                continue
            raw_v = payload.get(sname)
            try:
                if getattr(s, "type", "str") == "int":
                    settings[sname] = int(raw_v)
                elif getattr(s, "type", "str") == "float":
                    settings[sname] = float(raw_v)
                elif getattr(s, "type", "str") == "bool":
                    settings[sname] = bool(raw_v)
                else:
                    settings[sname] = str(raw_v)
            except (TypeError, ValueError):
                # a malformed dynamic-field value is a FIELD error —
                # killing the whole action with a raw UIError left the
                # editor with no indication which value was bad
                field_errors[sname] = (
                    f"must be a valid {getattr(s, 'type', 'str')}")
        if field_errors:
            return {"success": False, "field_errors": field_errors}
        # channel scope: mode select + per-guild selections → the
        # reserved settings structure (validated by plugin_scopes)
        mode = str(payload.get("pe-scope-mode") or "all")
        if mode not in ("all", "allow", "deny"):
            mode = "all"
        if mode != "all":
            rules = []
            for key, value in payload.items():
                if not key.startswith("ch:") or not isinstance(value, list):
                    continue
                gid = key[3:]
                if value:
                    rules.append({"guild_id": gid,
                                  "channel_ids": [str(c) for c in value]})
            settings["channels"] = {"mode": mode, "rules": rules}
        description = str(payload.get("pe-desc") or "")
        if ed.get("name"):
            await api.plugin_profile_update(
                plugin, ed["name"], description=description or None,
                settings=settings or None, surface="dashboard")
        else:
            await api.plugin_profile_create(plugin, name, description,
                                            settings or None,
                                            surface="dashboard")
            account = _profiles_state.get("account")
            if account and payload.get("pe-bind") is not False:
                try:
                    await api.accounts_plugin_profile(account, plugin, name,
                                                      surface="dashboard")
                except Exception:
                    pass   # plugin may not be assigned — creation still succeeded
        return {"success": True,
                "message": f"profile {name} saved for {plugin}",
                "refresh": ["prof-tiles"]}

    async def delete_profile(row, ctx):
        plugin, _, name = row.partition("|")
        await api.plugin_profile_delete(plugin, name)
        return {"success": True, "message": f"profile {name} deleted",
                "refresh": ["prof-tiles", "bp-profile"]}

    def _library_records(api):
        async def provide(ctx):
            try:
                profiles = await api.plugin_profiles_list() or []
            except Exception:
                profiles = []
            records = []
            for p in profiles:
                plugin = p.get("plugin", "?")
                name = p.get("name", "?")
                settings = p.get("settings") or {}
                scope = settings.get("channels") or {}
                mode = scope.get("mode", "all") if isinstance(scope, dict) else "all"
                n_rules = len(scope.get("rules") or []) if isinstance(scope, dict) else 0
                scope_txt = ("all channels" if mode == "all" or not scope
                             else f"{mode}: {n_rules} guild rule(s)")
                records.append({
                    "key": f"{plugin}|{name}",
                    "cells": [
                        {"primary": name,
                         "secondary": p.get("description") or "—"},
                        plugin,
                        scope_txt,
                    ]})
            return {"records": records}
        return provide

    def _eff_lines(api):
        async def provide(ctx):
            account = _profiles_state.get("account")
            plugin = _profiles_state.get("effective")
            if not account or not plugin:
                return []
            try:
                eff = await api.accounts_effective_settings(account, plugin)
            except Exception as e:
                return [{"label": "error", "value": str(e)}]
            return [{"label": str(k), "value": v}
                    for k, v in (eff or {}).items()]
        return provide

    (page
     .add_section(ui.Section(component_id="prof-sec")
        .add_component(ui.Toolbar(component_id="prof-bar")
                       .set_title("Account bindings")
                       .set_icon("M12 2L2 7l10 5 10-5-10-5zM2 12l10 5 10-5M2 17l10 5 10-5")
                       .add_component(ui.Select(component_id="prof-account")
                                      .set_label("")
                                      .set_options_provider(_account_options(api))
                                      .set_action(select_account)))
        .add_component(ui.TileView(component_id="prof-tiles")
                       .set_key_column("key")
                       .set_empty_text("Assign plugins to the account first "
                                       "(Accounts → plugins).")
                       .set_data_source(_prof_tiles(api))
                       .set_row_actions([
                           RowAction("bind", "bind / switch", open_bind),
                           RowAction("edit", "edit bound", edit_profile),
                           RowAction("unbind", "unbind", unbind),
                           RowAction("eff", "effective…", open_effective),
                       ])))
     .add_section(ui.Section(component_id="prof-lib-sec")
        .add_component(ui.Toolbar(component_id="prof-lib-bar")
                       .set_title("Profile library")
                       .set_icon("M4 6h16M4 12h16M4 18h10"))
        .add_component(ui.ListView(component_id="prof-lib")
                       .set_columns(["Profile", "Plugin", "Scope"])
                       .set_key_column("key")
                       .set_empty_text("no profiles in the library yet")
                       .set_data_source(_library_records(api))
                       .set_row_actions([
                           RowAction("edit", "Edit", edit_profile, variant="primary"),
                           RowAction("delete", "Delete", delete_profile, variant="danger",
                                     confirm="Delete this profile? Accounts bound "
                                             "to it fall back to plugin defaults."),
                       ])))
     .add_modal(ui.Modal(component_id="bind-modal").set_title("Bind profile")
        .add_component(ui.Form(component_id="bind-form")
                       .set_close_on_submit(True)
                       .set_submit_action(submit_bind).set_submit_label("Bind")
                       .add_fields(ui.RadioGroup(component_id="bp-profile")
                                   .set_label("Profile")
                                   .set_options_provider(_bind_options(api)))))
     .add_modal(ui.Modal(component_id="editor-modal").set_title("Profile editor")
        .add_component(ui.Form(component_id="editor-form")
                       .set_description("Settings fields come from the plugin's "
                                        "schema; values are validated "
                                        "server-side.")
                       .set_close_on_submit(True)
                       .set_fields_provider(_editor_fields(api))
                       .set_submit_action(submit_editor).set_submit_label("Save Profile")
                       .add_fields(ui.TextInput(component_id="pe-name")
                                   .set_label("Profile name").set_max_length(64)
                                   .set_required(True),
                                   ui.TextInput(component_id="pe-desc")
                                   .set_label("Description").set_max_length(200),
                                   ui.Select(component_id="pe-scope-mode")
                                   .set_label("Channel scope")
                                   .set_options(["all", "allow", "deny"])
                                   .set_description(
                                       "all: no restriction · allow: only "
                                       "checked channels · deny: checked "
                                       "channels are blocked. Guild channel "
                                       "lists appear when the account is "
                                       "online (live cache)."))))
     .add_modal(ui.Modal(component_id="effective-modal").set_title("Effective settings")
        .add_component(ui.ListView(component_id="eff-lines")
                       .set_empty_text("no settings")
                       .set_data_source(_eff_lines(api)))))
    return page
