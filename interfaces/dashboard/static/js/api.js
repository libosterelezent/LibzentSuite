"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   api.js — REST client and every data loader. Mutations live next to the
   components that own them; this file only reads (plus the one shared
   post-mutation refresh used by the group flows).
   The REST/WS contract: persisted identity (groups, plugins) comes from
   REST here; live runtime state (accounts/tasks/cooldowns) streams over
   the WebSocket. Capabilities are direct account→plugin assignments
   (see accounts.js openPluginPicker). See app.js connectWS().
   ═══════════════════════════════════════════════════════════════════════════ */

function api(path, opts={}) {
  const headers = {"Content-Type":"application/json", ...opts.headers};
  if (token()) headers["X-Dashboard-Token"] = token();
  // A hung request must never permanently disable its caller (withBusy
  // leaves action buttons spinning forever on a stalled fetch) — abort
  // after 15s and surface a clear message.
  const ctrl = new AbortController();
  const timer = setTimeout(()=>ctrl.abort(), 15000);
  return fetch(path, {...opts, signal: ctrl.signal, headers}).then(async r => {
    if (r.status===401) throw new Error("401 — set the dashboard token (Tools → Advanced)");
    let body; try { body = await r.json(); } catch { throw new Error(`HTTP ${r.status}`); }
    if (!body.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body.data;
  }).catch(e => {
    if (e && e.name === "AbortError") throw new Error("request timed out");
    throw e;
  }).finally(() => clearTimeout(timer));
}
function post(path, body={}) { return api(path, {method:"POST", body:JSON.stringify(body)}); }

/* ── Loaders (each swallows its own failure — one dead endpoint must never
      block the Promise.all startup batch) ─────────────────────────────────── */
async function loadManifests() { try { manifests = await api("/api/plugins")||[]; manifestsFailed = false; } catch(e) { manifestsFailed = true; showErr(e.message); } }

async function loadGroups(thenRender=true) {
  try { groupsList = await api("/api/groups")||[]; groupsFailed = false; }
  catch(e) { groupsFailed = true; showErr(e.message); }
  if (thenRender) render();
}

async function loadScenarios() { try { const list = await api("/api/simulation/list")||[];
  $("sim-sel").innerHTML = list.length
    ? list.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join("")
    : '<option value="">(no scenarios available)</option>';
  } catch(e) { $("sim-sel").innerHTML = '<option value="">(simulation unavailable)</option>'; showErr(e.message); } }

async function loadTasks() {
  try {
    taskList = await api("/api/tasks") || [];
    taskHistory = await api("/api/tasks/history?limit=50") || [];
    renderTaskTable();
  } catch(e) { showErr(e.message); }
}

async function loadCooldowns() {
  try {
    const cds = await api("/api/cooldowns") || [];
    cooldownList = cds;
    renderCooldowns(cds);
  } catch(e) { showErr(e.message); }
}

/* ── REST/WS synchronization (group flows) ────────────────────────────────── */

async function refreshAccountsNow() {
  // Group mutations update the server's in-memory account records; pull
  // them now instead of waiting for the next WS structural change.
  try {
    const fresh = await api("/api/accounts")||[];
    if (lastStats) { lastStats.accounts = fresh; lastSig = ""; }
  } catch { /* WS broadcast catches up */ }
}

// One coherent post-mutation update: fetch groups AND accounts, then a
// single render. loadGroups' own render is suppressed here — rendering
// between the two fetches showed fresh groups with stale account chips
// (double DOM rebuild + one frame of stale intermediate state).
async function afterGroupMutation() {
  await Promise.all([loadGroups(false), refreshAccountsNow()]);
  render();
}
