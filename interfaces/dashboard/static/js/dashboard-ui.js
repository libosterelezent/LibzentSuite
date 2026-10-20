"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   dashboard-ui.js — the GENERIC component renderer for builder-based plugin
   UIs (Stage D). A plugin declares a Layout through the DashboardUIToolset;
   the server serializes it to a JSON tree; this module renders ANY component
   type from that tree — it never knows about a specific plugin.

   Contract:
     * navigation  — GET /api/dashboard-ui/navigation builds sidebar entries
                     (one per plugin page); clicking fetches the page tree;
     * renderer    — renderDashboardUIComponent(comp, pluginName) recurses
                     through every container and leaf type;
     * actions     — buttons/inputs dispatch POST /api/dashboard-ui/action
                     and render message/field_errors/refresh inline;
     * data        — list-views/tables fetch GET /api/dashboard-ui/data with
                     loading → data → empty/error states per component.

   XSS safety: EVERY plugin-provided value passes through esc() before it
   enters markup — text, attribute values, URLs, table cells, option labels.
   Interactive wiring happens AFTER the innerHTML parse (element helpers
   are appended, never interpolated into template literals); listeners
   attach to elements, never to document.
   ═══════════════════════════════════════════════════════════════════════════ */

const DUI_DEFAULT_ICON = "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z";
const DUI_INPUT_TYPES = ["checkbox", "toggle", "text-input", "number-input",
                         "textarea", "select", "radio-group", "multi-select"];

/* navigation entries (from the registry), the id → component map of the
   currently rendered page, the last provider results and a fetch race
   guard for page switches. */
let duiNav = [];
let duiSeq = 0;
let duiComps = {};
let duiDataCache = {};
/* bulk-selection state: view component id -> Set of selected keys */
/* bulk selection: compId -> Set(keys); table expansion: compId -> true.
   The expansion flag survives the WS-tick data refresh — without it,
   loadDUIData re-renders from the ORIGINAL page_size and an expanded
   table collapses on its own (user-reported bug). */
const duiSelection = {};
const duiExpanded = {};
/* dynamic form fields: form id -> field defs from a fields provider */
const duiFormFields = {};
/* account-scoped pages: plugin -> the dashboard-selected account */
const duiPageAccount = {};
/* tab selection: pageKey -> active index */
const duiTabState = {};

function duiSelected(compId) {
  duiSelection[compId] = duiSelection[compId] || new Set();
  return duiSelection[compId];
}

function openDuiModal(modalId) {
  const dlg = document.querySelector('dialog.dui-modal[data-dui-comp="' + duiSlug(modalId) + '"]');
  if (dlg) dlg.showModal();
  return !!dlg;
}

/* ── Navigation ────────────────────────────────────────────────────────────── */
async function loadDashboardUINav() {
  try { duiNav = await api("/api/dashboard-ui/navigation") || []; }
  catch (e) { duiNav = []; showErr(e.message); }   // QA ticket 5: an empty sidebar must explain itself
  // SINGLE SOURCE OF TRUTH (review: "separate customisation from
  // structure"): the server's navigation defines the framework
  // sidebar — labels, icons, order. app.js consumes NAV/TITLES.
  if (typeof NAV !== "undefined") {
    NAV = duiNav.filter(e => e.plugin === "$framework").map(e => ({
      id: e.page_id, label: e.title || e.page_id,
      icon: e.icon || (typeof FALLBACK_ICONS !== "undefined"
                        ? (FALLBACK_ICONS[e.page_id] || null) : null)
                      || DUI_DEFAULT_ICON,
    }));
    TITLES = Object.fromEntries(NAV.map(n => [n.id, n.label]));
  }
  buildDashboardUINav();
}

function duiSlug(value) { return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "-"); }
function duiViewId(entry) { return "dui-" + duiSlug(entry.plugin) + "-" + duiSlug(entry.page_id); }
function duiPanelId(entry) { return "panel-" + duiViewId(entry); }

/* Rebuilds the sidebar entries + panel containers. Idempotent (clears its
   own [data-dui-*] remnants first) so app.js buildNav() can call it after
   wiping #snav on every navigation. */
function buildDashboardUINav() {
  $$("[data-dui-nav]").forEach(el => el.remove());
  $$("[data-dui-panel]").forEach(el => el.remove());
  for (const entry of duiNav) {
    // the framework's own pages ARE the main tabs (rendered into their
    // panels by framework-ui.js) — listing them again in the sidebar
    // would duplicate every entry
    if (entry.plugin === "$framework") continue;
    const viewId = duiViewId(entry);
    const a = document.createElement("button");
    a.className = "nav-item" + (currentView === viewId ? " active" : "");
    a.dataset.view = viewId;
    a.dataset.duiNav = "1";
    const iconPath = entry.icon || DUI_DEFAULT_ICON;
    a.innerHTML = `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="${esc(iconPath)}"/></svg><span>${esc(entry.title || entry.plugin)}</span>`;
    a.onclick = () => showDashboardUIPage(entry);
    $("snav").appendChild(a);
    const div = document.createElement("div");
    div.className = "panel";
    div.id = duiPanelId(entry);
    div.dataset.duiPanel = "1";
    $("content").appendChild(div);
  }
}

async function showDashboardUIPage(entry) {
  const panel = $(duiPanelId(entry));
  if (!panel) return;
  window.__duiCurrentPageId = entry.page_id;
  // this render replaces the GLOBAL duiComps index — framework tabs
  // must fully re-render when navigated back to (see framework-ui.js)
  $$(".panel[data-fw-rendered]").forEach(p => delete p.dataset.fwRendered);
  const viewId = duiViewId(entry);
  // currentView carries the dui view id so render()/showPanel() — which run
  // on every WS structural change — keep THIS panel active instead of
  // bouncing the user back to the page they came from mid-form.
  currentView = viewId;
  showPanel(duiPanelId(entry));
  $("page-title").textContent = entry.title || entry.plugin;
  $("sidebar").classList.remove("open");
  $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === viewId));
  panel.dataset.duiPlugin = entry.plugin;
  panel.innerHTML = `<div class="dui-skel" aria-hidden="true"><div></div><div></div><div></div></div>`;
  const seq = ++duiSeq;
  let page = null;
  const acct = duiPageAccount[entry.plugin] || "";
  try {
    page = await api("/api/dashboard-ui/page?plugin=" + encodeURIComponent(entry.plugin) +
                     "&page_id=" + encodeURIComponent(entry.page_id) +
                     (acct ? "&account=" + encodeURIComponent(acct) : ""));
  } catch (e) {
    if (seq !== duiSeq) return;
    panel.innerHTML = duiAlertHTML("err", "Page unavailable", e.message);
    return;
  }
  if (seq !== duiSeq) return;  // a newer click superseded this fetch
  if (!page || page.type !== "page") {
    panel.innerHTML = "";
    setFb(panel, "error", "plugin page not found");
    return;
  }
  renderDashboardUIPage(page, entry.plugin, panel);
  // account-scoped page: an account picker in the header supplies the
  // validated selection for every action/provider/check on the page
  if (page.account_scoped) renderDuiAccountPicker(entry.plugin, panel);
}

function renderDuiAccountPicker(pluginName, panel) {
  if (panel.querySelector("[data-dui-acct-sel]")) return;   // idempotent
  const bar = document.createElement("div");
  bar.className = "dui-acct-bar";
  bar.innerHTML = `<label class="dui-acct-label">Account</label>` +
    `<select data-dui-acct-sel="${esc(pluginName)}" aria-label="Account for ${esc(pluginName)}">` +
    accounts().map(a => `<option value="${esc(a.name)}"${duiPageAccount[pluginName] === a.name ? " selected" : ""}>${esc(a.name)}</option>`).join("") +
    `</select>`;
  panel.prepend(bar);
  const sel = bar.querySelector("select");
  if (!duiPageAccount[pluginName] && sel.options.length)
    duiPageAccount[pluginName] = sel.value;
  sel.addEventListener("change", () => {
    duiPageAccount[pluginName] = sel.value;
    const navEntry = duiNav.find(n => n.plugin === pluginName && n.page_id === (window.__duiCurrentPageId || ""));
    showDashboardUIPage(navEntry || {plugin: pluginName, page_id: window.__duiCurrentPageId, title: pluginName});
  });
}

/* ── Page shell ────────────────────────────────────────────────────────────── */
function renderDashboardUIPage(page, pluginName, panel) {
  duiComps = {};
  duiDataCache = {};
  duiIndexComponents(page);
  const flow = [], modals = [];
  for (const c of (page.children || []))
    (c.type === "modal" ? modals : flow).push(c);
  let html = `<div class="dui-page">`;
  if (page.description)
    html += `<p class="sub dui-desc">${esc(page.description)}</p>`;
  // Tab children form ONE tablist (genuine tabs — Stage D); sections and
  // other children stay in the flow around it
  const tabs = flow.filter(c => c.type === "tab");
  const rest = flow.filter(c => c.type !== "tab");
  html += rest.map(c => renderDashboardUIComponent(c, pluginName)).join("");
  if (tabs.length) html += duiTablistHTML(page, tabs, pluginName);
  html += `</div>`;
  html += modals.map(m => renderModal(m, pluginName)).join("");
  panel.innerHTML = html || '<div class="dui-page"><p class="dim">This page has no content.</p></div>';
  wireDashboardUI(panel, pluginName);
  wireDuiTabs(panel, pluginName);
}

