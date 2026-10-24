"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/accounts.js — THE account surfaces ("The Monitor", Variant A).

   Route #/accounts      — the fleet: searchable tiles/list (pref acct_view),
                           bulk-select bar, + Add Account, sync-group
                           management tab.
   Route #/accounts/:name — the account detail page: hero strip, ABILITIES,
                           MEMORY (plugin profiles), PACING, OPERATIONS.

   Live state comes from the store (WS stats frames); REST is mutated
   through post() with busy() double-submit guards, then pulled fresh via
   loadStatsNow()/loadGroups(). Every store subscription is returned to
   the router as the unmount function.
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── status/credential vocabularies (live statuses from core) ──────────────── */
const ACCT_TONES = {
  running: "ok",
  connecting: "warn", reconnecting: "warn",
  created: "warn", stopped: "warn",
  failed: "err", missing_credential: "err", invalid_credential: "err",
  error: "err", err: "err",
};
function acctStatus(st) {
  const key = String(st || "unknown");
  return {label: key.replace(/_/g, " "), tone: ACCT_TONES[key] || "dim"};
}
/* credential: "db" | "missing" | null (service unwired) — never a value */
function acctCred(c) {
  if (c === "db") return {label: "configured", tone: "ok"};
  if (c === "missing" || c === "") return {label: "missing", tone: "err"};
  return {label: "unknown", tone: "dim"};
}
function acctBadge(a) {
  const s = acctStatus(a.status);
  return el("span", {class: "badge badge-" + s.tone}, s.label);
}
function acctCredBadge(a) {
  const c = acctCred(a.credential);
  return el("span", {class: "badge badge-" + c.tone},
    "credential " + c.label);
}
function acctManifestMap() {
  const m = {};
  for (const man of storeGet("manifests") || []) m[man.name] = man;
  return m;
}
function acctUptime(s) { return s == null ? "—" : fmtDur(s); }
function acctLatency(ms) { return ms == null ? "—" : fmtLatency(ms); }

/* pacing value formatting: 0.8s / 3.0s / 16s */
function acctFmtSecs(n) {
  const v = +n;
  if (!Number.isFinite(v)) return "—";
  return (v >= 10 ? Math.round(v) : Math.round(v * 10) / 10) + "s";
}
function acctJitterTxt(eff) {
  const [lo, hi] = (eff && eff.jitter) || [];
  if (lo == null || hi == null) return "—";
  if (!+lo && !+hi) return "none";
  return acctFmtSecs(lo) + " – " + acctFmtSecs(hi);
}

/* switch-style double-submit guard (a .toggle has no text for busy()) */
function acctLockToggle(btn, fn) {
  return async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    try { return await fn(); }
    finally { btn.disabled = false; btn.removeAttribute("aria-busy"); }
  };
}

/* inline form error inside a dialog body (removed on retry) */
function acctFbError(host, msg) {
  const old = host.querySelector(".fb-error");
  if (old) old.remove();
  host.append(el("div", {class: "fb fb-error", role: "alert"}, String(msg)));
}

/* ══════════════════════════════════════════════════════════════════════════
   Route #/accounts — the fleet list
   ══════════════════════════════════════════════════════════════════════════ */

function acctListMount(root) {
  let tab = "accounts";            // accounts | groups
  let q = "";
  const sel = new Set();           // bulk-selection account names
  const mode = () => (prefGet("acct_view", "tiles") === "list" ? "list" : "tiles");
  const groupMode = () => (prefGet("group_view", "tiles") === "list" ? "list" : "tiles");
  let lastSig = "";

  /* static header (kept out of the re-render so the search keeps focus) */
  const search = el("input", {type: "search", placeholder: "Search accounts…",
    "aria-label": "Search accounts"});
  const tileBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button",
    "aria-pressed": "false", title: "Tile view"}, "Tiles");
  const listBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button",
    "aria-pressed": "false", title: "List view"}, "List");
  /* ONE header switcher for both tabs: it controls whichever tab is
     active (Phase A #7 — the groups tab ignored the header switcher
     because it only wrote the accounts pref). Per-tab persistence:
     accounts and groups remember their own view. */
  const activeMode = () => (tab === "groups" ? groupMode() : mode());
  const setMode = m => {
    prefSet(tab === "groups" ? "group_view" : "acct_view", m);
    paintModeBtns();
    render();
  };
  const paintModeBtns = () => {
    const m = activeMode();
    tileBtn.className = "btn btn-sm " + (m === "tiles" ? "btn-primary" : "btn-ghost");
    tileBtn.setAttribute("aria-pressed", m === "tiles" ? "true" : "false");
    listBtn.className = "btn btn-sm " + (m === "list" ? "btn-primary" : "btn-ghost");
    listBtn.setAttribute("aria-pressed", m === "list" ? "true" : "false");
  };
  tileBtn.onclick = () => setMode("tiles");
  listBtn.onclick = () => setMode("list");
  paintModeBtns();

  const hdr = el("div", {class: "view-hdr"},
    el("div", {},
      el("h2", {}, "Accounts"),
      el("p", {class: "dim"}, "The fleet — every registered Discord account, live.")),
    el("div", {class: "actions"},
      search,
      tileBtn, listBtn,
      el("button", {class: "btn btn-primary btn-sm", type: "button",
        onclick: () => acctOpenAdd(() => render())},
        el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
                   "stroke-width": "2", "aria-hidden": "true"},
          el("path", {d: "M12 5v14M5 12h14"})),
        "Add Account")));
  search.addEventListener("input", () => { q = search.value.trim().toLowerCase(); render(); });

  const tabs = el("div", {class: "tabbar", role: "tablist"});
  const body = el("div", {});
  root.append(hdr, tabs, body);

  function paintTabs() {
    const accts = storeGet("accounts") || [];
    const groups = storeGet("groups") || [];
    tabs.textContent = "";
    for (const [id, label, count] of [
      ["accounts", "Accounts", accts.length],
      ["groups", "Sync Groups", groups.length]]) {
      tabs.append(el("button", {
        class: "tab" + (tab === id ? " on" : ""), type: "button",
        role: "tab", "aria-selected": tab === id ? "true" : "false",
        onclick: () => { tab = id; paintTabs(); render(); }},
        label + " ", el("span", {class: "dim"}, "(" + count + ")")));
    }
  }

  /* cheap change signature — WS stats land every second; identical fleets
     must not rebuild the DOM under the pointer */
  function fleetSig() {
    return JSON.stringify([
      (storeGet("accounts") || []).map(a => [a.name, a.status, a.enabled,
        a.user_id, a.sync_group, a.latency_ms, a.uptime_s, a.credential,
        a.cooldowns, (a.assigned_plugins || []).join(","),
        JSON.stringify(a.plugins || {})]),
      storeGet("groups") || [],
      Object.keys(acctManifestMap()).join(","),
      tab, q, mode(), [...sel].join(","),
    ]);
  }

  function render() {
    paintTabs();
    paintModeBtns();
    body.textContent = "";
    if (tab === "groups") body.append(acctGroupsPane(() => render(), groupMode));
    else body.append(acctFleetPane({q, sel, mode: mode(),
      onSelChange: render, onMutate: render}));
    lastSig = fleetSig();
  }

  const unsub = storeSubscribe(key => {
    if (!["accounts", "groups", "manifests"].includes(key)) return;
    if (fleetSig() === lastSig) return;
    render();
  });
  render();
  return unsub;
}

