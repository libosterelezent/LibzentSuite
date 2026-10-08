"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   accounts.js — Accounts page: tile cards, list rows, the account details
   popup and the profile picker. Tile and List render the SAME data with
   the SAME action wiring (wireAccountActions) — only the markup differs,
   so business logic is never duplicated between views.
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
function effectiveCaps(acct, profs) {
  const set = new Set(); let hasKnown = false;
  for (const pn of acct.profiles||[]) {
    const p = (profs||profilesList).find(x=>x.name===pn);
    if (!p) continue; hasKnown = true;
    for (const pl of p.plugins||[]) set.add(pl);
  }
  return hasKnown ? set : null; // null = unrestricted
}

function renderAccounts() {
  const host = $("acct-view"); if (!host) return;
  $("cnt-accts").textContent = accounts().length;
  fillSelect($("task-filter-account"), accounts().map(a=>a.name), "all accounts");
  fillSelect($("log-acct"), accounts().map(a=>a.name), "all accounts");
  fillSelect($("audit-acct"), accounts().map(a=>a.name), "all accounts");
  fillDatalist("group-list", groupsList.map(g=>g.name));
  fillDatalist("prof-list", profilesList.map(p=>p.name));
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
      ${a.cooldowns?`<span class="item" style="color:var(--warn)">⏳ ${a.cooldowns}</span>`:""}
      ${a.sync_group?`<span class="item"><span class="chip group" data-group-chip="${esc(a.sync_group)}" title="Open group">${esc(a.sync_group)}</span></span>`:""}
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
      <button data-act="profile">Assign profile…</button>
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
  if (a.profiles&&a.profiles.length) bits.push(a.profiles.join(","));
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
      if (act==="profile") openProfilePicker(a.name);
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
  const caps = effectiveCaps(a);
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
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Profiles</b>
        <div class="sub" style="display:flex;flex-wrap:wrap;gap:4px">${(a.profiles||[]).map(p=>`<span class="chip profile">${esc(p)}</span>`).join(" ")||"None"}</div></div>
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Effective Capabilities</b>
        <div class="sub" style="display:flex;flex-wrap:wrap;gap:4px">${caps===null?'<span class="chip grant">unrestricted</span>':[...caps].map(c=>`<span class="chip plugin">${esc(c)}</span>`).join(" ")||"none"}</div></div>
      <div class="card2"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Enabled</b>
        <div class="sub">${a.enabled?"Yes":"No"}</div></div>
    </div>`;
  const og = body.querySelector("[data-open-group]");
  if (og) og.onclick = () => { const gname = og.dataset.openGroup; $("dlg-details").close(); openGroupDetails(gname); };
  $("dlg-details").showModal();
}

/* ── Profile picker (real /api/profiles/assign + unassign) ─────────────────── */
function openProfilePicker(acctName) {
  const a = accounts().find(x=>x.name===acctName); if (!a) return;
  const body = openDetailsPopup(`Assign profiles — ${a.name}`);
  if (!profilesList.length) {
    body.innerHTML = '<p class="dim">No profiles defined yet — create one on the Profiles page first.</p>';
    return;
  }
  const current = new Set(a.profiles||[]);
  const list = document.createElement("div");
  list.className = "form";
  for (const p of profilesList) {
    const l = document.createElement("label");
    l.style.cssText = "display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px;cursor:pointer";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = current.has(p.name); cb.dataset.profile = p.name;
    l.appendChild(cb);
    l.appendChild(document.createTextNode(p.name));
    if (p.description) { const s=document.createElement("span"); s.className="faint"; s.style.fontSize="11px"; s.textContent=" — "+p.description; l.appendChild(s); }
    list.appendChild(l);
  }
  const actions = document.createElement("div");
  actions.className = "form-actions";
  const save = document.createElement("button");
  save.className = "btn btn-primary"; save.textContent = "Save";
  save.onclick = async () => {
    const chosen = new Set([...list.querySelectorAll("input[data-profile]:checked")].map(i=>i.dataset.profile));
    try {
      const ops = [];
      for (const p of profilesList) {
        if (chosen.has(p.name) && !current.has(p.name)) ops.push(post("/api/profiles/assign",{account:a.name,profile:p.name}));
        if (!chosen.has(p.name) && current.has(p.name)) ops.push(post("/api/profiles/unassign",{account:a.name,profile:p.name}));
      }
      const rs = await withBusy(save, Promise.allSettled(ops));
      const failed = rs.filter(r=>r.status==="rejected");
      if (failed.length) { setFb(list, "error", failed.map(r=>r.reason&&r.reason.message||String(r.reason)).join("; ")); return; }
      $("dlg-details").close();
      await afterAccountsRest();
    } catch(e) { setFb(list, "error", e.message); }
  };
  const cancel = document.createElement("button");
  cancel.className = "btn btn-secondary"; cancel.textContent = "Cancel";
  cancel.dataset.dlgClose = "1";
  actions.append(cancel, save);
  list.appendChild(actions);
  body.appendChild(list);
}
