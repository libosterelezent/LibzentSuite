"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   shell.js — boot + app chrome. Builds the sidebar (6 main tabs + the
   PLUGINS section from builder navigation), the topbar (crumb/title, search,
   connection dot, theme button), attaches the router to #view, opens the
   WebSocket, and seeds the store. Loaded LAST.
   ═══════════════════════════════════════════════════════════════════════════ */

const NAV_MAIN = [
  {hash: "/home", label: "Overview", icon: "home", area: "workspace"},
  {hash: "/accounts", label: "Accounts", icon: "users", area: "workspace"},
  {hash: "/activity", label: "Activity & Tasks", icon: "activity", area: "workspace"},
  {hash: "/groups", label: "Fleet", icon: "bolt", area: "workspace"},
  {hash: "/policies", label: "Policies", icon: "shield", area: "workspace"},
  {hash: "/plugins", label: "Plugins", icon: "plug", area: "workspace"},
  {hash: "/tools", label: "Tools & Diagnostics", icon: "tools", area: "system"},
  {hash: "/settings", label: "Settings", icon: "gear", area: "system"},
];

function buildChrome() {
  const app = $("app");

  /* ── sidebar ── */
  const snav = $("snav");
  const workspaceSwitcher = el("div", {class: "workspace-switcher", "aria-label": "Current workspace"},
    el("span", {class: "workspace-monogram"}, "LZ"),
    el("span", {class: "workspace-info"},
      el("strong", {}, "Personal workspace"),
      el("small", {}, "Personal · Local")),
    el("span", {class: "workspace-caret", "aria-hidden": "true"}, "⌄"));
  snav.before(workspaceSwitcher);

  /* Move the live socket status into the persistent sidebar footer, as in
     the Figma shell. The same DOM nodes are retained so status updates keep
     using the existing WS.onStatus handler. */
  const sideFoot = $("side-foot");
  const conn = $("conn");
  const sideToggle = $("side-toggle");
  sideFoot.textContent = "";
  const runtime = el("div", {class: "side-runtime"},
    el("div", {class: "side-runtime-caption"}, "Local instance"),
    conn,
    el("div", {class: "side-runtime-endpoint"}, (location.host || "localhost") + " · local"));
  const profile = el("div", {class: "side-profile"},
    el("span", {class: "side-avatar"}, "LZ"),
    el("span", {class: "side-profile-copy"},
      el("strong", {}, "Personal workspace"),
      el("small", {}, "Workspace owner")),
    sideToggle);
  sideFoot.append(runtime, profile);

  const renderNav = () => {
    snav.textContent = "";
    for (const area of ["workspace", "system"]) {
      const items = NAV_MAIN.filter(n => n.area === area);
      snav.append(el("div", {class: "snav-label"}, area === "workspace" ? "Workspace" : "System"));
      for (const n of items) {
        const a = el("a", {class: "snav-item", href: "#" + n.hash, dataset: {hash: n.hash}},
          ico(n.icon), el("span", {}, n.label));
        const count = n.hash === "/accounts" ? (storeGet("accounts") || []).length
          : n.hash === "/activity" ? (storeGet("tasks") || []).filter(t => ["queued", "running"].includes(String(t.status || ""))).length : 0;
        if (count > 0) a.append(el("span", {class: "snav-count"}, String(count)));
        snav.append(a);
      }
    }

    const pluginPages = (storeGet("navigation") || []).filter(p => p.plugin !== "$framework");
    if (pluginPages.length) {
      snav.append(el("div", {class: "snav-label"}, "Plugin pages"));
      for (const p of pluginPages) {
        const mark = p.icon
          ? el("svg", {class: "snav-ico", viewBox: "0 0 24 24", fill: "none",
                       stroke: "currentColor", "stroke-width": "2", "aria-hidden": "true"},
              el("path", {d: p.icon}))
          : ico("plug", "snav-ico");
        snav.append(el("a", {
          class: "snav-item snav-plugin",
          href: "#/plugins/" + encodeURIComponent(p.plugin) + "/" + encodeURIComponent(p.page_id),
        }, mark, el("span", {}, p.title)));
      }
    }
  };
  renderNav();
  storeSubscribe((key) => {
    if (["navigation", "accounts", "tasks"].includes(key)) renderNav();
  });

  /* active tab highlight follows the route */
  Router.onRoute((route, params) => {
    const currentPath = (location.hash || "#/home").replace(/^#/, "");
    $(".snav-item").forEach(a => {
      const path = a.dataset.hash || "";
      a.classList.toggle("on", currentPath === path || currentPath.startsWith(path + "/"));
    });
    const crumb = $("page-crumb"), title = $("page-title");
    if (route) {
      const t = params && params.name ? params.name : (route.title || "");
      crumb.textContent = "Workspace";
      crumb.style.display = "";
      title.textContent = t;
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
  const helpBtn = $("help-btn");
  if (helpBtn) helpBtn.onclick = () => navigate("/tools");
  const notificationsBtn = $("notifications-btn");
  if (notificationsBtn) notificationsBtn.onclick = () => navigate("/activity");

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
