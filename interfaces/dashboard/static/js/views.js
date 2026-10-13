"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views.js — page renderers that are not Accounts/Groups: Home, Policies,
   and the Activity streams (tasks, cooldowns, logs, audit). Group flows
   live in groups.js; accounts in accounts.js; the Plugins and Profiles
   tabs (plugin-profile library, bindings, editor) in plugins-ui.js.
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Home ──────────────────────────────────────────────────────────────────── */
function getAttention() {
  const items = [];
  for (const a of accounts()) {
    if (a.status==="missing_credential"||a.status==="invalid_credential")
      items.push({level:"error",icon:"🔑",title:a.name,detail:"Credential "+(a.status==="invalid_credential"?"invalid":"missing"),action:()=>UI.go("fleet")});
    if (a.status==="failed")
      items.push({level:"error",icon:"✕",title:a.name,detail:"Account failed",action:()=>UI.go("fleet")});
  }
  return items;
}

/* renderHome's async plugin-stat fetches race the synchronous innerHTML
   reset — this seq counter drops results from superseded renders (the
   duiSeq pattern from dashboard-ui.js). */
let homeStatsSeq = 0;

function renderHome() {
  const attn = getAttention();
  const el = $("home-attn"); el.innerHTML = "";
  for (const a of attn) {
    const d = document.createElement("div"); d.className=`attn-item ${a.level}`;
    d.innerHTML=`<span class="icon">${a.icon}</span><div class="txt"><b>${esc(a.title)}</b><div class="detail">${esc(a.detail)}</div></div>`;
    d.onclick=a.action; el.appendChild(d);
  }
  const t = (lastStats&&lastStats.tasks)||{};
  const running = accounts().filter(a=>a.status==="running").length;
  const total = accounts().length;
  const byAcct = groupMap();
  // Persisted groups (REST) — includes EMPTY groups groupMap() cannot see
  const groupCount = groupsList.length;
  const cds = (lastStats&&lastStats.ratelimit&&lastStats.ratelimit.cooldowns&&lastStats.ratelimit.cooldowns.accounts)||{};
  $("home-metrics").innerHTML = `
    <div class="metric"><div class="label">Accounts</div><div class="value ${running===total?"ok":running===0?"err":"warn"}">${running}/${total}</div><div class="sub">online</div></div>
    <div class="metric"><div class="label">Groups</div><div class="value">${groupCount}</div><div class="sub">sync groups</div></div>
    <div class="metric"><div class="label">Tasks</div><div class="value">${Number(t.running??0)}</div><div class="sub">${Number(t.queued??0)} queued</div></div>
    <div class="metric"><div class="label">Cooldowns</div><div class="value">${Object.keys(cds).length}</div><div class="sub">active</div></div>`;
  const gr = $("home-groups"); gr.innerHTML = "";
  for (const g of groupsList.slice().sort((a,b)=>(b.member_count||0)-(a.member_count||0))) {
    const members = byAcct[g.name]||[];
    const online = members.filter(m=>m.status==="running").length;
    const count = g.member_count??members.length;
    const d = document.createElement("div"); d.className="row";
    d.style.cssText="padding:6px 10px;background:var(--surface2);border-radius:6px;margin:3px 0;cursor:pointer";
    d.innerHTML=`<b class="trunc" style="color:var(--warn)">${esc(g.name)}</b>${g.enabled===false?'<span class="faint" style="font-size:11px">disabled</span>':""}<span class="dim trunc" style="font-size:12px">${count} member${count!==1?"s":""}</span><span style="margin-left:auto;font-size:12px;color:${count&&online===count?"var(--ok)":"var(--dim)"}">${count?`● ${online}/${count}`:"empty"}</span>`;
    d.onclick=()=>openGroupDetails(g.name);
    gr.appendChild(d);
  }
  // plugin-contributed stat cards: appended INSIDE home-metrics so they
  // are rebuilt with it on every renderHome (no accumulation — the old
  // sibling-insert survived innerHTML rebuilds and duplicated)
  const statAccount = accounts().length ? accounts()[0].name : "";
  const seq = ++homeStatsSeq;
  if (statAccount) {
    loadPluginStats(statAccount).then(cards => {
      if (seq !== homeStatsSeq) return;  // a newer render superseded this fetch
      if (cards.length && currentView === "home") {
        const m = $("home-metrics");
        if (m) m.innerHTML += pluginStatsHTML(cards);
      }
    }).catch(() => {});
  }
  const indep = byAcct[""]||[];
  if (indep.length) { const d=btn("","","chip");
      d.style.cssText="display:block;margin:3px 0;padding:6px 10px;background:var(--surface2);border-radius:6px;font-size:12px;color:var(--dim);width:100%;text-align:left";
    d.textContent=`${indep.length} independent account${indep.length!==1?"s":""}`; d.onclick=()=>{UI.go("fleet");UI.tab("groups")}; gr.appendChild(d); }
  if (!gr.children.length) gr.innerHTML='<span class="dim">no groups yet</span>';
}

