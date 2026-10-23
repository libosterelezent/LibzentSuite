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
  const hdr = el("div", {class: "view-hdr"},
    el("h1", {}, "Fleet"),
    el("p", {class: "dim"},
      "Group-managed orchestration: members, work, cooldowns — and the why."));
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
          "Create a sync group (Accounts page), then join it to fleet management from a group below.")));
      return;
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
    const card = el("div", {
      class: "card acct-tile", style: "cursor:pointer;",
      onclick: () => navigate(`/groups/${encodeURIComponent(g.group)}`)},
      el("div", {class: "acct-name"}, g.group,
        g.paused ? el("span", {class: "badge badge-warn", style: "margin-left:var(--sp-2);"}, "paused") : null),
      el("div", {class: "meta"},
        el("b", {}, String(g.member_count)), " members · gen ",
        el("b", {}, String(g.generation ?? "—")),
        (g.active_cooldowns ? [" · ", el("b", {class: "warn"}, String(g.active_cooldowns)), " cooldowns"] : null)),
      el("div", {class: "meta dim"}, work),
      last ? el("div", {class: "meta dim", title: last.reason || ""},
        `last: ${last.kind}${last.account ? " · " + last.account : ""}`) : null);
    return card;
  }

  await load();
  /* live convergence (PHASE 9 §3): any fleet transition (this client,
     another dashboard, a service op) lands as a WS "fleet" frame and
     re-runs load() — guarded by alive + the stale-snapshot seq */
  const unsubFleet = storeSubscribe((key, value) => {
    if (key === "fleet" && state.alive) load();
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

  const overview = section("Overview", "what this group is doing and why", pauseBtn);
  const membersHost = section("Members", "eligibility + controls");
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
    const row = el("div", {class: "kv-row"});
    for (const [k, v] of Object.entries({
      "state": data.paused ? "paused" : "running",
      "generation": String(data.generation),
      "members": String(data.members.length),
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
    if (!data || !data.members.length) {
      membersHost.append(emptyState("No members",
        "Join an account from the accounts view or via fleet ops."));
      return;
    }
    const tbl = el("table", {class: "tbl"});
    tbl.append(el("thead", {}, el("tr", null,
      ...["Account", "Mode", "Eligibility", "Actions"].map(h => el("th", {}, h)))));
    const tbody = el("tbody");
    for (const m of data.members) {
      const paused = m.eligibility === "paused";
      const btn = el("button", {class: "btn btn-ghost btn-sm", type: "button"},
        paused ? "resume" : "pause");
      btn.onclick = () => op(btn, paused ? "member_resume" : "member_pause",
        {account: m.account},
        paused ? null : `Pause member ${m.account}? It stops receiving work.`);
      const leave = el("button", {class: "btn btn-danger btn-sm", type: "button"}, "leave");
      leave.onclick = () => op(leave, "leave", {account: m.account},
        `Remove ${m.account} from fleet management? Its work is released and its independent flow is restored.`);
      const badge = el("span", {class: "badge " + (
        m.eligibility === "eligible" ? "badge-ok"
        : m.eligibility === "paused" ? "badge-warn" : "badge-dim")},
        m.eligibility);
      tbody.append(el("tr", null,
        el("td", {}, m.account),
        el("td", {}, m.mode === "group_managed" ? "managed" : m.mode),
        el("td", {}, badge),
        el("td", {style: "display:flex;gap:4px;"}, btn, leave)));
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
  /* live convergence: transitions for THIS group re-run load(); the
     loadSeq guard keeps a stale in-flight snapshot from overwriting a
     newer one, and alive stops everything at unmount */
  const unsubFleet = storeSubscribe((key, value) => {
    if (key === "fleet" && state.alive &&
        (!value || !value.group || value.group === name))
      load();
  });
  return () => { state.alive = false; unsubFleet(); };
});