/* ── the accounts tab: search + bulk bar + tiles/list ─────────────────────── */
function acctFleetPane({q, sel, mode, onSelChange, onMutate}) {
  const all = storeGet("accounts") || [];
  const accts = q
    ? all.filter(a =>
        (a.name || "").toLowerCase().includes(q) ||
        (a.sync_group || "").toLowerCase().includes(q))
    : all;

  if (!all.length) return el("div", {class: "card"},
    el("div", {class: "empty"},
      el("svg", {class: "icon", viewBox: "0 0 24 24", fill: "none",
                 stroke: "currentColor", "stroke-width": "1.5", "aria-hidden": "true"},
        el("path", {d: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"})),
      el("h3", {}, "No accounts yet"),
      el("p", {}, "Add your first Discord account, assign its plugins, and the fleet comes alive here — live status, latency, pacing and abilities per account."),
      el("button", {class: "btn btn-primary", type: "button",
        onclick: () => acctOpenAdd(onMutate)}, "Add Account")));

  const pane = el("div", {});
  pane.append(acctBulkBar(sel, onSelChange, onMutate));

  if (mode === "list") {
    pane.append(acctListTable(accts, sel, onSelChange));
  } else {
    const grid = el("div", {class: "grid grid-2"});
    for (const a of accts) grid.append(acctTile(a, sel, onSelChange));
    pane.append(grid);
  }
  if (q && !accts.length) {
    pane.append(el("div", {class: "empty"},
      el("h3", {}, "No matches"),
      el("p", {}, "Nothing in the fleet matches “" + q + "”.")));
  }
  return pane;
}

/* bulk action bar — visible while anything is selected. Each button owns
   its own busy() guard (never the bar: busy() swaps the button content). */
function acctBulkBar(sel, onSelChange, onMutate) {
  const bar = el("div", {class: "toolbar"});
  const count = el("span", {class: "dim"}, sel.size + " selected");
  const paint = () => {
    bar.classList.toggle("hidden", sel.size === 0);
    count.textContent = sel.size + " selected";
  };

  /* one confirmed bulk mutation: confirmBox → per-account POSTs → refresh */
  const bulkBtn = (label, cls, bodyMsg, fn) => {
    const btn = el("button", {class: cls, type: "button"}, label);
    btn.onclick = busy(btn, async () => {
      if (!sel.size) return;
      const ok = await confirmBox({
        title: label + " — " + sel.size + " account" + (sel.size > 1 ? "s" : ""),
        body: bodyMsg, yes: label,
      });
      if (!ok) return;
      let failed = 0;
      for (const acctName of [...sel]) {
        try { await fn(acctName); }
        catch (e) { failed++; toast(acctName + ": " + e.message, "err"); }
      }
      sel.clear();
      await Promise.allSettled([loadStatsNow(), loadGroups()]);
      toast(label + (failed ? " — " + failed + " failed" : " done"),
        failed ? "err" : "ok");
      paint();
      onSelChange();
    });
    return btn;
  };

  bar.append(count,
    bulkBtn("Start", "btn btn-sm btn-secondary",
      "Start every selected account.",
      n => post("/api/accounts/start", {account: n})),
    bulkBtn("Stop", "btn btn-sm btn-secondary",
      "Gracefully stop every selected account.",
      n => post("/api/accounts/stop", {account: n})),
    bulkBtn("Enable", "btn btn-sm btn-secondary",
      "Enable every selected account.",
      n => post("/api/accounts/enable", {name: n, enabled: true})),
    bulkBtn("Disable", "btn btn-sm btn-secondary",
      "Disable every selected account (it will not start).",
      n => post("/api/accounts/enable", {name: n, enabled: false})),
    el("button", {class: "btn btn-sm btn-secondary", type: "button",
      onclick: () => acctOpenBulkMove([...sel], () => { sel.clear(); onSelChange(); })},
      "Move to group…"),
    el("button", {class: "btn btn-ghost btn-sm", type: "button",
      onclick: () => { sel.clear(); paint(); onSelChange(); }}, "Clear"));
  paint();
  return bar;
}

/* tile — one account card (structured rows: head / meta / chips / actions) */
function acctTile(a, sel, onSelChange) {
  const s = acctStatus(a.status);
  const plugins = a.assigned_plugins || [];
  const card = el("div", {class: "card acct-tile", title: "Open " + a.name});

  const head = el("div", {class: "row"},
    dot(s.tone === "ok" ? "on" : s.tone === "err" ? "err" : "off"),
    el("a", {class: "acct-name",
             href: "#/accounts/" + encodeURIComponent(a.name),
             title: "View " + a.name}, a.name),
    acctBadge(a),
    el("span", {class: "grow"}),
    a.user_id ? el("span", {class: "mono faint"}, String(a.user_id)) : null);

  const meta = el("div", {class: "row meta"},
    el("span", {}, "uptime ", el("b", {}, acctUptime(a.uptime_s))),
    el("span", {}, "latency ", el("b", {},
      acctLatency(a.latency_ms))),
    a.cooldowns ? el("span", {}, "cooldowns ",
      el("b", {}, String(a.cooldowns))) : null,
    acctCredBadge(a));

  const chips = el("div", {class: "row chips"});
  if (a.sync_group) chips.append(el("span", {class: "chip", title: "Sync group"},
    a.sync_group));
  for (const p of plugins.slice(0, 6)) chips.append(el("span", {class: "chip plugin"}, p));
  if (plugins.length > 6) chips.append(el("span", {class: "chip"}, "+" + (plugins.length - 6)));

  const isSel = sel.has(a.name);
  const selBox = el("input", {type: "checkbox", title: "Select " + a.name,
    "aria-label": "Select " + a.name});
  selBox.checked = isSel;
  selBox.addEventListener("change", () => {
    if (selBox.checked) sel.add(a.name); else sel.delete(a.name);
    onSelChange();
  });
  const startStop = a.status === "running"
    ? el("button", {class: "btn btn-sm btn-secondary", type: "button"}, "Stop")
    : el("button", {class: "btn btn-sm btn-primary", type: "button"}, "Start");
  startStop.onclick = busy(startStop, async () => {
    try {
      await post(a.status === "running" ? "/api/accounts/stop" : "/api/accounts/start",
        {account: a.name});
      toast((a.status === "running" ? "stop" : "start") + " requested", "ok");
      await loadStatsNow();
    } catch (e) { toast(e.message, "err"); }
  });

  const actions = el("div", {class: "row actions"}, startStop,
    el("label", {class: "sel"}, selBox, "select"));
  selBox.addEventListener("click", e => e.stopPropagation());

  card.append(head, chips, meta, actions);
  card.addEventListener("click", e => {
    if (e.target.closest("a, button, input, label")) return;
    navigate("#/accounts/" + encodeURIComponent(a.name));
  });
  return card;
}

/* list — the same fleet as a table */
function acctListTable(accts, sel, onSelChange) {
  const head = el("tr", {},
    el("th", {scope: "col"}, ""),
    el("th", {scope: "col"}, "Account"),
    el("th", {scope: "col"}, "Status"),
    el("th", {scope: "col"}, "Latency"),
    el("th", {scope: "col"}, "Uptime"),
    el("th", {scope: "col"}, "Credential"),
    el("th", {scope: "col"}, "Group"),
    el("th", {scope: "col"}, "Plugins"),
    el("th", {scope: "col"}, ""));
  const tbody = el("tbody", {});
  for (const a of accts) {
    const selBox = el("input", {type: "checkbox",
      title: "Select " + a.name, "aria-label": "Select " + a.name});
    selBox.checked = sel.has(a.name);
    selBox.addEventListener("change", () => {
      if (selBox.checked) sel.add(a.name); else sel.delete(a.name);
      onSelChange();
    });
    const plugins = a.assigned_plugins || [];
    tbody.append(el("tr", {},
      el("td", {}, selBox),
      el("td", {},
        el("a", {href: "#/accounts/" + encodeURIComponent(a.name)}, a.name),
        a.user_id ? " " : null,
        a.user_id ? el("span", {class: "mono faint"}, String(a.user_id)) : null),
      el("td", {}, acctBadge(a)),
      el("td", {class: "mono"}, acctLatency(a.latency_ms)),
      el("td", {class: "mono"}, acctUptime(a.uptime_s)),
      el("td", {}, acctCredBadge(a)),
      el("td", {}, a.sync_group
        ? el("span", {class: "chip"}, a.sync_group)
        : el("span", {class: "faint"}, "independent")),
      el("td", {}, plugins.length
        ? el("span", {class: "row"}, plugins.slice(0, 3).map(p =>
            el("span", {class: "chip plugin"}, p)))
        : el("span", {class: "faint"}, "—")),
      el("td", {class: "mono"}, a.cooldowns ? String(a.cooldowns) : "0")));
  }
  return el("div", {class: "tbl-wrap"},
    el("table", {class: "tbl"}, el("thead", {}, head), tbody));
}

/* ── the sync-groups tab ──────────────────────────────────────────────────── */
function acctGroupsPane(onMutate, groupMode) {
  const groups = storeGet("groups") || [];
  const accts = storeGet("accounts") || [];
  const pane = el("div", {},
    el("div", {class: "toolbar"},
      el("span", {class: "dim"}, "Sync groups pace their members on a shared clock."),
      el("button", {class: "btn btn-primary btn-sm", type: "button",
        onclick: () => acctOpenGroupCreate(onMutate)}, "New Group")));

  if (!groups.length) {
    pane.append(el("div", {class: "card"},
      el("div", {class: "empty"},
        el("svg", {class: "icon", viewBox: "0 0 24 24", fill: "none",
                   stroke: "currentColor", "stroke-width": "1.5", "aria-hidden": "true"},
          el("path", {d: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"})),
        el("h3", {}, "No sync groups"),
        el("p", {}, "A sync group paces its member accounts on one shared clock, so Mudae sees a natural conversation instead of a burst. Create one, then move accounts into it."),
        el("button", {class: "btn btn-primary", type: "button",
          onclick: () => acctOpenGroupCreate(onMutate)}, "New Group"))));
    return pane;
  }

  /* list mode comes from the HEADER switcher (group_view pref) */
  if (groupMode() === "list") {
    pane.append(acctGroupsTable(groups, accts, onMutate));
    return pane;
  }
  const grid = el("div", {class: "grid grid-2"});
  for (const g of groups) {
    const members = accts.filter(a => a.sync_group === g.name);
    const card = el("div", {class: "card"});
    card.append(el("div", {class: "row"},
      el("b", {}, g.name),
      el("span", {class: "badge " + (g.enabled === false ? "badge-warn" : "badge-ok")},
        g.enabled === false ? "paused" : "active"),
      el("span", {class: "dim"}, members.length + " member" + (members.length === 1 ? "" : "s"))));
    if (g.description) card.append(el("p", {class: "dim"}, g.description));

    /* member chips: link to the account, ✕ removes it from the group */
    const chips = el("div", {class: "row"});
    for (const m of members) {
      const rm = el("button", {type: "button",
        title: "Remove " + m.name + " from " + g.name,
        "aria-label": "Remove " + m.name + " from " + g.name,
        class: "btn btn-ghost btn-sm"}, "✕");
      rm.onclick = busy(rm, async () => {
        const ok = await confirmBox({title: "Remove member",
          body: "Remove " + m.name + " from " + g.name + "? It will pace independently.",
          yes: "Remove"});
        if (!ok) return;
        try {
          await post("/api/groups/remove-member", {group: g.name, account: m.name});
          toast(m.name + " removed", "ok");
          await Promise.allSettled([loadGroups(), loadStatsNow()]);
          onMutate();
        } catch (e) { toast(e.message, "err"); }
      });
      chips.append(el("span", {class: "chip"},
        el("a", {href: "#/accounts/" + encodeURIComponent(m.name)}, m.name),
        " ", rm));
    }
    if (!members.length) chips.append(el("span", {class: "faint"}, "no members yet"));
    card.append(chips);

    const addBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "Add member…");
    addBtn.onclick = () => acctOpenGroupAddMember(g, accts, onMutate);
    const editBtn = el("button", {class: "btn btn-sm btn-secondary", type: "button"}, "Edit");
    editBtn.onclick = () => acctOpenGroupEdit(g, onMutate);
    const delBtn = el("button", {class: "btn btn-sm btn-danger", type: "button"}, "Delete");
    delBtn.onclick = busy(delBtn, async () => {
      const ok = await confirmBox({title: "Delete group",
        body: "Delete " + g.name + "? Its " + members.length +
          " member(s) move back to independent pacing.",
        yes: "Delete"});
      if (!ok) return;
      try {
        await post("/api/groups/delete", {group: g.name, detach_members: true});
        toast("group deleted", "ok");
        await Promise.allSettled([loadGroups(), loadStatsNow()]);
        onMutate();
      } catch (e) { toast(e.message, "err"); }
    });
    card.append(el("div", {class: "row"}, addBtn, editBtn, delBtn));
    grid.append(card);
  }
  pane.append(grid);
  return pane;
}

/* groups as a table (list mode) — same actions as the tiles */
function acctGroupsTable(groups, accts, onMutate) {
  const head = el("tr", {},
    el("th", {scope: "col"}, "Group"),
    el("th", {scope: "col"}, "Status"),
    el("th", {scope: "col"}, "Members"),
    el("th", {scope: "col"}, "Description"),
    el("th", {scope: "col"}, ""));
  const tb = el("tbody", {});
  for (const g of groups) {
    const members = accts.filter(a => a.sync_group === g.name);
    const memberCell = el("td", {class: "row"});
    if (!members.length) memberCell.append(el("span", {class: "faint"}, "—"));
    for (const m of members) {
      const rm = el("button", {type: "button",
        title: "Remove " + m.name + " from " + g.name,
        "aria-label": "Remove " + m.name + " from " + g.name,
        class: "btn btn-ghost btn-sm"}, "✕");
      rm.onclick = busy(rm, async () => {
        const ok = await confirmBox({title: "Remove member",
          body: "Remove " + m.name + " from " + g.name + "? It will pace independently.",
          yes: "Remove"});
        if (!ok) return;
        try {
          await post("/api/groups/remove-member", {group: g.name, account: m.name});
          toast(m.name + " removed", "ok");
          await Promise.allSettled([loadGroups(), loadStatsNow()]);
          onMutate();
        } catch (e) { toast(e.message, "err"); }
      });
      memberCell.append(el("span", {class: "chip"},
        el("a", {href: "#/accounts/" + encodeURIComponent(m.name)}, m.name), rm));
    }
    collapseWrap(memberCell, 4);

    const addBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "Add member…");
    addBtn.onclick = () => acctOpenGroupAddMember(g, accts, onMutate);
    const editBtn = el("button", {class: "btn btn-sm btn-secondary", type: "button"}, "Edit");
    editBtn.onclick = () => acctOpenGroupEdit(g, onMutate);
    const delBtn = el("button", {class: "btn btn-sm btn-danger", type: "button"}, "Delete");
    delBtn.onclick = busy(delBtn, async () => {
      const ok = await confirmBox({title: "Delete group",
        body: "Delete " + g.name + "? Its " + members.length +
          " member(s) move back to independent pacing.",
        yes: "Delete"});
      if (!ok) return;
      try {
        await post("/api/groups/delete", {group: g.name, detach_members: true});
        toast("group deleted", "ok");
        await Promise.allSettled([loadGroups(), loadStatsNow()]);
        onMutate();
      } catch (e) { toast(e.message, "err"); }
    });

    tb.append(el("tr", {},
      el("td", {}, el("b", {}, g.name)),
      el("td", {}, el("span", {class: "badge " + (g.enabled === false ? "badge-warn" : "badge-ok")},
        g.enabled === false ? "paused" : "active")),
      memberCell,
      el("td", {class: "dim"}, g.description || "—"),
      el("td", {class: "row"}, addBtn, editBtn, delBtn)));
  }
  return el("div", {class: "tbl-wrap"},
    el("table", {class: "tbl"}, el("thead", {}, head), tb));
}

/* ══════════════════════════════════════════════════════════════════════════
   Dialogs: add account / bulk move / groups
   ══════════════════════════════════════════════════════════════════════════ */

function acctOpenAdd(onDone) {
  const nameIn = el("input", {type: "text", autocomplete: "off",
    placeholder: "e.g. libalt01", "aria-label": "Account name"});
  const credIn = el("input", {type: "password", autocomplete: "off",
    placeholder: "stored encrypted — never displayed again",
    "aria-label": "Credential token"});
  const groupSel = el("select", {"aria-label": "Sync group"});
  groupSel.append(el("option", {value: ""}, "independent (no group)"));
  for (const g of storeGet("groups") || [])
    groupSel.append(el("option", {value: g.name}, g.name));

  const manifests = storeGet("manifests") || [];
  const plugList = el("div", {class: "picklist"});
  if (!manifests.length) {
    plugList.append(el("p", {class: "hint"},
      "No plugins discovered yet — the account will run none. Assign later from its detail page."));
  }
  for (const m of manifests) {
    const cb = el("input", {type: "checkbox", value: m.name});
    plugList.append(el("label", {class: "pick-row"}, cb,
      el("span", {class: "pl-name"}, m.name),
      el("span", {class: "pl-desc"}, m.description || "")));
  }

  const form = el("div", {},
    el("div", {class: "field"},
      el("label", {}, "Account name"),
      nameIn,
      el("div", {class: "hint"}, "A label for this Discord account — you choose it, it never leaves the machine.")),
    el("div", {class: "field"},
      el("label", {}, "Sync group"), groupSel),
    el("div", {class: "field"},
      el("label", {}, "Credential (optional)"), credIn,
      el("div", {class: "hint"}, "The account token. Paste it once; it is encrypted at rest and write-only from then on.")),
    el("div", {class: "field"},
      el("label", {}, "Plugins"), plugList));

  dialog({title: "Add Account", bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Add Account", variant: "primary", onclick: (close) => {
      const btn = form.closest("dialog").querySelector(".dlg-foot .btn-primary");
      busy(btn, async () => {
        const name = nameIn.value.trim();
        if (!name) return acctFbError(form, "account name is required");
        const payload = {name};
        if (groupSel.value) payload.sync_group = groupSel.value;
        if (credIn.value.trim()) payload.credential = credIn.value.trim();
        const chosen = [...plugList.querySelectorAll("input:checked")].map(i => i.value);
        if (chosen.length) payload.plugins = chosen;
        try {
          await post("/api/accounts/add", payload);
          toast("account “" + name + "” added", "ok");
          close();
          await Promise.allSettled([loadStatsNow(), loadGroups()]);
          onDone();
        } catch (e) { acctFbError(form, e.message); }
      })();
    }},
  ]});
  nameIn.focus();
}

function acctOpenBulkMove(names, onDone) {
  const groups = storeGet("groups") || [];
  const list = el("div", {class: "picklist"});
  if (!groups.length) list.append(el("p", {class: "hint"},
    "No sync groups exist yet — create one on the Sync Groups tab first."));
  const mkRow = (value, label, desc, checked) => {
    const radio = el("input", {type: "radio", name: "acct-bulk-group", value: value});
    radio.checked = checked;
    return el("label", {class: "pick-row"}, radio,
      el("span", {class: "pl-name"}, label),
      el("span", {class: "pl-desc"}, desc));
  };
  list.append(mkRow("", "independent", "remove from any group", true));
  for (const g of groups)
    list.append(mkRow(g.name, g.name, g.description || "", false));

  const form = el("div", {},
    el("p", {class: "dim"}, "Move " + names.length + " account" +
      (names.length > 1 ? "s" : "") + " to a sync group."),
    list);

  dialog({title: "Move to Group", bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Move", variant: "primary", onclick: (close) => {
      const btn = form.closest("dialog").querySelector(".dlg-foot .btn-primary");
      busy(btn, async () => {
        const picked = list.querySelector("input:checked");
        const group = picked ? picked.value : "";
        let failed = 0;
        for (const n of names) {
          try {
            if (group) await post("/api/groups/set-account", {account: n, group});
            else await post("/api/groups/clear-account", {account: n});
          } catch (e) { failed++; toast(n + ": " + e.message, "err"); }
        }
        toast("moved" + (failed ? " — " + failed + " failed" : ""), failed ? "err" : "ok");
        close();
        await Promise.allSettled([loadGroups(), loadStatsNow()]);
        onDone();
      })();
    }},
  ]});
}