/* ── Policies ──────────────────────────────────────────────────────────────── */
let policyList = [];
/* Prefill the shared policy dialog for EDITING an existing row (same
   POST /api/ratelimit/set — the engine keys policies by (level, owner,
   action), so setting an existing key UPDATES it: memory first (instant),
   then persisted). */
function openPolicyEditor([level, owner, action, interval, jmin, jmax]) {
  $("rp-title").textContent = "Edit Policy";
  $("rp-action").value = action;
  $("rp-interval").value = interval;
  $("rp-jmin").value = jmin || "";
  $("rp-jmax").value = jmax || "";
  $("rp-level").value = level;
  $("rp-owner").value = owner || "";
  const dlg = $("dlg-policy");
  clearFb(dlg.querySelector(".form"));
  dlg.showModal(); $("rp-interval").focus();
}
async function loadPolicies() {
  try { policyList = await api("/api/ratelimit/policies")||[]; } catch(e) { showErr(e.message); }
  renderPolicyTable();
}
function renderPolicyTable() {
  const tbl = $("rl-policy-table"); if (!tbl) return;
  if (!policyList.length) { tbl.innerHTML="<tbody><tr><td style='text-align:center;padding:16px;color:var(--dim)'>No stored policies — Discord bucket pacing is active by default.</td></tr></tbody>"; return; }
  tbl.innerHTML = `<thead><tr><th>Scope</th><th>Owner</th><th>Action</th><th>Interval</th><th>Jitter</th><th></th></tr></thead><tbody>` +
    policyList.map(p=>`<tr><td>${scopeBadge(p.level)}</td><td>${esc(p.owner||"—")}</td><td>${esc(p.action)}</td>
      <td>${p.interval}s</td><td>${(p.jitter&&p.jitter.length)?esc(p.jitter[0]+"–"+p.jitter[1])+"s":"—"}</td>
      <td><button class="btn btn-ghost btn-sm" data-rledit="${esc(p.level)}|${esc(p.owner||"")}|${esc(p.action)}|${esc(p.interval)}|${esc(p.jitter&&p.jitter[0]||0)}|${esc(p.jitter&&p.jitter[1]||0)}">edit</button>
      <button class="btn btn-ghost btn-sm" data-rlclear="${esc(p.level)}|${esc(p.owner||"")}|${esc(p.action)}">clear</button></td></tr>`).join("")+"</tbody>";
  tbl.querySelectorAll("[data-rledit]").forEach(b=>b.onclick=()=>openPolicyEditor(b.dataset.rledit.split("|")));
  tbl.querySelectorAll("[data-rlclear]").forEach(b=>b.onclick=()=>{
    const [level, owner, action] = b.dataset.rlclear.split("|");
    confirmAction("Clear Policy", `Remove the ${level}${owner?" · "+owner:""} policy for '${action}'?`,
      ()=>post("/api/ratelimit/clear",{level, owner:owner||undefined, action}).then(loadPolicies).catch(e=>toast(e.message)));
  });
}

