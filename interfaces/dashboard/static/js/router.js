"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   router.js — hash router with mount/unmount lifecycle.
   Routes: "/accounts" exact, "/accounts/:name" params. registerRoute adds;
   navigate() goes there; hashchange auto-mounts. The active view's returned
   unmount() (or view.unmount) is awaited before the next mount — views own
   their store subscriptions and must clean them up.
   ═══════════════════════════════════════════════════════════════════════════ */

const Router = (() => {
  const routes = [];           // {pattern, segs, title, crumb, mount}
  let active = null;           // {route, params, unmount}
  let container = null;
  const routeSubs = [];
  /* MOUNT GENERATION (race fence): every mount() captures a sequence
     number; a newer navigation invalidates it. Without this, two rapid
     hashchanges interleaved at the mount await let the OLDER view's
     late fetch resolve LAST and clobber the newer view's DOM, hijack
     `active` (leaking the winner's subscriptions), and flip the title
     — while the URL says otherwise. */
  let mountSeq = 0;

  function match(hash) {
    const path = (hash || "").replace(/^#/, "") || "/home";
    const segs = path.split("/").filter(Boolean);
    for (const r of routes) {
      if (r.segs.length !== segs.length) continue;
      const params = {};
      let ok = true;
      for (let i = 0; i < segs.length; i++) {
        const p = r.segs[i];
        if (p.startsWith(":")) params[p.slice(1)] = decodeURIComponent(segs[i]);
        else if (p !== segs[i]) { ok = false; break; }
      }
      if (ok) return {route: r, params};
    }
    return null;
  }

  async function mount() {
    if (!container) return;
    const seq = ++mountSeq;
    const m = match(location.hash);
    if (!m) { navigate("/home"); return; }
    /* teardown the old view first — its subscriptions must die before the
       next view mounts its own (cross-view double-render guard). */
    if (active && active.unmount) { try { await active.unmount(); } catch {} }
    if (active && active.viewObj && active.viewObj.unmount) {
      try { await active.viewObj.unmount(); } catch {} }
    if (seq !== mountSeq) return;      // superseded mid-teardown
    active = null;
    container.textContent = "";
    container.scrollTop = 0;
    const viewObj = {params: m.params};
    let returned = null;
    try { returned = await m.route.mount(container, m.params, viewObj); }
    catch (e) {
      console.error(e);
      container.append(el("div", {class: "empty"},
        el("div", {class: "empty-msg"}, "view failed: " + (e.message || e))));
    }
    if (seq !== mountSeq) {
      /* a STALE mount finished after a newer navigation won: run its
         cleanup so its subscriptions/timers die, and touch NOTHING —
         the newer route owns the container, the title and the scope */
      if (typeof returned === "function") { try { await returned(); } catch {} }
      return;
    }
    active = {route: m.route, params: m.params,
              unmount: typeof returned === "function" ? returned : null,
              viewObj};
    document.title = (m.route.title ? m.route.title + " — " : "") + "LibzentSuite";
    /* page-scoped custom CSS follows the route (theme.js bridge) */
    if (typeof themeSetScope === "function") themeSetScope(m.route.pattern);
    for (const fn of routeSubs) {
      try { fn(m.route, m.params); } catch (e) { console.error(e); }
    }
  }

  window.addEventListener("hashchange", mount);

  return {
    attach(elContainer) { container = elContainer; mount(); },
    add(pattern, title, mountFn) {
      routes.push({pattern, title, segs: pattern.split("/").filter(Boolean), mount: mountFn});
    },
    navigate(hash) { if (location.hash === hash) mount(); else location.hash = hash; },
    current() { return active; },
    onRoute(fn) { routeSubs.push(fn); return () => {
      const i = routeSubs.indexOf(fn); if (i >= 0) routeSubs.splice(i, 1); }; },
    match,
  };
})();
const routerAdd = (pattern, title, mountFn) => Router.add(pattern, title, mountFn);
const navigate = hash => Router.navigate(hash);
