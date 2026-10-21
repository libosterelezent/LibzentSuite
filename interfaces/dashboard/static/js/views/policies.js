"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/policies.js — the Policies page: rate-limit policy CRUD and the
   pacing surfaces around it.
     Effective pacing — per-action interval/jitter from /ratelimit/effective,
                         plus the fleet send-lane state from /ratelimit/lane
     429 activity      — throttle totals from /ratelimit/stats (hits_429)
     Stored policies   — /ratelimit/policies table; add/edit dialog POSTs
                         /ratelimit/set, clear POSTs /ratelimit/clear
     Safety floor      — the enforced minimum interval (stats.floor),
                         raised via POST /ratelimit/floor {seconds}
     Warnings          — /ratelimit/warnings, rendered or empty
   The engine keys policies by (level, owner, action): setting an existing
   key UPDATES it. All mutations carry busy() double-submit guards.
   ═══════════════════════════════════════════════════════════════════════════ */

routerAdd("/policies", "Policies", async (container) => {
  const state = {alive: true, policies: [], stats: null};
  const RL_ACTIONS = ["message", "dm", "reaction", "button"];

  container.append(el("div", {class: "view-hdr"},
    el("h1", {}, "Policies"),
    el("p", {class: "dim"},
      "Send pacing for every Discord action: stored policies, the effective merge, throttle history and the safety floor."),
  ));
  const refreshBtn = el("button", {class: "btn btn-secondary btn-sm", type: "button"}, "↻ Refresh all");
  container.querySelector(".view-hdr").append(refreshBtn);

  const section = (title, body) => {
    const bodyEl = el("div", {class: "sec-body"});
    container.append(el("div", {class: "section"},
      el("div", {class: "sec-hdr"}, el("span", {}, title)), bodyEl));
    return bodyEl;
  };

  /* ── effective pacing + lane ────────────────────────────────────────── */
  const effHost = section("Effective pacing");
  const laneHost = section("Fleet send lane");
  async function loadEffective() {
    effHost.textContent = "";
    effHost.append(el("div", {class: "skel"}, el("div"), el("div"), el("div")));
    const results = await Promise.allSettled(RL_ACTIONS.map(a =>
      api("/api/ratelimit/effective?action=" + encodeURIComponent(a))));
    if (!state.alive) return;
    effHost.textContent = "";
    const grid = el("div", {class: "pacing-grid"});
    RL_ACTIONS.forEach((action, i) => {
      const r = results[i];
      if (r.status !== "fulfilled" || !r.value) {
        grid.append(el("div", {class: "pacing-item"},
          el("div", {class: "pl"}, action),
          el("div", {class: "pv"}, "—"),
          el("div", {class: "hint"}, r.reason ? r.reason.message : "no policy")));
        return;
      }
      const eff = r.value;
      const j = Array.isArray(eff.jitter) && eff.jitter.length
        ? `+${eff.jitter[0]}–${eff.jitter[1]}s jitter` : "no jitter";
      const owner = (eff.sources && eff.sources[0] && eff.sources[0].owner)
        ? ` · ${eff.sources[0].owner}` : "";
      grid.append(el("div", {class: "pacing-item"},
        el("div", {class: "pl"}, action + owner),
        el("div", {class: "pv"}, `${eff.interval}s`),
        el("div", {class: "hint"}, j)));
    });
    effHost.append(grid,
      el("p", {class: "hint"}, "Merge order, highest wins: global ← group ← account ← plugin policies. Discord bucket limits still apply on top."));
  }

  async function loadLane() {
    laneHost.textContent = "";
    try {
      const lane = await api("/api/ratelimit/lane");
      if (!state.alive) return;
      if (lane && lane.removed) {
        laneHost.append(emptyState("Fleet send lane removed",
          lane.reason || "Sync groups + per-account pacing replaced it."));
      } else {
        laneHost.append(el("div", {class: "kv"},
          el("span", {class: "k"}, "state"),
          el("span", {class: "v"}, lane && lane.enabled ? "enabled" : "disabled")),
          lane && lane.prefixrate ? el("div", {class: "kv"},
            el("span", {class: "k"}, "prefix rate"),
            el("span", {class: "v"}, String(lane.prefixrate))) : null);
      }
    } catch (e) { if (state.alive) laneHost.append(emptyState("Lane unavailable", e.message)); }
  }

  /* ── 429 activity ───────────────────────────────────────────────────── */
  const hitsHost = section("429 activity");
  function renderHits() {
    hitsHost.textContent = "";
    const hits = (state.stats && state.stats.hits_429) || {accounts: {}, total: 0};
    const accts = Object.entries(hits.accounts || {});
    if (!hits.total && !accts.length) {
      hitsHost.append(emptyState("No 429 throttles recorded",
        "Every send has stayed under Discord's limits."));
      return;
    }
    hitsHost.append(el("div", {class: "pacing-grid"},
      el("div", {class: "pacing-item"},
        el("div", {class: "pl"}, "fleet total"),
        el("div", {class: "pv" + (hits.total ? " warn" : "")}, String(hits.total ?? 0)),
        el("div", {class: "hint"}, "throttled sends"))));
    if (accts.length) {
      const tbl = el("table", {class: "tbl"},
        el("thead", {}, el("tr", null, el("th", {}, "Account"), el("th", {}, "Throttles"))));
      const tb = el("tbody");
      for (const [a, n] of accts) tb.append(el("tr", null,
        el("td", {}, a), el("td", {class: "mono"}, String(n))));
      tbl.append(tb);
      hitsHost.append(el("div", {class: "tbl-wrap"}, tbl));
    }
  }

  /* ── stored policies ────────────────────────────────────────────────── */
  const polHost = section("Stored policies");
  function renderPolicies() {
    polHost.textContent = "";
    if (!state.policies.length) {
      polHost.append(emptyState("No stored policies",
        "Discord bucket pacing is active by default — add a policy to pin an action tighter."));
      return;
    }
    const tbl = el("table", {class: "tbl"},
      el("thead", {}, el("tr", null,
        el("th", {}, "Scope"), el("th", {}, "Owner"), el("th", {}, "Action"),
        el("th", {}, "Interval"), el("th", {}, "Jitter"), el("th"))));
    const tb = el("tbody");
    for (const p of state.policies) {
      const editBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "edit");
      editBtn.onclick = () => openPolicyEditor(p);
      const clearBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "clear");
      clearBtn.onclick = async () => {
        const ok = await confirmBox({title: "Clear policy",
          body: `Remove the ${p.level}${p.owner ? " · " + p.owner : ""} policy for '${p.action}'?`});
        if (!ok) return;
        try {
          await busyRun(clearBtn, () => post("/api/ratelimit/clear",
            {level: p.level, owner: p.owner || undefined, action: p.action}));
          await loadPolicies();
        } catch (e) { toast(e.message, "err"); }
      };
      tb.append(el("tr", null,
        el("td", {}, el("span", {class: "badge badge-dim"}, p.level || "global")),
        el("td", {class: "mono"}, p.owner || "—"),
        el("td", {}, p.action || "—"),
        el("td", {class: "mono"}, `${p.interval}s`),
        el("td", {class: "mono"}, Array.isArray(p.jitter) && p.jitter.length
          ? `${p.jitter[0]}–${p.jitter[1]}s` : "—"),
        el("td", {style: "text-align:right;white-space:nowrap;"}, editBtn, " ", clearBtn)));
    }
    tbl.append(tb);
    polHost.append(el("div", {class: "tbl-wrap"}, tbl));
  }
  async function loadPolicies() {
    try {
      state.policies = await api("/api/ratelimit/policies") || [];
    } catch (e) { toast(e.message, "err"); state.policies = []; }
    if (state.alive) renderPolicies();
  }

  function openPolicyEditor(existing) {
    const f = {
      action: el("input", {type: "text", value: existing ? existing.action : "message",
        placeholder: "message | dm | reaction | button | …"}),
      interval: el("input", {type: "number", step: "any", min: "0",
        value: existing ? String(existing.interval) : "", placeholder: "seconds between sends"}),
      jmin: el("input", {type: "number", step: "any", min: "0",
        value: existing && Array.isArray(existing.jitter) ? String(existing.jitter[0] || 0) : "",
        placeholder: "0"}),
      jmax: el("input", {type: "number", step: "any", min: "0",
        value: existing && Array.isArray(existing.jitter) ? String(existing.jitter[1] || 0) : "",
        placeholder: "0"}),
      level: el("select", {"aria-label": "Scope"},
        ...["global", "group", "account", "plugin"].map(l =>
          el("option", {value: l, selected: existing && existing.level === l ? "" : undefined}, l))),
      owner: el("input", {type: "text",
        value: existing ? (existing.owner || "") : "", placeholder: "group / account / plugin name (optional)"}),
    };
    const field = (label, control, hint) => el("div", {class: "field"},
      el("label", {}, label), control, hint ? el("div", {class: "hint"}, hint) : null);
    const body = el("div", null,
      field("Action", f.action),
      field("Interval (s)", f.interval, "Positive seconds between sends of this action."),
      el("div", {class: "toolbar"},
        el("div", {class: "field"}, el("label", {}, "Jitter min (s)"), f.jmin),
        el("div", {class: "field"}, el("label", {}, "Jitter max (s)"), f.jmax)),
      el("div", {class: "toolbar"},
        el("div", {class: "field"}, el("label", {}, "Scope"), f.level),
        el("div", {class: "field"}, el("label", {}, "Owner"), f.owner)));
    dialog({
      title: existing ? "Edit policy" : "Add policy",
      bodyEl: body,
      actions: [
        {label: "Cancel", variant: "secondary", onclick: close => close()},
        {label: existing ? "Save policy" : "Add policy", variant: "primary",
         onclick: (close) => {
           const interval = parseFloat(f.interval.value);
           if (!Number.isFinite(interval) || interval <= 0) { toast("Interval must be positive seconds.", "err"); return; }
           const jmin = parseFloat(f.jmin.value) || 0, jmax = parseFloat(f.jmax.value) || 0;
           if (jmin < 0 || jmax < 0 || jmin > jmax) { toast("Jitter must be 0..max with min ≤ max.", "err"); return; }
           const body2 = {action: f.action.value.trim(), interval,
             jitter_min: jmin, jitter_max: jmax, level: f.level.value};
           const owner = f.owner.value.trim();
           if (owner) body2.owner = owner;
           post("/api/ratelimit/set", body2)
             .then(() => { close(); loadPolicies(); loadEffective(); })
             .catch(e => toast(e.message, "err"));
         }},
      ],
    });
    f.interval.focus();
  }

  /* add-policy action link in the section header */
  polHost.parentElement.querySelector(".sec-hdr").append(
    el("button", {class: "btn btn-ghost btn-sm", type: "button",
      onclick: () => openPolicyEditor(null)}, "+ add policy"));

  /* ── safety floor ───────────────────────────────────────────────────── */
  const floorHost = section("Safety floor");
  function renderFloor() {
    floorHost.textContent = "";
    const floor = state.stats ? state.stats.floor : null;
    const input = el("input", {type: "number", step: "any", min: "0",
      value: floor != null ? String(floor) : "", placeholder: "seconds"});
    const save = el("button", {class: "btn btn-primary btn-sm", type: "button"}, "Save floor");
    save.onclick = async () => {
      const seconds = parseFloat(input.value);
      if (!Number.isFinite(seconds) || seconds < 0) { toast("Floor must be zero or positive seconds.", "err"); return; }
      try {
        await busyRun(save, () => post("/api/ratelimit/floor", {seconds}));
        await loadStats();
      } catch (e) { toast(e.message, "err"); }
    };
    floorHost.append(
      el("div", {class: "pacing-grid"},
        el("div", {class: "pacing-item"},
          el("div", {class: "pl"}, "minimum enforced interval"),
          el("div", {class: "pv"}, floor != null ? floor + "s" : "—"),
          el("div", {class: "hint"}, "Configured intervals below the floor are raised to it."))),
      el("div", {class: "toolbar"},
        el("div", {class: "field"}, el("label", {}, "New floor (s)"), input), save));
  }

  /* ── warnings ───────────────────────────────────────────────────────── */
  const warnHost = section("Rate-limit warnings");
  async function loadWarnings() {
    warnHost.textContent = "";
    try {
      const ws = await api("/api/ratelimit/warnings") || [];
      if (!state.alive) return;
      warnHost.textContent = "";
      if (!ws.length) {
        warnHost.append(emptyState("No rate-limit warnings", "The pacing engine has nothing to complain about."));
        return;
      }
      for (const w of ws) {
        const msg = typeof w === "string" ? w : (w.message || JSON.stringify(w));
        const lvl = (w && w.level) || "warn";
        const cls = lvl === "danger" ? "badge-err" : lvl === "info" ? "badge-dim" : "badge-warn";
        warnHost.append(el("div", {class: "kv"},
          el("span", {class: "v"}, msg),
          w && w.code ? el("span", {class: "badge " + cls}, w.code) : null));
      }
    } catch (e) { if (state.alive) warnHost.append(emptyState("Warnings unavailable", e.message)); }
  }

  async function loadStats() {
    try { state.stats = await api("/api/ratelimit/stats"); }
    catch (e) { toast(e.message, "err"); state.stats = null; }
    if (!state.alive) return;
    renderHits();
    renderFloor();
  }

  refreshBtn.onclick = () => busy(refreshBtn, refreshAll);
  async function refreshAll() {
    await Promise.allSettled([loadEffective(), loadLane(), loadPolicies(), loadStats(), loadWarnings()]);
  }
  await refreshAll();

  return () => { state.alive = false; };
});
