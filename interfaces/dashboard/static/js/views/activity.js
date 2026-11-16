"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/activity.js — the Activity page: one route, four router tabs.
     Tasks     — scheduler queue + history, filters, cancel (single + bulk)
     Cooldowns — persistent pacing cooldowns as pacing cards, clear per card
     Logs      — filtered tail + history backfill, live via WS log frames
     Audit     — Mudae protocol audit trail, same pattern as logs
   Live updates: WS "task" frames re-fetch the task lists (rate-limited to
   one per 2s); WS "log"/"audit" frames append into the store ring buffers
   this view renders from. Everything must look designed at zero accounts.
   ═══════════════════════════════════════════════════════════════════════════ */

routerAdd("/activity", "Activity", async (container) => {
  const state = {
    tab: "tasks",
    taskStatus: "", taskAccount: "",
    logAccount: "", logSev: "",
    auditAccount: "",
    logSeen: 0, auditSeen: 0,     /* how many store entries are already on screen */
    unsubs: [], timers: {},
    alive: true,
  };

  /* ── shell ──────────────────────────────────────────────────────────── */
  container.append(el("div", {class: "view-hdr"},
    el("h1", {}, "Activity"),
    el("p", {class: "dim"}, "Scheduler tasks, pacing cooldowns and the log / audit tails."),
  ));
  const tabs = [["tasks", "Tasks"], ["cooldowns", "Cooldowns"], ["logs", "Logs"], ["audit", "Audit"]];
  const bar = el("div", {class: "tabbar", role: "tablist"});
  const panels = {};
  for (const [id, label] of tabs) {
    const btn = el("button", {class: "tab" + (id === state.tab ? " on" : ""), type: "button",
      role: "tab", "aria-selected": id === state.tab ? "true" : "false"}, label);
    btn.onclick = () => {
      state.tab = id;
      bar.querySelectorAll(".tab").forEach(b => {
        const on = b === btn;
        b.classList.toggle("on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      for (const k of Object.keys(panels)) panels[k].classList.toggle("hidden", k !== id);
      if (id === "tasks") loadTasks();
      if (id === "cooldowns") loadCooldowns();
      /* lines that arrived while another tab was active replay now */
      if (id === "logs") resetLogs();
      if (id === "audit") paintNewAuditLines(true);
    };
    bar.append(btn);
    panels[id] = el("div", {class: id === state.tab ? undefined : "hidden"});
  }
  container.append(bar, ...Object.values(panels));

  /* ── shared: account filter options from the live store ─────────────── */
  const accountOptions = (sel, current) => {
    sel.textContent = "";
    sel.append(el("option", {value: ""}, "all accounts"));
    for (const a of (storeGet("accounts") || []))
      sel.append(el("option", {value: a.name, selected: a.name === current ? "" : undefined}, a.name));
  };

  /* ═══════════════════════ TAB: tasks ═══════════════════════ */
  const taskStatusSel = el("select", {"aria-label": "Filter by status"},
    ...["", "queued", "running", "done", "failed", "cancelled"].map(s =>
      el("option", {value: s}, s || "all statuses")));
  const taskAcctSel = el("select", {"aria-label": "Filter by account"});
  taskStatusSel.onchange = () => { state.taskStatus = taskStatusSel.value; renderTasks(); };
  taskAcctSel.onchange = () => { state.taskAccount = taskAcctSel.value; renderTasks(); };
  const tasksRefresh = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "↻ Refresh");
  tasksRefresh.onclick = () => busy(tasksRefresh, loadTasks);
  const bulkCancel = el("button", {class: "btn btn-danger btn-sm", type: "button",
    title: "Cancel every task matching the current filter", disabled: true},
    "Cancel matching");
  /* destructive bulk action stays inert until there is something to cancel
     (audit P1: a live danger button above an empty queue) */
  const paintBulkCancel = () => {
    const active = (storeGet("tasks") || []).some(t =>
      ["queued", "running"].includes(t.status) &&
      (!state.taskAccount || t.account === state.taskAccount));
    bulkCancel.disabled = !active;
  };
  state.unsubs.push(storeSubscribe(key => { if (key === "tasks") paintBulkCancel(); }));
  bulkCancel.onclick = async () => {
    if (bulkCancel.disabled) return;
    const ok = await confirmBox({title: "Cancel matching tasks",
      body: "Request cancellation for every task matching the current account filter?",
      danger: true});
    if (!ok) return;
    try {
      await busyRun(bulkCancel, () => post("/api/tasks/cancel-matching",
        state.taskAccount ? {account: state.taskAccount} : {}));
      await loadTasks();
    } catch (e) { toast(e.message, "err"); }
  };
  panels.tasks.append(
    el("div", {class: "toolbar"}, taskStatusSel, taskAcctSel, bulkCancel, tasksRefresh),
    el("div", {class: "section"},
      el("div", {class: "sec-hdr"}, el("span", {}, "Queue")),
      el("div", {class: "sec-body"}, el("div", {class: "skel"}, el("div"), el("div"), el("div")))),
    el("div", {class: "section"},
      el("div", {class: "sec-hdr"}, el("span", {}, "History")),
      el("div", {class: "sec-body"}, emptyState("No finished tasks yet", "Completed task runs appear here."))));
  const queueHost = panels.tasks.children[1].querySelector(".sec-body");
  const historyHost = panels.tasks.children[2].querySelector(".sec-body");

  const statusBadge = s => {
    const cls = {running: "badge-ok", queued: "badge-dim", done: "badge-ok",
                 failed: "badge-err", cancelled: "badge-warn"}[s] || "badge-dim";
    return el("span", {class: "badge " + cls}, s || "—");
  };

  function renderTasks() {
    accountOptions(taskAcctSel, state.taskAccount);
    const all = storeGet("tasks") || [];
    const rows = all.filter(t =>
      (!state.taskStatus || t.status === state.taskStatus) &&
      (!state.taskAccount || t.account === state.taskAccount));
    queueHost.textContent = "";
    if (!rows.length) {
      queueHost.append(emptyState(
        all.length ? "No tasks match the filters" : "No active tasks",
        all.length ? "Loosen the status / account filters." :
          "Scheduled plugin work appears here the moment it is queued."));
    } else {
      const tbl = el("table", {class: "tbl"});
      tbl.append(el("thead", {}, el("tr", null,
        el("th", {}, "Account"), el("th", {}, "Plugin"), el("th", {}, "Action"),
        el("th", {}, "Status"), el("th", {}, "Priority"), el("th", {}, "Age"), el("th"))));
      const tb = el("tbody");
      for (const t of rows) {
        const cancellable = t.status === "queued" || t.status === "running";
        const cancelBtn = cancellable
          ? el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "cancel") : null;
        if (cancelBtn) cancelBtn.onclick = async () => {
          const ok = await confirmBox({title: "Cancel task",
            body: `Request cancellation for ${t.plugin} · ${t.action} on ${t.account || "—"}?`});
          if (!ok) return;
          try { await busyRun(cancelBtn, () => post("/api/tasks/cancel", {task_id: t.id})); await loadTasks(); }
          catch (e) { toast(e.message, "err"); }
        };
        tb.append(el("tr", null,
          el("td", {}, t.account || "—"), el("td", {}, t.plugin || "—"),
          el("td", {}, t.action || "—"), el("td", {}, statusBadge(t.status)),
          el("td", {class: "mono"}, String(t.priority ?? "—")),
          el("td", {class: "mono"}, fmtAge(t.submitted_at)),
          el("td", {}, cancelBtn || "")));
      }
      tbl.append(tb);
      queueHost.append(el("div", {class: "tbl-wrap"}, tbl));
    }

    historyHost.textContent = "";
    const hist = (storeGet("taskHistory") || []).slice(0, 20);
    if (!hist.length) {
      historyHost.append(emptyState("No finished tasks yet", "Completed task runs appear here."));
    } else {
      const tbl = el("table", {class: "tbl"});
      tbl.append(el("thead", {}, el("tr", null,
        el("th", {}, "Account"), el("th", {}, "Plugin"),
        el("th", {}, "Status"), el("th", {}, "Duration"), el("th", {}, "Finished"))));
      const tb = el("tbody");
      for (const t of hist) tb.append(el("tr", null,
        el("td", {}, t.account || "—"), el("td", {}, t.plugin || "—"),
        el("td", {}, statusBadge(t.status)),
        el("td", {class: "mono"}, t.duration_s != null ? Number(t.duration_s).toFixed(1) + "s" : "—"),
        el("td", {class: "mono"}, fmtAge(t.finished_at))));
      tbl.append(tb);
      historyHost.append(el("div", {class: "tbl-wrap"}, tbl));
    }
  }

  async function loadTasks() {
    try {
      const [tasks, history] = await Promise.all([
        api("/api/tasks"), api("/api/tasks/history?limit=50")]);
      if (!state.alive) return;
      storeSet("tasks", tasks || []);
      storeSet("taskHistory", history || []);
    } catch (e) { if (state.alive) toast(e.message, "err"); }
    renderTasks();
  }

  /* ═══════════════════════ TAB: cooldowns ═══════════════════════ */
  const cdsRefresh = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "↻ Refresh");
  cdsRefresh.onclick = () => busy(cdsRefresh, loadCooldowns);
  panels.cooldowns.append(
    el("div", {class: "toolbar"}, el("span", {class: "dim"}, "Persistent pacing cooldowns"), cdsRefresh),
    el("div", {class: "section"},
      el("div", {class: "sec-hdr"}, el("span", {}, "Active cooldowns")),
      el("div", {class: "sec-body"}, el("div", {class: "skel"}, el("div"), el("div")))));
  const cdsHost = panels.cooldowns.children[1].querySelector(".sec-body");

  function renderCooldowns() {
    const cds = (storeGet("cooldowns") || []).filter(c => (c.remaining_s ?? 0) > 0);
    cdsHost.textContent = "";
    if (!cds.length) {
      cdsHost.append(emptyState("No active cooldowns",
        "Nothing is pacing right now — sends go out as soon as policies allow."));
      return;
    }
    const grid = el("div", {class: "pacing-grid"});
    for (const cd of cds) {
      const rem = Math.max(0, cd.remaining_s ?? 0);
      const clearBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "Clear");
      clearBtn.onclick = async () => {
        try {
          await busyRun(clearBtn, () => post("/api/cooldowns/clear", {
            account: cd.account || undefined, plugin: cd.plugin || undefined,
            cooldown_key: cd.cooldown_key || undefined}));
          await loadCooldowns();
        } catch (e) { toast(e.message, "err"); }
      };
      grid.append(el("div", {class: "pacing-item"},
        el("div", {class: "pl"}, cd.account || "—"),
        el("div", {class: "pv"}, fmtDur(rem)),
        el("div", {class: "hint"}, `${cd.plugin || "—"} · ${cd.cooldown_key || "all"}`),
        el("div", {style: "margin-top:6px;"}, clearBtn)));
    }
    cdsHost.append(grid);
  }

  async function loadCooldowns() {
    try {
      const cds = await api("/api/cooldowns");
      if (state.alive) storeSet("cooldowns", cds || []);
    } catch (e) { if (state.alive) toast(e.message, "err"); }
    renderCooldowns();
  }

  /* ═══════════════════════ TAB: logs ═══════════════════════ */
  const logAcctSel = el("select", {"aria-label": "Filter logs by account"});
  logAcctSel.onchange = () => { state.logAccount = logAcctSel.value; resetLogs(); };
  const logSevSel = el("select", {"aria-label": "Filter logs by severity"},
    ...["", "DEBUG", "INFO", "WARNING", "ERROR"].map(s =>
      el("option", {value: s}, s || "all severities")));
  logSevSel.onchange = () => { state.logSev = logSevSel.value; resetLogs(); };
  const logBackfill = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "Load history");
  logBackfill.onclick = () => busy(logBackfill, async () => {
    try {
      const q = "/api/logs?n=200" + (state.logAccount ? "&account=" + encodeURIComponent(state.logAccount) : "")
        + (state.logSev ? "&severity=" + encodeURIComponent(state.logSev) : "");
      const entries = await api(q) || [];
      if (!state.alive) return;
      storeSet("logs", entries);
      state.logSeen = 0;
      paintNewLogLines(true);
    } catch (e) { toast(e.message, "err"); }
  });
  panels.logs.append(
    el("div", {class: "toolbar"}, logAcctSel, logSevSel, logBackfill),
    el("div", {class: "section"},
      el("div", {class: "sec-hdr"}, el("span", {}, "Log tail")),
      el("div", {class: "sec-body"},
        el("pre", {class: "mono",
          style: "margin:0;max-height:420px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;"},
          el("span", {class: "dim"}, "waiting for log frames…")))));
  const logBox = panels.logs.children[1].querySelector("pre");

  function logLineText(e) {
    const ts = e && e.ts ? new Date(e.ts * 1000).toTimeString().slice(0, 8) : "";
    return `${ts}  ${(e.severity || "INFO").padEnd(8)} ${e.source || "—"}: ${e.message || ""}`;
  }
  const logPasses = e =>
    (!state.logAccount || e.account === state.logAccount) &&
    (!state.logSev || e.severity === state.logSev);
  function paintNewLogLines(reset) {
    if (state.tab !== "logs") return;
    if (reset) { logBox.textContent = ""; state.logSeen = 0; }
    else if (state.logSeen === 0) logBox.textContent = "";   /* drop the placeholder */
    const all = storeGet("logs") || [];
    for (const e of all.slice(state.logSeen)) if (logPasses(e)) logBox.append(logLineText(e) + "\n");
    state.logSeen = all.length;
    logBox.scrollTop = logBox.scrollHeight;
    if (!logBox.textContent) logBox.append("no log lines match the filters");
  }
  function resetLogs() { state.logSeen = 0; paintNewLogLines(true); }

  /* ═══════════════════════ TAB: audit ═══════════════════════ */
  const auditAcctSel = el("select", {"aria-label": "Filter audit by account"});
  auditAcctSel.onchange = () => { state.auditAccount = auditAcctSel.value; paintNewAuditLines(true); };
  const auditBackfill = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "Load history");
  auditBackfill.onclick = () => busy(auditBackfill, async () => {
    try {
      const q = "/api/audit?n=200" + (state.auditAccount ? "&account=" + encodeURIComponent(state.auditAccount) : "");
      const entries = await api(q) || [];
      if (!state.alive) return;
      storeSet("audit", entries);
      paintNewAuditLines(true);
    } catch (e) { toast(e.message, "err"); }
  });
  panels.audit.append(
    el("div", {class: "toolbar"}, auditAcctSel, auditBackfill),
    el("div", {class: "section"},
      el("div", {class: "sec-hdr"}, el("span", {}, "Audit trail")),
      el("div", {class: "sec-body"},
        el("pre", {class: "mono",
          style: "margin:0;max-height:420px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;"},
          el("span", {class: "dim"}, "waiting for audit frames…")))));
  const auditBox = panels.audit.children[1].querySelector("pre");

  function auditLineText(e) {
    const ts = e && e.ts ? new Date(e.ts * 1000).toTimeString().slice(0, 8) : "";
    const arrow = e.direction === "out" ? "→" : "←";
    return `${ts}  ${arrow} ${(e.account || "—").padEnd(12)} ${(e.action || "—").padEnd(24)} ${(e.content || "").slice(0, 60)}`;
  }
  function paintNewAuditLines(reset) {
    if (state.tab !== "audit") return;
    if (reset) { auditBox.textContent = ""; state.auditSeen = 0; }
    else if (state.auditSeen === 0) auditBox.textContent = "";
    const all = storeGet("audit") || [];
    for (const e of all.slice(state.auditSeen)) if (!state.auditAccount || e.account === state.auditAccount)
      auditBox.append(auditLineText(e) + "\n");
    state.auditSeen = all.length;
    auditBox.scrollTop = auditBox.scrollHeight;
    if (!auditBox.textContent) auditBox.append("no audit entries match the filter");
  }

  /* ── live wiring ────────────────────────────────────────────────────── */
  state.unsubs.push(storeSubscribe((key) => {
    if (!state.alive) return;
    if (key === "accounts") {
      accountOptions(taskAcctSel, state.taskAccount);
      accountOptions(logAcctSel, state.logAccount);
      accountOptions(auditAcctSel, state.auditAccount);
    }
    if (key === "logs") paintNewLogLines(false);
    if (key === "audit") paintNewAuditLines(false);
  }));
  state.unsubs.push(WS.on("log", msg => { try { storeAppend("logs", msg); } catch {} }));
  state.unsubs.push(WS.on("audit", msg => { try { storeAppend("audit", msg); } catch {} }));
  /* WS task frames re-fetch both task lists, rate-limited to 1 per 2s —
     a burst of completions must not stampede the endpoint. */
  state.unsubs.push(WS.on("task", () => {
    if (!state.alive) return;
    clearTimeout(state.timers.task);
    state.timers.task = setTimeout(() => { if (state.alive) loadTasks(); }, 2000);
  }));

  /* ── boot ───────────────────────────────────────────────────────────── */
  accountOptions(taskAcctSel, state.taskAccount);
  accountOptions(logAcctSel, state.logAccount);
  accountOptions(auditAcctSel, state.auditAccount);
  loadTasks();
  loadCooldowns();

  /* unmount: kill subscriptions, timers and in-flight render guards */
  return () => {
    state.alive = false;
    clearTimeout(state.timers.task);
    for (const u of state.unsubs) { try { u(); } catch {} }
  };
});
