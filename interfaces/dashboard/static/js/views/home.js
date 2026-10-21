"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/home.js — Route #/home: the fleet at a glance.

   Attention strip (broken accounts, missing credentials, rate-limit
   warnings) → metric pills (fleet / running / cooldowns / tasks) → sync
   groups summary + recent activity feed. The log tail is seeded from
   GET /api/logs when the store is empty and kept live via WS "log" frames;
   every subscription (store + WS) dies with the view's unmount.
   ═══════════════════════════════════════════════════════════════════════════ */

const HOME_ERR_STATES = {
  failed: 1, missing_credential: 1, invalid_credential: 1, error: 1, err: 1,
};

function homeMount(root) {
  let localWarnings = null;   // REST fallback while the first stats frame lags
  let lastSig = "";

  root.append(el("div", {class: "view-hdr"},
    el("div", {},
      el("h2", {}, "Home"),
      el("p", {class: "dim"}, "The whole fleet, one glance — what needs you, and what it is doing."))));
  const body = el("div", {});
  root.append(body);

  /* ── data wiring ────────────────────────────────────────────────────────── */

  /* seed the log tail once (WS keeps it growing from here) */
  if (!(storeGet("logs") || []).length) {
    api("/api/logs?limit=30")
      .then(rows => { if (rows && rows.length) storeSet("logs", rows); })
      .catch(() => {});                       /* WS frames catch up */
  }
  /* tasks: fetch once for the pill (WS task frames land in other views) */
  api("/api/tasks")
    .then(rows => storeSet("tasks", Array.isArray(rows) ? rows : []))
    .catch(() => {});
  /* warnings ride every stats frame; REST covers a quiet socket */
  api("/api/ratelimit/warnings")
    .then(rows => { localWarnings = rows || []; scheduleRender(); })
    .catch(() => {});

  const offLog = WS.on("log", entry => {
    if (entry && entry.severity) storeAppend("logs", entry);
  });

  /* ── rendering ────────────────────────────────────────────────────────── */

  function attentionItems() {
    const items = [];
    for (const a of storeGet("accounts") || []) {
      const st = String(a.status || "");
      if (HOME_ERR_STATES[st]) {
        items.push({kind: "error", acct: a.name,
          title: a.name + " — " + st.replace(/_/g, " "),
          detail: a.last_error ? String(a.last_error) : "check the account page"});
      } else if (a.credential === "missing" || a.credential === "") {
        items.push({kind: "warn", acct: a.name,
          title: a.name + " — missing credential",
          detail: "the account cannot start until a token is stored"});
      }
    }
    const warnings = ((storeGet("stats") || {}).warnings) || localWarnings || [];
    for (const w of warnings) {
      if (!w || w.level === "info") continue;    // info never interrupts
      items.push({kind: w.level === "danger" ? "error" : "warn",
        href: "#/policies",
        title: "pacing: " + ((w.code || "warning").replace(/_/g, " ")),
        detail: String(w.message || "")});
    }
    return items;
  }

  function sig() {
    const s = storeGet("stats") || {};
    return JSON.stringify([
      (storeGet("accounts") || []).map(a =>
        [a.name, a.status, a.credential, a.cooldowns, a.sync_group]),
      storeGet("groups") || [],
      s.uptime_s, JSON.stringify(s.tasks || {}),
      (storeGet("logs") || []).slice(-14).map(l => [l.ts, l.message]),
      (storeGet("tasks") || []).length,
      JSON.stringify(s.warnings || localWarnings || []),
    ]);
  }

  function scheduleRender() {
    const next = sig();
    if (next === lastSig) return;
    lastSig = next;
    render();
  }

  function render() {
    const accts = storeGet("accounts") || [];
    const stats = storeGet("stats") || {};
    lastSig = sig();
    body.textContent = "";

    /* attention strip */
    const attn = attentionItems();
    if (attn.length) {
      const card = el("div", {class: "card"}, el("h4", {}, "Needs attention"));
      for (const it of attn) {
        const href = it.href || "#/accounts/" + encodeURIComponent(it.acct);
        card.append(el("a", {class: "attn-item " + it.kind, href},
          el("svg", {class: "icon", viewBox: "0 0 24 24", fill: "none",
                     stroke: "currentColor", "stroke-width": "2",
                     "aria-hidden": "true"},
            el("path", {d: "M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01"})),
          el("span", {class: "txt"},
            el("b", {}, it.title),
            el("span", {class: "detail"}, it.detail))));
      }
      body.append(card);
    }

    /* metric pills */
    const running = accts.filter(a => a.status === "running").length;
    const cooldowns = accts.reduce((n, a) => n + (a.cooldowns || 0), 0);
    const taskCount = (t => (t ? (t.queued || 0) + (t.running || 0) : 0))(stats.tasks);
    const pills = el("div", {class: "pill-row"});
    const pill = (k, v, cls) => el("span", {class: "ms-pill" + (cls ? " " + cls : "")},
      el("span", {class: "k"}, k), el("span", {class: "v"}, String(v)));
    pills.append(
      pill("fleet", accts.length),
      pill("running", running, running ? "ok" : ""),
      pill("cooldowns", cooldowns, cooldowns ? "err" : "ok"),
      pill("active tasks", taskCount));
    body.append(pills);

    /* zero-account onboarding */
    if (!accts.length) {
      body.append(el("div", {class: "card"},
        el("div", {class: "empty"},
          el("svg", {class: "icon", viewBox: "0 0 24 24", fill: "none",
                     stroke: "currentColor", "stroke-width": "1.5",
                     "aria-hidden": "true"},
            el("path", {d: "M13 2 3 14h9l-1 8 10-12h-9l1-8z"})),
          el("h3", {}, "Welcome to LibzentSuite"),
          el("p", {},
            "Nothing is registered yet. Add your first Discord account, give it a credential and a few abilities — status, pacing and activity light up here the moment it exists."),
          el("a", {class: "btn btn-primary", href: "#/accounts"}, "Add Account"))));
    }

    /* sync groups + recent activity */
    const grid = el("div", {class: "grid grid-2"});

    const groups = storeGet("groups") || [];
    const gCard = el("div", {class: "card"}, el("h4", {}, "Sync groups"));
    if (!groups.length) {
      gCard.append(el("p", {class: "hint"},
        "No sync groups. Group accounts to pace them on one shared clock — create groups from the Accounts page."));
    } else {
      for (const g of groups) {
        gCard.append(el("div", {class: "kv"},
          el("span", {class: "k"}, g.name),
          el("span", {class: "v"},
            String(g.member_count ?? 0) + " member" +
            ((g.member_count ?? 0) === 1 ? "" : "s"),
            " ", el("span", {class: "badge " +
              (g.enabled === false ? "badge-warn" : "badge-ok")},
              g.enabled === false ? "paused" : "active"))));
      }
      const indep = accts.filter(a => !a.sync_group).length;
      if (indep) gCard.append(el("p", {class: "hint"},
        indep + " account" + (indep > 1 ? "s" : "") + " pacing independently."));
    }
    grid.append(gCard);

    const logs = storeGet("logs") || [];
    const aCard = el("div", {class: "card"}, el("h4", {}, "Recent activity"));
    const feed = el("div", {});
    if (!logs.length) {
      aCard.append(el("p", {class: "hint"},
        "No activity yet — log lines stream here live once accounts run."));
    } else {
      for (const l of logs.slice(-12).reverse()) {
        feed.append(el("div", {class: "log-line"},
          el("span", {class: "ts"}, fmtTs(l.ts)),
          el("span", {class: "sev " + String(l.severity || "INFO").toUpperCase()},
            String(l.severity || "info")),
          el("span", {class: "msg"},
            (l.account ? "[" + l.account + "] " : "") + String(l.message || ""))));
      }
    }
    aCard.append(feed);
    grid.append(aCard);
    body.append(grid);
  }

  const unsub = storeSubscribe(key => {
    if (!["accounts", "groups", "stats", "logs", "tasks"].includes(key)) return;
    scheduleRender();
  });
  render();
  return () => { unsub(); offLog(); };
}

routerAdd("/home", "Home", homeMount);
