"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   accounts.js — Accounts page: tile cards, list rows, the account details
   popup and the plugin picker (direct account→plugin assignments). Tile and
   List render the SAME data with the SAME action wiring (wireAccountActions)
   — only the markup differs, so business logic is never duplicated between
   views.
   ═══════════════════════════════════════════════════════════════════════════ */

function accounts() { return (lastStats&&lastStats.accounts)||[]; }
function groupMap() {
  const m = {};
  for (const a of accounts()) {
    const g = a.sync_group || "";
    (m[g]=m[g]||[]).push(a);
  }
  return m;
}
function assignedPlugins(a) { return (a&&a.assigned_plugins)||[]; }

function renderAccounts() {
  const host = $("acct-view"); if (!host) return;
  $("cnt-accts").textContent = accounts().length;
  fillSelect($("task-filter-account"), accounts().map(a=>a.name), "all accounts");
  fillSelect($("log-acct"), accounts().map(a=>a.name), "all accounts");
  fillSelect($("audit-acct"), accounts().map(a=>a.name), "all accounts");
  fillDatalist("group-list", groupsList.map(g=>g.name));
  renderViewSel("vm-acct", acctViewMode);
  host.innerHTML = "";
  if (!accounts().length) {
    host.innerHTML = emptyState("No Accounts","Add your first Discord account to get started.","+ Add Account");
    const cta = host.querySelector("button"); if (cta) cta.onclick = () => $("btn-add-acct").click();
    return;
  }
  const wrap = document.createElement("div");
  if (acctViewMode === "list") {
    wrap.className = "listview";
    for (const a of accounts()) wrap.appendChild(acctRow(a));
  } else {
    wrap.className = "grid grid-2";
    for (const a of accounts()) wrap.appendChild(acctTile(a));
  }
  host.appendChild(wrap);
  updateBulkBar();
}

/* ── Tile view ─────────────────────────────────────────────────────────────── */
function acctTile(a) {
  const card = document.createElement("div"); card.className = "acct-card";
  card.innerHTML = `
    <div class="hdr">
      <span class="name trunc" title="View details">${esc(a.name)}</span>
      ${statusBadge(a.status)}
      <span class="id trunc">${esc(a.user_id||a.discord_id||"")}</span>
      ${accountMenuHTML()}
    </div>
    <div class="meta">
      ${a.credential?`<span class="item">🔑 ${credLbl(a.credential)}</span>`:'<span class="item" style="color:var(--err)">🔑 missing</span>'}
      ${a.uptime_s?`<span class="item">⏱ ${fmtDur(a.uptime_s)}</span>`:""}
      ${a.latency_ms!=null?`<span class="item">⚡ ${fmtLatency(a.latency_ms)}</span>`:""}
      ${a.cooldowns?`<span class="item" style="color:var(--warn)">⏳ ${esc(String(a.cooldowns))}</span>`:""}
      ${a.sync_group?`<span class="item"><span class="chip group" data-group-chip="${esc(a.sync_group)}" title="Open group">${esc(a.sync_group)}</span></span>`:""}
      ${assignedPlugins(a).length?`<span class="item" style="gap:4px">${assignedPlugins(a).map(p=>`<span class="chip plugin">${esc(p)}</span>`).join("")}</span>`:""}
    </div>
    <div class="actions">
      ${a.status==="running"
        ? '<button class="btn btn-secondary btn-sm" data-card-act="stop">Stop</button>'
        : '<button class="btn btn-primary btn-sm" data-card-act="start">Start</button>'}
      <button class="btn btn-ghost btn-sm" data-card-act="details">Details</button>
      <label style="margin-left:auto;display:flex;align-items:center;gap:4px;font-size:11px;color:var(--dim);cursor:pointer" title="Select for bulk actions">
        <input type="checkbox" data-bulk="${esc(a.name)}" ${selectedAccounts.has(a.name)?"checked":""} style="width:14px;height:14px">
      </label>
    </div>`;
  wireAccountActions(card, a);
  return card;
}

/* ── List view ─────────────────────────────────────────────────────────────── */
function acctRow(a) {
  const row = document.createElement("div"); row.className = "lrow acct";
  row.innerHTML = `
    <input type="checkbox" data-bulk="${esc(a.name)}" ${selectedAccounts.has(a.name)?"checked":""} title="Select ${esc(a.name)}">
    <div class="lmain">
      <span class="lname" title="View details">${esc(a.name)}</span>
      <span class="lcol id trunc">${esc(a.user_id||a.discord_id||"")}</span>
    </div>
    <span class="lcol lcol-status">${statusBadge(a.status)}</span>
    <span class="lcol lcol-group">${a.sync_group?`<span class="chip group" data-group-chip="${esc(a.sync_group)}" title="Open group">${esc(a.sync_group)}</span>`:'<span class="faint">independent</span>'}</span>
    <span class="lcol lmeta lcol-meta">${acctMetaText(a)}</span>
    <span class="lactions">
      ${a.status==="running"
        ? '<button class="btn btn-secondary btn-sm" data-card-act="stop">Stop</button>'
        : '<button class="btn btn-primary btn-sm" data-card-act="start">Start</button>'}
      <button class="btn btn-ghost btn-sm" data-card-act="details">Details</button>
      ${accountMenuHTML()}
    </span>`;
  wireAccountActions(row, a);
  return row;
}

