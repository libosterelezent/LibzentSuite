"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/tools.js — the Tools page: simulations and fleet-level controls.
     Simulation — /api/simulation/list picker; POST /api/simulation/run
                  {name} with a 210s budget (busy() guards the whole run,
                  the button stays disabled until the report lands)
     Advanced   — dashboard token connect (sessionStorage → reload),
                  global emergency stop, fleet resume, .env credential
                  import, and server shutdown — each behind the confirm
                  surface its blast radius deserves.
   ═══════════════════════════════════════════════════════════════════════════ */

routerAdd("/tools", "Tools", async (container) => {
  const state = {alive: true};

  container.append(el("div", {class: "view-hdr"},
    el("h1", {}, "Tools"),
    el("p", {class: "dim"},
      "Scenario simulations and the dangerous fleet controls."),
  ));

  const section = (title, hint) => {
    const hdr = el("div", {class: "sec-hdr"}, el("span", {}, title));
    if (hint) hdr.append(el("span", {class: "dim", style: "margin-left:var(--sp-2);"}, hint));
    const bodyEl = el("div", {class: "sec-body"});
    container.append(el("div", {class: "section"}, hdr, bodyEl));
    return bodyEl;
  };

  /* ── simulation ─────────────────────────────────────────────────────── */
  const simHost = section("Simulation", "scenario runs against the live engine");
  function renderSimulation(scenarios) {
    simHost.textContent = "";
    if (!scenarios || !scenarios.length) {
      simHost.append(emptyState("No scenarios registered",
        "Plugins expose simulation scenarios; none are currently available."));
      return;
    }
    const sel = el("select", {"aria-label": "Scenario", style: "min-width:260px;"},
      ...scenarios.map(s => el("option", {value: s.name}, s.name)));
    const desc = el("p", {class: "hint"},
      (scenarios[0] && scenarios[0].description) || "");
    sel.onchange = () => {
      const s = scenarios.find(x => x.name === sel.value);
      desc.textContent = (s && s.description) || "";
    };
    const run = el("button", {class: "btn btn-primary", type: "button"}, "Run scenario");
    const report = el("pre", {class: "mono",
      style: "margin:0;max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;"});
    /* busy() wraps the whole run: the button disables + spins until the
       report (or the error) lands — no double submits across a 3-minute run */
    run.onclick = busy(run, async () => {
      report.textContent = "running " + sel.value + " …";
      try {
        /* long run: the server budgets 200s itself — give the request 210s */
        const result = await api("/api/simulation/run", {
          method: "POST", body: JSON.stringify({name: sel.value}), timeoutMs: 210000});
        if (!state.alive) return;
        const text = result && result.log ? result.log : JSON.stringify(result, null, 2);
        report.textContent = typeof text === "string" ? text : JSON.stringify(text, null, 2);
        toast(`scenario ${sel.value} finished`, "ok");
      } catch (e) {
        if (!state.alive) return;
        report.textContent = "";
        toast(e.message, "err");
      }
    });
    simHost.append(el("div", {class: "toolbar"}, sel, run), desc, report);
  }

  async function loadScenarios() {
    let scenarios = null;
    try { scenarios = await api("/api/simulation/list"); }
    catch (e) { if (state.alive) toast(e.message, "err"); }
    if (!state.alive) return;
    renderSimulation(scenarios || []);
  }

  /* ── advanced ───────────────────────────────────────────────────────── */
  const advHost = section("Advanced", "token · emergency · lifecycle");

  /* dashboard token: sessionStorage only, then a clean reload so every
     surface re-fetches with auth. SECRET: masked input + reveal toggle —
     the token must never sit visible on screen (audit P1). */
  const tokenInput = el("input", {type: "password", placeholder: "dashboard token",
    value: token() || "", style: "width:260px;", autocomplete: "off"});
  const tokenReveal = el("button", {class: "btn btn-ghost btn-sm", type: "button",
    "aria-label": "Show or hide the token", title: "Show / hide",
    onclick: () => {
      tokenInput.type = tokenInput.type === "password" ? "text" : "password";
    }}, "👁");
  const tokenSave = el("button", {class: "btn btn-primary btn-sm", type: "button"}, "Connect");
  tokenSave.onclick = () => busy(tokenSave, async () => {
    const v = tokenInput.value.trim();
    if (!v) { toast("Token required.", "err"); return; }
    secretStore.setItem("dash_token", v);
    toast("token saved — reloading…", "ok");
    await new Promise(r => setTimeout(r, 400));
    location.reload();
  });

  const estop = el("button", {class: "btn btn-danger", type: "button"}, "Emergency stop");
  estop.onclick = async () => {
    const ok = await confirmBox({title: "GLOBAL EMERGENCY STOP", danger: true,
      body: "Cancel every task and stop every running account, fleet-wide?"});
    if (!ok) return;
    try { await busyRun(estop, () => post("/api/estop")); toast("emergency stop issued — all tasks cancelled", "err"); }
    catch (e) { toast(e.message, "err"); }
  };
  const resume = el("button", {class: "btn btn-secondary", type: "button"}, "Resume fleet");
  resume.onclick = () => busy(resume, async () => {
    try { await post("/api/resume"); toast("fleet resume requested", "ok"); }
    catch (e) { toast(e.message, "err"); }
  });
  const importEnv = el("button", {class: "btn btn-secondary", type: "button"}, "Import .env credentials");
  importEnv.onclick = () => busy(importEnv, async () => {
    try { await post("/api/credentials/import-env"); toast("environment credential import requested", "ok"); }
    catch (e) { toast(e.message, "err"); }
  });
  const shutdown = el("button", {class: "btn btn-danger", type: "button"}, "Shut down server");
  shutdown.onclick = async () => {
    const ok = await confirmBox({title: "SHUT DOWN THE SERVER", danger: true, yes: "Shut down",
      body: "The whole LibzentSuite process exits. Every account disconnects and the dashboard goes away. Really?"});
    if (!ok) return;
    try { await busyRun(shutdown, () => post("/api/shutdown")); toast("shutdown requested", "err"); }
    catch (e) { toast(e.message, "err"); }
  };

  advHost.append(
    el("div", {class: "field"},
      el("label", {}, "Dashboard token"),
      el("div", {class: "toolbar"}, tokenInput, tokenReveal, tokenSave),
      el("div", {class: "hint"}, "Connect this tab to the dashboard API. Stored in sessionStorage — cleared when the tab closes.")),
    el("div", {style: "display:flex;flex-direction:column;gap:var(--sp-2);"},
      el("div", {class: "toolbar"}, estop, resume),
      el("div", {class: "toolbar"}, importEnv, shutdown)));

  await loadScenarios();
  return () => { state.alive = false; };
});
