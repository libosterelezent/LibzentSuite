"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   shell.js — application-shell behaviour that does not belong to any one
   page: light/dark theme, collapsible sidebar rail, mobile drawer
   (backdrop, close button, swipe, scroll lock), deep links (#view +
   browser back/forward), document title, rail tooltips. (The "/" search shortcut lives in app.js wire() with the
   other global key handlers.)

   Loaded LAST (after app.js): it wraps UI.go() instead of editing it, so
   page code stays unaware of the shell. Preferences are non-sensitive and
   live in localStorage (like the accent/density theme layer).
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
  const root = document.documentElement;

  /* ── Theme: auto (follow the OS) → light/dark once the user picks ───────── */
  function applyTheme(mode) {
    if (mode === "light" || mode === "dark") root.dataset.theme = mode;
    else delete root.dataset.theme;
  }
  function currentIsLight() {
    if (root.dataset.theme) return root.dataset.theme === "light";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  }
  applyTheme(store.getItem("dash_theme"));
  const themeBtn = $("theme-btn");
  if (themeBtn) themeBtn.onclick = () => {
    const next = currentIsLight() ? "dark" : "light";
    store.setItem("dash_theme", next);
    applyTheme(next);
  };

  /* ── Sidebar rail (desktop) ─────────────────────────────────────────────── */
  function applySide(mode) {
    if (mode === "rail") root.dataset.side = "rail"; else delete root.dataset.side;
    const t = $("side-toggle");
    if (t) {
      const label = mode === "rail" ? "Expand sidebar" : "Collapse sidebar";
      t.title = label; t.setAttribute("aria-label", label);
    }
  }
  applySide(store.getItem("dash_side"));
  const sideToggle = $("side-toggle");
  if (sideToggle) sideToggle.onclick = () => {
    const next = root.dataset.side === "rail" ? "full" : "rail";
    store.setItem("dash_side", next);
    applySide(next);
  };

  /* ── Mobile drawer ──────────────────────────────────────────────────────── */
  const sidebar = $("sidebar"), backdrop = $("side-backdrop");
  function closeDrawer() { sidebar.classList.remove("open"); }
  function syncDrawer() {
    const open = sidebar.classList.contains("open");
    if (backdrop) backdrop.classList.toggle("open", open);
    document.body.classList.toggle("drawer-open", open);
    const mb = $("menu-btn");
    if (mb) mb.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { const first = sidebar.querySelector(".nav-item.active, .nav-item"); if (first) first.focus({preventScroll: true}); }
  }
  new MutationObserver(syncDrawer).observe(sidebar, {attributes: true, attributeFilter: ["class"]});
  if (backdrop) backdrop.onclick = closeDrawer;
  const sideClose = $("side-close");
  if (sideClose) sideClose.onclick = closeDrawer;
  // swipe left on the open drawer closes it
  let sx = null;
  sidebar.addEventListener("touchstart", e => { sx = e.touches[0].clientX; }, {passive: true});
  sidebar.addEventListener("touchend", e => {
    if (sx !== null && sx - e.changedTouches[0].clientX > 60) closeDrawer();
    sx = null;
  }, {passive: true});
  // leaving the mobile breakpoint must never strand a locked body
  window.matchMedia("(min-width: 768px)").addEventListener("change", e => { if (e.matches) closeDrawer(); });

  /* ── Rail tooltips: nav labels become titles (the text is hidden) ───────── */
  new MutationObserver(() => {
    sidebar.querySelectorAll(".nav-item").forEach(b => {
      const label = b.querySelector("span:not(.badge)");
      if (label && !b.title) b.title = label.textContent;
    });
  }).observe($("snav"), {childList: true});

  /* ── Title + deep links ─────────────────────────────────────────────────── */
  const BASE_TITLE = "LibzentSuite";
  function syncTitle() {
    const t = $("page-title").textContent;
    document.title = t ? `${t} · ${BASE_TITLE}` : BASE_TITLE;
  }
  let fromHash = false;
  const go = UI.go.bind(UI);
  UI.go = function (view) {
    go(view);
    syncTitle();
    if (!fromHash && location.hash !== "#" + view) history.pushState(null, "", "#" + view);
  };
  function routeFromHash() {
    const view = decodeURIComponent(location.hash.slice(1));
    if (!view || view === currentView) return;
    if (!NAV.some(n => n.id === view)) return;   // unknown / plugin page not loaded yet
    fromHash = true;
    try { UI.go(view); } finally { fromHash = false; }
  }
  window.addEventListener("hashchange", routeFromHash);
  // NAV arrives asynchronously from /api/dashboard-ui/navigation: honour an
  // initial #view as soon as it exists (bounded wait — never a busy loop)
  let waited = 0;
  const poll = setInterval(() => {
    waited += 100;
    if (NAV.length || waited > 6000) {
      clearInterval(poll); routeFromHash();
      // the first UI.go() ran before NAV existed and fell back to the raw id
      const entry = NAV.find(n => n.id === currentView);
      if (entry) $("page-title").textContent = entry.label;
      syncTitle();
    }
  }, 100);
})();
