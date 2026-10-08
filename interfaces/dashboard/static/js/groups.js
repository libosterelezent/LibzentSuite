"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   groups.js — Sync Groups page and every group popup/flow, over the REAL
   /api/groups/* backend (create/update/delete, add/remove/set/clear
   member). REST owns persisted identity (incl. EMPTY groups); the WS
   account stats provide live member state. Tile + list share wiring.
   ═══════════════════════════════════════════════════════════════════════════ */

function renderGroups() {
  const host = $("group-view"); if (!host) return;
  const byAcct = groupMap();
  // Authoritative group identity comes from GET /api/groups (persisted —
  // includes EMPTY groups groupMap() can never see); live member/runtime
  // info is joined in from the WS account stats (REST + WS separation).
  const persisted = groupsList.slice().sort((a,b)=>a.name.localeCompare(b.name));
  const extra = Object.keys(byAcct).filter(k=>k && !persisted.some(g=>g.name===k)).sort();
  $("cnt-groups").textContent = persisted.length;
  fillDatalist("group-list", persisted.map(g=>g.name).concat(extra));
  renderViewSel("vm-group", groupViewMode);
  host.innerHTML = "";
  if (!persisted.length && !extra.length) {
    host.innerHTML = groupsFailed
      ? emptyState("Groups Unavailable","The sync groups list could not be loaded — see the error toasts. The rest of the dashboard remains usable.")
      : emptyState("No Sync Groups","Create a group to let accounts coordinate their pacing.","+ Create Group");
    const cta = host.querySelector("button"); if (cta) cta.onclick = () => $("btn-create-group").click();
    return;
  }
  const wrap = document.createElement("div");
  const entries = persisted.map(g=>[g, byAcct[g.name]||[]])
    .concat(extra.map(name=>[{name, description:"", enabled:true, member_count:(byAcct[name]||[]).length}, byAcct[name]||[]]));
  if (groupViewMode === "list") {
    wrap.className = "listview";
    for (const [g, members] of entries) wrap.appendChild(groupRow(g, members));
  } else {
    wrap.className = "grid grid-2";
    for (const [g, members] of entries) wrap.appendChild(groupTile(g, members));
  }
  host.appendChild(wrap);
  const indep = byAcct[""]||[];
  if (indep.length && groupViewMode !== "list") {
    const card = document.createElement("div"); card.className="group-card"; card.style.opacity=.7;
    card.innerHTML=`<div class="hdr"><span class="name" style="color:var(--dim)">Independent</span>
      <span class="count" style="font-size:12px;color:var(--dim)">${indep.length} account${indep.length!==1?"s":""}</span></div>
      <div class="members">${indep.map(m=>`<span class="member"><span class="name" data-acct="${esc(m.name)}">${esc(m.name)}</span>${statusBadge(m.status)}</span>`).join("")}</div>`;
    card.querySelectorAll(".member .name").forEach(el=>el.onclick=()=>openAcctDetails(el.dataset.acct));
    wrap.appendChild(card);
  }
}

/* ── Tile view ─────────────────────────────────────────────────────────────── */
function groupTile(g, members) {
  const name = g.name;
  const online = members.filter(m=>m.status==="running").length;
  const card = document.createElement("div"); card.className="group-card";
  card.innerHTML = `
    <div class="hdr">
      <span class="name" title="Open group details">${esc(name)}</span>
      <span class="st ${g.enabled===false?"st-offline":"st-online"}">${g.enabled===false?"Disabled":"Enabled"}</span>
      <span class="st ${members.length&&online===members.length?"st-online":online===0?"st-offline":"st-warning"}" style="margin-left:auto">${members.length?`${online}/${members.length}`:"empty"}</span>
    </div>
    ${g.description?`<p class="desc" title="${esc(g.description)}">${esc(g.description)}</p>`:""}
    ${members.length ? `<div class="members">${members.map(m=>`
      <span class="member">
        <span class="name" data-acct="${esc(m.name)}">${esc(m.name)}</span>
        ${statusBadge(m.status)}
        ${m.cooldowns?'<span title="cooldown active">⏳</span>':""}
        <button class="rm" data-remove-member="${esc(m.name)}" title="Remove from group" aria-label="Remove ${esc(m.name)}">✕</button>
      </span>`).join("")}${members.length>8?`<span class="faint" style="font-size:11px;align-self:center">+${members.length-8} more in details</span>`:""}</div>`
      : `<p class="sub">Empty group — add a member or move accounts into it.</p>`}
    <div class="actions">
      <button class="btn btn-ghost btn-sm" data-gact="open">Open</button>
      <button class="btn btn-secondary btn-sm" data-gact="edit">Edit</button>
      <button class="btn btn-secondary btn-sm" data-gact="addmember">+ Member</button>
      <button class="btn btn-ghost btn-sm" data-gact="delete" style="color:var(--err)">Delete</button>
      <span style="flex:1"></span>
      <button class="btn btn-secondary btn-sm" data-gact="start" ${members.length?"":"disabled"}>Start</button>
      <button class="btn btn-secondary btn-sm" data-gact="stop" ${members.length?"":"disabled"}>Stop</button>
    </div>`;
  wireGroupActions(card, name, members);
  return card;
}