function acctOpenGroupCreate(onDone) {
  const nameIn = el("input", {type: "text", placeholder: "e.g. farm-a",
    "aria-label": "Group name"});
  const descIn = el("input", {type: "text", placeholder: "what unites these accounts?",
    "aria-label": "Group description"});
  const enCb = el("input", {type: "checkbox"});
  enCb.checked = true;
  const form = el("div", {},
    el("div", {class: "field"}, el("label", {}, "Name"), nameIn),
    el("div", {class: "field"}, el("label", {}, "Description"), descIn),
    el("label", {class: "pick-row"}, enCb,
      el("span", {class: "pl-name"}, "active"),
      el("span", {class: "pl-desc"}, "a paused group does not pace")));
  dialog({title: "New Sync Group", bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Create", variant: "primary", onclick: (close) => {
      const btn = form.closest("dialog").querySelector(".dlg-foot .btn-primary");
      busy(btn, async () => {
        const name = nameIn.value.trim();
        if (!name) return acctFbError(form, "group name is required");
        try {
          await post("/api/groups/create",
            {name, description: descIn.value.trim(), enabled: !!enCb.checked});
          toast("group created", "ok");
          close();
          await loadGroups();
          onDone();
        } catch (e) { acctFbError(form, e.message); }
      })();
    }},
  ]});
  nameIn.focus();
}

function acctOpenGroupEdit(g, onDone) {
  const nameIn = el("input", {type: "text", value: g.name || "",
    "aria-label": "Group name"});
  const descIn = el("input", {type: "text", value: g.description || "",
    "aria-label": "Group description"});
  const enCb = el("input", {type: "checkbox"});
  enCb.checked = g.enabled !== false;
  const form = el("div", {},
    el("div", {class: "field"}, el("label", {}, "Name"), nameIn,
      el("div", {class: "hint"}, "renaming moves every member's pacing scope with it")),
    el("div", {class: "field"}, el("label", {}, "Description"), descIn),
    el("label", {class: "pick-row"}, enCb,
      el("span", {class: "pl-name"}, "active"),
      el("span", {class: "pl-desc"}, "a paused group does not pace")));
  dialog({title: "Edit Group — " + g.name, bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Save", variant: "primary", onclick: (close) => {
      const btn = form.closest("dialog").querySelector(".dlg-foot .btn-primary");
      busy(btn, async () => {
        const payload = {group: g.name};
        if (nameIn.value.trim() && nameIn.value.trim() !== g.name)
          payload.name = nameIn.value.trim();
        if (descIn.value !== (g.description || "")) payload.description = descIn.value;
        if (enCb.checked !== (g.enabled !== false)) payload.enabled = enCb.checked;
        if (Object.keys(payload).length < 2)
          return acctFbError(form, "nothing to change");
        try {
          await post("/api/groups/update", payload);
          toast("group updated", "ok");
          close();
          await Promise.allSettled([loadGroups(), loadStatsNow()]);
          onDone();
        } catch (e) { acctFbError(form, e.message); }
      })();
    }},
  ]});
}

