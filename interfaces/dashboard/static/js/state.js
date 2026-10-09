"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   state.js — storage wrappers, application state, DOM/format helpers.
   Loaded first: every other module reads/writes these globals.
   ═══════════════════════════════════════════════════════════════════════════ */

/* Storage split by sensitivity:
    - store       (localStorage): non-sensitive UI preferences only (view
      mode). Survives browser restarts, contains nothing worth protecting.
    - secretStore (sessionStorage): the dashboard token ONLY. Cleared when
      the tab closes, so a shared machine never retains credentials; the
      documented reason sessionStorage is required at all is keeping the
      session alive across same-tab reloads (?token= bootstrap strips the
      URL immediately, so the in-tab copy is the only one). */
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

/* ── Application state ────────────────────────────────────────────────────── */
let lastStats = null, lastSig = "";
let manifests = [], groupsList = [], taskList = [], taskHistory = [], cooldownList = [];
let manifestsFailed = false, groupsFailed = false;
let ws = null, wsRetries = 0, wsWatchdog = null;
let selectedAccounts = new Set();
let currentView = "home", currentFleetTab = "accounts", currentActivityTab = "tasks", currentToolsTab = "sim";
/* view mode per page (tiles | list) — a UI preference, persisted in store */
let acctViewMode = store.getItem("pref_acct_view") === "list" ? "list" : "tiles";
let groupViewMode = store.getItem("pref_group_view") === "list" ? "list" : "tiles";
function setAcctViewMode(m) { acctViewMode = m; store.setItem("pref_acct_view", m); }
function setGroupViewMode(m) { groupViewMode = m; store.setItem("pref_group_view", m); }

/* ── DOM + format helpers ─────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);

/* esc(): HTML-escape ANY dynamic value before it enters markup — text,
   attribute values, URLs rendered into tags. All interpolated data goes
   through this (audit rule: no raw ${value} from API data in templates). */
function esc(s){const d=document.createElement("span");d.textContent=String(s??"");return d.innerHTML.replace(/"/g,"&quot;").replace(/'/g,"&#39;");}

/* btn()/btnG() return DOM ELEMENTS — only for append() wiring, NEVER for
   interpolation into innerHTML template literals (they stringify to
   [object HTMLButtonElement]). Static tests enforce this. */
function btn(l,fn,cls){const b=document.createElement("button");b.textContent=l;b.onclick=fn;if(cls)b.className=cls;return b;}
function btnG(l,fn,cls){const b=btn(l,null,cls);b.onclick=()=>{if(b.disabled)return;b.disabled=true;Promise.resolve(fn()).finally(()=>{b.disabled=false;});};return b;}

function fmtDur(s){s=Math.max(0,Math.round(+s||0));if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"m "+(s%60)+"s";return Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m";}
function fmtLatency(ms){if(ms==null)return"-";const n=+ms;return Number.isFinite(n)?Math.round(n)+"ms":"-";}
function fmtAge(ts){return ts?fmtDur((Date.now()-ts*1000)/1000):"-";}
function credLbl(s){return s==="db"?"Configured":"Missing";}