function duiTablistHTML(page, tabs, pluginName) {
  const pageKey = duiSlug(pluginName) + ":" + (page.id || "");
  const active = duiTabState[pageKey] != null ? duiTabState[pageKey] : 0;
  const clamp = Math.min(active, tabs.length - 1);
  const bar = tabs.map((t, i) =>
    `<button type="button" role="tab" aria-selected="${i === clamp}"` +
    ` id="dui-tab-${pageKey}-${i}" aria-controls="dui-tabpanel-${pageKey}-${i}"` +
    ` tabindex="${i === clamp ? 0 : -1}" data-dui-tab="${pageKey}" data-dui-tab-idx="${i}"` +
    ` class="dui-tab-btn${i === clamp ? " active" : ""}">${esc(t.title || t.id)}</button>`).join("");
  const panels = tabs.map((t, i) =>
    `<div role="tabpanel" id="dui-tabpanel-${pageKey}-${i}"` +
    ` aria-labelledby="dui-tab-${pageKey}-${i}" data-dui-tabpanel="${pageKey}"` +
    ` data-dui-tabpanel-idx="${i}"${i === clamp ? "" : " hidden"}>` +
    renderDashboardUIComponent(t, pluginName) + `</div>`).join("");
  return `<div class="dui-tabbar" role="tablist" aria-label="${esc(page.title || "sections")}">` +
    `<div class="dui-tabbar-row">${bar}</div></div>${panels}`;
}

function wireDuiTabs(root, pluginName) {
  root.querySelectorAll("[data-dui-tab]").forEach(btn => {
    if (btn.dataset.duiWired) return;
    btn.dataset.duiWired = "1";
    const pageKey = btn.dataset.duiTab;
    const select = (idx, focus) => {
      const idxN = Math.max(0, Math.min(idx,
        root.querySelectorAll(`[data-dui-tab="${pageKey}"]`).length - 1));
      duiTabState[pageKey] = idxN;
      root.querySelectorAll(`[data-dui-tab="${pageKey}"]`).forEach(b => {
        const on = +b.dataset.duiTabIdx === idxN;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", String(on));
        b.tabIndex = on ? 0 : -1;
      });
      root.querySelectorAll(`[data-dui-tabpanel="${pageKey}"]`).forEach(p => {
        const on = +p.dataset.duiTabpanelIdx === idxN;
        p.hidden = !on;
        // LAZY data: bodies inside a newly shown panel load now
        if (on) p.querySelectorAll("[data-dui-body]").forEach(el => {
          if (!el.dataset.duiLoaded) loadDUIData(pluginName, el.dataset.duiBody, el);
        });
        if (on) p.querySelectorAll("[data-dui-live]").forEach(el => {
          if (!el.dataset.duiLoaded) loadDUILive(pluginName, el.dataset.duiLive, el);
        });
      });
      if (focus) btn.focus();
    };
    btn.addEventListener("click", () => select(+btn.dataset.duiTabIdx, false));
    btn.addEventListener("keydown", e => {
      const n = root.querySelectorAll(`[data-dui-tab="${pageKey}"]`).length;
      const cur = +btn.dataset.duiTabIdx;
      if (e.key === "ArrowRight") { e.preventDefault(); select((cur + 1) % n, true); }
      if (e.key === "ArrowLeft") { e.preventDefault(); select((cur - 1 + n) % n, true); }
      if (e.key === "Home") { e.preventDefault(); select(0, true); }
      if (e.key === "End") { e.preventDefault(); select(n - 1, true); }
    });
  });
  // mark already-loaded bodies so lazy loading never double-fetches
  root.querySelectorAll("[data-dui-body]").forEach(el => {
    if (el.dataset.duiPlugin) el.dataset.duiLoaded = "1";
  });
}

function renderModal(comp, pluginName, disabled) {
  return `<dialog class="dui-modal" data-dui-comp="${esc(comp.id)}">` +
    `<div class="dlg-hdr"><span class="dlg-title">${esc(comp.title || "")}</span>` +
    `<button type="button" class="dlg-close" data-dlg-close aria-label="Close">✕</button></div>` +
    `<div class="dlg-body">` +
    (comp.description ? `<p class="sub">${esc(comp.description)}</p>` : "") +
    duiChildren(comp, pluginName, disabled) +
    `</div></dialog>`;
}

function duiIndexComponents(comp) {
  if (!comp || typeof comp !== "object") return;
  if (comp.id) duiComps[comp.id] = comp;
  for (const child of (comp.children || [])) duiIndexComponents(child);
}

/* ── Action dispatch ───────────────────────────────────────────────────────── */
async function dispatchUIAction(pluginName, componentId, payload) {
  const body = {plugin: pluginName, component_id: componentId,
                payload: payload || {}};
  const acct = duiPageAccount[pluginName];
  if (acct) body.account = acct;
  const result = await post("/api/dashboard-ui/action", body);
  if (result && Array.isArray(result.refresh) && result.refresh.length)
    refreshDashboardUIData(pluginName, result.refresh);
  if (result && result.modal) openDuiModal(result.modal);
  return result;
}

/* Re-fetch the listed component ids inside the plugin's active panel.
 * Covers BOTH data bodies (tables/lists/tiles) and live leaves (metrics,
 * texts) — a refresh list naming a modal's metric must update it too. */
function refreshDashboardUIData(pluginName, ids) {
  for (const id of (ids || [])) {
    const host = document.querySelector('[data-dui-body="' + id + '"][data-dui-plugin="' + duiSlug(pluginName) + '"]');
    if (host) { loadDUIData(pluginName, id, host, true); continue; }
    const leaf = document.querySelector('[data-dui-live="' + id + '"]');
    if (leaf) { loadDUILive(pluginName, id, leaf); continue; }
    // dynamic form fields (schema-driven editors) re-fetch too
    const fields = document.querySelector('[data-dui-fields="' + id + '"]');
    if (fields) loadDuiFormFields(pluginName, id, fields);
  }
}

/* ── Data providers (list-view / table) ────────────────────────────────────── */
async function loadDUIData(pluginName, compId, host, quiet) {
  if (!host) return;
  if (host.dataset) host.dataset.duiLoaded = "1";
  if (!quiet) host.innerHTML = `<div class="dui-skel" aria-hidden="true"><div></div><div></div></div>`;
  if (!quiet) clearFb(host.parentElement);
  let data = null;
  try {
    const acct = duiPageAccount[pluginName];
    data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(pluginName) +
                     "&component_id=" + encodeURIComponent(compId) +
                     (acct ? "&account=" + encodeURIComponent(acct) : ""));
  } catch (e) {
    host.innerHTML = duiAlertHTML("err", "Data unavailable", e.message);
    return;
  }
  duiDataCache[compId] = data;
  const comp = duiComps[compId] || {};
  let html;
  if (comp.type === "table") html = duiTableHTML(comp, data, compId);
  else if (comp.type === "tile-view") html = duiTilesHTML(comp, data, compId);
  else if (comp.type === "list-view" && Array.isArray(comp.columns))
    html = duiListRecordsHTML(comp, data, compId);
  else html = duiListHTML(comp, data);
  host.innerHTML = html || "";
  wireDUIMore(host, compId, pluginName);
  // row-action buttons render WITH the data (inside the body) — they
  // did not exist when wireDashboardUI ran, so wire them HERE on every
  // load/refresh or they are dead DOM
  wireDUIRowActions(host, pluginName);
  wireDuiSelection(host, pluginName, compId);
}

function duiEmpty(emptyText) {
  return `<div class="dui-empty sub">${esc(emptyText || "nothing to show yet")}</div>`;
}

function duiCell(value) {
  if (value == null) return "";
  if (typeof value === "object") return esc(JSON.stringify(value));
  return esc(String(value));
}