/* ── List view ─────────────────────────────────────────────────────────────── */
function groupRow(g, members) {
  const name = g.name;
  const online = members.filter(m=>m.status==="running").length;
  const row = document.createElement("div"); row.className="lrow grp";
  row.innerHTML = `
    <div class="lmain"><span class="lname" style="color:var(--warn)" title="Open group details">${esc(name)}</span></div>
    <span class="lcol lcol-status"><span class="st ${g.enabled===false?"st-offline":"st-online"}">${g.enabled===false?"off":"on"}</span></span>
    <span class="lcol lcol-members">${members.length?`${online}/${members.length} on`:"empty"}</span>
    <span class="lcol lmeta lcol-meta trunc">${esc(g.description||"")}</span>
    <span class="lactions">
      <button class="btn btn-ghost btn-sm" data-gact="open">Open</button>
      <button class="btn btn-secondary btn-sm" data-gact="edit">Edit</button>
      <button class="btn btn-secondary btn-sm" data-gact="addmember">+ Member</button>
      <button class="btn btn-ghost btn-sm" data-gact="delete" style="color:var(--err)">Del</button>
      <button class="btn btn-secondary btn-sm" data-gact="start" ${members.length?"":"disabled"}>Start</button>
      <button class="btn btn-secondary btn-sm" data-gact="stop" ${members.length?"":"disabled"}>Stop</button>
    </span>`;
  wireGroupActions(row, name, members);
  return row;
}

/* ── Shared action wiring (identical for tile and list) ────────────────────── */
function wireGroupActions(root, name, members) {
  const nm = root.querySelector(".lname, .name");
  if (nm) nm.onclick = () => openGroupDetails(name);
  root.querySelectorAll(".member .name[data-acct]").forEach(el=>el.onclick=()=>openAcctDetails(el.dataset.acct));
  root.querySelectorAll("[data-remove-member]").forEach(b=>b.onclick=()=>{
    if (b.disabled) return; b.disabled = true;
    clearFb(root);
    post("/api/groups/remove-member",{group:name,account:b.dataset.removeMember})
      .then(afterGroupMutation)
      .catch(e=>{ setFb(root,"error",e.message); b.disabled=false; });
  });
  root.querySelectorAll("[data-gact]").forEach(b=>b.onclick=()=>{
    if (b.disabled) return;
    const act = b.dataset.gact;
    if (act==="open") return openGroupDetails(name);
    if (act==="edit") return openGroupEdit(name);
    if (act==="addmember") return openMemberPicker(name);
    if (act==="delete") return deleteGroupFlow({name, member_count:members.length});
    // Start/Stop are scoped to THIS group's members — never the fleet.
    clearFb(root);
    withBusy(b, Promise.allSettled(members.map(m=>
      post(act==="start"?"/api/accounts/start":"/api/accounts/stop",{account:m.name})
    ))).then(rs=>{
      const failed = rs.filter(r=>r.status==="rejected");
      if (failed.length) setFb(root,"error",`${rs.length-failed.length}/${rs.length} succeeded — ${failed.map(r=>r.reason&&r.reason.message||String(r.reason)).join("; ")}`);
      else flashFb(root, `${rs.length} request${rs.length!==1?"s":""} sent`);
    });
  });
}

/* ── Group flows (real endpoints) ──────────────────────────────────────────── */
async function moveAccountGroup(accountName, group, scopeEl) {
  try {
    if (group===null) await post("/api/groups/clear-account",{account:accountName});
    else await post("/api/groups/set-account",{account:accountName, group});
    await afterGroupMutation();
    if (scopeEl) flashFb(scopeEl, `moved to ${group===null?"independent":group}`);
  } catch(e) {
    if (scopeEl) setFb(scopeEl,"error",e.message); else showErr(e.message);
  }
}

