"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   store.js — storage wrappers, application state, pub/sub.
   Loaded FIRST: every other module reads/writes these globals.
   ═══════════════════════════════════════════════════════════════════════════ */

/* Storage split by sensitivity:
    - store       (localStorage): non-sensitive UI preferences only. Survives
      browser restarts, contains nothing worth protecting.
    - secretStore (sessionStorage): the dashboard token ONLY. Cleared when
      the tab closes, so a shared machine never retains credentials. */
const _fallback = () => { const m = {};
  return { getItem:k=>k in m?m[k]:null, setItem:(k,v)=>{m[k]=String(v)}, removeItem:k=>{delete m[k]} }; };
const store = (() => {
  try { const t = localStorage; t.setItem("__p","1"); t.removeItem("__p"); return t; }
  catch { return _fallback(); }
})();
const secretStore = (() => {
  try { const t = sessionStorage; t.setItem("__p","1"); t.removeItem("__p"); return t; }
  catch { return _fallback(); }
})();
const token = () => secretStore.getItem("dash_token") || "";

/* One-time migration: pre-split SPA versions kept the token in
   localStorage; adopt it into the session store and scrub the old copy. */
(() => {
  if (!secretStore.getItem("dash_token") && store.getItem("dash_token")) {
    secretStore.setItem("dash_token", store.getItem("dash_token"));
  }
  try { store.removeItem("dash_token"); } catch {}
})();

/* ?token= bootstrap: adopt the URL token into the session store, then strip
   it from the address bar (history.replaceState — no reload, no back-trap).
   Returns true when a token was present. */
function adoptTokenFromUrl() {
  const q = new URLSearchParams(location.search);
  const t = q.get("token");
  if (!t) return token() !== "";
  secretStore.setItem("dash_token", t);
  q.delete("token");
  const rest = q.toString();
  history.replaceState({}, "", location.pathname + (rest ? "?" + rest : "") + location.hash);
  return true;
}

/* ── Reactive state ──────────────────────────────────────────────────────────
   Single app-state object + tiny pub/sub. store.set publishes; views
   subscribe and re-render what they own. Never mutate nested state in
   place — always store.set(key, newValue) so subscribers hear it. */
/* (PLAIN object, deliberately NOT Object.freeze: freezing made every
   storeSet throw "Cannot assign to read only property", killing all
   live updates. The unknown-key guard in storeSet is the typo trap.) */
const _state = {
  stats: null,          // latest /api/status payload (same shape as WS stats frames)
  accounts: [],         // convenience mirror of stats.accounts
  groups: [],
  manifests: [],        // plugin manifests (/api/plugins)
  navigation: [],       // builder UI pages (/api/dashboard-ui/navigation)
  tasks: [],
  taskHistory: [],
  cooldowns: [],
  logs: [],             // ring buffer, newest last
  audit: [],            // ring buffer, newest last
  tasksTouch: 0,        // bump key: a WS task frame arrived
  fleet: null,          // latest WS fleet transition frame (PHASE 9 §3;
                        // views refetch their fleet snapshot on change)
};
const _subs = [];
const storeGet = key => _state[key];
const storeSet = (key, value) => {
  if (!(key in _state)) throw new Error("unknown store key: " + key);
  if (_state[key] === value) return;
  _state[key] = value;
  for (const fn of _subs) { try { fn(key, value); } catch (e) { console.error(e); } }
};
const storeSubscribe = fn => { _subs.push(fn); return () => {
  const i = _subs.indexOf(fn); if (i >= 0) _subs.splice(i, 1); }; };

/* ring-buffer append for the log/audit feeds (bounded memory) */
const LOG_CAP = 500;
function storeAppend(key, entry) {
  const next = _state[key].concat(entry);
  if (next.length > LOG_CAP) next.splice(0, next.length - LOG_CAP);
  storeSet(key, next);
}

/* ── view-mode preferences (persisted, non-sensitive) ─────────────────────── */
function prefGet(key, dflt) { try { return store.getItem("pref_" + key) || dflt; } catch { return dflt; } }
function prefSet(key, val) { try { store.setItem("pref_" + key, val); } catch {} }
