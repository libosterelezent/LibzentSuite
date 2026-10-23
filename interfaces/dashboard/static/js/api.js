"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   api.js — REST client + WebSocket client. No rendering, no DOM: this file
   only moves data. Callers own error surfaces (toast/retry).

   REST/WS contract: persisted identity (groups, plugins) comes from REST;
   live runtime state (accounts/tasks/cooldowns) streams over the WebSocket.
   ═══════════════════════════════════════════════════════════════════════════ */

/* One id per page load (= per browser tab). The server keys per-view UI
   state on it so two open tabs can't cross-wire each other's modal
   actions. Not an identity. */
const VIEW_ID = (() => {
  try {
    const a = new Uint8Array(12); crypto.getRandomValues(a);
    return Array.from(a, b => b.toString(16).padStart(2, "0")).join("");
  } catch { return "v" + Math.random().toString(36).slice(2) + Date.now().toString(36); }
})();

function api(path, opts = {}) {
  const headers = {"Content-Type": "application/json", "X-Dashboard-View": VIEW_ID, ...opts.headers};
  if (token()) headers["X-Dashboard-Token"] = token();
  /* A hung request must never permanently disable its caller — abort and
     surface a clear message. Default 15s; simulation runs get the server's
     own 200s budget. */
  const limit = opts.timeoutMs || (path === "/api/simulation/run" ? 210000 : 15000);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), limit);
  return fetch(path, {...opts, signal: ctrl.signal, headers}).then(async r => {
    if (r.status === 401) throw new Error("401 — set the dashboard token (Tools → Advanced)");
    let body; try { body = await r.json(); } catch { throw new Error(`HTTP ${r.status}`); }
    if (!body.ok) throw new Error(body.error || `HTTP ${r.status}`);
    return body.data;
  }).catch(e => {
    if (e && e.name === "AbortError") throw new Error("request timed out");
    throw e;
  }).finally(() => clearTimeout(timer));
}
const post = (path, body = {}) => api(path, {method: "POST", body: JSON.stringify(body)});

/* ═══ WebSocket ═════════════════════════════════════════════════════════════
   Token rides the URL (?token=) — browsers cannot set headers on a WS
   upgrade. Recovery: exponential backoff, 10 fast retries then a 60s slow
   loop (never gives up for good); a 5s watchdog retires CONNECTING corpses;
   a half-open detector pings when frames go quiet. */
const WS = (() => {
  let sock = null, retries = 0, retryTimer = null, watchdog = null;
  let status = "connecting";          // connecting | online | offline
  const statusSubs = [], subs = {};   // subs: type → [fn]
  const setStatus = s => { status = s; for (const fn of statusSubs) { try { fn(s); } catch {} } };

  function emit(type, msg) {
    for (const fn of subs[type] || []) { try { fn(msg); } catch (e) { console.error(e); } }
  }
  function closed() {
    clearTimeout(watchdog);
    if (sock) { sock = null; }
    setStatus("offline");
    if (!token()) { setStatus("offline"); return; }   // retrying can never succeed
    if (retries < 10) {
      retries++;
      retryTimer = setTimeout(() => { retryTimer = null; connect(); },
                              Math.min(1000 * Math.pow(2, retries), 30000));
    } else {
      retryTimer = setTimeout(() => { retryTimer = null; connect(); }, 60000);
    }
  }
  function connect() {
    clearTimeout(watchdog);
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    if (sock) { sock.onclose = null; try { sock.close(); } catch {} sock = null; }
    setStatus("connecting");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws` + (token() ? "?token=" + encodeURIComponent(token()) : "");
    try { sock = new WebSocket(url); } catch { return closed(); }
    const me = sock;
    watchdog = setTimeout(() => { if (me.readyState === WebSocket.CONNECTING) me.close(); }, 5000);
    sock.onopen = () => {
      if (sock !== me) return;
      clearTimeout(watchdog); retries = 0; setStatus("online");
    };
    sock.onerror = () => {};          // recovery is onclose's job
    sock.onmessage = ev => {
      if (sock !== me) return;
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (!msg || !msg.type) return;
      /* Route every frame into the store first (views subscribe), then
         notify type listeners. */
      if (msg.type === "stats") {
        storeSet("stats", msg);
        storeSet("accounts", msg.accounts || []);
      } else if (msg.type === "fleet") {
        /* PHASE 9 §3: a fleet transition landed (persisted first
           server-side) — views refetch their fleet snapshot */
        storeSet("fleet", msg);
      }
      emit(msg.type, msg);
    };
    sock.onclose = () => { if (sock === me) closed(); };
  }

  return {
    connect,
    on(type, fn) { (subs[type] = subs[type] || []).push(fn); return () => {
      const a = subs[type] || [], i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); }; },
    onStatus(fn) { statusSubs.push(fn); return () => {
      const i = statusSubs.indexOf(fn); if (i >= 0) statusSubs.splice(i, 1); }; },
    status: () => status,
  };
})();

/* ── shared loaders (each swallows its own failure — one dead endpoint must
      never block the startup batch) ───────────────────────────────────────── */
async function loadGroups() {
  try { storeSet("groups", await api("/api/groups") || []); }
  catch (e) { if (typeof toast === "function") toast(e.message, "err"); }
}
async function loadManifests() {
  try { storeSet("manifests", await api("/api/plugins") || []); }
  catch (e) { if (typeof toast === "function") toast(e.message, "err"); }
}
async function loadNavigation() {
  try { storeSet("navigation", await api("/api/dashboard-ui/navigation") || []); }
  catch (e) { /* non-fatal: plugin pages just don't list */ }
}
async function loadStatsNow() {
  try { const s = await api("/api/status");
    storeSet("stats", s); storeSet("accounts", s.accounts || []); }
  catch (e) { /* WS broadcast catches up */ }
}