function deleteGroupFlow(g) {
  const count = g.member_count??0;
  if (count > 0) {
    confirmAction(`Delete Group ${g.name}`,
      `Group ${g.name} has ${count} member${count!==1?"s":""}. Deleting a group never deletes accounts — members return to independent (per-account) pacing.`,
      ()=>post("/api/groups/delete",{group:g.name,detach_members:true})
        .then(afterGroupMutation).catch(e=>toast(e.message)),
      "Delete + Detach Members");
  } else {
    confirmAction(`Delete Group ${g.name}`,
      `Delete the empty group ${g.name}?`,
      ()=>post("/api/groups/delete",{group:g.name,detach_members:false})
        .then(afterGroupMutation).catch(e=>toast(e.message)));
  }
}

/* ── Group popups ──────────────────────────────────────────────────────────── */
function openGroupPicker(title, currentGroup, onPick) {
  const body = openDetailsPopup(title);
  const list = document.createElement("div"); list.className = "form";
  const row = (label, value, hint, active) => {
    const l = document.createElement("label");
    l.style.cssText = "display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;font-size:13px;cursor:pointer;border:1px solid "+(active?"var(--accent2)":"transparent")+";background:"+(active?"rgba(91,163,245,.12)":"var(--surface2)");
    l.innerHTML = `<input type="radio" name="gpick" ${active?"checked":""} data-group="${esc(value||"")}">
      <b>${esc(label)}</b>${hint?`<span class="faint trunc" style="font-size:11px">${esc(hint)}</span>`:""}`;
    list.appendChild(l);
  };
  row("Independent / No Group", "", "per-account pacing", !currentGroup);
  for (const g of groupsList) row(g.name, g.name, g.enabled===false?"disabled":(g.description||""), currentGroup===g.name);
  const actions = document.createElement("div"); actions.className = "form-actions";
  const cancel = btn("Cancel",null,"btn btn-secondary"); cancel.dataset.dlgClose = "1";
  const move = btn("Move",null,"btn btn-primary");
  move.onclick = () => {
    const sel = list.querySelector('input[name="gpick"]:checked');
    if (!sel) return;
    $("dlg-details").close();
    onPick(sel.dataset.group==="" ? null : sel.dataset.group);
  };
  actions.append(cancel, move);
  list.appendChild(actions);
  body.appendChild(list);
}

async function openGroupDetails(name) {
  UI.go("fleet"); UI.tab("groups");
  let snap;
  try { snap = await api("/api/groups/info?group="+encodeURIComponent(name)); }
  catch(e) { toast(e.message); return; }
  const body = openDetailsPopup(snap.name);
  const members = snap.members||[];
  const count = snap.member_count??members.length;
  const online = members.filter(m=>m.status==="running").length;
  body.innerHTML = `
    <div class="row" style="margin-bottom:var(--sp-2)">
      <span class="st ${snap.enabled?"st-online":"st-offline"}">${snap.enabled?"Enabled":"Disabled"}</span>
      <span class="dim" style="font-size:12px">${count} member${count!==1?"s":""}${count?` · ${online} online`:""}</span>
    </div>
    ${snap.description?`<p class="sub" style="margin-bottom:var(--sp-3);overflow-wrap:anywhere">${esc(snap.description)}</p>`:""}
    <div class="card2" style="margin-bottom:var(--sp-3)"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Pacing</b>
      <div class="sub mono">${esc(snap.pacing_key||"")}</div></div>
    <b style="font-size:11px;color:var(--dim);text-transform:uppercase">Members</b>
    ${members.length ? `<div style="display:flex;flex-direction:column;gap:4px;margin:var(--sp-2) 0">${members.map(m=>`
      <div class="row" style="padding:4px 10px;background:var(--surface2);border-radius:6px;font-size:13px">
        <span style="cursor:pointer;font-weight:500" data-acct="${esc(m.name)}">${esc(m.name)}</span>
        ${statusBadge(m.status)}
        ${m.enabled===false?'<span class="chip deny">disabled</span>':""}
        <button class="btn btn-ghost btn-sm" style="margin-left:auto" data-remove="${esc(m.name)}">Remove</button>
      </div>`).join("")}</div>`
    : `<p class="sub">Empty group.</p>`}
    <div class="form-actions" style="justify-content:flex-start;margin-top:var(--sp-3)">
      <button class="btn btn-secondary btn-sm" data-gd="edit">Edit</button>
      <button class="btn btn-secondary btn-sm" data-gd="add">+ Add Member</button>
      <button class="btn btn-danger btn-sm" data-gd="del">Delete</button>
    </div>`;
  body.querySelectorAll("[data-acct]").forEach(el=>el.onclick=()=>{ $("dlg-details").close(); openAcctDetails(el.dataset.acct); });
  body.querySelectorAll("[data-remove]").forEach(b=>b.onclick=()=>{
    if (b.disabled) return; b.disabled = true;
    post("/api/groups/remove-member",{group:snap.name,account:b.dataset.remove})
      .then(()=>{ afterGroupMutation(); openGroupDetails(snap.name); })
      .catch(e=>{ setFb(body,"error",e.message); b.disabled=false; });
  });
  body.querySelector('[data-gd="edit"]').onclick = () => openGroupEdit(snap.name);
  body.querySelector('[data-gd="add"]').onclick = () => openMemberPicker(snap.name);
  body.querySelector('[data-gd="del"]').onclick = () => { $("dlg-details").close(); deleteGroupFlow(snap); };
  $("dlg-details").showModal();
}