/* shared fragments */
function accountMenuHTML() {
  return `<div class="menu-wrap"><button class="menu-btn" aria-label="Account menu"><svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg></button>
    <div class="menu">
      <button data-act="cred">Set credential</button>
      <button data-act="group">Move to group…</button>
      <button data-act="plugins">Assign Plugins…</button>
      <div class="sep"></div>
      <button data-act="estop" class="danger">Emergency stop</button>
      <button data-act="remove" class="danger">Remove account</button>
    </div></div>`;
}
function acctMetaText(a) {
  const bits = [];
  bits.push(a.credential ? "🔑 ok" : "🔑 missing");
  if (a.uptime_s) bits.push("⏱ "+fmtDur(a.uptime_s));
  if (a.latency_ms!=null) bits.push("⚡ "+fmtLatency(a.latency_ms));
  if (a.cooldowns) bits.push("⏳ "+a.cooldowns);
  if (assignedPlugins(a).length) bits.push(assignedPlugins(a).join(","));
  return bits.join(" · ");
}

/* ── Shared action wiring (identical for tile and list) ───────────────────── */
function wireAccountActions(root, a) {
  const menu = root.querySelector(".menu");
  if (menu) {
    root.querySelector(".menu-btn").onclick = e => {
      e.stopPropagation();
      $$(".menu.open").forEach(m=>m!==menu&&m.classList.remove("open"));
      menu.classList.toggle("open");
    };
    // NOTE: the global click-to-close lives ONCE in wire(), not here —
    // renderers run on every structural tick; per-card document listeners
    // accumulated into the listener-leak freeze.
    menu.querySelectorAll("button").forEach(mb => mb.onclick = () => {
      if (mb.disabled) return;
      menu.classList.remove("open");
      const act = mb.dataset.act;
      if (act==="cred") { $("cred-name").value=a.name; $("dlg-cred").showModal(); $("cred-val").value=""; }
      if (act==="estop") confirmAction("Emergency Stop",`Emergency stop ${a.name}?`,
        ()=>withBusy(mb, post("/api/accounts/emergency-stop",{name:a.name})).catch(e=>setFb(root,"error",e.message)));
      if (act==="remove") confirmAction("Remove Account",`Remove ${a.name}? This deletes its credential, cooldowns and state.`,
        ()=>post("/api/accounts/remove",{name:a.name})
          .then(()=>{ clearFb(root); afterAccountsRest(); })
          .catch(e=>setFb(root,"error",e.message)));
      if (act==="plugins") openPluginPicker(a.name);
      if (act==="group") openGroupPicker(`Move to group — ${a.name}`, a.sync_group||null,
        g=>moveAccountGroup(a.name,g,root));
    });
  }
  root.querySelectorAll("[data-card-act]").forEach(b => b.onclick = () => {
    const act = b.dataset.cardAct;
    if (act==="details") return openAcctDetails(a.name);
    if (b.disabled) return;
    clearFb(root);
    const path = act==="start" ? "/api/accounts/start" : "/api/accounts/stop";
    withBusy(b, post(path, {account:a.name}))
      .then(()=>flashFb(root, act==="start"?"start requested":"stop requested"))
      .catch(e=>setFb(root,"error",e.message));
  });
  const nameEl = root.querySelector(".lname, .name");
  if (nameEl && nameEl.classList.contains("name")) nameEl.onclick = () => openAcctDetails(a.name);
  else if (nameEl) nameEl.onclick = () => openAcctDetails(a.name);
  root.querySelectorAll("[data-group-chip]").forEach(c=>c.onclick=()=>openGroupDetails(c.dataset.groupChip));
  root.querySelectorAll("input[data-bulk]").forEach(cb => cb.onchange = e => {
    if (e.target.checked) selectedAccounts.add(a.name); else selectedAccounts.delete(a.name);
    updateBulkBar();
  });
}

/* ── Bulk selection bar ────────────────────────────────────────────────────── */
function updateBulkBar() {
  const bar = $("bulkbar");
  if (!bar) return;
  bar.classList.toggle("open", selectedAccounts.size > 0);
  $("bulk-count").textContent = `${selectedAccounts.size} selected`;
}

/* account state changed outside WS — pull authoritative rows now */
async function afterAccountsRest() {
  await refreshAccountsNow();
  render();
}

