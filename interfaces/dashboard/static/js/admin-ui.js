"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   admin-ui.js — the Admin panel: command permissions (per-account Discord
   user-id lists), prefix settings (mention/symbol/both per account),
   admin accounts (framework admin id list + per-account admin flags),
   and the Mudae bot id. Every mutation hits a real /api/admin/* endpoint.
   ═══════════════════════════════════════════════════════════════════════════ */

let adminOverview = null;

async function loadAdmin() {
  try {
    adminOverview = await api("/api/admin/overview") || null;
    renderAdmin();
  } catch (e) { toast(e.message); }
}

function renderAdmin() {
  const host = $("admin-content"); if (!host) return;
  if (!adminOverview) { host.innerHTML = emptyState("Admin Unavailable", "The access store could not be loaded."); return; }
  // Skip the rebuild while an admin input/select has focus — a WS
  // structural tick would otherwise wipe half-typed ids mid-entry; the
  // next render after focus leaves catches the data up.
  if (host.querySelector("input:focus, select:focus, textarea:focus")) return;
  const ov = adminOverview;
  host.innerHTML = "";

  // ── Admin id list ─────────────────────────────────────────────────
  const adminCard = document.createElement("div"); adminCard.className = "card";
  adminCard.innerHTML = `<h4>Framework Admins</h4>
    <p class="sub" style="margin-bottom:var(--sp-2)">Discord user ids that may command ANY account (bypass per-account lists). The old OWNER_USER_IDS env is ignored after the one-time migration.</p>
    <div id="admin-ids" style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:var(--sp-2)">
      ${(ov.admins||[]).map(id => `<span class="chip plugin" data-admin-id="${esc(id)}">${esc(id)} <b data-rm-admin="${esc(id)}" style="cursor:pointer;color:var(--err)">✕</b></span>`).join("") || '<span class="faint">no admins — nobody can command any account (fail-closed)</span>'}
    </div>
    <div class="row">
      <input id="admin-id-input" type="text" placeholder="discord user id" style="width:200px">
      <button class="btn btn-primary btn-sm" id="admin-id-add">+ Add Admin</button>
    </div>`;
  host.appendChild(adminCard);

  // ── Mudae bot id ──────────────────────────────────────────────────
  const mudaeCard = document.createElement("div"); mudaeCard.className = "card";
  mudaeCard.innerHTML = `<h4>Mudae Bot ID</h4>
    <div class="row">
      <input id="mudae-id-input" type="text" placeholder="432610292342587392" value="${esc(ov.mudae_bot_id ?? "")}" style="width:220px">
      <button class="btn btn-primary btn-sm" id="mudae-id-save">Save</button>
    </div>
    <p class="sub">Migrated from MUDAE_BOT_ID env — DB value wins; env is the fallback when empty.</p>`;
  host.appendChild(mudaeCard);

  // ── Per-account access rows ───────────────────────────────────────
  const accountsCard = document.createElement("div"); accountsCard.className = "card";
  accountsCard.innerHTML = `<h4>Per-Account Access</h4>
    <p class="sub" style="margin-bottom:var(--sp-2)">Command permissions, prefix mode, and the admin flag for each account. Prefix changes apply on next connect.</p>
    <div id="admin-account-rows"></div>`;
  host.appendChild(accountsCard);
  const rowsHost = accountsCard.querySelector("#admin-account-rows");
  const accts = accounts();  // live WS state for names
  const known = new Set([...(Object.keys(ov.accounts||{})), ...accts.map(a=>a.name)]);
  for (const name of [...known].sort()) {
    const row = ov.accounts?.[name] || {is_admin: false, prefix_mode: "mention", prefix_symbol: "", allowed_user_ids: []};
    rowsHost.appendChild(adminAccountRow(name, row));
  }

  wireAdmin();
}