function acctOpenGroupAddMember(g, accts, onDone) {
  const cands = accts.filter(a => a.sync_group !== g.name);
  const list = el("div", {class: "picklist"});
  if (!cands.length) list.append(el("p", {class: "hint"},
    "Every account is already in " + g.name + "."));
  for (const a of cands) {
    list.append(el("label", {class: "pick-row"},
      el("input", {type: "checkbox", value: a.name}),
      el("span", {class: "pl-name"}, a.name),
      el("span", {class: "pl-desc"},
        a.sync_group ? "currently in " + a.sync_group : "independent")));
  }
  const form = el("div", {},
    el("p", {class: "dim"}, "Add members to " + g.name + " — moving them off their current pacing."),
    list);
  dialog({title: "Add Members — " + g.name, bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Add", variant: "primary", onclick: (close) => {
      const btn = form.closest("dialog").querySelector(".dlg-foot .btn-primary");
      busy(btn, async () => {
        const chosen = [...list.querySelectorAll("input:checked")].map(i => i.value);
        if (!chosen.length) return acctFbError(form, "select at least one account");
        let failed = 0;
        for (const n of chosen) {
          try { await post("/api/groups/add-member", {group: g.name, account: n}); }
          catch (e) { failed++; toast(n + ": " + e.message, "err"); }
        }
        toast("members added" + (failed ? " — " + failed + " failed" : ""),
          failed ? "err" : "ok");
        close();
        await Promise.allSettled([loadGroups(), loadStatsNow()]);
        onDone();
      })();
    }},
  ]});
}

/* ══════════════════════════════════════════════════════════════════════════
   Route #/accounts/:name — the account detail page (Variant A)
   ══════════════════════════════════════════════════════════════════════════ */

function acctDetailMount(root, params) {
  const acctName = params.name || "";
  /* extras: REST-only detail data (live fields come from the store row) */
  const ex = {
    states: null,        // /api/plugins/state rows [{name,enabled,last_error,…}]
    bindings: null,      // /api/accounts/plugin-profiles {plugin: profile|null}
    profiles: null,      // /api/plugins/profiles rows
    effMsg: null, effDm: null,
    channels: null,      // /api/accounts/channels
    info: null,          // /api/accounts/info fallback (store miss)
    stats: null,         // /api/ui/stats — plugin-contributed StatCards
    dataNodes: null,     // PHASE 10: AccountPluginData scope nodes
    errors: {},          // slice key -> message: FAILED loads, distinct from
                         // loaded-empty; values keep their last-good data
  };
  let infoTried = false;
  let lastSig = "";
  let alive = true;      // stale-response guard: renders stop at unmount
  /* pending | found | missing — a direct-URL entry with an empty store
     must show LOADING, not a false "Account not found" while /info and
     the first WS frame are still in flight */
  let existence = "pending";

  const hdr = el("div", {class: "view-hdr"},
    el("div", {},
      el("a", {href: "#/accounts", class: "dim"}, "← Accounts"),
      el("p", {class: "dim"}, "The account as an object — abilities, memory, pacing, operations.")),
    el("div", {},
      el("button", {class: "btn btn-ghost btn-sm", type: "button",
        title: "Refresh", onclick: () => acctDetailRefresh(acctName, ex, scheduleRender)},
        el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
                   "stroke-width": "2", "aria-hidden": "true"},
          el("path", {d: "M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.5 15"})),
        "Refresh")));
  const heroWrap = el("div", {});
  const body = el("div", {});
  root.append(hdr, heroWrap, body);

  const getRow = () =>
    (storeGet("accounts") || []).find(a => a.name === acctName) || null;

  /* /api/accounts/info has a different (richer) shape — adapt the fields
     the detail page reads so a store miss can still render */
  const infoAsRow = info => {
    if (!info) return null;
    const st = info.status || {}, id = info.identity || {};
    const plugins = {};
    for (const p of info.plugins || []) plugins[p.name] = {enabled: !!p.enabled};
    return {
      name: acctName, status: st.state || "unknown",
      user_id: id.discord_id, latency_ms: st.latency_ms, uptime_s: st.uptime_s,
      credential: info.credential, enabled: null, sync_group: null,
      assigned_plugins: Object.keys(plugins), plugins,
      plugin_profiles: {}, cooldowns: null,
    };
  };

  function scheduleRender() { if (alive) render(); }

  /* volatile fields paint IN PLACE: the WS pushes fresh accounts every
     second, and uptime/ping must never rebuild the page (focused
     controls, open dialogs and half-typed inputs would die with it) */
  function paintLive(row) {
    if (!row) return;
    const set = (key, text) => {
      const node = heroWrap.querySelector('[data-live="' + key + '"]');
      if (node && node.textContent !== text) node.textContent = text;
    };
    set("uptime", acctUptime(row.uptime_s));
    set("latency", acctLatency(row.latency_ms));
    set("cooldowns", String(row.cooldowns));
  }

  function render() {
    if (!alive) return;
    const row = getRow() || infoAsRow(ex.info);
    heroWrap.textContent = "";
    body.textContent = "";
    if (!row) {
      if (existence === "pending") {
        heroWrap.append(el("div", {class: "card"},
          el("div", {class: "skel"}, el("div"), el("div"), el("div"))));
        return;
      }
      heroWrap.append(el("div", {class: "card"},
        el("div", {class: "empty"},
          el("svg", {class: "icon", viewBox: "0 0 24 24", fill: "none",
                     stroke: "currentColor", "stroke-width": "1.5", "aria-hidden": "true"},
            el("circle", {cx: "12", cy: "12", r: "10"}),
            el("path", {d: "M12 8v4M12 16h.01"})),
          el("h3", {}, "Account not found"),
          el("p", {}, "No account named “" + acctName +
            "” is registered. It may have been removed."),
          el("a", {class: "btn btn-primary", href: "#/accounts"}, "Back to Accounts"))));
      return;
    }
    const failed = Object.keys(ex.errors || {});
    if (failed.length)
      heroWrap.append(el("div", {class: "hint", role: "status"},
        "⚠ " + failed.length + " data source" + (failed.length === 1 ? "" : "s") +
        " unavailable (" + failed.join(", ") + ") — showing last known values"));
    heroWrap.append(acctHero(row));
    /* combined layout (mockup: account-detail combined-theme-engine):
       row 1 = Abilities | Memory · row 2 = Pacing | Live stats · ops full */
    body.append(
      el("div", {class: "detail-grid"},
        acctAbilitiesSection(row, ex, scheduleRender),
        acctMemorySection(row, ex, scheduleRender)),
      el("div", {class: "detail-grid"},
        acctPacingSection(row, ex),
        acctLiveStatsSection(row, ex)),
      acctOperationsSection(row, ex.channels, scheduleRender));
    paintLive(row);
    lastSig = acctDetailSig(row);
  }

  /* hadRow: when the account first appears in the store (cold direct-URL
     entry while the WS catches up), the extras batch must fire once */
  let hadRow = false;
  const unsub = storeSubscribe(key => {
    if (key !== "accounts") return;
    const nowHave = !!getRow();
    const fresh = nowHave && !hadRow;
    hadRow = nowHave;
    if (fresh) { existence = "found"; acctDetailRefresh(acctName, ex, scheduleRender); }
    const row = getRow() || infoAsRow(ex.info);
    if (!row) { render(); return; }        // vanished → not-found screen
    if (acctDetailSig(row) === lastSig) { paintLive(row); return; }  // volatile-only
    render();
  });

  /* boot: store may be empty (direct URL entry) — pull REST truth */
  render();
  if (!getRow() && !storeGet("stats"))
    loadStatsNow().then(scheduleRender).catch(() => {});
  if (getRow()) {
    existence = "found";
    acctDetailRefresh(acctName, ex, scheduleRender);
  } else if (!infoTried) {
    /* unknown so far: confirm existence via /info first — the extras
       batch would only 400 against a nonexistent account */
    infoTried = true;
    api("/api/accounts/info?account=" + encodeURIComponent(acctName))
      .then(info => {
        if (!alive) return;                // stale response after unmount
        ex.info = info;
        existence = "found";
        render();
        acctDetailRefresh(acctName, ex, scheduleRender);
      })
      .catch(() => {
        if (!alive) return;
        ex.info = null;
        existence = "missing";             // CONFIRMED absent, not still-loading
        render();
      });
  }
  return () => { alive = false; unsub(); };
}

/* structural signature: volatile per-second fields (uptime, latency)
   are deliberately EXCLUDED — they paint in place via paintLive(); a
 * sig change here means the page structure legitimately changes */
function acctDetailSig(row) {
  return JSON.stringify([row.name, row.status, row.enabled, row.user_id,
    row.sync_group, row.credential,
    row.cooldowns, row.assigned_plugins, row.plugins, row.plugin_profiles]);
}

/* refresh every REST-only slice the detail page shows. ONE batched
   render when all slices settle (the baseline rebuilt the page once
   per slice); a failed slice keeps its LAST GOOD value and records
   ex.errors[key] — never erasing another slice's successful data. */
function acctDetailRefresh(acctName, ex, scheduleRender) {
  const enc = encodeURIComponent(acctName);
  const slices = [
    ["states", "/api/plugins/state?account=" + enc],
    ["bindings", "/api/accounts/plugin-profiles?account=" + enc],
    ["profiles", "/api/plugins/profiles"],
    ["effMsg", "/api/ratelimit/effective?action=message&account=" + enc],
    ["effDm", "/api/ratelimit/effective?action=dm&account=" + enc],
    ["channels", "/api/accounts/channels?account=" + enc],
    ["stats", "/api/ui/stats?account=" + enc],
    ["dataNodes", "/api/accounts/data?account=" + enc],
  ];
  Promise.allSettled(slices.map(([key, path]) =>
    api(path).then(data => {
      delete ex.errors[key];
      ex[key] = data;
    }).catch(e => {
      ex.errors[key] = e.message || "unavailable";
      /* keep the prior value: null on first load (renders "—"),
         last-good on a refresh — a failed refresh never lies */
    })
  )).then(() => scheduleRender());
  loadStatsNow().catch(() => {});
}

