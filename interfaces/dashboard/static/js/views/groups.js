"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/groups.js — the Fleet Hub (PHASE 9, D6): bespoke SPA surface.
     #/groups         — fleet overview per group: members, assignment
                        counts, active cooldowns, pause state, last
                        transition ("what is this group doing and why")
     #/groups/:name   — the hub: OVERVIEW (state + why), MEMBERS
                        (eligibility + controls), TASK QUEUE (filters +
                        assign/withdraw/suspend/resume/cancel/handoff),
                        COOLDOWNS (source-tagged; manual clear only),
                        HISTORY (append-only transition log).
   Every mutation rides POST /api/fleet/op — the ONE dispatch into
   FleetService; destructive ops confirm first (spec §10).
   ═══════════════════════════════════════════════════════════════════════════ */

routerAdd("/groups", "Fleet", async (container) => {
  const state = {alive: true};
  /* child hosts ONLY: a late load() must never clear the ROUTER's
     container (a newer view may own it — see router's mount fence);
     every write goes through hdr/notice/host which the router's own
     clear detaches harmlessly */
  const newBtn = el("button", {class: "btn btn-primary btn-sm", type: "button"},
    el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
               "stroke-width": "2", "aria-hidden": "true"},
      el("path", {d: "M12 5v14M5 12h14"})),
    "New Group");
  newBtn.onclick = () => acctOpenGroupCreate(async () => {
    await Promise.allSettled([loadGroups(), loadStatsNow()]);
    await load();
  });
  const hdr = el("div", {class: "view-hdr"},
    el("div", {},
      el("h1", {}, "Fleet"),
      el("p", {class: "dim"},
        "Groups: membership + shared pacing + orchestration — one surface.")),
    el("div", {class: "actions"}, newBtn));
  const notice = el("div", null);
  const host = el("div", {class: "grid grid-2"},
    el("div", {class: "skel"}, el("div"), el("div"), el("div")));
  container.append(hdr, notice, host);
  let loadSeq = 0;   // a stale in-flight snapshot never overwrites a newer one

  async function load() {
    const seq = ++loadSeq;
    let groups;
    try {
      groups = await api("/api/fleet/groups");
    } catch (e) {
      if (!state.alive || seq !== loadSeq) return;
      notice.textContent = "";
      notice.append(emptyState("Fleet unavailable", e.message));
      host.textContent = "";
      return;
    }
    if (!state.alive || seq !== loadSeq) return;
    notice.textContent = "";
    host.textContent = "";
    if (!groups.length) {
      host.append(el("div", {class: "card"},
        emptyState("No groups yet",
          "A group paces its member accounts on one shared clock and — once joined to fleet management — orchestrates their work. Create the first one.")));
      return;
    }
    /* enrich with SYNC truth (the store's groups/accounts are the
       pacing-membership layer; the fleet bundle is the orchestration
       layer — the card shows BOTH honestly) */
    const syncGroups = storeGet("groups") || [];
    const accts = storeGet("accounts") || [];
    for (const g of groups) {
      const sync = syncGroups.find(x => x.name === g.group);
      g._sync = sync || null;
      g._syncMembers = accts.filter(a => a.sync_group === g.group).map(a => a.name);
    }
    for (const g of groups) host.append(groupCard(g));
  }

  function groupCard(g) {
    const counts = g.assignment_counts || {};
    const work = ["queued", "assigned", "running", "suspended"]
      .filter(s => counts[s])
      .map(s => `${counts[s]} ${s}`)
      .join(" · ") || "no live work";
    const last = g.last_transition;
    const syncCount = (g._syncMembers || []).length;
    const card = el("div", {
      class: "card acct-tile", style: "cursor:pointer;",
      onclick: () => navigate(`/groups/${encodeURIComponent(g.group)}`)},
      el("div", {class: "acct-name"}, g.group,
        g.paused ? el("span", {class: "badge badge-warn", style: "margin-left:var(--sp-2);"}, "fleet paused") : null,
        g._sync && g._sync.enabled === false
          ? el("span", {class: "badge badge-dim", style: "margin-left:var(--sp-2);"}, "pacing off") : null),
      el("div", {class: "meta"},
        el("b", {}, String(syncCount)), " pacing member" + (syncCount === 1 ? "" : "s"),
        g.member_count ? [" · ", el("b", {}, String(g.member_count)), " fleet-managed"] : [],
        (g.active_cooldowns ? [" · ", el("b", {class: "warn"}, String(g.active_cooldowns)), " cooldowns"] : null)),
      el("div", {class: "meta dim"}, work),
      last ? el("div", {class: "meta dim", title: last.reason || ""},
        `last: ${last.kind}${last.account ? " · " + last.account : ""}`) : null);
    return card;
  }

  await load();
  /* live convergence: fleet transitions (WS "fleet" frames) AND sync
     membership changes (the CRUD dialogs storeSet groups/accounts)
     re-run load() — guarded by alive + the stale-snapshot seq */
  const unsubFleet = storeSubscribe((key) => {
    if (!state.alive) return;
    if (key === "fleet" || key === "groups") load();
  });
  return () => { state.alive = false; unsubFleet(); };
});

