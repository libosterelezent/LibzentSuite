"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   app.js — navigation, render dispatch, WebSocket client, event wiring and
   boot. Loaded LAST: everything it references is already defined.
   ═══════════════════════════════════════════════════════════════════════════ */

const NAV = [
  {id:"home", label:"Home", icon:"M3 12l9-9 9 9v9a2 2 0 0 1-2 2h-4v-7h-6v7H5a2 2 0 0 1-2-2z"},
  {id:"fleet", label:"Accounts & Groups", icon:"M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"},
  {id:"plugins", label:"Plugins", icon:"M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z"},
  {id:"profiles", label:"Profiles", icon:"M12 2L2 7l10 5 10-5-10-5zM2 12l10 5 10-5M2 17l10 5 10-5"},
  {id:"activity", label:"Activity", icon:"M22 12h-4l-3 9L9 3l-3 9H2"},
  {id:"policies", label:"Policies", icon:"M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"},
  {id:"admin", label:"Admin", icon:"M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z"},
  {id:"tools", label:"Tools", icon:"M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z"},
];
const TITLES = {home:"Home",fleet:"Accounts & Groups",plugins:"Plugins",profiles:"Profiles",activity:"Activity",policies:"Policies",admin:"Admin",tools:"Tools"};

function buildNav() {
  const nav = $("snav"); nav.innerHTML = "";
  for (const item of NAV) {
    const a = document.createElement("button");
    a.className = "nav-item" + (item.id===currentView?" active":"");
    a.dataset.view = item.id;
    a.innerHTML = `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="${item.icon}"/></svg><span>${esc(item.label)}</span><span class="badge hidden" id="nav-badge-${item.id}"></span>`;
    a.onclick = () => UI.go(item.id);
    nav.appendChild(a);
  }
  const bn = $("bottomnav"); bn.innerHTML = "";
  // Mobile keeps the existing five items — Profiles stays reachable via
  // the full sidebar / drawer (a sixth item squeezes the bar past 400px).
  const bnItems = NAV.filter(x=>["home","fleet","activity","policies","tools"].includes(x.id));
  for (const item of bnItems) {
    const a = document.createElement("button");
    a.className = "bnav-item" + (item.id===currentView?" active":"");
    a.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="${item.icon}"/></svg><span>${esc(item.label.split(" ")[0])}</span>`;
    a.onclick = () => UI.go(item.id);
    bn.appendChild(a);
  }
  // Dynamic plugin surfaces: buildNav() wipes #snav on EVERY navigation,
  // so the plugin panels/pages must be re-appended here or they would
  // vanish after the first click. Both builders clear their own
  // [data-*] remnants first — idempotent, safe before their data loads.
  if (typeof buildPluginNav === "function") buildPluginNav();
  if (typeof buildDashboardUINav === "function") buildDashboardUINav();
}

const LOADERS = {};
const UI = {
  go(view) {
    currentView = view;
    // Popups do not survive navigation: close and clear the shared details
    // popup so no stale content/DOM (or its closures) outlives the page.
    const dt = $("dlg-details");
    if (dt && dt.open) dt.close();
    if (dt) dt.querySelector(".dlg-body").innerHTML = "";
    showPanel("panel-"+view);
    $("page-title").textContent = TITLES[view]||view;
    buildNav(); $("sidebar").classList.remove("open");
    if (LOADERS[view]) { LOADERS[view](); delete LOADERS[view]; }
    render();
  },
  tab(name) {
    $$("#panel-fleet .tabbar .tab").forEach(t=>t.classList.toggle("active",t.dataset.tab===name));
    currentFleetTab = name;
    $("tab-accounts").classList.toggle("hidden",name!=="accounts");
    $("tab-groups").classList.toggle("hidden",name!=="groups");
  },
  toolsTab(name) {
    if (!$("ttab-sim")) return;   // tools is a framework page — no legacy tabs
    $$("#panel-tools .tabbar .tab").forEach(t=>t.classList.toggle("active",t.dataset.ttab===name));
    currentToolsTab = name;
    $("ttab-sim").classList.toggle("hidden",name!=="sim");
    $("ttab-adv").classList.toggle("hidden",name!=="adv");
  },
  activityTab(name) {
    if (!$("atab-tasks")) return;   // activity is a framework page
    $$("#panel-activity .tabbar .tab").forEach(t=>t.classList.toggle("active",t.dataset.atab===name));
    currentActivityTab = name;
    ["tasks","cooldowns","logs","audit"].forEach(n=>$("atab-"+n).classList.toggle("hidden",n!==currentActivityTab));
    if (currentActivityTab==="cooldowns") loadCooldowns();
    if (currentActivityTab==="tasks") loadTasks();
  }
};

/* Panel activation is centralized: a render ALWAYS shows the current
   view's panel. This is what moves the dashboard off onboarding when the
   first WS stats arrive AFTER boot (renderHome used to fill hidden
   metrics while the onboarding screen stayed up forever). */
function showPanel(id) {
  const target = $(id) || $("panel-home");
  if (!target.classList.contains("active")) {
    $$(".panel").forEach(p => p.classList.remove("active"));
    target.classList.add("active");
  }
}

function render() {
  // Zero accounts → the onboarding WELCOME shows on Home only. Every other
  // view stays reachable — the user must be able to navigate to
  // Accounts to add the first one (Get Started used to snap straight
  // back to onboarding because this guard covered all views).
  const hasAccts = accounts().length > 0;
  if (!hasAccts && currentView==="home") { showPanel("panel-onboard"); return; }
  // Plugin dashboard-UI pages ("dui-*" views) manage their OWN panel
  // activation + content — showPanel()'s fallback to panel-home was
  // silently bouncing idle users off plugin pages every ~10s WS tick
  // when the panel was briefly absent during a buildNav rebuild.
  if (!currentView.startsWith("dui-")) showPanel("panel-"+currentView);
  // The main tabs that migrated to the builder API render through the
  // generic framework pages; the rest keep their hand-coded renderers.
  if (isFrameworkView(currentView)) {
    renderFrameworkTab(currentView);
  } else {
    if (currentView==="home") renderHome();
    if (currentView==="fleet") { renderAccounts(); renderGroups(); }
    if (currentView==="plugins") renderPlugins();
    if (currentView==="profiles") renderProfiles();
    if (currentView==="policies") renderPolicies();
    if (currentView==="admin") renderAdmin();
  }
  updateNavBadges();
}

function updateNavBadges() {
  const attn = getAttention();
  const badge = $("nav-badge-home");
  if (badge) { badge.textContent = attn.length; badge.classList.toggle("hidden",!attn.length); }
}

/* Lightweight per-second updates (no DOM rebuild) — metric numbers only.
   The structural render above handles layout. */
function renderTick() {
  if (currentView==="home") {
    const t = (lastStats&&lastStats.tasks)||{};
    const running = accounts().filter(a=>a.status==="running").length;
    const total = accounts().length;
    const m = $("home-metrics");
    if (m && m.querySelector(".metric .value")) {
      m.querySelector(".metric .value").textContent = `${running}/${total}`;
      const vals = m.querySelectorAll(".metric .value");
      if (vals[2]) vals[2].textContent = t.running??0;
      if (vals[3]) vals[3].textContent = (lastStats&&lastStats.ratelimit&&lastStats.ratelimit.cooldowns&&lastStats.ratelimit.cooldowns.accounts)?Object.keys(lastStats.ratelimit.cooldowns.accounts).length:0;
    }
  }
}

/* ── WebSocket ─────────────────────────────────────────────────────────────── */
let wsRetryTimer = null;
function connectWS() {
  // Browsers cannot set headers on a WebSocket upgrade, so the token MUST
  // ride the URL as ?token= — the same auth the server accepts for REST.
  const proto = location.protocol==="https:"?"wss":"ws";
  const url = `${proto}://${location.host}/ws` + (token() ? "?token=" + encodeURIComponent(token()) : "");
  // Cancel any pending backoff retry and retire the old socket SILENTLY
  // (onclose nulled first, else closing it here schedules another retry).
  if (wsRetryTimer) { clearTimeout(wsRetryTimer); wsRetryTimer = null; }
  if (ws) { ws.onclose = null; try { ws.close(); } catch {} ws = null; }
  try { ws = new WebSocket(url); } catch { return handleWsClosed(); }
  wsWatchdog = setTimeout(()=>{ if(ws.readyState===WebSocket.CONNECTING) ws.close(); }, 5000);
  ws.onopen = () => {
    clearTimeout(wsWatchdog); wsRetries = 0;
    $("wsdot").classList.add("on"); $("connmsg").textContent = "live";
  };
  ws.onmessage = ev => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    try {
      if (msg.type === "stats") {
        const sig = JSON.stringify((msg.accounts||[]).map(a=>[
          a.name,a.status,a.enabled,a.credential,a.sync_group,
          (a.assigned_plugins||[]).join(","),
          JSON.stringify(a.plugin_profiles||{}),
          a.latency_ms!=null?Math.round(a.latency_ms/10):null,
          Math.floor((a.uptime_s??0)/60), a.cooldowns??0]));
        lastStats = msg;
        // Only full-render when the STRUCTURAL signature changed (status
        // flip, account add/remove, credential change, plugin assignment,
        // PROFILE BINDING change, minute-level uptime, latency bucket,
        // cooldown count). Per-second cosmetic ticks don't rebuild the
        // DOM — the v1 pattern, preserved.
        if (sig !== lastSig) { lastSig = sig; render(); }
        else { renderTick(); refreshFrameworkData(); }
      }
      else if (msg.type==="log") logLine(msg);
      else if (msg.type==="audit") auditLine(msg);
      else if (msg.type==="task") scheduleTaskRefresh();
      else if (msg.type==="error") showErr(msg.error);
    } catch(e) { showErr("render error: "+(e.message||e)); }
  };
  ws.onclose = () => handleWsClosed();
}
function handleWsClosed() {
  clearTimeout(wsWatchdog);
  $("wsdot").classList.remove("on"); $("connmsg").textContent = "disconnected";
  if (wsRetries < 10) { wsRetries++; $("connmsg").textContent=`reconnecting (${wsRetries}/10)…`;
    wsRetryTimer = setTimeout(()=>{ wsRetryTimer=null; connectWS(); }, Math.min(1000*Math.pow(2,wsRetries),30000)); }
  else $("connmsg").textContent = "connection lost — set the token in Tools → Advanced";
}
let taskRefreshTimer = null;
function scheduleTaskRefresh() {
  // framework activity refreshes its own tables on the stats tick
  if (taskRefreshTimer || currentView!=="activity" || isFrameworkView("activity")) return;
  taskRefreshTimer = setTimeout(()=>{ taskRefreshTimer=null; if(currentView==="activity") loadTasks(); }, 1000);
}