/* ── 1. hero ─────────────────────────────────────────────────────────────── */
function acctHero(a) {
  const s = acctStatus(a.status);
  const hero = el("div", {class: "hero " + s.tone,
    title: a.last_error ? String(a.last_error) : null});
  const cred = acctCred(a.credential);
  /* bordered stat pills (combined mockup: UPTIME / PING / CRED / GRP);
     `live` marks VOLATILE pills — paintLive() updates their text in
     place on every WS push instead of rebuilding the page */
  const pill = (label, value, tone, live) => el("span",
    {class: "ms-pill" + (tone ? " " + tone : "")},
    el("span", {class: "k"}, label),
    el("b", {class: "v", dataset: live ? {live} : {}}, value));
  hero.append(
    el("span", {class: "hero-state"}, dot(
      s.tone === "ok" ? "on" : s.tone === "err" ? "err" : "off"),
      s.label.toUpperCase()),
    el("span", {class: "hero-name"}, a.name),
    a.user_id ? el("span", {class: "dim mono"}, "user " + a.user_id) : "",
    el("div", {class: "hero-meta"},
      pill("uptime", acctUptime(a.uptime_s), null, "uptime"),
      pill("ping", acctLatency(a.latency_ms),
        a.latency_ms != null && a.latency_ms < 200 ? "ok" : "", "latency"),
      pill("cred", cred.label,
        cred.tone === "ok" ? "ok" : cred.tone === "err" ? "err" : ""),
      pill("grp", a.sync_group || "independent"),
      a.cooldowns != null ? pill("429s", String(a.cooldowns),
        a.cooldowns > 0 ? "warn" : "", "cooldowns") : null,
      a.enabled === false ? pill("enabled", "no", "err") : null));
  return hero;
}

/* ── 2. ABILITIES ────────────────────────────────────────────────────────── */
function acctAbilitiesSection(a, ex, rerender) {
  const sec = el("div", {class: "section"});
  const assignAct = el("button", {class: "sec-act", type: "button"},
    "+ assign");
  assignAct.onclick = () => acctOpenAssign(a, rerender);
  sec.append(el("div", {class: "sec-hdr"},
    el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
               "stroke-width": "2", "aria-hidden": "true"},
      el("path", {d: "M13 2 3 14h9l-1 8 10-12h-9l1-8z"})),
    "Abilities", assignAct));

  const wrap = el("div", {class: "sec-body"});
  const assigned = a.assigned_plugins || [];
  const manifests = acctManifestMap();
  if (!assigned.length) {
    wrap.append(el("p", {class: "hint"},
      "No plugins assigned — this account does nothing yet. Use “+ assign”."));
  }
  for (const p of assigned) {
    /* LIVE row first (WS-refreshed every second); the REST state snapshot
       is only a fallback for rows that lack per-plugin info. TRI-STATE:
       the runtime row only exists for LOADED plugins — missing data is
       UNKNOWN, never "enabled" (the old `!== false` turned a not-loaded
       plugin into a green dot) */
    const state = (a.plugins || {})[p] ||
      (ex.states || []).find(s => s.name === p) || {};
    const stOn = state.enabled === true;
    const stOff = state.enabled === false;
    const stUnknown = !stOn && !stOff;
    const hasErr = !!(state.last_error || state.error_count);
    const man = manifests[p] || {};

    const toggle = el("button", {
      class: "toggle" + (stOn ? " on" : ""), type: "button",
      role: "switch", "aria-checked": stOn ? "true" : stOff ? "false" : "mixed",
      "aria-label": (stOn ? "Disable " : "Enable ") + p + " on " + a.name,
      title: stUnknown ? "State unknown (plugin not loaded) — click to enable"
        : (stOn ? "Disable" : "Enable") + " " + p});
    toggle.onclick = acctLockToggle(toggle, async () => {
      try {
        await post("/api/plugins/set-enabled",
          {account: a.name, plugin: p, enabled: !stOn});
        toast(p + (stOn ? " disabled" : " enabled"), "ok");
        await loadStatsNow();
      } catch (e) { toast(e.message, "err"); }
    });

    const un = iconBtn("M18 6 6 18M6 6l12 12",
      "Unassign " + p, null, "Unassign " + p);
    un.onclick = busy(un, async () => {
      const ok = await confirmBox({title: "Unassign ability",
        body: "Unassign " + p + " from " + a.name +
          "? Its bindings stay but take no effect until re-assigned.",
        yes: "Unassign"});
      if (!ok) return;
      try {
        await post("/api/accounts/plugins/unassign", {account: a.name, plugin: p});
        toast(p + " unassigned", "ok");
        await loadStatsNow();
        rerender();
      } catch (e) { toast(e.message, "err"); }
    });

    wrap.append(el("div", {class: "ability"},
      el("span", {class: "dot " + (hasErr ? "err" : stOn ? "on" : "off"),
                  "aria-hidden": "true",
                  title: stUnknown ? "state unknown (not loaded)" : undefined}),
      el("div", {class: "ab-info"},
        el("div", {class: "ab-n"}, p),
        el("div", {class: "ab-d"},
          hasErr ? String(state.last_error || "errors: " + state.error_count)
                 : (man.description || (stUnknown ? "not loaded — state unknown" : "plugin")))),
      el("div", {class: "ab-actions"},
        toggle,
        iconBtn("gear",
          "Plugin settings — " + p,
          () => acctOpenAbilityDialog(a, p, ex, rerender),
          "Settings, presets, logs, raw"),
        un)));
  }
  /* dashed assign button (combined mockup) — duplicates the header action
     as the row-list's natural terminal point */
  const addAbility = el("button", {class: "add-btn", type: "button",
    onclick: () => acctOpenAssign(a, rerender)}, "+ Assign ability…");
  wrap.append(addAbility);
  sec.append(wrap);
  return sec;
}

/* ── ability dialog (Phase A-11 steps 2-6): Settings/Presets/Logs/Raw ──────
   The mockup's combined modal: one wide four-tab dialog per assigned
   plugin. Opening it NEVER touches the account's execution lifecycle. */
function acctOpenAbilityDialog(a, pluginName, ex, rerender) {
  const man = acctManifestMap()[pluginName] || {};
  dialog({
    title: `${pluginName} — ${a.name}`,
    wide: true,
    tabs: [
      {id: "settings", label: "Settings",
        build: p => acctAbilitySettingsTab(p, a, pluginName, man)},
      {id: "presets", label: "Presets",
        build: p => acctAbilityPresetsTab(p, a, pluginName, ex, rerender)},
      {id: "logs", label: "Logs",
        build: p => acctAbilityLogsTab(p, a)},
      {id: "raw", label: "Raw",
        build: p => acctAbilityRawTab(p, a, pluginName)},
    ],
  });
}

/* Settings tab: the plugin's REAL schema-driven configuration for this
   account (verified contracts: /api/plugins/settings?account= +
   /api/plugins/set-setting). Graceful when the plugin declares none. */
function acctAbilitySettingsTab(panel, a, pluginName, man) {
  const schema = man.settings || [];
  if (!schema.length) {
    panel.append(emptyState("No settings",
      `${pluginName} declares no configurable settings.`));
    return;
  }
  const save = el("button", {class: "btn btn-primary btn-sm", type: "button"}, "Save settings");
  save.disabled = true;   /* no saving while the form loads */
  const bar = el("div", {class: "toolbar"}, save);
  const skel = el("div", {class: "skel"}, el("div"), el("div"));
  /* the form HOST is attached to the panel: schema fields must be
     VISIBLE here, and collectSchema must read from this same element
     (the baseline built fields into a detached div and collected from
     the panel — invisible controls, fabricated "saved" success) */
  const formHost = el("div", {class: "form"});
  panel.append(skel, formHost, bar);

  const load = () =>
    api("/api/plugins/settings?account=" + encodeURIComponent(a.name))
      .then(list => {
        skel.remove();
        const entry = (list || []).find(x => x.name === pluginName);
        const values = (entry && entry.settings) || {};
        formHost.textContent = "";
        for (const def of schema)
          formHost.append(schemaField(def, values[def.name]));
        save.disabled = false;
      })
      .catch(e => {
        skel.remove();
        formHost.textContent = "";
        formHost.append(emptyState("Settings unavailable", e.message));
        save.disabled = true;   /* failed load ≠ saveable */
      });

  save.onclick = busy(save, async () => {
    const values = collectSchema(formHost, schema);
    if (!values) return;
    let written = 0;
    try {
      for (const key of Object.keys(values)) {
        if (values[key] === null && schema.find(d => d.name === key)?.default == null) continue;
        await post("/api/plugins/set-setting",
          {account: a.name, plugin: pluginName, key, value: values[key]});
        written++;
      }
      toast(`settings saved for ${a.name} (${written} value${written === 1 ? "" : "s"})`, "ok");
    } catch (e) {
      /* partial failure is honest: what wrote, what didn't, and the
         form reloads from the server so the visible values match the
         persisted truth */
      toast(written
        ? `saved ${written}, then failed: ${e.message}`
        : e.message, "err");
    }
    await load();
  });
  load();
}

/* Presets tab: the profile library for this plugin + this account's
   binding. FRESH data: the panel fetches bindings+profiles when the
   tab first opens (tabs build lazily now) and re-fetches after every
   mutation — the open dialog always agrees with the server. */
function acctAbilityPresetsTab(panel, a, pluginName, ex, rerender) {
  const enc = encodeURIComponent(a.name);
  const skel = el("div", {class: "skel"}, el("div"), el("div"));
  panel.append(skel);
  const refresh = async () => {
    try {
      const [bindings, profiles] = await Promise.all([
        api("/api/accounts/plugin-profiles?account=" + enc),
        api("/api/plugins/profiles"),
      ]);
      ex.bindings = bindings || {};
      ex.profiles = profiles || [];
    } catch (e) {
      skel.remove();
      panel.textContent = "";
      panel.append(emptyState("Presets unavailable", e.message));
      return;
    }
    if (typeof rerender === "function") rerender();  // MEMORY section agrees
    renderPresets(panel, a, pluginName, ex, refresh);
  };
  refresh();
}