function renderPolicies() {
  const eff = $("rl-effective-view");
  if (lastStats && lastStats.ratelimit) {
    const rl = lastStats.ratelimit;
    eff.innerHTML = `<div class="rl-effective"><div class="value">${rl.floor||"—"}s</div><div class="label">Safety Floor</div></div>
      <p class="sub" style="margin-top:var(--sp-2);text-align:center">Discord bucket limits are the primary constraint.</p>`;
    const hk = $("rl-429"); if (hk) {
      const hits = rl.hits_429||{accounts:{},total:0};
      const accts = Object.entries(hits.accounts||{});
      hk.innerHTML = (hits.total||accts.length)
        ? `<div class="rl-block"><span class="label">Fleet total throttles (429)</span><span class="value" style="color:${hits.total?"var(--warn)":"var(--dim)"}">${hits.total??0}</span></div>` +
          (accts.length ? accts.map(([a,n]) =>
            `<div class="bucket-row"><span class="trunc" style="color:var(--dim)">${esc(a)}</span><span class="remaining ${n>=10?"zero":n>=3?"low":""}" style="margin-left:auto">${n}</span><span class="dim">hit${n!==1?"s":""}</span></div>`).join("") : "")
        : '<span class="dim">No 429 throttles recorded — every send stayed under Discord limits.</span>';
    }
    const floor = $("rl-floor");
    if (floor) floor.innerHTML = `<div class="rl-block"><span class="label">Minimum enforced interval</span>
      <span class="value">${esc(String(rl.floor??"—"))}s</span>
      <span class="sub">Configured intervals below this floor are raised to it.</span></div>`;
  }
  const warn = $("rl-warnings");
  if (warn) {
    // Formatted entries: message text + level/code chips (never raw JSON).
    const ws = (lastStats&&lastStats.warnings)||[];
    warn.innerHTML = ws.length
      ? `<div class="attn">${ws.map(w=>{
          const msg = typeof w==="string" ? w : (w.message||String(w));
          const lvl = (w&&w.level)||"warn";
          const code = (w&&w.code)||"";
          const cls = lvl==="danger"?"error":lvl==="warn"?"warn":"";
          const icon = lvl==="danger"?"⛔":lvl==="warn"?"⚠":"ℹ";
          return `<div class="attn-item ${cls}"><span class="icon">${icon}</span>
            <div class="txt"><div class="detail" style="overflow-wrap:anywhere">${esc(msg)}</div></div>
            ${code?`<span class="chip" style="flex-shrink:0">${esc(code)}</span>`:""}</div>`;
        }).join("")}</div>`
      : '<span class="dim">No rate-limit warnings.</span>';
  }
}

/* ── Activity: tasks / cooldowns / logs / audit ────────────────────────────── */
function renderTaskTable() {
  const cnt = $("cnt-tasks");
  const tbl = $("task-table");
  if (!cnt || !tbl) return;   // legacy activity hosts replaced by the framework page
  const st = $("task-filter-status")?.value||"";
  const ac = $("task-filter-account")?.value||"";
  const filtered = taskList.filter(t=>(!st||t.status===st)&&(!ac||t.account===ac));
  cnt.textContent = filtered.length;
  if (!filtered.length) { tbl.innerHTML="<tbody><tr><td style='text-align:center;padding:20px;color:var(--dim)'>No active tasks</td></tr></tbody>"; return; }
  tbl.innerHTML = `<thead><tr><th>Account</th><th>Plugin</th><th>Action</th><th>Status</th><th>Priority</th><th>Age</th><th></th></tr></thead><tbody>` +
    filtered.map(t=>`<tr><td>${esc(t.account)}</td><td>${esc(t.plugin)}</td><td>${esc(t.action)}</td>
      <td><span class="chip task-${esc(t.status)}">${esc(t.status)}</span></td><td>${esc(String(t.priority??""))}</td><td>${esc(fmtAge(t.submitted_at))}</td>
      <td>${t.status==="queued"||t.status==="running"?`<button class="btn btn-ghost btn-sm" data-cancel="${esc(t.id)}">cancel</button>`:""}</td></tr>`).join("")+"</tbody>";
  // innerHTML-serialized buttons carry no handlers — bind after parsing
  tbl.querySelectorAll("[data-cancel]").forEach(b=>b.onclick=()=>post("/api/tasks/cancel",{task_id:+b.dataset.cancel}).then(loadTasks).catch(e=>toast(e.message)));
  const hist = $("task-history");
  if (taskHistory.length) {
    hist.innerHTML = `<thead><tr><th>Account</th><th>Plugin</th><th>Status</th><th>Duration</th><th>Finished</th></tr></thead><tbody>` +
      taskHistory.slice(0,20).map(t=>`<tr><td>${esc(t.account)}</td><td>${esc(t.plugin)}</td>
        <td><span class="chip task-${esc(t.status)}">${esc(t.status)}</span></td><td>${t.duration_s!=null?Number(t.duration_s).toFixed(1)+"s":"-"}</td><td>${esc(fmtAge(t.finished_at))}</td></tr>`).join("")+"</tbody>";
  }
  else hist.innerHTML = "";
}