function duiTableHTML(comp, data, compId) {
  let columns = [], rows = [];
  if (data && !Array.isArray(data) && typeof data === "object") {
    columns = Array.isArray(data.columns) ? data.columns.map(String) : [];
    rows = Array.isArray(data.rows) ? data.rows : [];
  } else if (Array.isArray(data) && data.length &&
             data.every(r => r && typeof r === "object" && !Array.isArray(r))) {
    const keys = [];
    for (const row of data)
      for (const key of Object.keys(row))
        if (!keys.includes(key)) keys.push(key);
    columns = (Array.isArray(comp.columns) && comp.columns.length
               ? comp.columns.map(String) : keys);
    rows = data.map(row => columns.map(key => row[key]));
  } else if (Array.isArray(data)) {
    columns = (Array.isArray(comp.columns) && comp.columns.length
               ? comp.columns.map(String)
               : (data[0] || []).map((_, i) => "Column " + (i + 1)));
    rows = data;
  } else {
    return duiEmpty("table data must be a list or a {columns, rows} object");
  }
  if (!rows.length) return duiEmpty(comp.empty_text);
  // expanded state wins over the declared page_size (WS-tick refreshes
  // re-render with the original comp — the flag keeps "show all" stuck)
  const pageSize = duiExpanded[compId] ? 0
    : Math.max(0, Math.round(+comp.page_size || 0));
  const visible = pageSize && rows.length > pageSize ? rows.slice(0, pageSize) : rows;
  // Row-action key resolution: the declared key column when it exists
  // among the columns, else the "__key" pseudo-cell PAST the visible
  // columns (composite or display-cleaned keys). Cells render strictly
  // by column index so the extra cell never shows.
  const keyCol = String(comp.key_column || "");
  let keyIdx = keyCol ? columns.indexOf(keyCol) : -1;
  if (keyCol === "__key" && keyIdx < 0) keyIdx = columns.length;
  const rowActions = Array.isArray(comp.row_actions) ? comp.row_actions : [];
  const selectable = comp.selectable === true;
  // precompute per-row keys (with the synthetic fallback) — the header
  // select-all and the body checkboxes must agree on the same keys
  const rowKeys = visible.map((r, rowIndex) => {
    let key = keyIdx >= 0 && r ? String(r[keyIdx] ?? "") : "";
    if (!key) key = "#" + rowIndex;
    return key;
  });
  const allSelected = selectable && visible.length > 0
    && rowKeys.every(k => duiSelected(compId).has(k));
  const head = (selectable
    ? `<th class="dui-sel-th"><input type="checkbox" data-dui-selall="${esc(compId)}"` +
      ` aria-label="Select all rows"${allSelected ? " checked" : ""}></th>` : "")
    + columns.map(c => `<th>${esc(c)}</th>`).join("")
    + (rowActions.length ? `<th></th>` : "");
  const body = visible.map((r, rowIndex) => {
    const cells = columns.map((_, i) => `<td>${duiCell(r ? r[i] : "")}</td>`).join("");
    const key = rowKeys[rowIndex];
    const selCell = selectable
      ? `<td class="dui-sel-td"><input type="checkbox" data-dui-sel="${esc(compId)}" data-dui-key="${esc(key)}"${duiSelected(compId).has(key) ? " checked" : ""}></td>` : "";
    let actionCell = "";
    if (rowActions.length) {
      actionCell = `<td class="dui-rowacts">` + rowActions.map(a =>
        `<button type="button" class="btn btn-sm btn-${esc(a.variant || "secondary")}"` +
        ` data-dui-rowact="${esc(compId)}" data-dui-raction="${esc(a.id)}"` +
        ` data-dui-row="${esc(key)}"${a.confirm ? ` title="${esc(a.confirm)}"` : ""}>${esc(a.label)}</button>`
      ).join(" ") + `</td>`;
    }
    return `<tr>${selCell}${cells}${actionCell}</tr>`;
  }).join("");
  const more = pageSize && rows.length > pageSize
    ? `<button type="button" class="btn btn-ghost btn-sm" data-dui-more="${esc(compId)}">show all ${rows.length} rows</button>`
    : "";
  return duiBulkBarHTML(comp, compId) +
    `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>${more}`;
}

/* ── TileView bodies + the shared bulk-selection machinery ─────────────── */
function duiTilesHTML(comp, data, compId) {
  let tiles = null;
  if (data && !Array.isArray(data) && typeof data === "object")
    tiles = Array.isArray(data.tiles) ? data.tiles : null;
  else if (Array.isArray(data)) tiles = data;
  if (!tiles) return duiEmpty("tile data must be a list or a {tiles: […]} object");
  if (!tiles.length) return duiEmpty(comp.empty_text);
  const rowActions = Array.isArray(comp.row_actions) ? comp.row_actions : [];
  const keyCol = String(comp.key_column || "key");
  const ST = {ok: "online", warn: "warning", err: "offline", info: "info"};
  const cards = tiles.map((t, tileIndex) => {
    if (!t || typeof t !== "object") return "";
    const keyCol = String(comp.key_column || "key");
    let key = String(t[keyCol] ?? t.key ?? "");
    if (!key) key = "#" + tileIndex;   // unique fallback — never share a selection key
    const tone = ["ok", "warn", "err", "info"].includes(t.tone) ? t.tone : null;
    const sel = comp.selectable === true
      ? `<label class="dui-tile-check" title="Select for bulk actions">` +
        `<input type="checkbox" data-dui-sel="${esc(compId)}" data-dui-key="${esc(key)}"${duiSelected(compId).has(key) ? " checked" : ""}></label>` : "";
    // status pill: the same .st badges the legacy cards used
    const status = t.status
      ? `<span class="st st-${tone ? ST[tone] : "dim"}">${esc(t.status)}</span>` : "";
    // chips: plain strings or {text, kind} with the legacy chip colors
    const chips = (Array.isArray(t.chips) ? t.chips : []).map(c => {
      const text = (c && typeof c === "object") ? c.text : c;
      const kind = (c && typeof c === "object" &&
                    ["group", "plugin", "ok", "warn", "err"].includes(c.kind))
        ? ` ${esc(c.kind)}` : "";
      return `<span class="chip${kind}">${esc(text)}</span>`;
    }).join("");
    // meta: compact icon-prefaced items (🔑 db · ⏱ 1h · ⚡ 88ms · ⏳ 2)
    const meta = (Array.isArray(t.meta) ? t.meta : []).map(m =>
      Array.isArray(m) && m.length >= 2
        ? `<span class="item" title="${esc(String(m[0]))}">${esc(String(m[0]))} ${duiCell(m[1])}</span>`
        : "").join("");
    const lines = (Array.isArray(t.lines) ? t.lines : [])
      .map(l => Array.isArray(l) && l.length >= 2
        ? `<span class="dui-kv"><span class="dim">${esc(l[0])}</span><span class="dui-kv-v">${duiCell(l[1])}</span></span>`
        : "").join("");
    const acts = rowActions.length
      ? `<div class="dui-tile-actions">` + rowActions.map(a =>
          `<button type="button" class="btn btn-sm btn-${esc(a.variant || "secondary")}"` +
          ` data-dui-rowact="${esc(compId)}" data-dui-raction="${esc(a.id)}"` +
          ` data-dui-row="${esc(key)}"${a.confirm ? ` title="${esc(a.confirm)}"` : ""}>${esc(a.label)}</button>`).join(" ") + `</div>` : "";
    return `<div class="dui-tile${tone ? ` dui-tile-${esc(tone)}` : ""}">` + sel +
      `<div class="dui-tile-hdr"><span class="dui-tile-title" title="${esc(key)}">${esc(t.title ?? key)}</span>${status}</div>` +
      (meta ? `<div class="dui-tile-meta">${meta}</div>` : "") +
      (chips ? `<div class="dui-tile-chips">${chips}</div>` : "") +
      (lines ? `<div class="dui-tile-lines">${lines}</div>` : "") + acts + `</div>`;
  }).join("");
  return duiBulkBarHTML(comp, compId) + `<div class="dui-tiles">${cards}</div>`;
}

/* ── ListView RECORDS mode: aligned record rows (the account-list
   contract). Four zones per row: selection | cells (identity first,
   strong) | actions (trailing, aligned). Status badges carry TEXT —
   color is never the only signal. ─────────────────────────────────── */
