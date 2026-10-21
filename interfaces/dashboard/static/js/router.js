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
    const m = match(location.hash);
    if (!m) { navigate("/home"); return; }
    /* teardown the old view first — its subscriptions must die before the
       next view mounts its own (cross-view double-render guard). */
    if (active && active.unmount) { try { await active.unmount(); } catch {} }
    if (active && active.viewObj && active.viewObj.unmount) {
      try { await active.viewObj.unmount(); } catch {} }
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
    active = {route: m.route, params: m.params,
              unmount: typeof returned === "function" ? returned : null,
              viewObj};
    document.title = (m.route.title ? m.route.title + " — " : "") + "LibzentSuite";
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