routerAdd("/groups/:name", "Fleet hub", async (container, params) => {
  const name = decodeURIComponent(params.name || "");
  const state = {alive: true, filter: "all"};
  /* fatal-error host (load failures render HERE, never by clearing the
     router's container — a newer view may own it) */
  const fatal = el("div", null);
  container.append(el("div", {class: "view-hdr"},
    el("h1", {}, `Fleet — ${name}`),
    el("p", {class: "dim"}, "Members, work, cooldowns, history — one group under management.")),
    fatal);
  let loadSeq = 0;   // stale in-flight snapshots never overwrite newer ones

  const section = (title, hint, actions) => {
    const hdr = el("div", {class: "sec-hdr"}, el("span", {}, title));
    if (hint) hdr.append(el("span", {class: "dim",
      style: "margin-left:var(--sp-2);"}, hint));
    if (actions) hdr.append(actions);
    const bodyEl = el("div", {class: "sec-body"});
    container.append(el("div", {class: "section"}, hdr, bodyEl));
    return bodyEl;
  };

  const op = async (button, opName, opParams, confirmMsg) => {
    const fire = () => busy(button, async () => {
      try {
        await post("/api/fleet/op", {op: opName, params: opParams});
        toast(`${opName} ok`, "ok");
        await load();
      } catch (e) { toast(e.message, "err"); }
    });
    if (confirmMsg)
      confirmBox({title: "Confirm", body: confirmMsg, danger: true})
        .then(ok => { if (ok) fire(); });
    else fire();
  };

  const pauseBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"});
  pauseBtn.onclick = () => op(pauseBtn,
    pauseBtn.dataset.mode === "paused" ? "group_resume" : "group_pause",
    {group: name},
    pauseBtn.dataset.mode === "paused"
      ? null
      : `Pause group ${name}? Members stop receiving new work; in-flight sends drain.`);

  /* the unified Members section: SYNC membership (pacing) + FLEET state
     (orchestration) per account — the one place both layers are managed.
     Group settings (edit/delete) live in this section's header. */
  const addBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "Add member…");
  const editBtn = el("button", {class: "btn btn-sm btn-secondary", type: "button"}, "Edit group");
  const delBtn = el("button", {class: "btn btn-sm btn-danger", type: "button"}, "Delete group");
  addBtn.onclick = () => acctOpenGroupAddMember(
    {name}, storeGet("accounts") || [], async () => {
      await Promise.allSettled([loadGroups(), loadStatsNow()]);
      await load();
    });
  editBtn.onclick = () => {
    const g = (storeGet("groups") || []).find(x => x.name === name);
    if (g) acctOpenGroupEdit(g, async () => {
      await Promise.allSettled([loadGroups(), loadStatsNow()]);
      navigate("/groups");   // renamed: the hub keyed by the old name dies
      await Promise.allSettled([loadGroups()]);
    });
  };
  delBtn.onclick = busy(delBtn, async () => {
    const syncMembers = (storeGet("accounts") || [])
      .filter(a => a.sync_group === name).length;
    const ok = await confirmBox({title: "Delete group",
      body: `Delete ${name}? Its ${syncMembers} pacing member(s) move back to ` +
        `independent pacing and fleet-managed members are released.`,
      yes: "Delete"});
    if (!ok) return;
    try {
      await post("/api/groups/delete", {group: name, detach_members: true});
      toast("group deleted", "ok");
      await Promise.allSettled([loadGroups(), loadStatsNow()]);
      navigate("/groups");
    } catch (e) { toast(e.message, "err"); }
  });

  const overview = section("Overview", "what this group is doing and why", pauseBtn);
  const membersHost = section("Members", "pacing membership + fleet eligibility",
    el("div", {style: "display:flex;gap:4px;"}, addBtn, editBtn, delBtn));
  const queueHost = section("Task queue", "assignments and their why", (() => {
    const sel = el("select", {"aria-label": "Filter", style: "margin-left:auto;"},
      ...["all", "queued", "assigned", "running", "suspended", "failed",
          "completed", "cancelled"].map(s => el("option", {value: s}, s)));
    sel.onchange = () => { state.filter = sel.value; renderQueue(); };
    return sel;
  })());
  const coolHost = section("Cooldowns", "source-tagged; only manual clears");
  const historyHost = section("History", "append-only transition log");

  let data = null;

  function renderOverview() {
    overview.textContent = "";
    if (!data) return;
    const counts = {};
    for (const a of data.assignments) counts[a.status] = (counts[a.status] || 0) + 1;
    const syncCount = (storeGet("accounts") || [])
      .filter(a => a.sync_group === name).length;
    const row = el("div", {class: "kv-row"});
    for (const [k, v] of Object.entries({
      "state": data.paused ? "paused" : "running",
      "generation": String(data.generation),
      "pacing members": String(syncCount),
      "fleet-managed": String(data.members.length),
      "queued": String(counts.queued || 0),
      "assigned": String(counts.assigned || 0),
      "running": String(counts.running || 0),
      "suspended": String(counts.suspended || 0),
      "cooldowns": String(data.cooldowns.length)}))
      row.append(el("div", {class: "kv"},
        el("span", {class: "k"}, k), el("span", {class: "v"}, v)));
    overview.append(row);
    pauseBtn.textContent = data.paused ? "▶ resume group" : "⏸ pause group";
    pauseBtn.dataset.mode = data.paused ? "paused" : "running";
  }

  function renderMembers() {
    membersHost.textContent = "";
    if (!data) return;
    /* union of BOTH layers' members: sync pacing membership (the store,
       live) and fleet-managed members (the bundle). Rows show pacing
       membership + fleet state side by side; actions act on the layer
       they belong to. */
    const accts = storeGet("accounts") || [];
    const syncMembers = accts.filter(a => a.sync_group === name).map(a => a.name);
    const fleetByAccount = {};
    for (const m of data.members) fleetByAccount[m.account] = m;
    const rows = [...new Set([...syncMembers, ...Object.keys(fleetByAccount)])]
      .sort();

    if (!rows.length) {
      membersHost.append(emptyState("No members",
        "Add a pacing member to share the group's clock; join it to fleet management when the hub should drive its work."));
      return;
    }
    const tbl = el("table", {class: "tbl"});
    tbl.append(el("thead", {}, el("tr", null,
      ...["Account", "Pacing", "Fleet", "Actions"].map(h => el("th", {}, h)))));
    const tbody = el("tbody");
    for (const account of rows) {
      const inSync = syncMembers.includes(account);
      const fm = fleetByAccount[account] || null;
      const tds = [];
      tds.push(el("td", {},
        el("a", {href: "#/accounts/" + encodeURIComponent(account)}, account)));
      tds.push(el("td", {}, inSync
        ? el("span", {class: "badge badge-ok"}, "grouped")
        : el("span", {class: "badge badge-dim", title: "paces independently"},
          "independent")));
      tds.push(el("td", {}, fm
        ? el("span", {class: "badge " + (
            fm.eligibility === "eligible" ? "badge-ok"
            : fm.eligibility === "paused" ? "badge-warn" : "badge-dim")},
          fm.eligibility)
        : el("span", {class: "dim", title: "its independent flow runs; the hub does not drive it"},
          "not managed")));
      const acts = el("div", {style: "display:flex;gap:4px;flex-wrap:wrap;"});
      if (inSync) {
        const rm = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "remove");
        rm.onclick = busy(rm, async () => {
          const ok = await confirmBox({title: "Remove member",
            body: `Remove ${account} from ${name}? It will pace independently.`,
            yes: "Remove"});
          if (!ok) return;
          try {
            await post("/api/groups/remove-member", {group: name, account});
            toast(account + " removed", "ok");
            await Promise.allSettled([loadGroups(), loadStatsNow()]);
            await load();
          } catch (e) { toast(e.message, "err"); }
        });
        acts.append(rm);
      } else {
        const add = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "add pacing");
        add.onclick = busy(add, async () => {
          try {
            await post("/api/groups/add-member", {group: name, account});
            toast(account + " added", "ok");
            await Promise.allSettled([loadGroups(), loadStatsNow()]);
            await load();
          } catch (e) { toast(e.message, "err"); }
        });
        acts.append(add);
      }
      if (fm) {
        const paused = fm.eligibility === "paused";
        const pb = el("button", {class: "btn btn-ghost btn-sm", type: "button"},
          paused ? "resume" : "pause");
        pb.onclick = () => op(pb, paused ? "member_resume" : "member_pause",
          {account},
          paused ? null : `Pause member ${account}? It stops receiving work.`);
        const lv = el("button", {class: "btn btn-danger btn-sm", type: "button"}, "leave");
        lv.onclick = () => op(lv, "leave", {account},
          `Remove ${account} from fleet management? Its work is released and its independent flow is restored.`);
        acts.append(pb, lv);
      } else {
        const join = el("button", {class: "btn btn-sm btn-secondary", type: "button"}, "join fleet");
        join.onclick = () => op(join, "join", {account, group: name},
          `Bring ${account} under fleet management? Its independent flow is suspended while managed.`);
        acts.append(join);
      }
      tds.push(el("td", {}, acts));
      tbody.append(el("tr", null, ...tds));
    }
    tbl.append(tbody);
    membersHost.append(el("div", {class: "tbl-wrap"}, tbl));
  }

  function renderQueue() {
    queueHost.querySelector(".tbl-wrap")?.remove();
    queueHost.querySelector(".empty")?.remove();
    if (!data) return;
    const rows = data.assignments.filter(
      a => state.filter === "all" || a.status === state.filter);
    if (!rows.length) {
      queueHost.append(emptyState("No assignments",
        state.filter === "all"
          ? "Fleet work arrives when plugins declare tasks on managed accounts."
          : `Nothing ${state.filter}.`));
      return;
    }
    const tbl = el("table", {class: "tbl"});
    tbl.append(el("thead", {}, el("tr", null,
      ...["Task", "Status", "Owner", "Why", "Actions"].map(h => el("th", {}, h)))));
    const tbody = el("tbody");
    for (const a of rows) {
      const terminal = a.status === "completed" || a.status === "cancelled";
      const acts = el("div", {style: "display:flex;gap:4px;flex-wrap:wrap;"});
      if (a.status === "queued") {
        const sel = el("select", {"aria-label": "Assign to", style: "height:var(--ctrl-h);"},
          el("option", {value: ""}, "assign to…"),
          ...data.members.filter(m => m.eligibility === "eligible")
            .map(m => el("option", {value: m.account}, m.account)));
        sel.onchange = () => {
          if (sel.value) op(sel, "assign",
            {assignment: a.id, account: sel.value});
        };
        acts.append(sel);
      }
      if (a.status === "assigned") {
        const w = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "withdraw");
        w.onclick = () => op(w, "withdraw", {assignment: a.id});
        const h = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "handoff");
        h.onclick = () => {
          const others = data.members
            .filter(m => m.eligibility === "eligible" && m.account !== a.account);
          if (!others.length) { toast("no eligible destination", "err"); return; }
          const to = others[0].account;
          op(h, "handoff", {assignment: a.id, account: to},
            `Hand ${a.task_id} from ${a.account} to ${to}?`);
        };
        acts.append(w, h);
      }
      if (a.status === "suspended") {
        const r = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "resume");
        r.onclick = () => op(r, "resume_task", {assignment: a.id});
        acts.append(r);
      }
      if (!terminal) {
        const c = el("button", {class: "btn btn-danger btn-sm", type: "button"}, "cancel");
        c.onclick = () => op(c, "cancel", {assignment: a.id},
          `Cancel ${a.task_id}? This cannot be undone.`);
        acts.append(c);
      }
      const why = a.error || (a.status === "queued" && !a.account
        ? "awaiting a member" : "");
      tbody.append(el("tr", null,
        el("td", {}, a.task_id,
          el("div", {class: "dim", style: "font-size:10px;"}, a.id)),
        el("td", {}, el("span", {class: "badge " + (
          a.status === "running" || a.status === "assigned" ? "badge-ok"
          : a.status === "suspended" || a.status === "queued" ? "badge-dim"
          : a.status === "failed" ? "badge-err" : "badge-dim")}, a.status)),
        el("td", {}, a.account || "—"),
        el("td", {class: "dim"}, why || "—"),
        el("td", {}, acts)));
    }
    tbl.append(tbody);
    queueHost.append(el("div", {class: "tbl-wrap"}, tbl));
  }

  function renderCooldowns() {
    coolHost.textContent = "";
    if (!data || !data.cooldowns.length) {
      coolHost.append(emptyState("No active cooldowns", "Nothing is blocking the fleet."));
      return;
    }
    const tbl = el("table", {class: "tbl"});
    tbl.append(el("thead", {}, el("tr", null,
      ...["Scope", "Reason", "Source", "Expires", ""].map(h => el("th", {}, h)))));
    const tbody = el("tbody");
    for (const c of data.cooldowns) {
      const manual = c.source === "manual";
      const clear = manual
        ? el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "clear")
        : null;
      if (clear) clear.onclick = () => op(clear, "cooldown_clear", {id: c.id});
      tbody.append(el("tr", null,
        el("td", {}, `${c.scope_type}:${c.scope_id}`),
        el("td", {}, c.reason),
        el("td", {}, el("span", {class: "badge " + (manual ? "badge-warn" : "badge-dim")},
          c.source)),
        el("td", {class: "mono"}, fmtAge(c.expires_at)),
        el("td", {}, clear || "")));
    }
    tbl.append(tbody);
    coolHost.append(el("div", {class: "tbl-wrap"}, tbl));
  }

  function renderHistory() {
    historyHost.textContent = "";
    if (!data || !data.transitions.length) {
      historyHost.append(emptyState("No history", "Transitions land here as the fleet moves."));
      return;
    }
    const tbl = el("table", {class: "tbl"});
    tbl.append(el("thead", {}, el("tr", null,
      ...["When", "Kind", "Who", "Reason"].map(h => el("th", {}, h)))));
    const tbody = el("tbody");
    for (const t of data.transitions) {
      tbody.append(el("tr", null,
        el("td", {class: "dim"}, fmtTs(t.occurred_at)),
        el("td", {}, el("span", {class: "mono"}, t.kind)),
        el("td", {}, t.account || t.assignment_id || "—"),
        el("td", {class: "dim"}, t.reason || "—")));
    }
    tbl.append(tbody);
    historyHost.append(el("div", {class: "tbl-wrap"}, tbl));
  }

  async function load() {
    const seq = ++loadSeq;
    try {
      data = await api("/api/fleet/group?group=" + encodeURIComponent(name));
    } catch (e) {
      if (!state.alive || seq !== loadSeq) return;
      data = null;
      for (const sec of container.querySelectorAll(".section"))
        sec.classList.add("hidden");
      fatal.textContent = "";
      fatal.append(emptyState("Group unavailable", e.message),
        el("a", {class: "btn btn-ghost btn-sm", href: "#/groups"}, "← fleet list"));
      return;
    }
    if (!state.alive || seq !== loadSeq) return;
    fatal.textContent = "";
    for (const sec of container.querySelectorAll(".section"))
      sec.classList.remove("hidden");
    renderOverview();
    renderMembers();
    renderQueue();
    renderCooldowns();
    renderHistory();
  }

  await load();
  /* live convergence: fleet transitions for THIS group re-run load();
     sync membership edits (groups/accounts store keys from the CRUD
     dialogs) refresh the unified member table too; the loadSeq guard
     keeps a stale in-flight snapshot from overwriting a newer one, and
     alive stops everything at unmount */
  const unsubFleet = storeSubscribe((key, value) => {
    if (!state.alive) return;
    if (key === "fleet"
        && (!value || !value.group || value.group === name))
      load();
    else if (key === "groups" || key === "accounts") {
      renderMembers();   // pacing membership changed; hub bundle untouched
      renderOverview();  // the pacing-member count follows the store
    }
  });
  return () => { state.alive = false; unsubFleet(); };
});

/* ═══════════════════════════════════════════════════════════════════════════
   Group CRUD dialogs (moved from the retired Accounts 'Sync Groups' tab:
   ONE groups surface — this page owns membership + pacing + orchestration)
   ═══════════════════════════════════════════════════════════════════════════ */
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