function renderPresets(panel, a, pluginName, ex, refresh) {
  const bindings = ex.bindings || a.plugin_profiles || {};
  const active = bindings[pluginName];
  const profiles = (ex.profiles || []).filter(r => r.plugin === pluginName);

  panel.append(el("div", {class: "kv"},
    el("span", {class: "k"}, "active preset"),
    el("span", {class: "v"}, active || "plugin defaults")));

  if (!profiles.length) {
    panel.append(emptyState("No presets yet",
      "Save the plugin's current settings as the first preset below."));
  }
  for (const r of profiles) {
    const loadBtn = el("button", {class: "load", type: "button"}, "load");
    loadBtn.onclick = busy(loadBtn, async () => {
      try {
        await post("/api/accounts/plugin-profile",
          {account: a.name, plugin: pluginName, profile: r.name});
        toast(`preset "${r.name}" loaded`, "ok");
        await loadStatsNow();
        await refresh();
      } catch (e) { toast(e.message, "err"); }
    });
    const delBtn = el("button", {class: "del", type: "button"}, "delete");
    delBtn.onclick = busy(delBtn, async () => {
      const ok = await confirmBox({title: "Delete preset",
        body: `Delete ${pluginName} preset "${r.name}"? Accounts bound to it fall back to plugin defaults.`,
        yes: "Delete"});
      if (!ok) return;
      try {
        await post("/api/plugins/profiles/delete", {plugin: pluginName, name: r.name});
        toast("preset deleted", "ok");
        await refresh();
      } catch (e) { toast(e.message, "err"); }
    });
    panel.append(el("div", {class: "preset"},
      el("span", {class: "pn"}, r.name),
      active === r.name ? el("span", {class: "badge badge-ok"}, "active") : null,
      el("span", {class: "pc"}, Object.keys(r.settings || {}).length + " settings"),
      el("div", {class: "pa"}, loadBtn, delBtn)));
  }
  if (active) {
    const clearBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button",
      title: "Unbind and fall back to plugin defaults"}, "clear binding (" + active + ")");
    clearBtn.onclick = busy(clearBtn, async () => {
      try {
        await post("/api/accounts/plugin-profile",
          {account: a.name, plugin: pluginName, profile: null});
        toast("binding cleared — plugin defaults", "ok");
        await loadStatsNow();
        await refresh();
      } catch (e) { toast(e.message, "err"); }
    });
    panel.append(clearBtn);
  }

  /* save the CURRENT account settings as a new preset (GPT step 4:
     settings fetched + serialized before create) */
  const nameIn = el("input", {type: "text",
    placeholder: "preset name — lowercase, digits, dashes",
    "aria-label": "New preset name", autocomplete: "off"});
  const saveBtn = el("button", {class: "btn btn-secondary btn-sm", type: "button"}, "Save current as preset");
  saveBtn.onclick = busy(saveBtn, async () => {
    const pname = nameIn.value.trim();
    if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(pname)) {
      toast("Preset name: lowercase letters, digits, dashes.", "err");
      return;
    }
    try {
      const list = await api("/api/plugins/settings?account=" + encodeURIComponent(a.name));
      const entry = (list || []).find(x => x.name === pluginName);
      const settings = (entry && entry.settings) || {};
      if (!Object.keys(settings).length) { toast("No settings to save.", "err"); return; }
      await post("/api/plugins/profiles/create",
        {plugin: pluginName, name: pname, description: `saved from ${a.name}`, settings});
      toast(`preset "${pname}" saved`, "ok");
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  });
  panel.append(el("div", {class: "toolbar"}, nameIn, saveBtn));
}

/* Logs tab: this account's real log tail (bounded), not mockup samples. */
function acctAbilityLogsTab(panel, a) {
  const skel = el("div", {class: "skel"}, el("div"), el("div"), el("div"));
  panel.append(skel);
  api("/api/logs?account=" + encodeURIComponent(a.name) + "&n=100").then(rows => {
    skel.remove();
    if (!rows || !rows.length) {
      panel.append(emptyState("No log entries", "Nothing logged for this account yet."));
      return;
    }
    const box = el("div", {class: "logbox"});
    for (const r of rows.slice(-200)) {
      box.append(el("div", {class: "log-line"},
        el("span", {class: "ts mono"}, fmtTs(r.ts)),
        el("span", {class: "sev " + String(r.severity || "info").toLowerCase()},
          String(r.severity || "INFO").toUpperCase()),
        el("span", {class: "log-msg"}, r.message ?? "")));
    }
    panel.append(box);
  }).catch(e => {
    skel.remove();
    panel.append(emptyState("Logs unavailable", e.message));
  });
}

/* Raw tab: the plugin's real state row, credential-redacted. Layered:
       1. KEY-NAME match (nested — the JSON replacer recurses) covering
          case variants and common credential-like names,
       2. VALUE-SHAPE match (Discord-token / JWT / bearer shapes) so an
          oddly-named credential still redacts,
       3. an output cap so a huge state blob cannot wedge the panel. */
function acctAbilityRawTab(panel, a, pluginName) {
  const skel = el("div", {class: "skel"}, el("div"), el("div"), el("div"));
  panel.append(skel);
  api("/api/plugins/state?account=" + encodeURIComponent(a.name)).then(rows => {
    skel.remove();
    const st = (rows || []).find(x => x.name === pluginName);
    if (!st) { panel.append(emptyState("No state", "The plugin is not loaded on this account.")); return; }
    const REDACT_KEYS =
      /(token|credential|secret|password|authorization|auth|bearer|session|cookie|api[_-]?key|private[_-]?key)/i;
    const REDACT_VALUES =
      /^(eyJ[A-Za-z0-9_-]{8,}\.|mfa\.[A-Za-z0-9_-]{10,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,})/;
    const scrub = v => JSON.parse(JSON.stringify(v, (k, val) => {
      if (typeof k === "string" && REDACT_KEYS.test(k)) return "[redacted]";
      if (typeof val === "string" && REDACT_VALUES.test(val.trim()))
        return "[redacted]";
      return val;
    }));
    let text = JSON.stringify(scrub(st), null, 2);
    const CAP = 64 * 1024;
    if (text.length > CAP)
      text = text.slice(0, CAP) + "\n\u2026 [truncated at " + CAP + " bytes]";
    const pre = el("pre", {class: "dui-code mono"}, text);
    panel.append(pre);
  }).catch(e => {
    skel.remove();
    panel.append(emptyState("Raw state unavailable", e.message));
  });
}

/* ── 3. MEMORY (plugin profiles) ─────────────────────────────────────────── */

/* positive channel-set summary: "N channels" | "all channels" */
function scopeSummary(settings) {
  const scope = settings && settings.channels;
  const n = (scope && Array.isArray(scope.channels)) ? scope.channels.length : 0;
  return n ? `${n} channel${n === 1 ? "" : "s"}` : "all channels";
}

function acctMemorySection(a, ex, rerender) {
  const sec = el("div", {class: "section"});
  const galleryBtn = el("button", {class: "sec-act", type: "button"}, "Profile gallery");
  galleryBtn.onclick = () => acctProfileGallery(a, ex, rerender);
  sec.append(el("div", {class: "sec-hdr"},
    el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
               "stroke-width": "2", "aria-hidden": "true"},
      el("path", {d: "M21 12c0 1.66-4 3-9 3s-9-1.34-9-3M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5M21 12V5c0-1.66-4-3-9-3S3 3.34 3 5"})),
    "Memory", galleryBtn));
  const wrap = el("div", {class: "sec-body"});
  const assigned = a.assigned_plugins || [];
  const bindings = ex.bindings || a.plugin_profiles || {};

  if (!assigned.length) {
    wrap.append(el("p", {class: "hint"},
      "Profiles bind per assigned plugin — assign an ability first (see ABILITIES above)."));
    sec.append(wrap);
    return sec;
  }
  if (ex.profiles === null) {           // still loading
    wrap.append(el("p", {class: "hint"}, "loading profile library…"));
    sec.append(wrap);
    return sec;
  }

  /* concise: one kv row of bindings — detail + management live in the gallery */
  const row = el("div", {class: "kv-row"});
  for (const p of assigned) {
    const bound = bindings[p];
    const prof = bound
      ? (ex.profiles || []).find(r => r.plugin === p && r.name === bound)
      : null;
    row.append(el("div", {class: "kv"},
      el("span", {class: "k"}, p),
      el("span", {class: "v"},
        bound ? bound + " · " + scopeSummary(prof && prof.settings) : "default")));
  }
  wrap.append(row);

  /* runtime-state strip: per-plugin RUNTIME enablement — distinct from
     the AccountPluginData documents below (runtime flag vs stored data) */
  const statesRow = el("div", {class: "kv-row", style: "margin-top:var(--sp-2)"},
    el("div", {class: "kv"},
      el("span", {class: "k dim", style: "text-transform:uppercase;"}),
      el("span", {class: "v dim"}, "runtime state")));
  for (const p of assigned) {
    const st = (a.plugins || {})[p] || (ex.states || []).find(s => s.name === p) || {};
    statesRow.append(el("div", {class: "kv"},
      el("span", {class: "k"}, p),
      el("span", {class: "v",
        title: st.enabled === true ? "enabled (runtime)"
          : st.enabled === false ? "disabled (runtime)"
          : "state unknown — plugin not loaded"},
        st.enabled === true ? "on"
          : st.enabled === false ? "off" : "—",
        st.error_count ? el("span", {class: "badge badge-err"},
          String(st.error_count) + " err") : null)));
  }
  wrap.append(statesRow);
  wrap.append(acctPluginDataBrowser(a, ex, rerender));
  sec.append(wrap);
  return sec;
}

/* PHASE 10 (B3): the AccountPluginData browser — the MEMORY section's
   per-plugin per-channel Data view. One fetch lists every scope node;
   the LOCAL render puts NUMERIC keys in stat pills and everything else
   in a kv inspector (fleet rollup is opt-in via task-definition metric
   hints, not schema guessing). Selections survive rerenders. */
const _dataPick = {};   // account -> {plugin, key} (module-scoped on purpose)