/* ── Account details popup ─────────────────────────────────────────────────── */
function openAcctDetails(name) {
  const a = accounts().find(x=>x.name===name); if (!a) return;
  const body = openDetailsPopup(name);
  const plugins = assignedPlugins(a);
  body.innerHTML = `
    <div class="row" style="margin-bottom:var(--sp-3)">${statusBadge(a.status)}
      <span class="dim mono trunc" style="font-size:11px">${esc(a.user_id||a.discord_id||"")}</span></div>
    <div class="grid" style="gap:var(--sp-2)">
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Runtime</b>
        <div class="sub">Latency: ${esc(fmtLatency(a.latency_ms))} · Uptime: ${esc(fmtDur(a.uptime_s||0))}</div></div>
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Credential</b>
        <div class="sub">${esc(credLbl(a.credential))}</div></div>
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Sync Group</b>
        <div class="sub row">${a.sync_group?`<span class="chip group">${esc(a.sync_group)}</span> <button class="btn btn-ghost btn-sm" data-open-group="${esc(a.sync_group)}">Open Group</button>`:"Independent"}</div></div>
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Assigned Plugins</b>
        <div class="sub row" style="gap:4px">${plugins.length?plugins.map(p=>`<span class="chip plugin">${esc(p)}<button data-unassign-plugin="${esc(p)}" title="Unassign ${esc(p)}" aria-label="Unassign ${esc(p)}" style="background:none;border:none;color:var(--faint);cursor:pointer;font-size:11px;padding:0 0 0 2px">✕</button></span>`).join(""):'<span class="faint">None assigned</span>'}
          <button class="btn btn-ghost btn-sm" data-assign-plugins="1">Assign Plugins…</button></div></div>
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Enabled</b>
        <div class="sub">${a.enabled?"Yes":"No"}</div></div>
    </div>`;
  const og = body.querySelector("[data-open-group]");
  if (og) og.onclick = () => { const gname = og.dataset.openGroup; $("dlg-details").close(); openGroupDetails(gname); };
  const ap = body.querySelector("[data-assign-plugins]");
  if (ap) ap.onclick = () => openPluginPicker(a.name);
  // per-plugin quick unassign (same pattern as group-member remove)
  body.querySelectorAll("[data-unassign-plugin]").forEach(b => b.onclick = () => {
    if (b.disabled) return; b.disabled = true;
    clearFb(body);
    post("/api/accounts/plugins/unassign",{account:a.name,plugin:b.dataset.unassignPlugin})
      .then(()=>afterAccountsRest().then(()=>openAcctDetails(a.name)))
      .catch(e=>{ setFb(body,"error",e.message); b.disabled=false; });
  });
  $("dlg-details").showModal();
}

/* ── Plugin picker (direct account→plugin assignment) ──────────────────────── */

/* Build a checkbox list of every discovered plugin (manifests — no
   hardcoding), pre-checking the names in `current` (a Set or null). */
function fillPluginChecks(host, current) {
  host.innerHTML = "";
  host.classList.add("picklist");
  if (!(manifests||[]).length) {
    const p = document.createElement("span");
    p.className = "faint"; p.style.fontSize = "11px";
    p.textContent = "No plugins discovered yet — the account will run none.";
    host.appendChild(p);
    return;
  }
  for (const m of manifests) {
    const l = document.createElement("label");
    l.className = "pick-row";
    // structured grid: fixed 18px checkbox | non-shrinking name | ellipsized
    // description — a long description can never squeeze the name/checkbox
    l.innerHTML = `<input type="checkbox" value="${esc(m.name)}"${current&&current.has(m.name)?" checked":""}>
      <span class="pl-name" title="${esc(m.name)}">${esc(m.name)}</span>
      <span class="pl-desc" title="${esc(m.description||"")}">${esc(m.description||"")}</span>`;
    host.appendChild(l);
  }
}

/* Desired-state editor: checkboxes from the manifests with the account's
   current assignments pre-checked; Save POSTs the WHOLE set to
   /api/accounts/plugins/set (all-or-nothing — the server validates every
   name before writing anything, so an unknown name 400s and this dialog
   stays open with the error). */
async function openPluginPicker(acctName) {
  const a = accounts().find(x=>x.name===acctName); if (!a) return;
  const body = openDetailsPopup(`Assign Plugins — ${a.name}`);
  if (!(manifests||[]).length) {
    body.innerHTML = '<p class="dim">No plugins discovered — install a plugin package first.</p>';
    return;
  }
  // Prefer the authoritative assignment list; the WS snapshot field is the
  // fallback when the fetch fails.
  let current = new Set(assignedPlugins(a));
  try {
    const fresh = await api("/api/accounts/plugins?account="+encodeURIComponent(a.name));
    if (Array.isArray(fresh)) current = new Set(fresh);
  } catch { /* keep the WS-snapshot fallback */ }
  const list = document.createElement("div");
  list.className = "picklist";
  fillPluginChecks(list, current);
  const actions = document.createElement("div");
  actions.className = "form-actions";
  const cancel = document.createElement("button");
  cancel.className = "btn btn-secondary"; cancel.textContent = "Cancel";
  cancel.dataset.dlgClose = "1";
  const save = document.createElement("button");
  save.className = "btn btn-primary"; save.textContent = "Save";
  save.onclick = async () => {
    clearFb(list);
    const chosen = [...list.querySelectorAll("input:checked")].map(i=>i.value);
    try {
      await withBusy(save, post("/api/accounts/plugins/set",{account:a.name,plugins:chosen}));
      $("dlg-details").close();
      await afterAccountsRest();
    } catch(e) { setFb(list, "error", e.message); }
  };
  actions.append(cancel, save);
  list.appendChild(actions);
  body.appendChild(list);
}