function adminAccountRow(name, row) {
  const div = document.createElement("div"); div.className = "card2";
  div.style.marginBottom = "var(--sp-2)";
  const perms = (row.allowed_user_ids||[]);
  div.innerHTML = `
    <div class="row" style="margin-bottom:6px">
      <b>${esc(name)}</b>
      ${row.is_admin ? '<span class="chip grant">ADMIN</span>' : ""}
      <span class="faint" style="font-size:11px">prefix: ${esc(row.prefix_mode)}${row.prefix_symbol ? " "+esc(row.prefix_symbol) : ""}</span>
      <span class="spacer" style="flex:1"></span>
      <button class="btn ${row.is_admin ? "btn-danger" : "btn-secondary"} btn-sm" data-flag-admin="${esc(name)}" data-val="${row.is_admin ? "false" : "true"}">
        ${row.is_admin ? "Remove Admin" : "Make Admin"}</button>
    </div>
    <div class="row" style="gap:4px;flex-wrap:wrap">
      <span class="faint" style="font-size:11px">command perms:</span>
      ${perms.map(id => `<span class="chip plugin" data-perm-id="${esc(id)}">${esc(id)} <b data-rm-perm="${esc(id)}" data-acct="${esc(name)}" style="cursor:pointer;color:var(--err)">✕</b></span>`).join("") || '<span class="faint" style="font-size:11px">empty — admins only (fail-closed)</span>'}
    </div>
    <div class="row" style="margin-top:6px">
      <input data-perm-input="${esc(name)}" type="text" placeholder="discord user id" style="width:180px">
      <button class="btn btn-secondary btn-sm" data-add-perm="${esc(name)}">+ Allow</button>
      <span class="spacer" style="flex:1"></span>
      <select data-prefix-mode="${esc(name)}" style="width:110px">
        ${["mention","symbol","both"].map(m => `<option value="${m}" ${row.prefix_mode===m?"selected":""}>${m}</option>`).join("")}
      </select>
      <input data-prefix-symbol="${esc(name)}" type="text" placeholder="sm!" value="${esc(row.prefix_symbol||"")}" style="width:70px" maxlength="4">
      <button class="btn btn-secondary btn-sm" data-set-prefix="${esc(name)}">Set Prefix</button>
    </div>`;
  return div;
}

function wireAdmin() {
  const adminCard = $("admin-id-add");
  if (adminCard) adminCard.onclick = async () => {
    const v = $("admin-id-input").value.trim();
    if (!v || !/^\d+$/.test(v)) { setFb(adminCard.closest(".card"), "error", "Enter a numeric Discord user id."); return; }
    try { await post("/api/admin/ids/add", {user_id: parseInt(v, 10)}); await loadAdmin(); }
    catch (e) { setFb(adminCard.closest(".card"), "error", e.message); }
  };
  $$("[data-rm-admin]").forEach(b => b.onclick = async () => {
    try { await post("/api/admin/ids/remove", {user_id: parseInt(b.dataset.rmAdmin, 10)}); await loadAdmin(); }
    catch (e) { toast(e.message); }
  });
  const mudaeSave = $("mudae-id-save");
  if (mudaeSave) mudaeSave.onclick = async () => {
    const raw = $("mudae-id-input").value.trim();
    const body = raw === "" ? {mudae_bot_id: null} : {mudae_bot_id: parseInt(raw, 10)};
    if (raw !== "" && !/^\d+$/.test(raw)) { setFb(mudaeSave.closest(".card"), "error", "Mudae id must be numeric."); return; }
    try { await post("/api/admin/mudae-id/set", body); flashFb(mudaeSave.closest(".card"), "saved"); await loadAdmin(); }
    catch (e) { setFb(mudaeSave.closest(".card"), "error", e.message); }
  };
  $$("[data-flag-admin]").forEach(b => b.onclick = async () => {
    try {
      await post("/api/admin/account-flag", {account: b.dataset.flagAdmin, is_admin: b.dataset.val === "true"});
      await loadAdmin();
    } catch (e) { toast(e.message); }
  });
  $$("[data-add-perm]").forEach(b => b.onclick = async () => {
    const input = document.querySelector(`[data-perm-input="${CSS.escape(b.dataset.addPerm)}"]`);
    const v = (input?.value || "").trim();
    if (!v || !/^\d+$/.test(v)) { setFb(b.closest(".card2"), "error", "Enter a numeric Discord user id."); return; }
    try { await post("/api/admin/perms/add", {account: b.dataset.addPerm, user_id: parseInt(v, 10)}); await loadAdmin(); }
    catch (e) { setFb(b.closest(".card2"), "error", e.message); }
  });
  $$("[data-rm-perm]").forEach(b => b.onclick = async () => {
    try { await post("/api/admin/perms/remove", {account: b.dataset.acct, user_id: parseInt(b.dataset.rmPerm, 10)}); await loadAdmin(); }
    catch (e) { toast(e.message); }
  });
  $$("[data-set-prefix]").forEach(b => b.onclick = async () => {
    const name = b.dataset.setPrefix;
    const mode = document.querySelector(`[data-prefix-mode="${CSS.escape(name)}"]`)?.value || "mention";
    const symbol = document.querySelector(`[data-prefix-symbol="${CSS.escape(name)}"]`)?.value?.trim() || "";
    try { await post("/api/admin/prefix/set", {account: name, mode, symbol}); flashFb(b.closest(".card2"), `prefix → ${mode}${symbol ? " "+symbol : ""}`); await loadAdmin(); }
    catch (e) { setFb(b.closest(".card2"), "error", e.message); }
  });
}