function duiListRecordsHTML(comp, data, compId) {
  let records = null;
  if (data && !Array.isArray(data) && typeof data === "object")
    records = Array.isArray(data.records) ? data.records : null;
  else if (Array.isArray(data)) records = data;
  if (!records)
    return duiEmpty("records data must be a list or a {records: […]} object");
  if (!records.length) return duiEmpty(comp.empty_text);
  const cols = Array.isArray(comp.columns) ? comp.columns : [];
  const rowActions = Array.isArray(comp.row_actions) ? comp.row_actions : [];
  const selectable = comp.selectable === true;
  const keyCol = String(comp.key_column || "key");
  // header row: selection slot + labels + trailing actions slot
  // shrinkable tracks: cells wrap internally instead of overflowing the
  // page. Identity keeps a modest floor and the LARGER fr share (it wins
  // the surplus at desktop); data columns keep a readable 90px floor;
  // actions are capped and wrap at their own boundary.
  const gridCols = (selectable ? "28px " : "") +
    cols.map((c, i) => i === 0 ? "minmax(140px, 1.6fr)" : "minmax(72px, 1fr)")
        .join(" ") + (rowActions.length ? " minmax(180px, 540px)" : "");
  const head = `<div class="dui-lrows-head" role="row" style="grid-template-columns:${gridCols}">` +
    (selectable ? `<span class="dui-lrows-sel"></span>` : "") +
    cols.map(c => `<span class="dui-lrows-h" role="columnheader">${esc(c)}</span>`).join("") +
    (rowActions.length ? `<span class="dui-lrows-h dui-lrows-h-actions"></span>` : "") +
    `</div>`;
  const rows = records.map(rec => {
    if (!rec || typeof rec !== "object") return "";
    const key = String(rec[keyCol] ?? rec.key ?? "");
    const cells = Array.isArray(rec.cells) ? rec.cells : [];
    const sel = selectable
      ? `<label class="dui-lrow-sel" title="Select for bulk actions">` +
        `<input type="checkbox" data-dui-sel="${esc(compId)}" data-dui-key="${esc(key)}"${duiSelected(compId).has(key) ? " checked" : ""}></label>` : "";
    const cellHtml = cols.map((_, i) => {
      const v = cells[i];
      if (v && typeof v === "object" && !Array.isArray(v)) {
        if ("primary" in v || "secondary" in v)
          return `<span class="dui-lrow-cell dui-lrow-identity">` +
            `<span class="dui-lrow-primary">${esc(v.primary ?? "")}</span>` +
            (v.secondary ? `<span class="dui-lrow-secondary">${esc(v.secondary)}</span>` : "") +
            `</span>`;
        if ("chips" in v) {
          const chips = (Array.isArray(v.chips) ? v.chips : []).map(c => {
            const text = (c && typeof c === "object") ? c.text : c;
            const kind = (c && typeof c === "object" &&
                          ["group", "plugin", "ok", "warn", "err"].includes(c.kind))
              ? ` ${esc(c.kind)}` : "";
            return `<span class="chip${kind}">${esc(text)}</span>`;
          }).join(" ");
          return `<span class="dui-lrow-cell dui-lrow-chips">${chips}</span>`;
        }
        if ("text" in v) {
          const tone = ["ok", "warn", "err", "info"].includes(v.tone) ? v.tone : null;
          return `<span class="dui-lrow-cell dui-lrow-status">` +
            `<span class="dui-lrow-status-line${tone ? ` st-${tone === "ok" ? "online" : tone === "err" ? "offline" : tone === "warn" ? "warning" : "info"}` : ""}">` +
            `<span class="dui-lrow-dot${tone ? ` dui-dot-${tone}` : ""}" aria-hidden="true"></span>` +
            `${esc(v.text)}</span>` +
            (v.meta ? `<span class="dui-lrow-status-meta">${esc(v.meta)}</span>` : "") +
            `</span>`;
        }
      }
      return `<span class="dui-lrow-cell">${duiCell(v)}</span>`;
    }).join("");
    const acts = rowActions.length
      ? `<span class="dui-lrow-actions">` + rowActions.map(a =>
          `<button type="button" class="btn btn-sm btn-${esc(a.variant || "secondary")}"` +
          ` data-dui-rowact="${esc(compId)}" data-dui-raction="${esc(a.id)}"` +
          ` data-dui-row="${esc(key)}"${a.confirm ? ` title="${esc(a.confirm)}"` : ""}>${esc(a.label)}</button>`).join("") +
        `</span>` : "";
    return `<div class="dui-lrow" role="row" data-dui-key="${esc(key)}" style="grid-template-columns:${gridCols}">` +
      sel + cellHtml + acts + `</div>`;
  }).join("");
  return duiBulkBarHTML(comp, compId) +
    `<div class="dui-lrows" role="rowgroup">${head}${rows}</div>`;
}

function duiBulkBarHTML(comp, compId) {
  const bulk = Array.isArray(comp.bulk_actions) ? comp.bulk_actions : [];
  if (!bulk.length) return "";
  return `<div class="dui-bulkbar" data-dui-bulkbar="${esc(compId)}">` +
    `<span class="dui-bulk-count">0 selected</span>` +
    bulk.map(a => `<button type="button" class="btn btn-${esc(a.variant || "secondary")} btn-sm"` +
      ` data-dui-bulk="${esc(compId)}" data-dui-baction="${esc(a.id)}"${a.confirm ? ` data-dui-confirm="${esc(a.confirm)}"` : ""}>${esc(a.label)}</button>`).join("") +
    `<button type="button" class="btn btn-ghost btn-sm" data-dui-bulkclear="${esc(compId)}">clear</button></div>`;
}

function updateDuiBulkBar(compId) {
  const bar = document.querySelector(`[data-dui-bulkbar="${compId}"]`);
  if (!bar) return;
  const n = duiSelected(compId).size;
  bar.classList.toggle("open", n > 0);
  const cnt = bar.querySelector(".dui-bulk-count");
  if (cnt) cnt.textContent = `${n} selected`;
}

function duiResetExpansions() {
  /* USER DIRECTIVE: an expanded table collapses automatically when the
     active window moves elsewhere — expansion is transient view state,
     so navigating away resets it (UI.go calls this). */
  for (const key of Object.keys(duiExpanded)) delete duiExpanded[key];
}

/* Wire checkboxes + bulk buttons in freshly rendered bodies. */
function wireDuiSelection(root, pluginName, compId) {
  root.querySelectorAll(`input[data-dui-sel="${compId}"]`).forEach(cb => {
    if (cb.dataset.duiWired) return;
    cb.dataset.duiWired = "1";
    cb.addEventListener("change", () => {
      const set = duiSelected(compId);
      if (cb.checked) set.add(cb.dataset.duiKey); else set.delete(cb.dataset.duiKey);
      updateDuiBulkBar(compId);
      syncDuiSelAll(root, compId);
    });
  });
  // header SELECT-ALL: toggles every row of this table (user request:
  // select all entries at once). Computed from the CACHED DATA, not the
  // DOM — the 1s stats refresh re-renders the body mid-flight and DOM
  // scans race it. After updating the selection the body re-renders,
  // so checkboxes reflect the state.
  const selAll = root.querySelector(`input[data-dui-selall="${compId}"]`);
  if (selAll && !selAll.dataset.duiWired) {
    selAll.dataset.duiWired = "1";
    selAll.addEventListener("change", () => {
      const comp = duiComps[compId] || {};
      const data = duiDataCache[compId];
      let rows = null;
      if (Array.isArray(data)) rows = data;
      else if (data && Array.isArray(data.rows)) rows = data.rows;
      const columns = (data && Array.isArray(data.columns)) ? data.columns : [];
      const keyCol = String(comp.key_column || "");
      const keyIdx = keyCol ? columns.indexOf(keyCol) : -1;
      const set = duiSelected(compId);
      (rows || []).forEach((r, i) => {
        let key = keyIdx >= 0 && r ? String(r[keyIdx] ?? "") : "";
        if (!key) key = "#" + i;
        if (selAll.checked) set.add(key); else set.delete(key);
      });
      updateDuiBulkBar(compId);
      const bodyHost = root.querySelector("[data-dui-body]");
      if (bodyHost) {
        bodyHost.innerHTML = duiTableHTML(comp, data, compId);
        wireDUIMore(bodyHost, compId, pluginName);
        wireDuiSelection(bodyHost, pluginName, compId);
        updateDuiBulkBar(compId);
      }
    });
  }
  syncDuiSelAll(root, compId);

/* header select-all reflector: checked = all visible selected,
   indeterminate = some selected. */
function syncDuiSelAll(root, compId) {
  const selAll = root.querySelector(`input[data-dui-selall="${compId}"]`);
  if (!selAll) return;
  const boxes = Array.from(
    root.querySelectorAll(`input[data-dui-sel="${compId}"]`));
  const checked = boxes.filter(b => b.checked).length;
  selAll.checked = boxes.length > 0 && checked === boxes.length;
  selAll.indeterminate = checked > 0 && checked < boxes.length;
}
  root.querySelectorAll(`button[data-dui-bulk="${compId}"]`).forEach(btn => {
    if (btn.dataset.duiWired) return;
    btn.dataset.duiWired = "1";
    const host = btn.closest(".dui-item") || btn.parentElement;
    const fire = async () => {
      const rows = [...duiSelected(compId)];
      try {
        const res = await withBusy(btn, dispatchUIAction(pluginName, compId,
          { action: btn.dataset.duiBaction, rows }));
        if (!res) return;
        if (res.field_errors && Object.keys(res.field_errors).length)
          setFbFloat(host, "error", Object.values(res.field_errors).join("; "), null, btn);
        else if (res.message)
          res.success ? flashFbFloat(host, res.message, null, btn) : setFbFloat(host, "error", res.message, null, btn);
        if (res.success !== false && !(res.field_errors && Object.keys(res.field_errors).length))
          duiSelection[compId] = new Set();   // bulk ops consume the selection
      } catch (e) { setFbFloat(host, "error", e.message, null, btn); }
    };
    btn.onclick = () => {
      const rows = duiSelected(compId);
      if (!rows.size) return;
      const confirmText = btn.dataset.duiConfirm;
      if (confirmText) confirmAction("Confirm", confirmText, fire);
      else fire();
    };
  });
  root.querySelectorAll(`button[data-dui-bulkclear="${compId}"]`).forEach(btn => {
    if (btn.dataset.duiWired) return;
    btn.dataset.duiWired = "1";
    btn.onclick = () => { duiSelection[compId] = new Set(); updateDuiBulkBar(compId);
      root.querySelectorAll(`input[data-dui-sel="${compId}"]`).forEach(cb => cb.checked = false); };
  });
  updateDuiBulkBar(compId);
}

/* Wire row-action buttons (dispatch {"action", "row"} through the
   TABLE's action channel; the server routes to the RowAction).
   Called from wireDashboardUI for static markup AND from loadDUIData —
   row buttons render inside table bodies, which fill asynchronously
   after wiring ran. */