/* ── Event wiring (runs exactly ONCE — no listener may be registered in a
      render path; the v1 per-render document listener leak froze pages) ───── */
function wire() {
  // Token (Tools → Advanced — kept out of the header on purpose)
  $("adv-token").value = token();
  $("adv-token-save").onclick = () => {
    const field = $("adv-token");
    const card = $("token-card");
    secretStore.setItem("dash_token", field.value.trim());
    wsRetries = 0; connectWS(); render();
    flashFb(card, "token saved — reconnecting…");
  };
  $("hdr-gear").onclick = () => { UI.go("tools"); UI.toolsTab("adv"); };

  // Fleet tabs + view selectors
  $$("#panel-fleet .tabbar .tab").forEach(t=>t.onclick=()=>UI.tab(t.dataset.tab));
  bindViewSel("vm-acct", setAcctViewMode, ()=>{ renderAccounts(); });
  bindViewSel("vm-group", setGroupViewMode, ()=>{ renderGroups(); });

  // Profiles tab: account selector + library/binding refresh
  $("pp-acct").onchange = () => { ppAccount = $("pp-acct").value; loadAccountBindings(); };
  $("pp-refresh").onclick = async () => {
    await loadPluginProfiles(false);
    if (ppAccount) await loadAccountBindings(); else render();
  };

  // Activity + Tools tabs
  $$("#panel-activity .tabbar .tab").forEach(t=>t.onclick=()=>UI.activityTab(t.dataset.atab));
  $$("#panel-tools .tabbar .tab").forEach(t=>t.onclick=()=>UI.toolsTab(t.dataset.ttab));

  // Add account (plugins = checkbox list built from the discovered
  // manifests — repopulated on every open, the list may have loaded late)
  $("btn-add-acct").onclick = () => { $("aa-name").value="";$("aa-group").value="";$("aa-cred").value="";
    fillPluginChecks($("aa-plugins"), null);
    const dlg=$("dlg-add-acct"); clearFb(dlg.querySelector(".form")); dlg.showModal(); $("aa-name").focus(); };
  $("aa-submit").onclick = async () => {
    const name = $("aa-name").value.trim();
    const form = $("dlg-add-acct").querySelector(".form");
    clearFb(form);
    if (!name) { setFb($("aa-name").closest(".field"),"error","Account name is required."); return; }
    const body = { name };
    const g = $("aa-group").value.trim(); if (g) body.sync_group = g;
    // empty selection = zero plugins (a valid desired state)
    body.plugins = [...$("aa-plugins").querySelectorAll("input:checked")].map(i=>i.value);
    const c = $("aa-cred").value; if (c) body.credential = c;
    try { await withBusy($("aa-submit"), post("/api/accounts/add", body)); $("dlg-add-acct").close(); await refreshAccountsNow(); render(); }
    catch(e) { setFb(form,"error",e.message); }
  };

  // Create group (real backend: POST /api/groups/create)
  $("btn-create-group").onclick = () => {
    $("cg-name").value=""; $("cg-desc").value=""; $("cg-enabled").checked=true;
    const dlg=$("dlg-create-group"); clearFb(dlg.querySelector(".form")); dlg.showModal(); $("cg-name").focus();
  };
  $("cg-submit").onclick = async () => {
    const name = $("cg-name").value.trim();
    const form = $("dlg-create-group").querySelector(".form");
    clearFb(form);
    if (!name) { setFb($("cg-name").closest(".field"),"error","Group name is required."); return; }
    try {
      await withBusy($("cg-submit"), post("/api/groups/create",{name, description:$("cg-desc").value, enabled:$("cg-enabled").checked}));
      $("dlg-create-group").close(); await afterGroupMutation();
    } catch(e) { setFb(form,"error",e.message); }
  };

  // Credential dialog
  $("cred-save").onclick = async () => {
    const name = $("cred-name").value, val = $("cred-val").value;
    if (!name||!val) return;
    try { await withBusy($("cred-save"), post("/api/accounts/credential",{name,credential:val})); $("dlg-cred").close(); await refreshAccountsNow(); render(); }
    catch(e) { setFb($("dlg-cred").querySelector(".dlg-body"),"error",e.message); }
  };

  // Bulk actions — partial failures report next to the bar, never as
  // blanket success (per-action allSettled accounting).
  $("bulk-clear").onclick = () => { selectedAccounts.clear(); renderAccounts(); };
  $("bulk-start").onclick = () => bulkLifecycle("/api/accounts/start");
  $("bulk-stop").onclick = () => bulkLifecycle("/api/accounts/stop");
  $("bulk-group").onclick = () => {
    if (!selectedAccounts.size) return;
    const names = [...selectedAccounts];
    openGroupPicker(`Move ${names.length} account${names.length!==1?"s":""} to group`, null, async group => {
      const rs = await Promise.allSettled(names.map(n=>
        group===null
          ? post("/api/groups/clear-account",{account:n})
          : post("/api/groups/set-account",{account:n, group})));
      const failed = rs.filter(r=>r.status==="rejected");
      if (failed.length) setFb($("bulkbar"),"error",`${rs.length-failed.length}/${rs.length} moved — ${failed.map(r=>r.reason&&r.reason.message||String(r.reason)).join("; ")}`);
      else flashFb($("bulkbar"), `${rs.length} account${rs.length!==1?"s":""} moved`);
      selectedAccounts.clear();
      updateBulkBar();
      await afterGroupMutation();
    });
  };

  // Task filters + streams
  $("task-filter-status").onchange = renderTaskTable;
  $("task-filter-account").onchange = renderTaskTable;
  $("task-refresh").onclick = loadTasks;
  $("log-backfill").onclick = async () => {
    try { const entries = await api("/api/logs?n=200"+($("log-acct").value?"&account="+encodeURIComponent($("log-acct").value):""))||[];
      for (const e of entries) logLine(e); } catch(e) { showErr(e.message); }
  };
  $("log-clear").onclick = () => { $("logbox").innerHTML=""; };
  $("log-acct").onchange = () => { $("logbox").innerHTML=""; };
  $("audit-backfill").onclick = async () => {
    try { const entries = await api("/api/audit?n=200"+($("audit-acct").value?"&account="+encodeURIComponent($("audit-acct").value):""))||[];
      for (const e of entries) auditLine(e); } catch(e) { showErr(e.message); }
  };

  // Stored policies (real /api/ratelimit/* endpoints)
  $("rl-add").onclick = () => {
    $("rp-title").textContent = "Add Policy";
    $("rp-action").value="message"; $("rp-interval").value=""; $("rp-jmin").value=""; $("rp-jmax").value="";
    $("rp-level").value="global"; $("rp-owner").value="";
    const dlg=$("dlg-policy"); clearFb(dlg.querySelector(".form")); dlg.showModal(); $("rp-interval").focus();
  };
  $("rl-refresh").onclick = loadPolicies;
  $("rp-submit").onclick = async () => {
    const form = $("dlg-policy").querySelector(".form");
    clearFb(form);
    const interval = parseFloat($("rp-interval").value);
    if (!Number.isFinite(interval) || interval<=0) { setFb($("rp-interval").closest(".field"),"error","Interval must be a positive number of seconds."); return; }
    const body = { action:$("rp-action").value, interval,
      jitter_min: parseFloat($("rp-jmin").value)||0, jitter_max: parseFloat($("rp-jmax").value)||0,
      level:$("rp-level").value };
    const owner = $("rp-owner").value.trim(); if (owner) body.owner = owner;
    try { await withBusy($("rp-submit"), post("/api/ratelimit/set", body)); $("dlg-policy").close(); await loadPolicies(); }
    catch(e) { setFb(form,"error",e.message); }
  };

  // Advanced
  $("adv-estop").onclick = () => confirmAction("GLOBAL EMERGENCY STOP","This affects ALL running accounts and tasks.",
    ()=>post("/api/estop").catch(e=>setFb($("adv-estop").closest(".card"),"error",e.message)));
  $("adv-resume").onclick = () => withBusy($("adv-resume"), post("/api/resume"))
    .then(()=>flashFb($("adv-resume").closest(".card"),"resume requested"))
    .catch(e=>setFb($("adv-resume").closest(".card"),"error",e.message));
  $("adv-import").onclick = () => withBusy($("adv-import"), post("/api/credentials/import-env"))
    .then(()=>flashFb($("adv-import").closest(".card"),"import requested"))
    .catch(e=>setFb($("adv-import").closest(".card"),"error",e.message));

  // Simulation
  $("sim-run").onclick = async () => {
    const name = $("sim-sel").value; if (!name) return;
    try { const report = await withBusy($("sim-run"), post("/api/simulation/run",{name}));
      $("sim-report").innerHTML = `<pre style="background:var(--bg);padding:var(--sp-3);border-radius:6px;font-family:var(--mono);font-size:11px;overflow-x:auto;max-height:300px">${esc(report&&report.log||JSON.stringify(report,null,2))}</pre>`;
    } catch(e) { setFb($("sim-report"),"error",e.message); }
  };

  // Onboarding CTA — the zero-account welcome's "Get Started" jump into
  // the Accounts tab (must actually navigate: render() only pins the
  // onboarding panel to Home).
  $("onboard-cta").onclick = () => { UI.go("fleet"); UI.tab("accounts"); };

  // Mobile menu
  $("menu-btn").onclick = () => $("sidebar").classList.toggle("open");

  // Popup lifecycle: every close ✕ / Cancel is delegated through ONE
  // listener; the details popup's body is cleared on close so no stale
  // popup DOM (or its closures) survives navigation.
  document.querySelectorAll("dialog").forEach(d => d.addEventListener("close", () => {
    if (d.id === "dlg-details") d.querySelector(".dlg-body").innerHTML = "";
    const f = d.querySelector("[data-fb]"); if (f) f.remove();
  }));

  // ONE global click handler: closes overflow menus, applies [data-dlg-close]
  // buttons and dismisses search results when clicking elsewhere. (A
  // per-card listener per render accumulated into the listener-leak freeze.)
  document.addEventListener("click", e => {
    const closer = e.target.closest("[data-dlg-close]");
    if (closer) { const d = closer.closest("dialog"); if (d) d.close(); }
    $$(".menu.open").forEach(m => { if (!m.contains(e.target)) m.classList.remove("open"); });
    if (!$("search-wrap").contains(e.target)) $("search-results").classList.remove("open");
  });
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey||e.metaKey) && e.key==="k") { $("search").focus(); e.preventDefault(); }
    if (e.key==="Escape") { $$(".menu.open").forEach(m=>m.classList.remove("open")); $("sidebar").classList.remove("open"); }
  });

  // Global search (accounts, groups, plugins — all esc()'d)
  $("search").oninput = () => {
    const q = $("search").value.toLowerCase().trim();
    const results = $("search-results");
    if (!q) { results.classList.remove("open"); return; }
    const items = [];
    for (const a of accounts()) if (a.name.toLowerCase().includes(q)) items.push({label:a.name,type:"Account",action:()=>openAcctDetails(a.name)});
    for (const g of groupsList) if (g.name.toLowerCase().includes(q)) items.push({label:g.name,type:"Group",action:()=>openGroupDetails(g.name)});
    for (const m of manifests) if (m.name.toLowerCase().includes(q)) items.push({label:m.name,type:"Plugin",action:()=>UI.go("plugins")});
    results.innerHTML = items.slice(0,8).map((item,i)=>`<div class="result" data-i="${i}">${esc(item.label)}<span class="type">${esc(item.type)}</span></div>`).join("")||"<div class='result dim'>No results</div>";
    results.classList.add("open");
    results.querySelectorAll(".result[data-i]").forEach(el=>el.onclick=()=>{results.classList.remove("open");$("search").value="";items[+el.dataset.i].action();});
  };
}

