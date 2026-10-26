"use strict";
/* Overview route — Figma-aligned presentation over the existing live store.
   Values below come from REST/WebSocket state; the view never seeds fake
   accounts, fake tasks, or invented runtime measurements. */

const HOME_ERR_STATES = {
  failed: 1, missing_credential: 1, invalid_credential: 1, error: 1, err: 1,
};

function homeMount(root) {
  let localWarnings = null;
  let lastSig = "";

  const addAccount = el("button", {class: "btn btn-primary", type: "button"},
    ico("plus"), "Add account");
  addAccount.onclick = () => navigate("/accounts");
  const diagnostics = el("button", {class: "btn btn-secondary", type: "button"},
    ico("pulse"), "Run diagnostics");
  diagnostics.onclick = () => navigate("/tools");

  const head = el("div", {class: "overview-head"},
    el("div", {class: "overview-intro"},
      el("h1", {}, "Overview"),
      el("p", {class: "dim"}, "Your accounts, execution queue, and local workspace health.")),
    el("div", {class: "overview-actions"}, diagnostics, addAccount));

  const statsHost = el("section", {class: "overview-stats", "aria-label": "Workspace summary"});
  const attentionHost = el("div", {class: "overview-attention-host"});
  const accountRows = el("tbody");
  const accountCount = el("span", {class: "overview-footnote"});
  const activityHost = el("div", {class: "overview-activity-list"});
  const taskHost = el("div", {class: "overview-task-list"});
  const pluginHost = el("div", {class: "overview-plugin-list"});
  const runtimeHost = el("div", {class: "overview-runtime-list"});
  const updated = el("span", {class: "overview-updated"}, "Waiting for runtime data");

  const accountTable = el("div", {class: "tbl-wrap overview-table-wrap"},
    el("table", {class: "tbl overview-account-table"},
      el("thead", {}, el("tr", {},
        el("th", {}, "Account"), el("th", {}, "Group"),
        el("th", {}, "Session"), el("th", {}, "Last sync"), el("th", {}))),
      accountRows));

  const accountPanel = el("section", {class: "overview-panel"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Account profiles"),
        el("p", {class: "dim"}, "Connection and session status")),
      el("a", {class: "overview-link", href: "#/accounts"}, "Manage accounts", "→")),
    accountTable,
    el("div", {class: "overview-panel-foot"}, accountCount,
      el("a", {class: "overview-link", href: "#/accounts"}, "View all accounts", "→")));

  const taskPanel = el("section", {class: "overview-panel"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Task execution"),
        el("p", {class: "dim", id: "overview-task-summary"}, "Live scheduler queue")),
      el("a", {class: "overview-link", href: "#/activity"}, "View queue", "→")),
    taskHost,
    el("div", {class: "overview-panel-foot"},
      el("span", {class: "overview-footnote"}, "Live task state"),
      el("a", {class: "overview-link", href: "#/activity"}, "Open activity", "→")));

  const activityPanel = el("section", {class: "overview-panel"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Recent activity"),
        el("p", {class: "dim"}, "Latest runtime events")),
      el("a", {class: "overview-link", href: "#/activity"}, "Open logs", "→")),
    activityHost,
    el("div", {class: "overview-panel-foot"},
      el("span", {class: "overview-footnote"}, "Newest events first"),
      el("a", {class: "overview-link", href: "#/activity"}, "View history", "→")));

  const pluginsPanel = el("section", {class: "overview-panel"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Plugins"),
        el("p", {class: "dim"}, "Installed extensions")),
      el("a", {class: "overview-link", href: "#/plugins"}, "Manage", "→")),
    pluginHost,
    el("div", {class: "overview-panel-foot"},
      el("span", {class: "overview-footnote", id: "overview-plugin-count"}, "Plugin inventory"),
      el("a", {class: "overview-link", href: "#/plugins"}, "View plugins", "→")));

  const runtimePanel = el("section", {class: "overview-panel"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Local instance"),
        el("p", {class: "dim"}, "Runtime health at a glance")),
      el("a", {class: "overview-link", href: "#/tools"}, "Details", "→")),
    runtimeHost,
    el("div", {class: "overview-panel-foot"},
      el("span", {class: "overview-footnote"}, "Data stays on this instance"),
      el("a", {class: "overview-link", href: "#/tools"}, "Diagnostics", "→")));

  const mainGrid = el("div", {class: "overview-main-grid"}, accountPanel, taskPanel);
  const lowerGrid = el("div", {class: "overview-lower-grid"},
    activityPanel, pluginsPanel, runtimePanel);
  const page = el("div", {class: "overview-page"}, head, statsHost,
    attentionHost, mainGrid, lowerGrid,
    el("div", {class: "overview-page-footer"}, ico("refresh"), updated));
  root.append(page);

  /* Seed live feeds once; WebSocket events and store subscriptions refresh
     this screen afterward. Each request is allowed to fail independently. */
  if (!(storeGet("logs") || []).length) {
    api("/api/logs?limit=30")
      .then(rows => { if (Array.isArray(rows) && rows.length) storeSet("logs", rows); })
      .catch(() => {});
  }
  api("/api/tasks")
    .then(rows => storeSet("tasks", Array.isArray(rows) ? rows : []))
    .catch(() => {});
  api("/api/ratelimit/warnings")
    .then(rows => { localWarnings = Array.isArray(rows) ? rows : []; scheduleRender(); })
    .catch(() => {});

  const offLog = WS.on("log", entry => {
    if (entry && entry.severity) storeAppend("logs", entry);
  });

  const accounts = () => storeGet("accounts") || [];
  const taskList = () => storeGet("tasks") || [];
  const groups = () => storeGet("groups") || [];
  const manifests = () => storeGet("manifests") || [];
  const logs = () => storeGet("logs") || [];
  const statusText = value => String(value || "").replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  const finiteNum = value => value != null && Number.isFinite(Number(value)) ? Number(value) : null;
  const taskCounts = () => {
    const server = (storeGet("stats") || {}).tasks || {};
    const rows = taskList();
    const count = (key, alternatives, predicate) => {
      const keys = [key].concat(alternatives || []);
      for (const k of keys) {
        const n = finiteNum(server[k]);
        if (n !== null) return Math.max(0, n);
      }
      return rows.filter(predicate).length;
    };
    return {
      running: count("running", ["active"], t => t.status === "running"),
      queued: count("queued", ["pending"], t => t.status === "queued"),
      completed: count("completed", ["done", "succeeded"], t => ["done", "completed", "succeeded"].includes(t.status)),
      failed: count("failed", ["errors"], t => ["failed", "error"].includes(t.status)),
    };
  };
  const cooldownCount = () => accounts().reduce((n, a) => {
    const v = a.cooldowns;
    if (Array.isArray(v)) return n + v.length;
    if (v && typeof v === "object") return n + Object.keys(v).length;
    return n + Math.max(0, finiteNum(v) || 0);
  }, 0);
  const activeWarnings = () => {
    const fromStats = ((storeGet("stats") || {}).warnings);
    if (Array.isArray(fromStats) && fromStats.length) return fromStats;
    return localWarnings || (Array.isArray(fromStats) ? fromStats : []);
  };
  const sessionState = a => {
    const state = String(a.status || "").toLowerCase();
    const missing = a.credential === "missing" || a.credential === "";
    if (missing || HOME_ERR_STATES[state]) return {label: "Reconnect", tone: "warn"};
    if (["running", "connected", "online", "ready"].includes(state))
      return {label: "Ready", tone: "ok"};
    if (["disabled", "stopped", "offline", "paused"].includes(state))
      return {label: "Paused", tone: "dim"};
    if (state === "idle") return {label: "Idle", tone: "dim"};
    return {label: state ? statusText(state) : "Unknown", tone: "dim"};
  };
  const lastSync = a => {
    const raw = a.last_sync_ts ?? a.last_sync_at ?? a.last_sync ?? a.last_seen ?? a.updated_at;
    if (raw == null || raw === "") return "—";
    let seconds = null;
    if (typeof raw === "number" || /^\d+(?:\.\d+)?$/.test(String(raw))) {
      seconds = Number(raw);
      if (seconds > 1000000000000) seconds /= 1000;
    } else {
      const ms = Date.parse(String(raw));
      if (Number.isFinite(ms)) seconds = ms / 1000;
    }
    if (seconds !== null && Number.isFinite(seconds))
      return Math.max(0, Math.round((Date.now() / 1000) - seconds)) < 5
        ? "Just now" : fmtDur((Date.now() / 1000) - seconds) + " ago";
    return String(raw);
  };
  const initials = name => String(name || "?").trim().split(/\s+/).slice(0, 2)
    .map(part => part.charAt(0).toUpperCase()).join("") || "?";
  const warningItems = () => {
    const items = [];
    for (const a of accounts()) {
      const st = String(a.status || "").toLowerCase();
      if (HOME_ERR_STATES[st]) items.push({
        tone: "error", href: "#/accounts/" + encodeURIComponent(a.name || ""),
        title: (a.name || "Account") + " needs attention",
        detail: a.last_error ? String(a.last_error) : "Review the account session and credentials."
      });
      else if (a.credential === "missing" || a.credential === "") items.push({
        tone: "warn", href: "#/accounts/" + encodeURIComponent(a.name || ""),
        title: (a.name || "Account") + " needs credentials",
        detail: "A credential is required before the profile can start."
      });
    }
    for (const w of activeWarnings()) {
      if (!w || w.level === "info") continue;
      items.push({
        tone: w.level === "danger" ? "error" : "warn",
        href: "#/policies",
        title: "Policy signal: " + statusText(w.code || "warning"),
        detail: String(w.message || "Review the current pacing warning.")
      });
    }
    return items;
  };

  function renderMetric(label, value, detail, iconName, tone, textValue) {
    return el("article", {class: "overview-metric-card"},
      el("div", {class: "overview-metric-top"},
        el("span", {class: "overview-metric-label"}, label),
        el("span", {class: "overview-metric-icon " + tone}, ico(iconName))),
      el("div", {class: "overview-metric-value" + (textValue ? " is-text" : "")}, String(value)),
      el("div", {class: "overview-metric-detail"}, detail));
  }
  function emptyState(title, detail) {
    return el("div", {class: "overview-empty"},
      el("span", {class: "overview-empty-mark"}, "—"),
      el("strong", {}, title), el("p", {}, detail));
  }
  function stateBadge(state) {
    return el("span", {class: "overview-state overview-state-" + state.tone}, state.label);
  }
  function renderAttention(items) {
    attentionHost.textContent = "";
    if (!items.length) return;
    const first = items[0];
    const tone = items.some(x => x.tone === "error") ? "error" : "warn";
    const alert = el("div", {class: "overview-alert overview-alert-" + tone},
      el("span", {class: "overview-alert-symbol"}, tone === "error" ? "!" : "△"),
      el("div", {class: "overview-alert-copy"},
        el("strong", {}, items.length + (items.length === 1 ? " item needs attention" : " items need attention")),
        el("span", {}, first.title + (first.detail ? " · " + first.detail : ""))),
      el("a", {class: "btn btn-secondary btn-sm", href: first.href}, "Review"));
    attentionHost.append(alert);
  }

  function renderAccounts() {
    const rows = accounts();
    accountRows.textContent = "";
    if (!rows.length) {
      accountRows.append(el("tr", {}, el("td", {colspan: "5"},
        emptyState("No account profiles yet", "Add a profile to start seeing live session status here."))));
    } else {
      for (const a of rows.slice(0, 6)) {
        const status = sessionState(a);
        const action = el("button", {class: "overview-row-action", type: "button",
          title: "Open " + (a.name || "account"), "aria-label": "Open " + (a.name || "account")}, "···");
        action.onclick = () => navigate("/accounts/" + encodeURIComponent(a.name || ""));
        accountRows.append(el("tr", {},
          el("td", {}, el("div", {class: "overview-account-cell"},
            el("span", {class: "overview-avatar"}, initials(a.name)),
            el("span", {class: "overview-account-copy"},
              el("strong", {}, a.name || "Unnamed account"),
              el("small", {}, statusText(a.profile_type || a.kind || "Account profile"))))),
          el("td", {}, el("span", {class: "overview-group-name"}, a.sync_group || "Personal")),
          el("td", {}, stateBadge(status)),
          el("td", {class: "overview-sync-time"}, lastSync(a)),
          el("td", {class: "overview-actions-cell"}, action)));
      }
    }
    accountCount.textContent = rows.length
      ? "Showing " + Math.min(rows.length, 6) + " of " + rows.length + " profiles"
      : "No profiles registered";
  }

  function renderTasks() {
    const all = taskList();
    const counts = taskCounts();
    const active = all.filter(t => ["running", "queued", "assigned"].includes(String(t.status || "").toLowerCase()));
    const order = {running: 0, assigned: 1, queued: 2};
    active.sort((a, b) => (order[String(a.status || "").toLowerCase()] ?? 9) -
      (order[String(b.status || "").toLowerCase()] ?? 9));
    const summary = document.getElementById("overview-task-summary");
    if (summary) summary.textContent = counts.running + " running · " + counts.queued + " queued";
    taskHost.textContent = "";
    if (!active.length) {
      taskHost.append(emptyState("Queue is clear", all.length
        ? "No tasks are queued or running right now."
        : "Scheduled work will appear here when the runtime queues tasks."));
      return;
    }
    for (const task of active.slice(0, 4)) {
      const status = String(task.status || "queued").toLowerCase();
      const tone = status === "running" ? "accent" : status === "queued" ? "dim" : "warn";
      const title = task.name || [task.plugin, task.action].filter(Boolean).join(" · ") || "Scheduled task";
      const sub = [task.account, task.action && task.name ? task.action : ""].filter(Boolean).join(" · ");
      const row = el("div", {class: "overview-task-row"},
        el("div", {class: "overview-task-symbol " + tone}, ico(status === "running" ? "pulse" : "clock")),
        el("div", {class: "overview-task-copy"},
          el("strong", {}, title),
          el("span", {}, sub || "Runtime scheduler"),
          el("div", {class: "overview-task-meta"},
            el("span", {class: "overview-state overview-state-" + tone}, statusText(status)),
            task.submitted_at ? el("span", {}, fmtAge(task.submitted_at) + " ago") : null)));
      const p = finiteNum(task.progress);
      if (p !== null && p >= 0) {
        const pct = Math.max(0, Math.min(100, p <= 1 && p > 0 ? p * 100 : p));
        row.append(el("div", {class: "overview-task-progress"},
          el("span", {style: "width:" + pct + "%"})));
      }
      taskHost.append(row);
    }
  }

  function renderActivity() {
    const entries = logs().slice(-4).reverse();
    activityHost.textContent = "";
    if (!entries.length) {
      activityHost.append(emptyState("No activity yet", "Log and audit events will appear as the runtime emits them."));
      return;
    }
    for (const log of entries) {
      const sev = String(log.severity || "info").toLowerCase();
      const tone = ["error", "err", "critical"].includes(sev) ? "error"
        : ["warn", "warning"].includes(sev) ? "warn"
        : ["success", "ok"].includes(sev) ? "ok" : "accent";
      const mark = tone === "error" ? "×" : tone === "warn" ? "!" : tone === "ok" ? "✓" : "·";
      activityHost.append(el("div", {class: "overview-activity-row"},
        el("span", {class: "overview-activity-symbol " + tone}, mark),
        el("div", {class: "overview-activity-copy"},
          el("strong", {}, String(log.message || "Runtime event")),
          el("span", {}, [log.account, log.plugin].filter(Boolean).join(" · ") || statusText(sev)),
          el("small", {}, log.ts ? fmtTs(log.ts) : "Recent event"))));
    }
  }

  function renderPlugins() {
    const items = manifests();
    pluginHost.textContent = "";
    const count = document.getElementById("overview-plugin-count");
    if (count) count.textContent = items.length + (items.length === 1 ? " plugin available" : " plugins available");
    if (!items.length) {
      pluginHost.append(emptyState("No plugin inventory", "Installed extensions will appear here when the plugin manager responds."));
      return;
    }
    for (const item of items.slice(0, 3)) {
      const label = item.update_available || item.updateAvailable ? "Update"
        : item.loaded === true || item.active === true || item.enabled === true ? "Active" : "Installed";
      const tone = label === "Active" ? "ok" : label === "Update" ? "warn" : "dim";
      pluginHost.append(el("div", {class: "overview-plugin-row"},
        el("span", {class: "overview-plugin-icon"}, ico("plug")),
        el("span", {class: "overview-plugin-copy"},
          el("strong", {}, item.name || "Unnamed plugin"),
          el("small", {}, item.version ? "v" + item.version : item.description || "Extension")),
        el("span", {class: "overview-state overview-state-" + tone}, label)));
    }
  }

  function renderRuntime() {
    const stats = storeGet("stats") || {};
    const ws = WS.status();
    const label = ws === "online" ? "Online" : ws === "connecting" ? "Connecting" : "Disconnected";
    const tone = ws === "online" ? "ok" : ws === "connecting" ? "warn" : "error";
    const uptime = finiteNum(stats.uptime_s);
    const runtimeFacts = [
      ["Connection", label, tone],
      ["Endpoint", location.host || "Local instance", "dim"],
      ["Connected profiles", String(accounts().filter(a => ["running", "connected", "online"].includes(String(a.status || "").toLowerCase())).length), "dim"],
      ["Runtime uptime", uptime === null ? "—" : fmtDur(uptime), "dim"],
    ];
    runtimeHost.textContent = "";
    for (const [name, value, state] of runtimeFacts) {
      runtimeHost.append(el("div", {class: "overview-runtime-row"},
        el("span", {class: "overview-runtime-key"}, name),
        state === "dim" ? el("strong", {}, value) : el("span", {class: "overview-runtime-value overview-runtime-" + state},
          el("i", {}, ""), value)));
    }
  }

  function render() {
    const accts = accounts();
    const groupRows = groups();
    const stats = storeGet("stats") || {};
    const counts = taskCounts();
    const warnings = warningItems();
    const ready = accts.filter(a => ["running", "connected", "online", "ready"].includes(String(a.status || "").toLowerCase())).length;
    const reconnect = accts.filter(a => HOME_ERR_STATES[String(a.status || "").toLowerCase()]
      || a.credential === "missing" || a.credential === "").length;
    const cooldowns = cooldownCount();
    const ws = WS.status();
    const wsLabel = ws === "online" ? "Healthy" : ws === "connecting" ? "Connecting" : "Disconnected";

    statsHost.textContent = "";
    statsHost.append(
      renderMetric("Account profiles", accts.length, groupRows.length + " groups · " + ready + " ready · " + reconnect + " reconnect", "users", "violet"),
      renderMetric("Task execution", counts.running, counts.queued + " queued · " + counts.completed + " completed", "pulse", "green"),
      renderMetric("Policy signals", warnings.length, cooldowns + " active cooldowns · live pacing state", "shield", warnings.length ? "amber" : "blue"),
      renderMetric("Local instance", wsLabel, "Runtime connection · " + (stats.uptime_s != null ? fmtDur(stats.uptime_s) + " uptime" : "local"), "db", ws === "online" ? "green" : "amber", true));
    renderAttention(warnings);
    renderAccounts();
    renderTasks();
    renderActivity();
    renderPlugins();
    renderRuntime();
    updated.textContent = "Updated " + new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) + " · live data when connected";
  }

  function sig() {
    const stats = storeGet("stats") || {};
    return JSON.stringify([
      accounts().map(a => [a.name, a.status, a.credential, a.cooldowns, a.sync_group, a.last_sync]),
      groups(), manifests(),
      stats.uptime_s, stats.tasks, stats.warnings,
      taskList().map(t => [t.id, t.status, t.progress, t.account, t.action]),
      logs().slice(-8).map(l => [l.ts, l.severity, l.message]),
      WS.status(), localWarnings,
    ]);
  }
  function scheduleRender() {
    const next = sig();
    if (next === lastSig) return;
    lastSig = next;
    render();
  }

  const unsub = storeSubscribe(key => {
    if (["accounts", "groups", "stats", "logs", "tasks", "manifests"].includes(key))
      scheduleRender();
  });
  const offStatus = WS.onStatus(scheduleRender);
  render();
  return () => { unsub(); offLog(); offStatus(); };
}