function acctPluginDataBrowser(a, ex, rerender) {
  const host = el("div", {style: "margin-top:var(--sp-3);"});
  host.append(el("div", {class: "k dim",
    style: "font-size:var(--fs-xs);text-transform:uppercase;"},
    "plugin data (per channel)"));
  const nodes = Array.isArray(ex.dataNodes) ? ex.dataNodes : null;
  if (nodes === null) {
    host.append(el("p", {class: "hint"}, "loading plugin data…"));
    return host;
  }
  const assigned = a.assigned_plugins || [];
  const pluginsWithData = [...new Set(nodes.map(n => n.plugin))];
  const pickPlugins = [...new Set([...pluginsWithData,
    ...assigned.filter(p => pluginsWithData.includes(p))])].sort();
  if (!pickPlugins.length) {
    host.append(el("p", {class: "hint"},
      "No plugin documents yet — plugin flows write per-channel data through their services API."));
    return host;
  }
  const pick = _dataPick[a.name] || {};
  if (!pickPlugins.includes(pick.plugin)) pick.plugin = pickPlugins[0];

  const scopeLabel = (guildId, channelId, channels) => {
    if (guildId === "0" && channelId === "0") return "plugin-global";
    const guild = (channels || []).find(g => String(g.guild_id) === guildId);
    const gname = (guild && guild.guild_name) || guildId;
    if (channelId === "0") return `guild-level · ${gname}`;
    const ch = guild && (guild.channels || []).find(
      c => String(c.id) === channelId);
    return `#${(ch && ch.name) || channelId} · ${gname}`;
  };

  const pluginNodes = nodes.filter(n => n.plugin === pick.plugin);
  const keys = pluginNodes.map(
    n => `${n.guild_id}/${n.channel_id}`);
  if (!keys.includes(pick.key)) pick.key = keys[0];
  _dataPick[a.name] = {plugin: pick.plugin, key: pick.key};

  const pluginSel = el("select", {"aria-label": "Data plugin"},
    ...pickPlugins.map(p => el("option", {value: p,
      selected: p === pick.plugin ? "" : undefined}, p)));
  pluginSel.onchange = () => {
    _dataPick[a.name] = {plugin: pluginSel.value, key: null};
    rerender();
  };
  const scopeSel = el("select", {"aria-label": "Data scope"},
    ...pluginNodes.map(n => {
      const key = `${n.guild_id}/${n.channel_id}`;
      return el("option", {value: key,
        selected: key === pick.key ? "" : undefined},
        scopeLabel(n.guild_id, n.channel_id, ex.channels));
    }));
  scopeSel.onchange = () => {
    _dataPick[a.name].key = scopeSel.value;
    rerender();
  };
  host.append(el("div", {class: "toolbar",
    style: "flex-wrap:wrap;gap:var(--sp-2);"}, pluginSel,
    pluginNodes.length ? scopeSel : null));

  const node = pluginNodes.find(
    n => `${n.guild_id}/${n.channel_id}` === pick.key);
  if (!node) {
    host.append(el("p", {class: "hint"},
      "No documents for this plugin yet."));
    return host;
  }
  host.append(acctPluginDataCard(node));
  return host;
}

/* one document card: numeric keys -> stat pills, the rest -> kv rows */
function acctPluginDataCard(node) {
  const card = el("div", {class: "card", style: "margin-top:var(--sp-2);"});
  card.append(el("div", {class: "dim",
    style: "font-size:var(--fs-xs);display:flex;gap:var(--sp-3);"},
    el("span", {}, "v" + String(node.version ?? 1)),
    node.updated_at ? el("span", {}, fmtAge(node.updated_at)) : null));
  const entries = Object.entries(node.data || {});
  if (!entries.length) {
    card.append(el("p", {class: "hint"}, "empty document"));
    return card;
  }
  const numeric = entries.filter(
    ([, v]) => typeof v === "number" && Number.isFinite(v));
  if (numeric.length) {
    const pills = el("div", {class: "kv-row", style: "margin-top:var(--sp-2);"});
    for (const [k, v] of numeric)
      pills.append(el("span", {class: "ms-pill"},
        el("span", {class: "k"}, k),
        el("b", {class: "v mono"}, String(Math.round(v * 1000) / 1000))));
    card.append(pills);
  }
  const rest = entries.filter(
    ([k, v]) => !(typeof v === "number" && Number.isFinite(v)));
  for (const [k, v] of rest)
    card.append(el("div", {class: "kv"},
      el("span", {class: "k"}, k),
      el("span", {class: "v",
        style: "overflow-wrap:anywhere;"},
        typeof v === "string" ? v : JSON.stringify(v))));
  return card;
}

/* the gallery: every profile, grouped by plugin; bind/unbind this account,
   edit (schema + channel scope), delete — one dialog, no page sprawl */
async function acctProfileGallery(a, ex, rerender) {
  const dlg = dialog({title: `Profile gallery — ${a.name}`, wide: true,
    bodyEl: el("div", {class: "skel"}, el("div"), el("div"), el("div"))});
  let chanGroups = [];
  try {
    chanGroups = await api("/api/accounts/channels?account=" +
      encodeURIComponent(a.name)) || [];
  } catch { chanGroups = []; }   /* offline account: scope editing degrades */

  const refresh = async () => {
    try {
      ex.profiles = await api("/api/plugins/profiles") || [];
      ex.bindings = await api("/api/accounts/plugin-profiles?account=" +
        encodeURIComponent(a.name)) || {};
    } catch (e) { toast(e.message, "err"); return; }
    await acctDetailRefresh(a.name, ex, rerender);
    dlg.body.textContent = "";
    dlg.body.append(galleryBody(a, ex, chanGroups, rerender, refresh));
  };
  await refresh();
}

function galleryBody(a, ex, chanGroups, rerender, refresh) {
  const wrap = el("div", null);
  const assigned = a.assigned_plugins || [];
  const bindings = ex.bindings || a.plugin_profiles || {};
  const byPlugin = {};
  for (const r of (ex.profiles || []))
    (byPlugin[r.plugin] = byPlugin[r.plugin] || []).push(r);
  const order = [
    ...assigned.filter(p => byPlugin[p]),
    ...Object.keys(byPlugin).filter(p => !assigned.includes(p)).sort()];

  if (!order.length)
    return wrap.append(el("div", {class: "empty"},
      el("div", {class: "empty-msg"},
        "No profiles in the library yet — build one from a plugin's settings.")));

  for (const p of order) {
    const isAssigned = assigned.includes(p);
    const group = el("div", {class: "preset-group"},
      el("div", {class: "pg-name"}, p,
        isAssigned ? null : el("span", {class: "faint"}, " · not assigned")));
    for (const r of byPlugin[p]) {
      const active = bindings[p] === r.name;
      const bindBtn = el("button", {class: "load", type: "button",
        title: isAssigned ? "Bind this profile to " + a.name
                          : "Assign " + p + " to the account first"});
      bindBtn.textContent = active ? "rebind" : "bind";
      if (!isAssigned) bindBtn.disabled = true;
      bindBtn.onclick = busy(bindBtn, async () => {
        try {
          await post("/api/accounts/plugin-profile",
            {account: a.name, plugin: p, profile: r.name});
          toast(`“${r.name}” bound to ${a.name}`, "ok");
          await loadStatsNow();
          await refresh();
        } catch (e) { toast(e.message, "err"); }
      });
      const editBtn = el("button", {class: "load", type: "button"}, "edit");
      editBtn.onclick = () =>
        acctProfileEditor(a, p, r, chanGroups, refresh);
      const delBtn = el("button", {class: "del", type: "button"}, "delete");
      delBtn.onclick = busy(delBtn, async () => {
        const ok = await confirmBox({title: "Delete profile",
          body: `Delete ${p} profile “${r.name}” for EVERY account? This cannot be undone.`,
          yes: "Delete"});
        if (!ok) return;
        try {
          await post("/api/plugins/profiles/delete", {plugin: p, name: r.name});
          toast("profile deleted", "ok");
          await refresh();
        } catch (e) { toast(e.message, "err"); }
      });
      group.append(el("div", {class: "preset" + (active ? " on" : "")},
        el("span", {class: "pn"}, r.name),
        active ? el("span", {class: "badge badge-ok"}, "active") : null,
        el("span", {class: "pc"}, scopeSummary(r.settings)),
        el("div", {class: "pa"}, bindBtn, editBtn, delBtn)));
    }
    if (isAssigned && bindings[p]) {
      const clearBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"},
        "clear binding (" + bindings[p] + ")");
      clearBtn.onclick = busy(clearBtn, async () => {
        try {
          await post("/api/accounts/plugin-profile",
            {account: a.name, plugin: p, profile: null});
          toast("binding cleared — plugin defaults", "ok");
          await loadStatsNow();
          await refresh();
        } catch (e) { toast(e.message, "err"); }
      });
      group.append(clearBtn);
    }
    wrap.append(group);
  }
  return wrap;
}

/* profile editor (bespoke port): schema fields via the global schemaField,
   channel scope via the builder's channel-tree picker; offline account
   keeps the stored scope untouched instead of silently clearing it */
function acctProfileEditor(a, plugin, prof, chanGroups, onSaved) {
  const manifests = storeGet("manifests") || [];
  const manifest = manifests.find(m => m.name === plugin);
  const schema = (manifest && manifest.settings) || [];
  const settings = (prof && prof.settings) || {};
  const storedScope = (settings.channels && Array.isArray(settings.channels.channels))
    ? settings.channels.channels.map(String) : [];

  const nameInput = el("input", {type: "text", maxlength: "64",
    value: prof ? prof.name : "", placeholder: "e.g. farm-fast"});
  const descInput = el("input", {type: "text", maxlength: "200",
    value: prof ? (prof.description || "") : "", placeholder: "what this bundle is for"});

  const pickerGroups = (chanGroups || [])
    .map(g => ({
      id: String(g.guild_id || ""),
      name: String(g.guild_name || g.guild_id || ""),
      channels: (g.channels || [])
        .map(c => ({id: String(c.id || ""), name: String(c.name || c.id || "")}))
        .filter(c => c.id)}))
    .filter(g => g.id && g.channels.length);
  const scopeField = el("div", {class: "field"},
    el("label", {}, "Channels"),
    pickerGroups.length
      ? bChannelTree({groups: pickerGroups, value: storedScope})
      : el("div", {class: "hint"},
          storedScope.length
            ? `scoped to ${storedScope.length} channel${storedScope.length === 1 ? "" : "s"} — start ${a.name} to re-pick`
            : `all channels — start ${a.name} to pick specific channels`));

  const form = el("div", {class: "form"},
    el("div", {class: "field"}, el("label", {}, "Profile name"), nameInput),
    el("div", {class: "field"}, el("label", {}, "Description"), descInput),
    schema.length
      ? el("div", null, ...schema.map(def => schemaField(def, settings[def.name])))
      : el("p", {class: "hint"},
          "This plugin declares no settings — the profile carries name, description and channel scope."),
    scopeField);

  dialog({title: `Edit profile — ${plugin}`, wide: true, bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Save profile", variant: "primary", onclick: (close) => {
      const name = nameInput.value.trim();
      if (!name) { toast("Profile name is required.", "err"); return; }
      const values = collectSchema(form, schema);
      if (!values) return;
      const picked = [...form.querySelectorAll(".ctree-chan:checked")].map(i => i.value);
      if (pickerGroups.length) {
        if (picked.length) values.channels = {channels: picked};
        /* empty pick = unrestricted: the key stays absent */
      } else if (storedScope.length) {
        values.channels = settings.channels;   /* offline: preserve as-is */
      }
      const body = {plugin, name: prof.name, description: descInput.value,
                    settings: values};
      if (name !== prof.name) body.name_new = name;
      post("/api/plugins/profiles/update", body)
        .then(() => { close(); toast(`profile ${name} saved`, "ok"); onSaved && onSaved(); })
        .catch(e => toast(e.message, "err"));
    }},
  ]});
  nameInput.focus();
}