function wireDUIRowActions(root, pluginName) {
  root.querySelectorAll("[data-dui-rowact]").forEach(el => {
    if (el.dataset.duiWired) return;   // idempotent across refreshes
    el.dataset.duiWired = "1";
    const compId = el.dataset.duiRowact;
    const comp = duiComps[compId] || {};
    const def = (comp.row_actions || []).find(a => a.id === el.dataset.duiRaction) || {};
    // feedback goes on the component wrapper OUTSIDE the data body —
    // the action's own refresh re-renders the body, which would wipe
    // any message placed inside it (the "silent success" trap)
    const body = el.closest("[data-dui-body]");
    const host = (body && body.parentElement) || el.closest(".tbl-wrap")
                 || el.parentElement;
    // USER DIRECTIVE: remove/delete-type outcomes are HIGH-PRIORITY —
    // they surface the 5s header prompt, not a component chip.
    const removalLike = /^(remove|delete|unload|forget|reload|estop)$/i.test(el.dataset.duiRaction || "");
    const highPriority = (m) => removalLike || /\b(created|added|removed|deleted)\b/i.test(m || "");
    const fire = async () => {
      try {
        const res = await withBusy(el, dispatchUIAction(pluginName, compId,
          { action: el.dataset.duiRaction, row: el.dataset.duiRow }));
        if (!res) return;
        if (res.field_errors && Object.keys(res.field_errors).length)
          setFbFloat(host, "error", Object.values(res.field_errors).join("; "), null, el);
        else if (res.message)
          res.success
            ? (highPriority(res.message) ? showHeaderPrompt(res.message)
                           : flashFbFloat(host, res.message, null, el))
            : setFbFloat(host, "error", res.message, null, el);
        else if (res.success !== false && removalLike)
          showHeaderPrompt(`"${el.dataset.duiRow}" removed`);
      } catch (e) { setFbFloat(host, "error", e.message, null, el); }
    };
    el.onclick = () => {
      if (def.confirm) confirmAction("Confirm", def.confirm, fire);
      else fire();
    };
  });
}

/* Reveal the rows a page_size hid (works off the cached provider result).
   The expansion is REMEMBERED per component (duiExpanded) so the WS-tick
   data refresh re-renders expanded instead of collapsing back. */
function wireDUIMore(host, compId, pluginName) {
  const btn = host.querySelector("[data-dui-more]");
  if (!btn) return;
  btn.onclick = () => {
    const comp = duiComps[compId] || {};
    duiExpanded[compId] = true;   // survives refreshes — no self-collapse
    const fresh = duiTableHTML({ ...comp, page_size: 0 }, duiDataCache[compId], compId);
    const box = host.querySelector(".dui-data") || host;
    box.innerHTML = fresh;
    // the regenerated table contains NEW row-action buttons and
    // selection checkboxes — without re-wiring, every control below
    // the fold is dead after expanding
    wireDUIMore(box, compId, pluginName);
    if (pluginName) {
      wireDUIRowActions(box, pluginName);
      wireDuiSelection(box, pluginName, compId);
      updateDuiBulkBar(compId);
    }
  };
}

function duiListHTML(comp, data) {
  if (data == null || (Array.isArray(data) && !data.length))
    return duiEmpty(comp.empty_text);
  let items = null;
  if (Array.isArray(data)) items = data;
  else if (typeof data === "object") items = Object.entries(data).map(([k, v]) => ({ label: k, value: v }));
  if (!items) return duiEmpty("list data must be a list or an object");
  const rows = items.map(item => {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      if ("label" in item || "value" in item)
        return `<div class="dui-kv"><span class="dim">${esc(item.label ?? "")}</span><span class="dui-kv-v">${duiCell(item.value)}</span></div>`;
      return `<div class="dui-kv">${Object.entries(item).map(([k, v]) => `<span class="dim">${esc(k)}</span><span class="dui-kv-v">${duiCell(v)}</span>`).join("")}</div>`;
    }
    return `<div class="lrow" style="grid-template-columns:minmax(0,1fr)"><span class="lcol">${duiCell(item)}</span></div>`;
  }).join("");
  return `<div class="dui-list">${rows}</div>`;
}

/* ── The generic renderer ──────────────────────────────────────────────────── */
// Build inline layout styles from the component's serialized properties
// (all validated server-side: CSS-safe size strings, bounded ints and
// whitelisted keywords — never arbitrary style text crosses the wire).
const DUI_ALIGN = {auto:"auto", start:"flex-start", center:"center",
                   end:"flex-end", stretch:"stretch", baseline:"baseline"};
const DUI_JUSTIFY = {start:"flex-start", center:"center", end:"flex-end",
                     between:"space-between", around:"space-around",
                     evenly:"space-evenly"};

function duiSize(comp) {
  let s = "";
  if (comp.width) s += ` width:${esc(comp.width)};`;
  if (comp.height) s += ` height:${esc(comp.height)};`;
  return s;
}

// flex participation: how this child behaves INSIDE a Row/Column/Container
function duiFlex(comp) {
  let s = "";
  if (comp.grow != null) s += ` flex-grow:${Math.max(0, Math.round(+comp.grow || 0))};`;
  if (comp.shrink != null) s += ` flex-shrink:${Math.max(0, Math.round(+comp.shrink || 0))};`;
  if (DUI_ALIGN[comp.align_self]) s += ` align-self:${DUI_ALIGN[comp.align_self]};`;
  return s;
}

// leaves: the .dui-item wrapper is the flex child — size + flex hints
function duiItemStyle(comp) {
  const s = duiSize(comp) + duiFlex(comp);
  return s ? ` style="${s}"` : "";
}

// containers: same child hints PLUS their own two-axis alignment
function duiBoxStyle(comp) {
  let s = duiSize(comp) + duiFlex(comp);
  if (DUI_ALIGN[comp.align]) s += ` align-items:${DUI_ALIGN[comp.align]};`;
  if (DUI_JUSTIFY[comp.justify]) s += ` justify-content:${DUI_JUSTIFY[comp.justify]};`;
  if (comp.gap) s += ` gap:${esc(comp.gap)};`;
  return s ? ` style="${s}"` : "";
}

function renderDashboardUIComponent(comp, pluginName) {
  if (!comp || typeof comp !== "object") return "";
  const disabled = comp.disabled === true;
  switch (comp.type) {
    case "page":
    case "tab": return renderTab(comp, pluginName, disabled);
    case "section": return renderSection(comp, pluginName, disabled);
    case "card": return renderCard(comp, pluginName, disabled);
    case "row": return renderRow(comp, pluginName, disabled);
    case "column": return renderColumn(comp, pluginName, disabled);
    case "container": return renderContainer(comp, pluginName, disabled);
    case "grid": return renderGrid(comp, pluginName, disabled);
    case "toolbar": return renderToolbar(comp, pluginName, disabled);
    case "modal": return renderModal(comp, pluginName, disabled);
    case "tile-view": return renderTileView(comp);
    case "text": return renderText(comp);
    case "display-text": return renderDisplayText(comp);
    case "alert": return renderAlert(comp);
    case "separator": return renderSeparator(comp);
    case "description": return renderDescription(comp);
    case "header": return renderHeader(comp);
    case "footer": return renderFooter(comp);
    case "badge": return renderBadge(comp);
    case "metric": return renderMetric(comp);
    case "button": return renderButton(comp, disabled);
    case "checkbox":
    case "toggle": return renderToggle(comp, disabled);
    case "text-input": return renderTextInput(comp, disabled);
    case "number-input": return renderNumberInput(comp, disabled);
    case "textarea": return renderTextarea(comp, disabled);
    case "select":
    case "radio-group": return renderSelect(comp, disabled);
    case "multi-select": return renderMultiSelect(comp, disabled);
    case "list-view": return renderListView(comp);
    case "table": return renderTable(comp);
    case "progress": return renderProgress(comp);
    case "form": return renderForm(comp, pluginName, disabled);
    default: return renderUnknown(comp);
  }
}

function duiChildren(comp, pluginName, disabled) {
  return (comp.children || [])
    .map(child => renderDashboardUIComponent({ ...child, disabled: child.disabled === true || disabled }, pluginName))
    .join("");
}

