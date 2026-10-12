"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   plugin-ui.js — renders plugin-contributed UI: Home stat cards and
   sidebar panels. Plugins provide STRUCTURED data (never HTML); every
   value is esc()'d at render time by this module's own templates.
   ═══════════════════════════════════════════════════════════════════════════ */

let pluginPanels = [];

async function loadPluginUI() {
  try {
    pluginPanels = await api("/api/ui/panels") || [];
  } catch { pluginPanels = []; }
  buildPluginNav();
  // Builder-based plugin pages (Stage D) load after the simple panels —
  // an additional surface, both stay alive. Fire-and-forget: it catches
  // its own failure so a dead endpoint can never block boot.
  loadDashboardUINav();
}

/* ── Home stat cards (called by views.js renderHome) ─────────────────── */
async function loadPluginStats(account) {
  if (!account) return [];
  try { return await api("/api/ui/stats?account=" + encodeURIComponent(account)) || []; }
  catch { return []; }
}

function pluginStatsHTML(cards) {
  if (!cards || !cards.length) return "";
  return cards.map(c => `<div class="metric">
    <div class="label">${esc(c.label)}</div>
    <div class="value ${esc(c.level || "")}">${esc(c.value)}</div>
    ${c.sub ? `<div class="sub">${esc(c.sub)}</div>` : ""}
    <div class="sub faint" style="font-size:9px">${esc(c.plugin)}</div>
  </div>`).join("");
}

/* ── Sidebar panels ───────────────────────────────────────────────────── */
function buildPluginNav() {
  // remove any previous plugin nav entries + panels (idempotent)
  $$("[data-plugin-panel]").forEach(el => el.remove());
  if (!pluginPanels.length) return;   // no simple panels — no nav entries
  for (const panel of pluginPanels) {
    const pid = panel.id.replace(/[^a-zA-Z0-9_-]/g, "-");
    // nav entry
    const a = document.createElement("button");
    a.className = "nav-item" + (currentView === "plugin-" + pid ? " active" : "");
    a.dataset.view = "plugin-" + pid;
    a.dataset.pluginPanel = "1";
    const iconPath = panel.icon || "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z";
    a.innerHTML = `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="${esc(iconPath)}"/></svg><span>${esc(panel.title)}</span>`;
    a.onclick = () => showPluginPanel(pid, panel);
    $("snav").appendChild(a);
    // panel container
    const div = document.createElement("div");
    div.className = "panel";
    div.id = "panel-plugin-" + pid;
    div.dataset.pluginPanel = "1";
    $("content").appendChild(div);
  }
}

function showPluginPanel(pid, panel) {
  const el = $("panel-plugin-" + pid);
  if (!el) return;
  // currentView carries the plugin view id so render() — which runs on
  // every WS structural change — keeps THIS panel active instead of
  // bouncing the user back to the page they came from (same fix as the
  // dui-* pages in showDashboardUIPage).
  currentView = "plugin-" + pid;
  showPanel("panel-plugin-" + pid);
  el.innerHTML = renderPanelSections(panel);
  $("page-title").textContent = panel.title;
  $("sidebar").classList.remove("open");
  $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === "plugin-" + pid));
}

function renderPanelSections(panel) {
  let html = "";
  for (const sec of (panel.sections || [])) {
    if (sec.title) html += `<h4 style="margin:var(--sp-3) 0 var(--sp-2);font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em">${esc(sec.title)}</h4>`;
    const data = sec.data;
    switch (sec.kind) {
      case "metrics":
        html += `<div class="metrics">${pluginStatsHTML(data)}</div>`;
        break;
      case "table":
        if (data && data.columns && data.rows) {
          html += `<div class="tbl-wrap"><table><thead><tr>${data.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${data.rows.map(r => `<tr>${(r || []).map(v => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
        }
        break;
      case "list":
        if (Array.isArray(data)) {
          html += `<div style="display:flex;flex-direction:column;gap:4px">${data.map(kv => `<div class="row" style="padding:6px 10px;background:var(--surface2);border-radius:6px"><span class="dim">${esc(kv.label || "")}</span><span style="margin-left:auto;font-weight:500">${esc(kv.value || "")}</span></div>`).join("")}</div>`;
        }
        break;
      case "text":
      default:
        html += `<p class="sub" style="overflow-wrap:anywhere">${esc(typeof data === "string" ? data : JSON.stringify(data))}</p>`;
        break;
    }
  }
  return html || '<p class="dim">This panel has no content yet.</p>';
}
