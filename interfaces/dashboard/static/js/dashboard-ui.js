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

/* ── Navigation ────────────────────────────────────────────────────────────── */
async function loadDashboardUINav() {
  try { duiNav = await api("/api/dashboard-ui/navigation") || []; }
  catch { duiNav = []; }
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
  try {
    page = await api("/api/dashboard-ui/page?plugin=" + encodeURIComponent(entry.plugin) +
                     "&page_id=" + encodeURIComponent(entry.page_id));
  } catch (e) {
    if (seq !== duiSeq) return;
    panel.innerHTML = "";
    setFb(panel, "error", e.message);
    return;
  }
  if (seq !== duiSeq) return;  // a newer click superseded this fetch
  if (!page || page.type !== "page") {
    panel.innerHTML = "";
    setFb(panel, "error", "plugin page not found");
    return;
  }
  renderDashboardUIPage(page, entry.plugin, panel);
}

/* ── Page shell ────────────────────────────────────────────────────────────── */
function renderDashboardUIPage(page, pluginName, panel) {
  duiComps = {};
  duiDataCache = {};
  duiIndexComponents(page);
  let html = `<div class="dui-page">`;
  if (page.description)
    html += `<p class="sub dui-desc">${esc(page.description)}</p>`;
  html += (page.children || []).map(c => renderDashboardUIComponent(c, pluginName)).join("");
  html += `</div>`;
  panel.innerHTML = html || '<div class="dui-page"><p class="dim">This page has no content.</p></div>';
  wireDashboardUI(panel, pluginName);
}

function duiIndexComponents(comp) {
  if (!comp || typeof comp !== "object") return;
  if (comp.id) duiComps[comp.id] = comp;
  for (const child of (comp.children || [])) duiIndexComponents(child);
}

/* ── Action dispatch ───────────────────────────────────────────────────────── */
async function dispatchUIAction(pluginName, componentId, payload) {
  const result = await post("/api/dashboard-ui/action", {
    plugin: pluginName, component_id: componentId, payload: payload || {}
  });
  if (result && Array.isArray(result.refresh) && result.refresh.length)
    refreshDashboardUIData(pluginName, result.refresh);
  return result;
}

/* Re-fetch the listed component ids inside the plugin's active panel. */
function refreshDashboardUIData(pluginName, ids) {
  for (const id of (ids || [])) {
    const host = document.querySelector('[data-dui-body="' + id + '"][data-dui-plugin="' + duiSlug(pluginName) + '"]');
    if (host) loadDUIData(pluginName, id, host);
  }
}

/* ── Data providers (list-view / table) ────────────────────────────────────── */
async function loadDUIData(pluginName, compId, host, quiet) {
  if (!host) return;
  if (!quiet) host.innerHTML = `<div class="dui-skel" aria-hidden="true"><div></div><div></div></div>`;
  if (!quiet) clearFb(host.parentElement);
  let data = null;
  try {
    data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(pluginName) +
                     "&component_id=" + encodeURIComponent(compId));
  } catch (e) {
    host.innerHTML = "";
    setFb(host.parentElement || host, "error", e.message);
    return;
  }
  duiDataCache[compId] = data;
  const comp = duiComps[compId] || {};
  const html = comp.type === "table" ? duiTableHTML(comp, data, compId)
                                     : duiListHTML(comp, data);
  host.innerHTML = html || "";
  wireDUIMore(host, compId);
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
  const pageSize = Math.max(0, Math.round(+comp.page_size || 0));
  const visible = pageSize && rows.length > pageSize ? rows.slice(0, pageSize) : rows;
  // Row-action key resolution: the declared key column when it exists
  // among the columns, else the "__key" pseudo-cell PAST the visible
  // columns (composite or display-cleaned keys). Cells render strictly
  // by column index so the extra cell never shows.
  const keyCol = String(comp.key_column || "");
  let keyIdx = keyCol ? columns.indexOf(keyCol) : -1;
  if (keyCol === "__key" && keyIdx < 0) keyIdx = columns.length;
  const rowActions = Array.isArray(comp.row_actions) ? comp.row_actions : [];
  const head = columns.map(c => `<th>${esc(c)}</th>`).join("")
    + (rowActions.length ? `<th></th>` : "");
  const body = visible.map(r => {
    const cells = columns.map((_, i) => `<td>${duiCell(r ? r[i] : "")}</td>`).join("");
    let actionCell = "";
    if (rowActions.length) {
      const key = keyIdx >= 0 && r ? String(r[keyIdx] ?? "") : "";
      actionCell = `<td class="dui-rowacts">` + rowActions.map(a =>
        `<button type="button" class="btn btn-sm btn-${esc(a.variant || "secondary")}"` +
        ` data-dui-rowact="${esc(compId)}" data-dui-raction="${esc(a.id)}"` +
        ` data-dui-row="${esc(key)}"${a.confirm ? ` title="${esc(a.confirm)}"` : ""}>${esc(a.label)}</button>`
      ).join(" ") + `</td>`;
    }
    return `<tr>${cells}${actionCell}</tr>`;
  }).join("");
  const more = pageSize && rows.length > pageSize
    ? `<button type="button" class="btn btn-ghost btn-sm" data-dui-more="${esc(compId)}">show all ${rows.length} rows</button>`
    : "";
  return `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>${more}`;
}

