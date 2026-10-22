"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   shell.js — boot + app chrome. Builds the sidebar (6 main tabs + the
   PLUGINS section from builder navigation), the topbar (crumb/title, search,
   connection dot, theme button), attaches the router to #view, opens the
   WebSocket, and seeds the store. Loaded LAST.
   ═══════════════════════════════════════════════════════════════════════════ */

const NAV_MAIN = [
  {hash: "/home",     label: "Home",     icon: "home"},
  {hash: "/accounts", label: "Accounts", icon: "users"},
  {hash: "/activity", label: "Activity", icon: "activity"},
  {hash: "/policies", label: "Policies", icon: "shield"},
  {hash: "/admin",    label: "Admin",    icon: "admin"},
  {hash: "/tools",    label: "Tools",    icon: "tools"},
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
        ico(n.icon), el("span", {}, n.label)));
    }
    const pluginPages = (storeGet("navigation") || []).filter(p => p.plugin !== "$framework");
    if (pluginPages.length) {
      snav.append(el("div", {class: "snav-label"}, "Plugins"));
      for (const p of pluginPages) {
        /* plugin pages carry their own icon path from the builder — render
           it inline (same stroke language); icon-less pages fall back to
           the sprite plug symbol (never a bare dot) */
        const mark = p.icon
          ? el("svg", {class: "snav-ico", viewBox: "0 0 24 24", fill: "none",
                       stroke: "currentColor", "stroke-width": "2", "aria-hidden": "true"},
              el("path", {d: p.icon}))
          : ico("plug", "snav-ico");
        snav.append(el("a", {
          class: "snav-item snav-plugin",
          href: `#/plugins/${encodeURIComponent(p.plugin)}/${encodeURIComponent(p.page_id)}`,
        }, mark, el("span", {}, p.title)));
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
      /* proper-case the leaf for non-nav routes (plugin pages etc.) */
      const section = nav ? nav.label
        : leaf.charAt(0).toUpperCase() + leaf.slice(1);
      crumb.textContent = section;
      const t = params && params.name ? params.name : (route.title || "");
      title.textContent = t;
      /* single title in the header: hide the crumb when it duplicates the
         title (was case-sensitive: "plugins" vs "Plugins" both showed) */
      crumb.style.display =
        t.toLowerCase() === section.toLowerCase() ? "none" : "";
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