routerAdd("/home", "Overview", homeMount);

/* A real Settings destination in the nav. Theme controls remain in the
   existing Theme Engine; admin permissions and diagnostics keep their own
   established routes and API contracts. */
routerAdd("/settings", "Settings", container => {
  const openTheme = el("button", {class: "btn btn-primary", type: "button"},
    ico("gear"), "Open appearance settings");
  openTheme.onclick = () => {
    if (typeof themeOpen === "function") themeOpen();
    else toast("Theme engine is not available yet.", "err");
  };
  const appearance = el("section", {class: "overview-panel overview-settings-card"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Appearance"),
        el("p", {class: "dim"}, "Theme, accent, density, fonts, and custom CSS."))),
    el("div", {class: "overview-settings-body"}, openTheme));
  const admin = el("section", {class: "overview-panel overview-settings-card"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Framework administration"),
        el("p", {class: "dim"}, "Command permissions and framework identity."))),
    el("div", {class: "overview-settings-body"},
      el("a", {class: "btn btn-secondary", href: "#/admin"}, "Open administration", "→")));
  const runtime = el("section", {class: "overview-panel overview-settings-card"},
    el("div", {class: "overview-panel-head"},
      el("div", {}, el("h2", {}, "Local runtime"),
        el("p", {class: "dim"}, "Diagnostics, logs, and current service status."))),
    el("div", {class: "overview-settings-body"},
      el("a", {class: "btn btn-secondary", href: "#/tools"}, "Open diagnostics", "→")));
  container.append(
    el("div", {class: "view-hdr"},
      el("div", {}, el("h1", {}, "Settings"),
        el("p", {class: "dim"}, "Workspace preferences and local application controls."))),
    el("div", {class: "overview-settings-grid"}, appearance, admin, runtime));
});
