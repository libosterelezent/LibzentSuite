"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/admin.js — the Admin page: who may command the fleet.
     Overview        — /api/admin/overview: framework admin ids, the Mudae
                       bot id, and per-account access rows (command perms,
                       prefix mode/symbol, admin flag, forget stale rows)
     Credentials     — /api/credentials/status readout
   Mutations: /api/admin/ids/add|remove, /api/admin/perms/add|remove,
   /api/admin/prefix/set, /api/admin/account-flag, /api/admin/mudae-id/set,
   /api/admin/forget-account (danger confirm). Fail-closed everywhere: an
   empty id list means NOBODY may command that account.
   ═══════════════════════════════════════════════════════════════════════════ */

routerAdd("/admin", "Admin", async (container) => {
  const state = {alive: true, overview: null, credentials: null};

  container.append(el("div", {class: "view-hdr"},
    el("h1", {}, "Admin"),
    el("p", {class: "dim"},
      "Command permissions, prefixes and framework identity. Changes apply on next connect."),
  ));
  const refreshBtn = el("button", {class: "btn btn-secondary btn-sm", type: "button"}, "↻ Refresh");
  container.querySelector(".view-hdr").append(refreshBtn);

  const section = (title, hint, body) => {
    const hdr = el("div", {class: "sec-hdr"}, el("span", {}, title));
    if (hint) hdr.append(el("span", {class: "dim", style: "margin-left:var(--sp-2);"}, hint));
    const bodyEl = el("div", {class: "sec-body"});
    container.append(el("div", {class: "section"}, hdr, bodyEl));
    return bodyEl;
  };

  const numericOrToast = v => {
    if (!/^\d+$/.test(v)) { toast("Enter a numeric Discord user id.", "err"); return null; }
    return parseInt(v, 10);
  };

  /* ── framework admins ───────────────────────────────────────────────── */
  const adminsHost = section("Framework admins", "may command ANY account");
  function renderAdmins() {
    adminsHost.textContent = "";
    const ov = state.overview || {};
    const admins = ov.admins || [];
    const input = el("input", {type: "text", placeholder: "discord user id",
      style: "width:200px;"});
    const add = el("button", {class: "btn btn-primary btn-sm", type: "button"}, "+ Add admin");
    add.onclick = async () => {
      const v = input.value.trim();
      const id = numericOrToast(v);
      if (id == null) return;
      try { await busyRun(add, () => post("/api/admin/ids/add", {user_id: id})); await load(); }
      catch (e) { toast(e.message, "err"); }
    };
    const chips = el("div", {class: "row"});
    if (!admins.length) {
      chips.append(el("span", {class: "hint"},
        "no admins — nobody can command any account (fail-closed)"));
    } else for (const id of admins) {
      const rm = el("button", {class: "btn btn-ghost btn-sm", type: "button",
        "aria-label": "Remove admin " + id, title: "Remove admin"}, "✕");
      rm.onclick = async () => {
        try { await busyRun(rm, () => post("/api/admin/ids/remove", {user_id: id})); await load(); }
        catch (e) { toast(e.message, "err"); }
      };
      chips.append(el("span", {class: "chip mono"}, String(id), rm));
    }
    adminsHost.append(chips, el("div", {class: "toolbar"}, input, add),
      el("p", {class: "hint"}, "The old OWNER_USER_IDS env was migrated once and is ignored since."));
  }

  /* ── mudae bot id ───────────────────────────────────────────────────── */
  const mudaeHost = section("Mudae bot id", "detects Mudae responses");
  function renderMudae() {
    mudaeHost.textContent = "";
    const current = (state.overview && state.overview.mudae_bot_id) ?? "";
    const input = el("input", {type: "text", placeholder: "432610292342587392",
      value: String(current || ""), style: "width:220px;"});
    const save = el("button", {class: "btn btn-primary btn-sm", type: "button"}, "Save");
    save.onclick = async () => {
      const raw = input.value.trim();
      if (raw !== "" && !/^\d+$/.test(raw)) { toast("Mudae id must be numeric.", "err"); return; }
      const body = raw === "" ? {mudae_bot_id: null} : {mudae_bot_id: parseInt(raw, 10)};
      try { await busyRun(save, () => post("/api/admin/mudae-id/set", body)); await load(); }
      catch (e) { toast(e.message, "err"); }
    };
    mudaeHost.append(el("div", {class: "toolbar"}, input, save),
      el("p", {class: "hint"}, "DB value wins; the MUDAE_BOT_ID env is the fallback when empty."));
  }

  /* ── per-account access ─────────────────────────────────────────────── */
  const acctsHost = section("Per-account access", "command perms · prefix · admin flag");
  function renderAccounts() {
    acctsHost.textContent = "";
    const ov = state.overview || {};
    const liveNames = new Set((storeGet("accounts") || []).map(a => a.name));
    const known = new Set([...Object.keys(ov.accounts || {}), ...liveNames]);
    if (!known.size) {
      acctsHost.append(emptyState("No accounts",
        "Add an account first — access rows configure themselves per account."));
      return;
    }
    for (const name of [...known].sort()) {
      const row = (ov.accounts || {})[name] ||
        {is_admin: false, prefix_mode: "mention", prefix_symbol: "", allowed_user_ids: []};
      const stale = !liveNames.has(name);
      const card = el("div", {class: "card"});

      /* header: name, flags, admin toggle, forget */
      const flagBtn = el("button", {
        class: "btn btn-sm " + (row.is_admin ? "btn-danger" : "btn-secondary"), type: "button"},
        row.is_admin ? "Remove admin" : "Make admin");
      flagBtn.onclick = async () => {
        try {
          await busyRun(flagBtn, () => post("/api/admin/account-flag",
            {account: name, is_admin: !row.is_admin}));
          await load();
        } catch (e) { toast(e.message, "err"); }
      };
      const forgetBtn = stale
        ? el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "✕ remove") : null;
      if (forgetBtn) forgetBtn.onclick = async () => {
        const ok = await confirmBox({title: "Forget access row",
          body: `Remove the stale access row for "${name}"? (the account itself no longer exists)`,
          danger: true});
        if (!ok) return;
        try { await busyRun(forgetBtn, () => post("/api/admin/forget-account", {account: name})); await load(); }
        catch (e) { toast(e.message, "err"); }
      };
      card.append(el("div", {class: "toolbar"},
        el("strong", {}, name),
        row.is_admin ? el("span", {class: "badge badge-ok"}, "ADMIN") : null,
        stale ? el("span", {class: "badge badge-warn", title: "account no longer exists — stale access row"}, "stale") : null,
        el("span", {class: "dim mono"}, `prefix: ${row.prefix_mode || "mention"}${row.prefix_symbol ? " " + row.prefix_symbol : ""}`),
        el("span", {style: "flex:1;"}),
        forgetBtn, flagBtn));

      /* command perms */
      const permsRow = el("div", {class: "row"});
      const perms = row.allowed_user_ids || [];
      if (!perms.length) permsRow.append(el("span", {class: "hint"}, "no ids — framework admins only (fail-closed)"));
      for (const id of perms) {
        const rm = el("button", {class: "btn btn-ghost btn-sm", type: "button",
          "aria-label": "Revoke " + id, title: "Revoke"}, "✕");
        rm.onclick = async () => {
          try {
            await busyRun(rm, () => post("/api/admin/perms/remove", {account: name, user_id: id}));
            await load();
          } catch (e) { toast(e.message, "err"); }
        };
        permsRow.append(el("span", {class: "chip mono"}, String(id), rm));
      }
      const permInput = el("input", {type: "text", placeholder: "discord user id", style: "width:180px;"});
      const permAdd = el("button", {class: "btn btn-secondary btn-sm", type: "button"}, "+ Allow");
      permAdd.onclick = async () => {
        const id = numericOrToast(permInput.value.trim());
        if (id == null) return;
        try { await busyRun(permAdd, () => post("/api/admin/perms/add", {account: name, user_id: id})); await load(); }
        catch (e) { toast(e.message, "err"); }
      };
      card.append(
        el("div", {class: "hint"}, "command perms"), permsRow,
        el("div", {class: "toolbar"}, permInput, permAdd));

      /* prefix */
      const modeSel = el("select", {"aria-label": "Prefix mode for " + name},
        ...["mention", "symbol", "both"].map(m =>
          el("option", {value: m, selected: (row.prefix_mode || "mention") === m ? "" : undefined}, m)));
      const symbolInput = el("input", {type: "text", placeholder: "sm!",
        value: row.prefix_symbol || "", maxlength: "4", style: "width:70px;"});
      const setPrefix = el("button", {class: "btn btn-secondary btn-sm", type: "button"}, "Set prefix");
      setPrefix.onclick = async () => {
        try {
          await busyRun(setPrefix, () => post("/api/admin/prefix/set",
            {account: name, mode: modeSel.value, symbol: symbolInput.value.trim()}));
          await load();
        } catch (e) { toast(e.message, "err"); }
      };
      card.append(el("div", {class: "toolbar"},
        el("span", {class: "hint"}, "prefix"), modeSel, symbolInput, setPrefix));

      acctsHost.append(card);
    }
  }

  /* ── credentials status ─────────────────────────────────────────────── */
  const credHost = section("Credentials", "stored account tokens");
  function renderCredentials() {
    credHost.textContent = "";
    const data = state.credentials;
    if (data == null || (Array.isArray(data) && !data.length) ||
        (typeof data === "object" && !Array.isArray(data) && !Object.keys(data).length)) {
      credHost.append(emptyState("No credentials stored",
        "Add an account with a token, or import from the environment (Tools → Advanced)."));
      return;
    }
    if (Array.isArray(data)) {
      const tbl = el("table", {class: "tbl"}, el("thead"));
      const tb = el("tbody");
      const keys = new Set();
      for (const r of data) if (r && typeof r === "object") for (const k of Object.keys(r)) keys.add(k);
      const cols = [...keys];
      /* human values: internal enums ("db") and booleans don't leak raw */
      const human = v => {
        if (v === true) return "yes";
        if (v === false) return "no";
        if (v == null || v === "") return "—";
        const s = String(v);
        return {db: "configured", env: "environment",
                none: "missing", missing: "missing"}[s.toLowerCase()] || s;
      };
      tbl.querySelector("thead").append(el("tr", null, ...cols.map(c => el("th", {}, c))));
      for (const r of data) tb.append(el("tr", null,
        ...cols.map(c => el("td", {class: "mono"},
          r && typeof r === "object" ? human(r[c]) : "—"))));
      tbl.append(tb);
      credHost.append(el("div", {class: "tbl-wrap"}, tbl));
    } else {
      for (const [k, v] of Object.entries(data))
        credHost.append(el("div", {class: "kv"},
          el("span", {class: "k"}, k), el("span", {class: "v"}, String(v))));
    }
  }

  /* ── load + boot ────────────────────────────────────────────────────── */
  async function load() {
    const jobs = [];
    jobs.push(api("/api/admin/overview")
      .then(d => { state.overview = d || null; })
      .catch(e => { toast(e.message, "err"); state.overview = null; }));
    jobs.push(api("/api/credentials/status")
      .then(d => { state.credentials = d ?? null; })
      .catch(e => { state.credentials = null; }));
    await Promise.allSettled(jobs);
    if (!state.alive) return;
    if (!state.overview) {
      container.append(emptyState("Admin unavailable", "The access store could not be loaded — see the error toast."));
      return;
    }
    renderAdmins();
    renderMudae();
    renderAccounts();
    renderCredentials();
  }
  refreshBtn.onclick = () => busy(refreshBtn, load);
  await load();

  return () => { state.alive = false; };
});