function renderTab(comp, pluginName, disabled) {
  return `<div class="dui-tab" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    (comp.title ? `<div class="dui-sec-title">${esc(comp.title)}</div>` : "") +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

function renderSection(comp, pluginName, disabled) {
  const icon = comp.icon
    ? `<svg class="dui-sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="${esc(comp.icon)}"/></svg>` : "";
  return `<div class="dui-section" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    (comp.title ? `<div class="dui-sec-title">${icon}${esc(comp.title)}</div>` : "") +
    (comp.description ? `<p class="sub">${esc(comp.description)}</p>` : "") +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

function renderCard(comp, pluginName, disabled) {
  const tone = ["info", "ok", "warn", "err"].includes(comp.tone) ? ` dui-card-${esc(comp.tone)}` : "";
  return `<div class="card dui-card${tone}" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    (comp.title ? `<h4>${esc(comp.title)}</h4>` : "") +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

function renderRow(comp, pluginName, disabled) {
  return `<div class="dui-row" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

function renderColumn(comp, pluginName, disabled) {
  return `<div class="dui-col" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

function renderContainer(comp, pluginName, disabled) {
  const dir = comp.direction === "horizontal" ? " dui-container-h" : "";
  return `<div class="dui-container${dir}" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

function renderToolbar(comp, pluginName, disabled) {
  const icon = comp.icon
    ? `<svg class="dui-sec-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="${esc(comp.icon)}"/></svg>` : "";
  return `<div class="dui-toolbar" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    (comp.title ? `<span class="dui-toolbar-title">${icon}${esc(comp.title)}</span>` : "") +
    `<div class="dui-toolbar-actions">` +
    duiChildren(comp, pluginName, disabled) + `</div></div>`;
}

function renderGrid(comp, pluginName, disabled) {
  let cols = "";
  if (comp.columns === "auto")
    cols = `repeat(auto-fill, minmax(${esc(comp.min_child || "240px")}, 1fr))`;
  else {
    const n = Math.round(+comp.columns);
    if (Number.isFinite(n) && n >= 1 && n <= 6)
      cols = `repeat(${n}, minmax(0, 1fr))`;
  }
  let s = duiSize(comp) + duiFlex(comp);
  if (cols) s += ` grid-template-columns:${cols};`;
  if (comp.gap) s += ` gap:${esc(comp.gap)};`;
  return `<div class="dui-grid" data-dui-comp="${esc(comp.id)}"${s ? ` style="${s}"` : ""}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

/* ── Display leaves ────────────────────────────────────────────────────────── */
function renderText(comp) {
  const value = comp.value ?? "";
  const live = comp.live ? ` data-dui-live="${esc(comp.id)}"` : "";
  switch (comp.variant) {
    case "muted": return `<p class="sub dui-text" data-dui-comp="${esc(comp.id)}"${live}>${esc(value)}</p>`;
    case "code": return `<pre class="dui-code mono" data-dui-comp="${esc(comp.id)}"${live}>${esc(value)}</pre>`;
    case "heading": return `<h4 class="dui-heading" data-dui-comp="${esc(comp.id)}"${live}>${esc(value)}</h4>`;
    default: return `<p class="dui-text" data-dui-comp="${esc(comp.id)}"${live}>${esc(value)}</p>`;
  }
}

function renderDisplayText(comp) {
  const tone = ["default", "dim", "ok", "warn", "err"].includes(comp.tone) ? comp.tone : "default";
  const size = ["xs", "sm", "md", "lg", "xl"].includes(comp.size) ? comp.size : "md";
  const weights = {normal: 400, medium: 500, semibold: 600, bold: 700};
  let s = "";
  if (["left", "center", "right"].includes(comp.align)) s += ` text-align:${comp.align};`;
  if (weights[comp.weight]) s += ` font-weight:${weights[comp.weight]};`;
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    `<div class="dui-dt dui-dt-${esc(tone)} dui-dt-${esc(size)}${comp.monospace ? " mono" : ""}"${s ? ` style="${s}"` : ""}>` +
    `${esc(comp.value ?? "")}</div></div>`;
}

const DUI_ALERT_ICONS = {info: "i", ok: "&#10003;", warn: "!", err: "&#10005;"};

function duiAlertHTML(variant, title, message) {
  const v = ["info", "ok", "warn", "err"].includes(variant) ? variant : "info";
  return `<div class="dui-alert dui-alert-${esc(v)}">` +
    `<span class="dui-alert-icon" aria-hidden="true">${DUI_ALERT_ICONS[v]}</span>` +
    `<div class="dui-alert-body">` +
    (title ? `<div class="dui-alert-title">${esc(title)}</div>` : "") +
    `<div class="dui-alert-msg">${esc(message ?? "")}</div>` +
    `</div></div>`;
}

function renderAlert(comp) {
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    duiAlertHTML(comp.variant, comp.title, comp.value) + `</div>`;
}

function renderSeparator(comp) {
  return `<hr class="dui-sep ${comp.variant === "dashed" ? "dui-sep-dashed" : ""}" data-dui-comp="${esc(comp.id)}">`;
}

function renderDescription(comp) {
  return `<p class="dui-desc" data-dui-comp="${esc(comp.id)}">${esc(comp.value ?? "")}</p>`;
}

function renderHeader(comp) {
  return `<div class="dui-header" data-dui-comp="${esc(comp.id)}">
    <h3>${esc(comp.title ?? "")}</h3>
    ${comp.subtitle ? `<p class="dui-desc">${esc(comp.subtitle)}</p>` : ""}
  </div>`;
}

function renderFooter(comp) {
  return `<footer class="dui-footer" data-dui-comp="${esc(comp.id)}">${esc(comp.value ?? "")}</footer>`;
}

function renderBadge(comp) {
  const variant = ["info", "ok", "warn", "err"].includes(comp.variant) ? comp.variant : "info";
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    `<span class="dui-badge dui-badge-${esc(variant)}">${esc(comp.value ?? "")}</span></div>`;
}

function renderMetric(comp) {
  const level = ["ok", "warn", "err"].includes(comp.level) ? esc(comp.level) : "";
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}><div class="metric"${comp.live ? ` data-dui-live="${esc(comp.id)}"` : ""}>` +
    (comp.label ? `<div class="label">${esc(comp.label)}</div>` : "") +
    `<div class="value ${level}">${esc(comp.value ?? "")}</div>` +
    (comp.sub ? `<div class="sub">${esc(comp.sub)}</div>` : "") +
    `</div></div>`;
}

/* Live-leaf refresh: fetch the provider result and update the metric or
   text IN PLACE — no DOM rebuild, so refresh cycles never stomp a form
   the user is typing in elsewhere on the page. */
async function loadDUILive(pluginName, id, el) {
  if (!el) return;
  let data = null;
  try {
    data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(pluginName) +
                     "&component_id=" + encodeURIComponent(id));
  } catch { return; }   // leave the last good value in place
  if (!data || typeof data !== "object" || Array.isArray(data)) return;
  if (typeof data.value === "undefined" && typeof data.level === "undefined") return;
  if (el.classList.contains("metric")) {
    const v = el.querySelector(".value");
    if (v && data.value != null) {
      v.textContent = String(data.value);
      v.className = "value " + (["ok", "warn", "err"].includes(data.level) ? data.level : "");
    }
    const s = el.querySelector(".sub");
    if (s && data.sub != null) s.textContent = String(data.sub);
    return;
  }
  if (data.value != null) el.textContent = String(data.value);
}

function renderProgress(comp) {
  const value = Math.max(0, Math.min(100, Math.round(+comp.value || 0)));
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    (comp.label ? `<div class="dui-plabel"><span>${esc(comp.label)}</span><span class="dim">${value}%</span></div>` : "") +
    `<div class="dui-prog" role="progressbar" aria-valuenow="${value}" aria-valuemin="0" aria-valuemax="100"><div class="dui-prog-fill" style="width:${value}%"></div></div></div>`;
}

/* ── Action + input leaves ─────────────────────────────────────────────────── */
function duiLabel(comp) {
  if (!comp.label) return "";
  return `<label>${esc(comp.label)}${comp.required ? ` <span class="dui-req" aria-hidden="true">*</span>` : ""}</label>`;
}

function duiHint(comp) {
  return comp.description ? `<div class="hint">${esc(comp.description)}</div>` : "";
}

function renderButton(comp, disabled) {
  const variant = ["primary", "secondary", "danger", "ghost"].includes(comp.variant) ? comp.variant : "secondary";
  const off = disabled || comp.disabled === true;
  // USER GUARD: button descriptions render as native TOOLTIPS — never
  // as visible hint text. A visible hint inside a Toolbar inflates the
  // fixed-height bar and overlaps the content below (bug report:
  // Rescan hint). Tooltips cannot overlap by construction.
  const tip = comp.description ? ` title="${esc(comp.description)}"` : "";
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}${off ? ' data-dui-disabled="1"' : ""}>` +
    `<button type="button" class="btn btn-${esc(variant)}${comp.loading ? " busy" : ""}" data-dui-act="${esc(comp.id)}"${comp.open_modal ? ` data-dui-open="${esc(comp.open_modal)}"` : ""}${tip}${off || comp.loading ? " disabled" : ""}>${esc(comp.label || comp.id)}</button>` +
    `</div>`;
}

function renderToggle(comp, disabled) {
  const off = disabled || comp.disabled === true;
  const checked = comp.value === true ? " checked" : "";
  const cls = comp.type === "toggle" ? " class=\"dui-switch\"" : "";
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
    `<label class="pick-row${comp.type === "toggle" ? " dui-switch-row" : ""}">` +
    `<input type="checkbox"${cls}${checked}${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""}>` +
    `<span class="pl-name">${esc(comp.label || comp.id)}</span>` +
    (comp.description ? `<span class="pl-desc">${esc(comp.description)}</span>` : "") +
    `</label></div>`;
}

function renderTextInput(comp, disabled) {
  const off = disabled || comp.disabled === true;
  const maxLength = Math.max(0, Math.round(+comp.max_length || 0));
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
    duiLabel(comp) +
    `<input type="text" value="${esc(comp.value ?? "")}"${comp.placeholder ? ` placeholder="${esc(comp.placeholder)}"` : ""}${maxLength ? ` maxlength="${maxLength}"` : ""}${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""}>` +
    duiHint(comp) + `</div>`;
}

function renderNumberInput(comp, disabled) {
  const off = disabled || comp.disabled === true;
  const attr = (name, v) => {
    const n = +v;
    return (v != null && v !== "" && Number.isFinite(n)) ? ` ${name}="${n}"` : "";
  };
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
    duiLabel(comp) +
    `<input type="number" value="${esc(comp.value ?? "")}"${comp.placeholder ? ` placeholder="${esc(comp.placeholder)}"` : ""}${attr("min", comp.min_value)}${attr("max", comp.max_value)}${attr("step", comp.step)}${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""}>` +
    duiHint(comp) + `</div>`;
}

function renderTextarea(comp, disabled) {
  const off = disabled || comp.disabled === true;
  const rows = Math.max(1, Math.round(+comp.rows || 3));
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
    duiLabel(comp) +
    `<textarea rows="${rows}"${comp.placeholder ? ` placeholder="${esc(comp.placeholder)}"` : ""}${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""}>${esc(comp.value ?? "")}</textarea>` +
    duiHint(comp) + `</div>`;
}

async function loadDuiFormFields(pluginName, id, host) {
  let data = null;
  try {
    data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(pluginName) +
                     "&component_id=" + encodeURIComponent(id));
  } catch (e) {
    host.innerHTML = duiAlertHTML("err", "Fields unavailable", e.message);
    return;
  }
  const defs = (data && Array.isArray(data.fields)) ? data.fields : null;
  if (!defs) {
    host.innerHTML = duiAlertHTML("warn", "No fields",
      "the fields provider returned no definitions");
    return;
  }
  duiFormFields[id] = defs;
  const TYPE_MAP = {text: "text-input", number: "number-input",
                    textarea: "textarea", select: "select",
                    checkbox: "checkbox", multiselect: "multi-select",
                    radio: "radio-group"};
  host.innerHTML = defs.map(d => {
    if (!d || !d.id) return "";
    const comp = {
      type: TYPE_MAP[d.type] || "text-input",
      id: String(d.id),
      label: d.label != null ? String(d.label) : String(d.id),
      value: d.value,
      placeholder: d.placeholder,
      description: d.description,
      required: d.required === true,
      options: Array.isArray(d.options) ? d.options : [],
      min_value: d.min, max_value: d.max, step: d.step,
    };
    return renderDashboardUIComponent(comp, pluginName, false);
  }).join("") || `<span class="dim">no settings in the schema</span>`;
  // options-backed selects inside dynamic fields
}

async function loadDuiOptions(pluginName, id, wrapper) {
  let data = null;
  try {
    data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(pluginName) +
                     "&component_id=" + encodeURIComponent(id));
  } catch (e) { wrapper.innerHTML = ""; setFb(wrapper, "error", e.message); return; }
  if (!data || typeof data !== "object" || !Array.isArray(data.options)) return;
  const comp = duiComps[id] || {};
  const values = (Array.isArray(data.value) ? data.value : []).map(String);
  if (comp.type === "multi-select") {
    const host = wrapper.querySelector(".picklist");
    if (!host) return;
    host.innerHTML = data.options.map(o =>
      `<label class="pick-row">` +
      `<input type="checkbox" value="${esc(String(o.value ?? o))}"${values.includes(String(o.value ?? o)) ? " checked" : ""}>` +
      `<span class="pl-name">${esc(String(o.label ?? o.value ?? o))}</span></label>`).join("")
      || duiEmpty("no options");
    return;
  }
  if (comp.type === "radio-group") {
    const host = wrapper.querySelector(".picklist");
    if (!host) return;
    host.innerHTML = data.options.map(o =>
      `<label class="pick-row">` +
      `<input type="radio" name="dui-rg-${esc(id)}" value="${esc(String(o.value ?? o))}">` +
      `<span class="pl-name">${esc(String(o.label ?? o.value ?? o))}</span></label>`).join("")
      || duiEmpty("no options");
    return;
  }
  const sel = wrapper.querySelector("select");
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = `<option value=""></option>` + data.options.map(o =>
    `<option value="${esc(String(o.value ?? o))}">${esc(String(o.label ?? o.value ?? o))}</option>`).join("");
  if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
}

