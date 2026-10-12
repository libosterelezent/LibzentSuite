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
from typing import Any, Dict, List

from core.dashboard_ui import DashboardUIToolset, RowAction
from core.dashboard_ui.registry import DashboardUIRegistry

#: reserved registry plugin name for the framework's own pages
FRAMEWORK_PLUGIN = "$framework"

#: main-tab views the SPA renders through the framework pages
FRAMEWORK_VIEWS = ("home", "plugins", "activity", "policies", "admin", "tools")


def attach_framework_ui(registry: DashboardUIRegistry, api: Any) -> int:
    """Build and register the framework's main-tab pages.

    Called once from the bootstrap once the ControlAPI exists. Returns
    the number of pages registered. Re-attaching replaces the previous
    layout atomically — attach() validates the new tree before dropping
    the old one.
    """
    ui = DashboardUIToolset()
    layout = ui.Layout(component_id="framework-root")
    for build in (_build_home, _build_plugins, _build_activity,
                  _build_policies, _build_admin, _build_tools):
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
     .add_section(ui.Section(component_id="home-groups-sec", title="Sync Groups")
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
        try:
            state = await api.plugins_state()
        except Exception:
            state = []
        current = None
        for entry in (state or []):
            if entry.get("account") == account and entry.get("plugin") == plugin:
                current = entry.get("enabled")
                break
        if current is None:
            return {"success": False,
                    "message": f"{plugin!r} is not attached to {account!r}"}
        await api.plugins_set_enabled(account, plugin, not current, surface="dashboard")
        return {"success": True,
                "message": f"{plugin} {'enabled' if not current else 'disabled'} on {account}",
                "refresh": ["plug-state"]}

    (page
     .add_section(ui.Section(component_id="plug-scan-sec")
        .add_component(ui.Row(component_id="plug-scan-row")
           .add_component(ui.Button(component_id="plug-rescan")
                          .set_label("↻ Rescan plugin directory")
                          .set_description(
                              "Re-scan plugins/ for new or externally-changed packages.")
                          .set_action(rescan))))
     .add_section(ui.Section(component_id="plug-man-sec", title="Discovered plugins")
                       .set_icon("M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4")
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
                       ]))
        .add_component(ui.Separator(component_id="plug-sep")))
     .add_section(ui.Section(component_id="plug-state-sec", title="Per-account state")
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
        try:
            state = await api.plugins_state()
        except Exception:
            state = []
        rows = []
        for e in (state or []):
            # __key cell past the visible columns: "account|plugin"
            rows.append([e.get("account", "?"), e.get("plugin", "?"),
                         "yes" if e.get("enabled") else "no",
                         "yes" if e.get("active") else "no",
                         str(e.get("error_count", 0)),
                         f"{e.get('account', '?')}|{e.get('plugin', '?')}"])
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
     .add_section(ui.Section(component_id="act-tasks-sec", title="Tasks")
        .add_component(ui.Button(component_id="act-tasks-refresh")
                       .set_label("↻ Refresh").set_variant("ghost")
                       .set_action(lambda ctx=None, payload=None: {"refresh": ["act-tasks"]}))
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
     .add_section(ui.Section(component_id="act-cds-sec", title="Cooldowns")
        .add_component(ui.Button(component_id="act-cds-refresh")
                       .set_label("↻ Refresh").set_variant("ghost")
                       .set_action(lambda ctx=None, payload=None: {"refresh": ["act-cds"]}))
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
     .add_section(ui.Section(component_id="act-logs-sec", title="Recent logs")
        .set_icon("M4 6h16M4 12h16M4 18h10")
        .add_component(ui.Button(component_id="act-logs-refresh")
                       .set_label("↻ Refresh logs").set_variant("ghost")
                       .set_action(lambda ctx=None, payload=None: {"refresh": ["act-logs"]}))
        .add_component(ui.Text(component_id="act-logs").set_variant("code")
                       .set_value("— refresh to load —")
                       .set_data_source(_logs_data(api))))
     .add_section(ui.Section(component_id="act-audit-sec", title="Audit trail")
        .add_component(ui.Button(component_id="act-audit-refresh")
                       .set_label("↻ Refresh audit").set_variant("ghost")
                       .set_action(lambda ctx=None, payload=None: {"refresh": ["act-audit"]}))
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
                         f"{str(e.get('account', 'system')):12} "
                         f"{str(e.get('level', 'info')):5} "
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
                         f"{str(e.get('account', 'system')):12} "
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
            .set_submit_action(add_policy)
            .set_submit_label("Save policy")
            .add_fields(
                ui.Select(component_id="pol-action").set_label("Action")
                .set_options(["message", "dm", "reaction", "component"]),
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
     .add_section(ui.Section(component_id="pol-table-sec", title="Stored policies")
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

    async def admin_ids_text(ctx=None):
        try:
            ov = await api.admin_overview()
        except Exception:
            ov = {}
        ids = ov.get("admins") or []
        return {"value": ", ".join(str(i) for i in ids) or "— none —"}

    async def add_admin(ctx, payload):
        try:
            user_id = int(str(payload.get("adm-add-id")).strip())
        except (TypeError, ValueError):
            return {"success": False, "field_errors": {
                "adm-add-id": "Admin id must be an integer Discord user id."}}
        await api.admin_add_id(user_id, surface="dashboard")
        return {"success": True, "message": f"admin {user_id} added",
                "refresh": ["adm-ids", "adm-accounts"]}

    async def remove_admin(ctx, payload):
        try:
            user_id = int(str(payload.get("adm-remove-id")).strip())
        except (TypeError, ValueError):
            return {"success": False, "field_errors": {
                "adm-remove-id": "Admin id must be an integer Discord user id."}}
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
        except Exception:
            ov = {}
        row_data = ((ov.get("accounts") or {}).get(row)) or {}
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

    top = (ui.Container(component_id="adm-top")
           .add_component(ui.Column(component_id="adm-ids-col")
                          .add_component(ui.Header(component_id="adm-ids-h")
                                         .set_title("Admin user ids"))
                          .add_component(ui.Text(component_id="adm-ids")
                                         .set_variant("code").set_value("—")
                                         .set_data_source(admin_ids_text)))
           .add_component(ui.Form(component_id="adm-mudae-form", title="Mudae bot id")
                          .set_submit_action(set_mudae).set_submit_label("Save")
                          .add_fields(ui.TextInput(component_id="adm-mudae-id")
                                      .set_label("Bot user id")
                                      .set_placeholder("integer, or empty to clear")
                                      .set_max_length(32))))

    forms = (ui.Container(component_id="adm-forms")
             .add_component(ui.Form(component_id="adm-add-form", title="Add admin id")
                            .set_submit_action(add_admin).set_submit_label("Add")
                            .add_fields(ui.TextInput(component_id="adm-add-id")
                                        .set_label("User id").set_max_length(32)))
             .add_component(ui.Form(component_id="adm-remove-form",
                                    title="Remove admin id")
                            .set_submit_action(remove_admin).set_submit_label("Remove")
                            .add_fields(ui.TextInput(component_id="adm-remove-id")
                                        .set_label("User id").set_max_length(32)))
             .add_component(ui.Form(component_id="pr-form", title="Set account prefix")
                            .set_submit_action(set_prefix).set_submit_label("Apply")
                            .add_fields(ui.TextInput(component_id="pr-account")
                                        .set_label("Account").set_max_length(64),
                                        ui.Select(component_id="pr-mode")
                                        .set_label("Mode")
                                        .set_options(["mention", "symbol", "both"]),
                                        ui.TextInput(component_id="pr-symbol")
                                        .set_label("Symbol")
                                        .set_placeholder("e.g. $ (symbol/both modes)")
                                        .set_max_length(8))))

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
        .add_component(ui.Grid(component_id="adm-top-grid").set_columns(2).add_component(top)),
        ui.Section(component_id="adm-forms-sec")
        .add_component(ui.Grid(component_id="adm-forms-grid")
                       .set_columns("auto").set_min_child("260px")
                       .add_component(forms)),
        ui.Section(component_id="adm-accounts-sec",
                   title="Account access rows").add_component(accounts))
    return page


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
     .add_section(ui.Section(component_id="adv-sec", title="Fleet controls")
        .set_icon("M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z")
        .add_component(ui.Row(component_id="adv-row")
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
