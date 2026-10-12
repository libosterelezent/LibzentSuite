"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   framework-ui.js — the MAIN TABS as builder pages.

   Home, Plugins, Activity, Policies, Admin and Tools are no longer
   hand-coded panels: they are pages registered by core/framework_ui.py
   under the reserved plugin name "$framework" and rendered here with the
   SAME generic component renderer plugin pages use (dashboard-ui.js).
   The dashboard dogfoods its own UI API.

   Contract with app.js:
     * isFrameworkView(view)  — is this tab a framework page?
     * renderFrameworkTab(v)  — fetch + render the page into #panel-<v>
                               (seq-guarded against stale fetches)
     * refreshFrameworkData() — throttled in-place refresh of the ACTIVE
                               tab's data bodies + live leaves (no DOM
                               rebuild, forms survive); called from the
                               WS tick path and after actions.
   ═══════════════════════════════════════════════════════════════════════════ */

const FRAMEWORK_PLUGIN = "$framework";
const FRAMEWORK_VIEWS = new Set(["home", "fleet", "plugins", "activity",
                                "policies", "admin", "tools", "profiles"]);
let fwSeq = 0;
let fwLastRefresh = 0;

function isFrameworkView(view) {
  return FRAMEWORK_VIEWS.has(view);
}

async function renderFrameworkTab(view) {
  const panel = $("panel-" + view);
  if (!panel) return;
  // duiComps is a GLOBAL index of the LAST rendered page — every OTHER
  // framework view's marker must die here or returning to it would take
  // the quiet-refresh path against the wrong component index (tiles
  // rendering as list junk, dead buttons)
  $$(".panel[data-fw-rendered]").forEach(p => {
    if (p !== panel) delete p.dataset.fwRendered;
  });
  const seq = ++fwSeq;
  panel.innerHTML = `<div class="dui-skel" aria-hidden="true"><div></div><div></div><div></div></div>`;
  let page = null;
  try {
    page = await api("/api/dashboard-ui/page?plugin=" + encodeURIComponent(FRAMEWORK_PLUGIN) +
                     "&page_id=" + encodeURIComponent(view));
  } catch (e) {
    if (seq !== fwSeq) return;
    panel.innerHTML = duiAlertHTML("err", "Page unavailable",
                                   e.message || String(e));
    return;
  }
  if (seq !== fwSeq) return;   // a newer navigation superseded this fetch
  if (!page || page.type !== "page") {
    panel.innerHTML = duiAlertHTML("warn", "Page not found",
      "The framework page for this tab is missing — the dashboard server " +
      "may predate the builder migration.");
    return;
  }
  renderDashboardUIPage(page, FRAMEWORK_PLUGIN, panel);
  panel.dataset.fwRendered = view;   // REAL-TIME marker: quiet updates from here on
}

/* Refresh the active framework tab's dynamic parts IN PLACE: data bodies
   re-fetch quietly (no skeleton flash) and live metrics/texts update.
   Structural changes still go through render() → renderFrameworkTab. */
function refreshFrameworkData(force) {
  if (!isFrameworkView(currentView)) return;
  const now = Date.now();
  if (!force && now - fwLastRefresh < 1200) return;   // throttle the WS tick path
  fwLastRefresh = now;
  const panel = $("panel-" + currentView);
  if (!panel) return;
  panel.querySelectorAll("[data-dui-body]").forEach(el => {
    if (el.querySelector(".dui-skel")) return;   // still mid-load
    loadDUIData(FRAMEWORK_PLUGIN, el.dataset.duiBody, el, true);
  });
  loadDUILeaves(panel, FRAMEWORK_PLUGIN);
}
