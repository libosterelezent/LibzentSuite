"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   shell.js — boot + app chrome. Builds the sidebar (6 main tabs + the
   PLUGINS section from builder navigation), the topbar (crumb/title, search,
   connection dot, theme button), attaches the router to #view, opens the
   WebSocket, and seeds the store. Loaded LAST.
   ═══════════════════════════════════════════════════════════════════════════ */

const NAV_MAIN = [
  {hash: "/home",     label: "Home",     icon: "M3 12l9-9 9 9v9a2 2 0 0 1-2 2h-4v-7h-6v7H5a2 2 0 0 1-2-2z"},
  {hash: "/accounts", label: "Accounts", icon: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"},
  {hash: "/activity", label: "Activity", icon: "M22 12h-4l-3 9L9 3l-3 9H2"},
  {hash: "/policies", label: "Policies", icon: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"},
  {hash: "/admin",    label: "Admin",    icon: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z"},
  {hash: "/tools",    label: "Tools",    icon: "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"},
];

function buildChrome() {
  const app = $("app");

  /* ── sidebar ── */
  const snav = $("snav");
  const renderNav = () => {
    snav.textContent = "";
    snav.append(el("div", {class: "snav-label"}, "Workspace"));
    for (const n of NAV_MAIN) {
      snav.append(el("a", {class: "snav-item", href: "#" + n.hash, dataset: {hash: n.hash}},
        el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
                   "stroke-width": "2", "aria-hidden": "true"}, el("path", {d: n.icon})),
        el("span", {}, n.label)));
    }
    const pluginPages = (storeGet("navigation") || []).filter(p => p.plugin !== "$framework");
    if (pluginPages.length) {
      snav.append(el("div", {class: "snav-label"}, "Plugins"));
      for (const p of pluginPages) {
        snav.append(el("a", {
          class: "snav-item snav-plugin",
          href: `#/plugins/${encodeURIComponent(p.plugin)}/${encodeURIComponent(p.page_id)}`,
        }, el("span", {}, p.title)));
      }
      snav.append(el("a", {class: "snav-item snav-dim", href: "#/plugins"},
        el("span", {}, "manage plugins…")));
    }
  };
  renderNav();
  storeSubscribe((key) => { if (key === "navigation") renderNav(); });

  /* active tab highlight follows the route */
  Router.onRoute((route, params) => {
    $$(".snav-item").forEach(a => a.classList.toggle("on",
      a.dataset.hash === location.hash ||
      (a.dataset.hash === "/accounts" && location.hash.startsWith("#/accounts"))));
    const crumb = $("page-crumb"), title = $("page-title");
    if (route) {
      const leaf = route.pattern.split("/").filter(Boolean)[0] || "";
      const nav = NAV_MAIN.find(n => n.hash === "/" + leaf);
      crumb.textContent = nav ? nav.label : leaf;
      const t = params && params.name ? params.name : (route.title || "");
      title.textContent = t;
      /* "Home Home" duplication: hide the crumb when it equals the title */
      crumb.style.display = t === crumb.textContent ? "none" : "";
    }
  });

  /* collapse rail (desktop) + drawer (mobile) */
  const sideToggle = $("side-toggle");
  if (sideToggle) sideToggle.onclick = () => {
    const collapsed = document.body.classList.toggle("side-rail");
    prefSet("side_collapsed", collapsed ? "1" : "0");
  };
  if (prefGet("side_collapsed", "0") === "1") document.body.classList.add("side-rail");
  const menuBtn = $("menu-btn");
  if (menuBtn) menuBtn.onclick = () => document.body.classList.add("side-open");
  const backdrop = $("side-backdrop");
  if (backdrop) backdrop.onclick = () => document.body.classList.remove("side-open");
  const sideClose = $("side-close");
  if (sideClose) sideClose.onclick = () => document.body.classList.remove("side-open");

  /* ── topbar: connection dot ── */
  const connDot = $("conn-dot"), connMsg = $("conn-msg");
  const paintConn = s => {
    if (!connDot) return;
    connDot.className = "conn-dot " + (s === "online" ? "on" : s === "connecting" ? "mid" : "off");
    connMsg.textContent = s === "online" ? "live" : s === "connecting" ? "connecting…" :
      (token() ? "disconnected" : "token required");
  };
  paintConn(WS.status());
  WS.onStatus(paintConn);

  /* ── topbar: theme button (theme.js loads after us) ── */
  const themeBtn = $("theme-btn");
  if (themeBtn) themeBtn.onclick = () => {
    if (typeof themeOpen === "function") themeOpen();
    else toast("theme engine not loaded", "err");
  };

  /* ── topbar: search — accounts, groups, plugin pages; Ctrl+K focuses ── */
  const box = $("search"), results = $("search-results");
  const runSearch = () => {
    const q = (box.value || "").trim().toLowerCase();
    if (!q) { results.classList.remove("open"); results.textContent = ""; return; }
    const hits = [];
    for (const a of storeGet("accounts") || []) {
      if ((a.name || "").toLowerCase().includes(q))
        hits.push({label: a.name, sub: "account", href: "#/accounts/" + encodeURIComponent(a.name)});
    }
    for (const g of storeGet("groups") || []) {
      if ((g.name || "").toLowerCase().includes(q))
        hits.push({label: g.name, sub: "sync group", href: "#/accounts"});
    }
    for (const p of storeGet("navigation") || []) {
      if ((p.title || "").toLowerCase().includes(q) || (p.plugin || "").toLowerCase().includes(q))
        hits.push({label: p.title, sub: "plugin · " + p.plugin,
                   href: `#/plugins/${encodeURIComponent(p.plugin)}/${encodeURIComponent(p.page_id)}`});
    }
    results.textContent = "";
    for (const h of hits.slice(0, 8)) {
      results.append(el("a", {class: "search-hit", href: h.href},
        el("span", {}, h.label), el("span", {class: "dim"}, h.sub)));
    }
    if (!hits.length) results.append(el("div", {class: "search-hit dim"}, "no matches"));
    results.classList.add("open");
  };
  if (box) {
    box.addEventListener("input", runSearch);
    box.addEventListener("keydown", e => {
      if (e.key === "Escape") { box.value = ""; runSearch(); box.blur(); }
    });
    document.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); box.focus(); }
    });
    document.addEventListener("click", e => {
      if (!e.target.closest("#search-wrap")) results.classList.remove("open");
    });
  }
}

async function bootDashboard() {
  adoptTokenFromUrl();
  buildChrome();
  Router.attach($("view"));
  WS.connect();
  await Promise.allSettled([loadStatsNow(), loadGroups(), loadManifests(), loadNavigation()]);
}

document.addEventListener("DOMContentLoaded", bootDashboard);