function renderCooldowns(cds) {
  $("cnt-cd").textContent = cds.filter(c=>(c.remaining_s??0)>0).length;
  const list = $("cd-list"); list.innerHTML = "";
  if (!cds.length) { list.innerHTML=emptyState("No Cooldowns","No active cooldowns."); return; }
  for (const cd of cds) {
    const rem = Math.max(0,(cd.remaining_s??0));
    const d = document.createElement("div"); d.className="cd-card";
    d.innerHTML=`<div class="info"><div class="who trunc">${esc(cd.account||"")}</div><div class="what trunc">${esc(cd.plugin||"")} · ${esc(cd.cooldown_key||"all")}</div></div>
      <div class="remaining ${rem<=0?"expired":""}">${rem<=0?"expired":esc(fmtDur(rem))}</div>
      <button class="btn btn-ghost btn-sm" data-cda="${esc(cd.account||"")}" data-cdp="${esc(cd.plugin||"")}" data-cdk="${esc(cd.cooldown_key||"")}">Clear</button>`;
    list.appendChild(d);
  }
  // clear requires account+plugin (cooldown_key optional) — bound after parsing
  list.querySelectorAll("[data-cda]").forEach(b=>b.onclick=()=>{
    const body = {account:b.dataset.cda, plugin:b.dataset.cdp};
    if (b.dataset.cdk) body.cooldown_key = b.dataset.cdk;
    post("/api/cooldowns/clear",body).then(loadCooldowns).catch(e=>toast(e.message));
  });
}

function logLine(entry) {
  const box = $("logbox"); if (!box) return;
  const filter = $("log-acct") ? $("log-acct").value : "";
  const sev = $("log-sev") ? $("log-sev").value : "";
  if (filter && entry.account !== filter) return;
  if (sev && entry.severity !== sev) return;
  const d = document.createElement("div"); d.className="log-line";
  const ts = entry.ts ? new Date(entry.ts*1000).toTimeString().slice(0,8) : "";
  d.innerHTML=`<span class="ts">${esc(ts)}</span><span class="sev ${esc(entry.severity||"INFO")}">${esc(entry.severity||"INFO")}</span><span class="msg">${esc(entry.source||"")}: ${esc(entry.message||"")}</span>`;
  box.appendChild(d); while (box.children.length>400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}
function auditLine(entry) {
  const box = $("auditbox"); if (!box) return;
  const filter = $("audit-acct") ? $("audit-acct").value : "";
  if (filter && entry.account !== filter) return;
  const d = document.createElement("div"); d.className=`audit-line ${entry.direction==="out"?"out":"in"}`;
  const ts = entry.ts ? new Date(entry.ts*1000).toTimeString().slice(0,8) : "";
  const arrow = entry.direction==="out" ? "→" : "←";
  d.innerHTML=`<span class="ts">${esc(ts)}</span><span>${arrow}</span><b>${esc(entry.account||"")}</b><span>${esc(entry.action||"")}</span><span class="dim">${esc((entry.content||"").slice(0,60))}</span>`;
  box.appendChild(d); while (box.children.length>300) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}