/* Reveal the rows a page_size hid (works off the cached provider result). */
function wireDUIMore(host, compId) {
  const btn = host.querySelector("[data-dui-more]");
  if (!btn) return;
  btn.onclick = () => {
    const comp = duiComps[compId] || {};
    const fresh = duiTableHTML({ ...comp, page_size: 0 }, duiDataCache[compId], compId);
    const box = host.querySelector(".dui-data") || host;
    box.innerHTML = fresh;
    wireDUIMore(box, compId);
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
    case "text": return renderText(comp);
    case "display-text": return renderDisplayText(comp);
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
  return `<div class="dui-section" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
    (comp.title ? `<div class="dui-sec-title">${esc(comp.title)}</div>` : "") +
    (comp.description ? `<p class="sub">${esc(comp.description)}</p>` : "") +
    duiChildren(comp, pluginName, disabled) + `</div>`;
}

function renderCard(comp, pluginName, disabled) {
  return `<div class="card dui-card" data-dui-comp="${esc(comp.id)}"${duiBoxStyle(comp)}${disabled ? ' data-dui-disabled="1"' : ""}>` +
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
  return `<div class="dui-item" data-dui-comp="${esc(comp.id)}"${duiItemStyle(comp)}${off ? ' data-dui-disabled="1"' : ""}>` +
    `<button type="button" class="btn btn-${esc(variant)}${comp.loading ? " busy" : ""}" data-dui-act="${esc(comp.id)}"${off || comp.loading ? " disabled" : ""}>${esc(comp.label || comp.id)}</button>` +
    duiHint(comp) + `</div>`;
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
    `<select${off ? " disabled" : ""}${comp.action === true ? ` data-dui-in="${esc(comp.id)}"` : ""}>${opts}</select>` +
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

/* ── Forms ─────────────────────────────────────────────────────────────────── */
function renderForm(comp, pluginName, disabled) {
  const off = disabled || comp.disabled === true;
  const fields = (comp.children || []).filter(c => DUI_INPUT_TYPES.includes(c.type));
  const body = fields.map(f => renderDashboardUIComponent(f, pluginName, off)).join("");
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
    const fire = async () => {
      try {
        const res = await withBusy(el, dispatchUIAction(pluginName, id, {}));
        if (!res) return;
        if (res.field_errors && Object.keys(res.field_errors).length)
          setFb(host, "error", Object.values(res.field_errors).join("; "));
        else if (res.message)
          res.success ? flashFb(host, res.message) : setFb(host, "error", res.message);
      } catch (e) { setFb(host, "error", e.message); }
    };
    const comp = duiComps[id] || {};
    el.onclick = () => {
      if (comp.confirm) confirmAction("Confirm", comp.confirm, fire);
      else fire();
    };
  });

  // standalone inputs with declared actions: dispatch {value} on change
  root.querySelectorAll("[data-dui-in]").forEach(el => {
    const id = el.dataset.duiIn;
    const comp = duiComps[id] || {};
    const host = el.closest("[data-dui-comp]") || el.parentElement;
    el.addEventListener("change", async () => {
      try {
        const res = await dispatchUIAction(pluginName, id, { value: duiFieldValue(comp, host) });
        if (res && res.message)
          res.success ? flashFb(host, res.message) : setFb(host, "error", res.message);
      } catch (e) { setFb(host, "error", e.message); }
    });
  });

  // table row-action buttons: dispatch {"action", "row"} through the
  // TABLE's own action channel (the server routes to the RowAction)
  root.querySelectorAll("[data-dui-rowact]").forEach(el => {
    const compId = el.dataset.duiRowact;
    const comp = duiComps[compId] || {};
    const def = (comp.row_actions || []).find(a => a.id === el.dataset.duiRaction) || {};
    const host = el.closest(".tbl-wrap") || el.parentElement;
    const fire = async () => {
      try {
        const res = await withBusy(el, dispatchUIAction(pluginName, compId,
          { action: el.dataset.duiRaction, row: el.dataset.duiRow }));
        if (!res) return;
        if (res.field_errors && Object.keys(res.field_errors).length)
          setFb(host, "error", Object.values(res.field_errors).join("; "));
        else if (res.message)
          res.success ? flashFb(host, res.message) : setFb(host, "error", res.message);
      } catch (e) { setFb(host, "error", e.message); }
    };
    el.onclick = () => {
      if (def.confirm) confirmAction("Confirm", def.confirm, fire);
      else fire();
    };
  });

  // live leaves (metrics / texts with data providers): first fetch
  loadDUILeaves(root, pluginName);

  // forms: collect field values client-side, submit as one payload
  root.querySelectorAll("[data-dui-form]").forEach(formEl => {
    const id = formEl.dataset.duiForm;
    const comp = duiComps[id] || {};
    const btn = formEl.querySelector('[data-dui-submit="' + id + '"]');
    if (!btn) return;
    btn.onclick = async () => {
      const payload = {};
      for (const field of (comp.children || [])) {
        if (!DUI_INPUT_TYPES.includes(field.type) || !field.id) continue;
        payload[field.id] = duiFieldValue(field, formEl);
      }
      // client-side forms never dispatch: the only one is the dashboard's
      // own token editor (the token must not round-trip a server action)
      if (comp.client_action === "save-token") {
        const tok = String(payload["token-value"] || "").trim();
        if (!tok) { setFb(formEl, "error", "Token required."); return; }
        secretStore.setItem("dash_token", tok);
        wsRetries = 0; connectWS();
        flashFb(formEl, "token saved — reconnecting…");
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
        if (res.message)
          res.success ? flashFb(formEl, res.message) : setFb(formEl, "error", res.message);
      } catch (e) { setFb(formEl, "error", e.message); }
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