function openGroupEdit(name) {
  const g = groupsList.find(x=>x.name===name);
  if (!g) { toast("unknown group: "+name); return; }
  const body = openDetailsPopup(`Edit group — ${g.name}`);
  const form = document.createElement("div"); form.className = "form";
  form.innerHTML = `
    <div class="field"><label>Name</label><input id="ge-name" value="${esc(g.name)}"></div>
    <div class="field"><label>Description</label><input id="ge-desc" value="${esc(g.description||"")}"></div>
    <div class="field"><label>Enabled</label>
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" id="ge-enabled" ${g.enabled?"checked":""}> Group is active</label></div>
    <p class="hint" style="font-size:11px;color:var(--faint)">Renaming moves every member (and their shared pacing) to the new name.</p>`;
  const actions = document.createElement("div"); actions.className = "form-actions";
  const cancel = btn("Cancel",null,"btn btn-secondary"); cancel.dataset.dlgClose = "1";
  const save = btn("Save",null,"btn btn-primary");
  save.onclick = async () => {
    clearFb(form);
    const nn = form.querySelector("#ge-name").value.trim();
    if (!nn) { setFb(form,"error","Group name is required."); return; }
    // Only send the fields that actually changed.
    const payload = {group:g.name};
    if (nn!==g.name) payload.name = nn;
    const nd = form.querySelector("#ge-desc").value;
    if (nd !== (g.description||"")) payload.description = nd;
    const ne = form.querySelector("#ge-enabled").checked;
    if (ne !== !!g.enabled) payload.enabled = ne;
    if (Object.keys(payload).length===1) { $("dlg-details").close(); return; }
    try { await withBusy(save, post("/api/groups/update",payload)); $("dlg-details").close(); await afterGroupMutation(); }
    catch(e){ setFb(form,"error",e.message); }
  };
  actions.append(cancel, save);
  form.appendChild(actions);
  body.appendChild(form);
  $("dlg-details").showModal();
}

function openMemberPicker(groupName) {
  const body = openDetailsPopup(`Add member — ${groupName}`);
  const candidates = accounts().filter(a=>a.sync_group!==groupName);
  if (!candidates.length) {
    body.innerHTML = '<p class="dim">Every account is already a member of this group.</p>';
    return;
  }
  const note = document.createElement("p"); note.className = "sub";
  note.textContent = "Adding an account REPLACES its previous group membership.";
  body.appendChild(note);
  const list = document.createElement("div"); list.className = "form";
  for (const a of candidates) {
    const l = document.createElement("label");
    l.style.cssText = "display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface2);border-radius:6px;font-size:13px;cursor:pointer;flex-wrap:wrap";
    l.innerHTML = `<input type="checkbox" data-member="${esc(a.name)}"> <b>${esc(a.name)}</b> ${statusBadge(a.status)}
      ${a.sync_group?`<span class="chip group" style="margin-left:auto">${esc(a.sync_group)}</span>`:'<span class="faint" style="margin-left:auto;font-size:11px">independent</span>'}`;
    list.appendChild(l);
  }
  const actions = document.createElement("div"); actions.className = "form-actions";
  const cancel = btn("Cancel",null,"btn btn-secondary"); cancel.dataset.dlgClose = "1";
  const add = btn("Add",null,"btn btn-primary");
  add.onclick = async () => {
    const chosen = [...list.querySelectorAll("input[data-member]:checked")].map(i=>i.dataset.member);
    if (!chosen.length) { setFb(list,"error","Select at least one account."); return; }
    clearFb(list);
    const rs = await withBusy(add, Promise.allSettled(chosen.map(n=>post("/api/groups/add-member",{group:groupName,account:n}))));
    const failed = rs.filter(r=>r.status==="rejected");
    if (failed.length) { setFb(list,"error",`${rs.length-failed.length}/${rs.length} added — ${failed.map(r=>r.reason&&r.reason.message||String(r.reason)).join("; ")}`); add.disabled=false; return; }
    $("dlg-details").close();
    await afterGroupMutation();
  };
  actions.append(cancel, add);
  list.appendChild(actions);
  body.appendChild(list);
}