function bulkLifecycle(path) {
  const names = [...selectedAccounts];
  if (!names.length) return;
  Promise.allSettled(names.map(n=>post(path,{account:n}))).then(rs=>{
    const failed = rs.filter(r=>r.status==="rejected");
    if (failed.length) setFb($("bulkbar"),"error",`${rs.length-failed.length}/${rs.length} succeeded — ${failed.map(r=>r.reason&&r.reason.message||String(r.reason)).join("; ")}`);
    else flashFb($("bulkbar"), `${rs.length} request${rs.length!==1?"s":""} sent`);
  });
}

/* ── Init ──────────────────────────────────────────────────────────────────── */
try {
  const qtoken = new URLSearchParams(location.search).get("token");
  if (qtoken) { secretStore.setItem("dash_token",qtoken.trim()); $("adv-token").value=qtoken.trim(); history.replaceState(null,"",location.pathname); }

  applyDashTheme();   // decorative layer: persisted accent + density
  buildNav(); wire(); connectWS();
  // Activate the initial view — .panel{display:none} means NOTHING is
  // visible until UI.go() adds .active; without this the first paint is
  // a blank main area (nav clicks would also fix it, but first load
  // must not depend on one).
  UI.go("home");
  // Every loader catches its own failures (toast + per-page fallback
  // state), so one dead endpoint can never block the others or the render.
  Promise.all([loadManifests(),loadGroups(),loadScenarios(),loadPluginUI()])
    .then(()=>render())
    .catch(e=>{ showErr("startup: "+(e&&e.message||e)); render(); });
  LOADERS.fleet = ()=>{ loadGroups(); };
  // home/plugins/activity/policies/admin/tools are framework pages now —
  // their data comes from server-side providers, no legacy loader needed
  // the (still legacy) Profiles tab reads the per-plugin profile library
  LOADERS.profiles = ()=>{ loadPluginProfiles(); };
  $("side-foot").textContent = "v2 · " + new Date().getFullYear();
} catch (e) {
  // Report loudly instead of leaving a blank page with no explanation.
  toast("dashboard init failed: " + (e && (e.message || e) || e), "error");
  throw e;
}