function duiOptions(comp) {
  return (comp.options || []).map(o => {
    const value = typeof o === "object" && o !== null ? o.value : o;
    const label = typeof o === "object" && o !== null ? (o.label ?? o.value) : o;
    return { value: String(value), label: String(label ?? value)};
  });
}

function renderSelect(comp, disabled) {
  const off = disabled || comp.disabled === true;
  const current = String(comp.value ?? "");
  if (comp.options_live) {
    // dynamic choices: populated by loadDuiOptions after the parse
    const live = `<option value="">loading…</option>`;
    if (comp.type === "radio-group") {
      return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
        duiLabel(comp) + `<div class="picklist">${live}</div>` + duiHint(comp) + `</div>`;
    }
    return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
      duiLabel(comp) + `<select${off ? " disabled" : ""} aria-label="${esc(comp.label || comp.id)}">${live}</select>` +
      duiHint(comp) + `</div>`;
  }
  if (comp.type === "radio-group") {
    const radios = duiOptions(comp).map(o =>
      `<label class="pick-row">` +
      `<input type="radio" name="dui-rg-${esc(comp.id)}" value="${esc(o.value)}"${o.value === current ? " checked" : ""}${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""}>` +
      `<span class="pl-name">${esc(o.label)}</span></label>`).join("");
    return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
      duiLabel(comp) + `<div class="picklist">${radios || duiEmpty("no options")}</div>` + duiHint(comp) + `</div>`;
  }
  const opts = duiOptions(comp).map(o =>
    `<option value="${esc(o.value)}"${o.value === current ? " selected" : ""}>${esc(o.label)}</option>`).join("");
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
    duiLabel(comp) +
    `<select${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""} aria-label="${esc(comp.label || comp.id)}">${opts}</select>` +
    duiHint(comp) + `</div>`;
}

function renderMultiSelect(comp, disabled) {
  const off = disabled || comp.disabled === true;
  const values = (Array.isArray(comp.value) ? comp.value : []).map(String);
  const checks = duiOptions(comp).map(o =>
    `<label class="pick-row">` +
    `<input type="checkbox" value="${esc(o.value)}"${values.includes(o.value) ? " checked" : ""}${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""}>` +
    `<span class="pl-name">${esc(o.label)}</span></label>`).join("");
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)} data-dui-field="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
    duiLabel(comp) + `<div class="picklist">${checks || duiEmpty("no options")}</div>` + duiHint(comp) + `</div>`;
}

/* ── Data leaves (body filled by loadDUIData) ─────────────────────────────── */
function renderListView(comp) {
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    (comp.title ? `<div class="dui-sec-title">${esc(comp.title)}</div>` : "") +
    `<div class="dui-data" data-dui-body="${esc(comp.id)}"></div></div>`;
}

function renderTable(comp) {
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    (comp.title ? `<div class="dui-sec-title">${esc(comp.title)}</div>` : "") +
    `<div class="dui-data" data-dui-body="${esc(comp.id)}"></div></div>`;
}

function renderTileView(comp) {
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    (comp.title ? `<div class="dui-sec-title">${esc(comp.title)}</div>` : "") +
    `<div class="dui-data" data-dui-body="${esc(comp.id)}"></div></div>`;
}

/* ── Forms ─────────────────────────────────────────────────────────────────── */
function renderForm(comp, pluginName, disabled) {
  const off = disabled || comp.disabled === true;
  const fields = (comp.children || []).filter(c => DUI_INPUT_TYPES.includes(c.type));
  let body = fields.map(f => renderDashboardUIComponent(f, pluginName, off)).join("");
  // field GRID: a declared columns count (or auto-fill) turns the form
  // tower into a compact grid; submit stays on its own full-width row
  let gridStyle = "";
  if (comp.columns === "auto")
    gridStyle = `grid-template-columns:repeat(auto-fill, minmax(220px, 1fr));`;
  else {
    const n = Math.round(+comp.columns);
    if (Number.isFinite(n) && n >= 1 && n <= 6)
      gridStyle = `grid-template-columns:repeat(${n}, minmax(0, 1fr));`;
  }
  if (gridStyle)
    body = `<div class="dui-form-grid" style="${gridStyle}">${body}</div>`;
  if (comp.fields_live)
    body = `<div class="dui-form-grid dui-fields-live" data-dui-fields="${esc(comp.id)}">` +
           `<span class="dim">loading fields…</span></div>` + body;
  return `<div class="dui-form" data-dui-comp="${esc(comp.id)}" data-dui-form="${esc(comp.id)}"${off ? ' data-dui-disabled="1"' : ""}>` +
    (comp.title ? `<div class="dui-sec-title">${esc(comp.title)}</div>` : "") +
    (comp.description ? `<p class="sub">${esc(comp.description)}</p>` : "") +
    body +
    `<div class="form-actions"><button type="button" class="btn btn-primary" data-dui-submit="${esc(comp.id)}"${off ? " disabled" : ""}>${esc(comp.submit_label || "Submit")}</button></div>` +
    `</div>`;
}