/* ── 4. PACING ───────────────────────────────────────────────────────────── */
function acctPacingSection(a, ex) {
  const sec = el("div", {class: "section"});
  const tune = el("a", {class: "sec-act", href: "#/policies"}, "Configure");
  sec.append(el("div", {class: "sec-hdr"},
    el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
               "stroke-width": "2", "aria-hidden": "true"},
      el("circle", {cx: "12", cy: "12", r: "10"}),
      el("path", {d: "M12 6v6l4 2"})),
    "Pacing", tune));
  const grid = el("div", {class: "pacing-grid"});
  const item = (label, value, vClass) => el("div", {class: "pacing-item"},
    el("div", {class: "pl"}, label),
    el("div", {class: "pv" + (vClass ? " " + vClass : "")}, value));

  const cd = a.cooldowns || 0;
  grid.append(
    item("Sync group", a.sync_group || "independent"),
    item("Messages", ex.effMsg ? acctFmtSecs(ex.effMsg.interval) : "…"),
    item("Message jitter", acctJitterTxt(ex.effMsg)),
    item("DMs", ex.effDm ? acctFmtSecs(ex.effDm.interval) : "…"),
    item("DM jitter", acctJitterTxt(ex.effDm)),
    item("429 cooldowns", cd ? cd + " active" : "0 active", cd ? "warn" : "ok"));

  const sources = (ex.effMsg && ex.effMsg.sources) || [];
  const secBody = el("div", {class: "sec-body"}, grid);
  if (sources.length > 1) {
    secBody.append(el("p", {class: "hint"},
      "Composed from " + sources.length + " policies: " +
      sources.map(s => s.level + (s.owner ? ":" + s.owner : "")).join(", ") +
      ". Tune them on the Policies page."));
  }
  sec.append(secBody);
  return sec;
}

/* ── 4b. LIVE STATS (plugin-contributed StatCards, per account) ──────────── */
function acctLiveStatsSection(a, ex) {
  const sec = el("div", {class: "section"});
  sec.append(el("div", {class: "sec-hdr"},
    el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
               "stroke-width": "2", "aria-hidden": "true"},
      el("path", {d: "M3 12h4l3 8 4-16 3 8h4"})),
    "Live stats"));
  const wrap = el("div", {class: "sec-body"});
  const cards = ex.stats;
  if (cards === null) {
    wrap.append(el("p", {class: "hint"}, "loading plugin stats…"));
  } else if (!cards.length) {
    wrap.append(el("p", {class: "hint"},
      "No plugin stats yet — plugins that report activity surface live counters here."));
  } else {
    const row = el("div", {class: "pill-row", style: "margin-bottom:0"});
    for (const c of cards) {
      row.append(el("span", {class: "ms-pill " + (c.level || ""),
        title: c.sub || c.label},
        el("span", {class: "k"}, c.label),
        el("b", {class: "v"}, String(c.value ?? "—"))));
    }
    wrap.append(row);
  }
  sec.append(wrap);
  return sec;
}

/* ── 5. OPERATIONS ───────────────────────────────────────────────────────── */
function acctOperationsSection(a, channels, rerender) {
  const sec = el("div", {class: "section"});
  sec.append(el("div", {class: "sec-hdr"},
    el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
               "stroke-width": "2", "aria-hidden": "true"},
      el("path", {d: "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"})),
    "Operations"));

  const wrap = el("div", {class: "sec-body"});
  const bar = el("div", {class: "toolbar"});

  const running = a.status === "running";
  const startStop = el("button",
    {class: "btn btn-sm " + (running ? "btn-secondary" : "btn-primary"), type: "button"},
    running ? "Stop" : "Start");
  startStop.onclick = busy(startStop, async () => {
    try {
      await post(running ? "/api/accounts/stop" : "/api/accounts/start",
        {account: a.name});
      toast((running ? "stop" : "start") + " requested", "ok");
      await loadStatsNow();
    } catch (e) { toast(e.message, "err"); }
  });

  const enabled = a.enabled !== false;
  const enToggle = el("button", {
    class: "toggle" + (enabled ? " on" : ""), type: "button",
    role: "switch", "aria-checked": enabled ? "true" : "false",
    "aria-label": (enabled ? "Disable" : "Enable") + " " + a.name,
    title: enabled ? "Disable account" : "Enable account"});
  enToggle.onclick = acctLockToggle(enToggle, async () => {
    try {
      await post("/api/accounts/enable", {name: a.name, enabled: !enabled});
      toast(a.name + (enabled ? " disabled" : " enabled"), "ok");
      await loadStatsNow();
    } catch (e) { toast(e.message, "err"); }
  });

  const credBtn = el("button", {class: "btn btn-sm btn-secondary", type: "button"},
    "Set credential…");
  credBtn.onclick = () => acctOpenCredential(a);

  const estopBtn = el("button", {class: "btn btn-sm btn-danger", type: "button"},
    "Emergency stop");
  estopBtn.onclick = busy(estopBtn, async () => {
    const ok = await confirmBox({title: "Emergency stop",
      body: "Cancel " + a.name + "'s tasks NOW and disconnect immediately? In-flight work is dropped.",
      yes: "Emergency Stop"});
    if (!ok) return;
    try {
      await post("/api/accounts/emergency-stop", {name: a.name});
      toast("emergency stop executed", "ok");
      await loadStatsNow();
    } catch (e) { toast(e.message, "err"); }
  });

  const rmBtn = el("button", {class: "btn btn-sm btn-danger", type: "button"},
    "Remove account");
  rmBtn.onclick = busy(rmBtn, async () => {
    const ok = await confirmBox({title: "Remove account",
      body: "Remove " + a.name +
        "? Its credential, cooldowns and state are deleted with it.",
      yes: "Remove"});
    if (!ok) return;
    try {
      await post("/api/accounts/remove", {name: a.name});
      toast("account removed", "ok");
      navigate("/accounts");
    } catch (e) { toast(e.message, "err"); }
  });

  bar.append(startStop,
    el("span", {class: "row"}, el("span", {class: "dim"}, "enabled"), enToggle),
    credBtn, estopBtn, rmBtn);
  wrap.append(bar);

  /* channels: visible text channels from the live cache ([] while offline) */
  const chans = el("div", {class: "row"});
  if (channels === null) {
    chans.append(el("span", {class: "faint"}, "channels: loading…"));
  } else if (!channels.length) {
    chans.append(el("span", {class: "faint"},
      "channels: none visible (account offline or no cache yet)"));
  } else {
    let total = 0;
    const chips = [];
    for (const g of channels) {
      for (const c of (g.channels || []).slice(0, 4)) {
        total++;
        if (chips.length < 12)
          chips.push(el("span", {class: "chip", title: g.guild_name || g.guild_id},
            "#" + c.name));
      }
    }
    chans.append(el("span", {class: "dim"}, "channels "), ...chips,
      total > chips.length ? el("span", {class: "chip"}, "+" + (total - chips.length)) : "");
  }
  wrap.append(chans);
  sec.append(wrap);
  return sec;
}

/* credential dialog — write-only value, never displayed back */
function acctOpenCredential(a) {
  const credIn = el("input", {type: "password", autocomplete: "off",
    placeholder: "paste the account token", "aria-label": "Credential token"});
  const form = el("div", {},
    el("div", {class: "field"},
      el("label", {}, "Credential for " + a.name), credIn,
      el("div", {class: "hint"},
        "Stored encrypted. It is never shown again anywhere in the dashboard.")));
  dialog({title: "Set Credential", bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Save", variant: "primary", onclick: (close) => {
      const btn = form.closest("dialog").querySelector(".dlg-foot .btn-primary");
      busy(btn, async () => {
        const credential = credIn.value.trim();
        if (!credential) return acctFbError(form, "paste a token or Cancel");
        try {
          await post("/api/accounts/credential", {name: a.name, credential});
          toast("credential stored", "ok");
          close();
          await loadStatsNow();
        } catch (e) { acctFbError(form, e.message); }
      })();
    }},
  ]});
  credIn.focus();
}

/* assignment dialog — available (unassigned) discovered plugins */
function acctOpenAssign(a, rerender) {
  const assigned = new Set(a.assigned_plugins || []);
  const manifests = storeGet("manifests") || [];
  const available = manifests.filter(m => !assigned.has(m.name));
  const list = el("div", {class: "picklist"});
  if (!available.length) {
    list.append(el("p", {class: "hint"},
      "Every discovered plugin is already assigned to " + a.name + "."));
  }
  for (const m of available) {
    list.append(el("label", {class: "pick-row"},
      el("input", {type: "checkbox", value: m.name}),
      el("span", {class: "pl-name"}, m.name),
      el("span", {class: "pl-desc"}, m.description || "")));
  }
  const form = el("div", {},
    el("p", {class: "dim"}, "Assign abilities to " + a.name + "."), list);
  dialog({title: "Assign Abilities", bodyEl: form, actions: [
    {label: "Cancel", variant: "secondary", onclick: close => close()},
    {label: "Assign", variant: "primary", onclick: (close) => {
      const btn = form.closest("dialog").querySelector(".dlg-foot .btn-primary");
      busy(btn, async () => {
        const chosen = [...list.querySelectorAll("input:checked")].map(i => i.value);
        if (!chosen.length) return acctFbError(form, "select at least one plugin");
        try {
          /* whole-set replace: server validates every name all-or-nothing */
          await post("/api/accounts/plugins/set",
            {account: a.name, plugins: [...assigned, ...chosen]});
          toast(chosen.length + " abilit" + (chosen.length > 1 ? "ies" : "y") + " assigned", "ok");
          close();
          await loadStatsNow();
          rerender();
        } catch (e) { acctFbError(form, e.message); }
      })();
    }},
  ]});
}

/* plugin-profile binding dialog (the ability settings icon) */

/* ── route registration ──────────────────────────────────────────────────── */
routerAdd("/accounts", "Accounts", acctListMount);
routerAdd("/accounts/:name", "Account", acctDetailMount);