function renderUnknown(comp) {
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}>` +
    `<div class="dui-unknown sub">unsupported component type "${esc(comp.type || "?")}"</div></div>`;
}

/* ── Wiring (runs once per rendered page, AFTER the innerHTML parse) ───────── */
function wireDashboardUI(root, pluginName) {
  // buttons: dispatch + inline feedback; confirm-gated when declared
  root.querySelectorAll("[data-dui-act]").forEach(el => {
    const id = el.dataset.duiAct;
    const host = el.closest("[data-dui-comp]") || el.parentElement;
    // USER FIX DIRECTIVE #1: anchor under the BUTTON'S OWN ROW — the
    // component host can be a tall section (fleet controls, Appearance
    // and Dashboard cards), which dropped the prompt at its bottom edge
    const anchor = typeof fbAnchorFor === "function" ? fbAnchorFor(el) : host;
    // USER DIRECTIVE: remove/delete-type outcomes are HIGH-PRIORITY —
    // they surface the 5s header prompt, not a component chip.
    const removalLike = /^(remove|delete|unload|forget|reload|estop)$/i.test(id || "");
    const highPriority = (m) => removalLike || /\b(created|added|removed|deleted)\b/i.test(m || "");
    const fire = async () => {
      try {
        const res = await withBusy(el, dispatchUIAction(pluginName, id, {}));
        if (!res) return;
        if (res.field_errors && Object.keys(res.field_errors).length)
          setFbFloat(host, "error", Object.values(res.field_errors).join("; "), anchor, el);
        else if (res.message)
          res.success
            ? (highPriority(res.message) ? showHeaderPrompt(res.message)
                           : flashFbFloat(host, res.message, anchor, el))
            : setFbFloat(host, "error", res.message, anchor, el);
        else if (res.success !== false && removalLike)
          showHeaderPrompt("removed");
      } catch (e) { setFbFloat(host, "error", e.message, anchor, el); }
    };
    const comp = duiComps[id] || {};
    el.onclick = () => {
      if (el.dataset.duiOpen) { openDuiModal(el.dataset.duiOpen); return; }
      if (comp.confirm) confirmAction("Confirm", comp.confirm, fire);
      else fire();
    };
  });

  // standalone inputs with declared actions: dispatch {value} on change
  root.querySelectorAll("[data-dui-in]").forEach(el => {
    const id = el.dataset.duiIn;
    const comp = duiComps[id] || {};
    const host = el.closest("[data-dui-comp]") || el.parentElement;
    const anchor = typeof fbAnchorFor === "function" ? fbAnchorFor(el) : host;
    el.addEventListener("change", async () => {
      try {
        const res = await dispatchUIAction(pluginName, id, { value: duiFieldValue(comp, host) });
        if (res && res.message)
          res.success ? flashFbFloat(host, res.message, anchor, el) : setFbFloat(host, "error", res.message, anchor, el);
      } catch (e) { setFbFloat(host, "error", e.message, anchor, el); }
    });
  });

  // row-action buttons in STATIC markup (data-body ones are wired by
  // loadDUIData when their table renders)
  wireDUIRowActions(root, pluginName);

  // dynamic options: selects/multi-selects with an options provider
  root.querySelectorAll("[data-dui-field]").forEach(el => {
    const id = el.dataset.duiField;
    const comp = duiComps[id] || {};
    if (comp.options_live) loadDuiOptions(pluginName, id, el);
  });

  // dynamic form fields: forms with a fields provider (schema-driven)
  root.querySelectorAll("[data-dui-fields]").forEach(el => {
    loadDuiFormFields(pluginName, el.dataset.duiFields, el);
  });

  // live leaves (metrics / texts with data providers): first fetch
  loadDUILeaves(root, pluginName);

  // forms: collect field values client-side, submit as one payload
  root.querySelectorAll("[data-dui-form]").forEach(formEl => {
    const id = formEl.dataset.duiForm;
    const comp = duiComps[id] || {};
    const btn = formEl.querySelector('[data-dui-submit="' + id + '"]');
    if (!btn) return;
    // user directive #1: form feedback anchors under the SUBMIT ROW
    // (the component the user acted on), never the whole form's bottom
    const fbRow = btn.closest(".dui-row, .form-actions, .dui-toolbar") || btn.parentElement;
    btn.onclick = async () => {
      const payload = {};
      formEl.querySelectorAll("[data-dui-field] .fb").forEach(fb => fb.remove());
      const defs = [...(comp.children || []),
                    ...(duiFormFields[id] || [])];
      for (const field of defs) {
        if (!DUI_INPUT_TYPES.includes(field.type) || !field.id) continue;
        const value = duiFieldValue(field, formEl);
        // client-side validation (PR #11): flag the offending field
        // inline and abort before the POST — a usability mirror of the
        // authoritative server-side schema validation
        const err = duiValidateField(field, value);
        if (err) {
          const slot = formEl.querySelector('[data-dui-field="' + field.id + '"]');
          if (slot) setFb(slot, "error", err);
          return;
        }
        payload[field.id] = value;
      }
      // client-side forms never dispatch: the dashboard's own token
      // editor (the token must not round-trip a server action) and the
      // decorative theme picker (pure presentation, nothing to persist
      // server-side)
      if (comp.client_action === "save-token") {
        const tok = String(payload["token-value"] || "").trim();
        if (!tok) { setFbFloat(formEl, "error", "Token required.", fbRow, btn); return; }
        secretStore.setItem("dash_token", tok);
        wsRetries = 0; connectWS();
        // QA ticket 1c: reload the surfaces that failed pre-auth so the
        // save recovers the whole shell without a manual refresh
        Promise.allSettled([
          typeof loadDashboardUINav === "function" ? loadDashboardUINav() : null,
          typeof loadPluginUI === "function" ? loadPluginUI() : null,
        ]).then(() => { buildNav(); render(); });
        flashFbFloat(formEl, "token saved — reconnecting…", fbRow, btn);
        return;
      }
      if (comp.client_action === "save-theme") {
        applyDashTheme(String(payload["theme-accent"] || ""),
                       String(payload["theme-density"] || ""));
        flashFbFloat(formEl, "theme applied", fbRow, btn);
        return;
      }
      try {
        const res = await withBusy(btn, dispatchUIAction(pluginName, id, payload));
        formEl.querySelectorAll("[data-dui-field] .fb").forEach(fb => fb.remove());
        if (!res) return;
        if (res.field_errors)
          for (const fid of Object.keys(res.field_errors)) {
            const slot = formEl.querySelector('[data-dui-field="' + fid + '"]');
            if (slot) setFb(slot, "error", res.field_errors[fid]);
          }
        if (res.message) {
          const highPriority =
            /\b(created|added|removed|deleted)\b/i.test(res.message || "");
          res.success
            ? (highPriority ? showHeaderPrompt(res.message)
                            : flashFbFloat(formEl, res.message, fbRow, btn))
            : setFbFloat(formEl, "error", res.message, fbRow, btn);
        }
        if (comp.close_on_submit && res.success !== false
            && !(res.field_errors && Object.keys(res.field_errors).length)) {
          const dlg = formEl.closest("dialog");
          if (dlg) dlg.close();
        }
      } catch (e) { setFbFloat(formEl, "error", e.message, fbRow, btn); }
    };
  });

  // data components: first fetch with skeleton → data/empty/error. The
  // plugin slug on the body scopes refresh targeting to this page's
  // plugin (component ids are unique per plugin, not globally).
  root.querySelectorAll("[data-dui-body]").forEach(el => {
    el.dataset.duiPlugin = duiSlug(pluginName);
    loadDUIData(pluginName, el.dataset.duiBody, el);
  });
}

/* Fetch every live leaf under a root (initial render). */
function loadDUILeaves(root, pluginName) {
  root.querySelectorAll("[data-dui-live]").forEach(el =>
    loadDUILive(pluginName, el.dataset.duiLive, el));
}

/* One field's current value from the DOM (form collection + input actions). */
function duiFieldValue(comp, scope) {
  const field = scope.querySelector('[data-dui-field="' + (comp && comp.id) + '"]');
  if (!field) return null;
  if (comp.type === "multi-select")
    return [...field.querySelectorAll('input[type="checkbox"]:checked')].map(i => i.value);
  if (comp.type === "radio-group") {
    const radio = field.querySelector('input[type="radio"]:checked');
    return radio ? radio.value : null;
  }
  const input = field.querySelector("input, select, textarea");
  if (!input) return null;
  if (comp.type === "checkbox" || comp.type === "toggle") return input.checked;
  if (comp.type === "number-input") {
    if (input.value === "" || input.value == null) return null;
    const n = parseFloat(input.value);
    return Number.isFinite(n) ? n : null;
  }
  return input.value;
}

/* Client-side form validation (mirrors collectProfileSettings in
   plugins-ui.js): required/non-empty, number bounds, text length.
   Returns "" when the value is acceptable, else the inline message. */
function duiValidateField(field, value) {
  if (field.required) {
    const empty = (field.type === "checkbox" || field.type === "toggle")
      ? value !== true
      : value == null || value === "" || (Array.isArray(value) && !value.length);
    if (empty) return `${field.label || field.id} is required.`;
  }
  if (field.type === "number-input" && value != null) {
    if (field.min_value != null && value < +field.min_value)
      return `${field.label || field.id} must be at least ${field.min_value}.`;
    if (field.max_value != null && value > +field.max_value)
      return `${field.label || field.id} must be at most ${field.max_value}.`;
  }
  if ((field.type === "text-input" || field.type === "textarea") && field.max_length &&
      String(value ?? "").length > +field.max_length)
    return `${field.label || field.id} must be at most ${field.max_length} characters.`;
  return "";
}
