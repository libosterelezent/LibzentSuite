"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   views/plugins.js — two surfaces over the plugin system:
     #/plugins                      the registry — one card per manifest:
                                    enabled toggle + settings editor (both
                                    ACCOUNT-scoped on the server: an account
                                    picker appears per card), the profile
                                    library (create/update/delete), and
                                    reload/unload.
     #/plugins/:plugin(/:pageId)?   a builder page — fetch the component
                                    tree and mount it through render.js.
   Live-server contract notes: /api/plugins/set-enabled, /set-setting and
   /settings all key on ACCOUNT+plugin (the registry therefore disables the
   per-account controls at zero accounts instead of faking a global toggle).
   ═══════════════════════════════════════════════════════════════════════════ */

/* ═══════════════════════════ route 1: the registry ═══════════════════════ */

routerAdd("/plugins", "Plugins", async (container) => {
  const state = {alive: true, profiles: [], profileFailed: false};
  const mode = () => (prefGet("plugin_view", "tiles") === "list" ? "list" : "tiles");

  const tileBtn = el("button", {class: "btn btn-sm", type: "button",
    "aria-pressed": "false", title: "Tile view"}, "Tiles");
  const listBtn = el("button", {class: "btn btn-sm", type: "button",
    "aria-pressed": "false", title: "List view"}, "List");
  const paintModeBtns = () => {
    tileBtn.className = "btn btn-sm " + (mode() === "tiles" ? "btn-primary" : "btn-ghost");
    tileBtn.setAttribute("aria-pressed", mode() === "tiles" ? "true" : "false");
    listBtn.className = "btn btn-sm " + (mode() === "list" ? "btn-primary" : "btn-ghost");
    listBtn.setAttribute("aria-pressed", mode() === "list" ? "true" : "false");
  };
  tileBtn.onclick = () => { prefSet("plugin_view", "tiles"); paintModeBtns(); render(); };
  listBtn.onclick = () => { prefSet("plugin_view", "list"); paintModeBtns(); render(); };
  paintModeBtns();

  container.append(el("div", {class: "view-hdr"},
    el("div", {},
      el("h1", {}, "Plugins"),
      el("p", {class: "dim"},
        "The discovered plugin registry: per-account enablement, settings, the shared profile library and lifecycle.")),
    el("div", {class: "actions"},
      tileBtn, listBtn,
      el("button", {class: "btn btn-secondary btn-sm", type: "button"}, "↻ Refresh")),
  ));
  const refreshBtn = container.querySelector(".view-hdr .btn-secondary");
  const grid = el("div", {});
  container.append(grid);

  const paintGrid = () => {
    grid.style.cssText = mode() === "list"
      ? "display:grid;grid-template-columns:minmax(0,1fr);gap:var(--sp-3);align-items:start;"
      : "display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:var(--sp-3);align-items:start;";
  };

  const profilesOf = plugin => state.profiles.filter(p => p.plugin === plugin);

  /* ── settings + profile editors share this schema field builder ─────── */
  function schemaField(def, value) {
    const cur = value == null ? (def.default ?? "") : value;
    if (def.type === "bool") {
      const i = el("input", {type: "checkbox"});
      i.checked = cur === true;
      return el("div", {class: "field", dataset: {key: def.name}},
        el("label", {}, `${def.name} (${def.type})`),
        el("label", {style: "display:inline-flex;gap:var(--sp-2);align-items:center;"}, i,
          el("span", {class: "dim"}, def.description || "")));
    }
    const input = def.type === "int" || def.type === "float"
      ? el("input", {type: "number", step: def.type === "int" ? "1" : "any",
          min: def.min != null ? String(def.min) : undefined,
          max: def.max != null ? String(def.max) : undefined})
      : el("input", {type: "text"});
    input.value = String(cur ?? "");
    return el("div", {class: "field", dataset: {key: def.name}},
      el("label", {}, `${def.name} (${def.type})`), input,
      def.description ? el("div", {class: "hint"}, def.description) : null);
  }
  function collectSchema(form, schema) {
    const out = {};
    for (const def of schema) {
      const slot = form.querySelector(`[data-key="${CSS.escape(def.name)}"]`);
      if (!slot) continue;
      const input = slot.querySelector("input");
      if (def.type === "bool") { out[def.name] = input.checked; continue; }
      if (def.type === "int" || def.type === "float") {
        if (input.value === "") { out[def.name] = def.default ?? null; continue; }
        const n = Number(input.value);
        if (!Number.isFinite(n) || (def.type === "int" && !Number.isInteger(n))) {
          toast(`${def.name} must be ${def.type === "int" ? "an integer" : "a finite number"}.`, "err");
          return null;
        }
        out[def.name] = n; continue;
      }
      out[def.name] = input.value;
    }
    return out;
  }

  /* ── per-card: settings editor dialog ───────────────────────────────── */
  function openSettingsEditor(manifest, accounts) {
    const schema = manifest.settings || [];
    if (!accounts.length) {
      dialog({title: `Settings — ${manifest.name}`,
        bodyEl: el("div", null,
          el("p", {class: "dim"}, manifest.description || ""),
          schema.length
            ? el("div", null, ...schema.map(s => el("div", {class: "kv"},
                el("span", {class: "k"}, s.name), el("span", {class: "v"}, JSON.stringify(s.default)))))
            : emptyState("No settings", "This plugin declares no settings schema."),
          el("p", {class: "hint"},
            "Settings are per account — add an account first to edit values.")),
        actions: [{label: "Close", variant: "secondary", onclick: close => close()}]});
      return;
    }
    const acctSel = el("select", {"aria-label": "Account"},
      ...accounts.map(a => el("option", {value: a.name}, a.name)));
    const skel = el("div", {class: "skel"}, el("div"), el("div"));
    const form = el("div", null,
      el("div", {class: "field"}, el("label", {}, "Account"), acctSel), skel);
    const dlg = dialog({title: `Settings — ${manifest.name}`, wide: true, bodyEl: form, actions: []});
    const loadValues = async () => {
      form.querySelectorAll("[data-key]").forEach(n => n.remove());
      let list = null;
      try { list = await api("/api/plugins/settings?account=" + encodeURIComponent(acctSel.value)); }
      catch (e) { skel.remove(); form.append(el("p", {class: "hint"}, "load failed: " + e.message)); return; }
      skel.remove();
      const entry = (list || []).find(x => x.name === manifest.name);
      const values = (entry && entry.settings) || {};
      if (!schema.length) { form.append(emptyState("No settings", "This plugin declares no settings schema.")); return; }
      for (const def of schema) form.append(schemaField(def, values[def.name]));
    };
    acctSel.onchange = loadValues;
    loadValues();
    const save = el("button", {class: "btn btn-primary", type: "button"}, "Save");
    save.onclick = busy(save, async () => {
      const values = collectSchema(form, schema);
      if (!values) return;
      try {
        /* one set-setting per CHANGED key — the server validates each
           against the schema and 400s naming the offending field */
        for (const key of Object.keys(values)) {
          await post("/api/plugins/set-setting",
            {account: acctSel.value, plugin: manifest.name, key, value: values[key]});
        }
        toast(`settings saved for ${acctSel.value}`, "ok");
        dlg.close();
      } catch (e) { toast(e.message, "err"); }
    });
    dlg.querySelector(".dlg-body").after(el("div", {class: "dlg-foot"}, save));
  }

  /* ── per-card: profile library editor dialog ────────────────────────── */
  function openProfileEditor(manifest, existing) {
    const schema = manifest.settings || [];
    const settings = (existing && existing.settings) || {};
    const nameInput = el("input", {type: "text", placeholder: "e.g. farm-fast",
      value: existing ? existing.name : ""});
    const descInput = el("input", {type: "text", placeholder: "what this bundle is for",
      value: existing ? (existing.description || "") : ""});
    const form = el("div", null,
      el("div", {class: "field"}, el("label", {}, "Profile name"), nameInput,
        el("div", {class: "hint"}, "unique within this plugin — lowercase letters, digits, dashes")),
      el("div", {class: "field"}, el("label", {}, "Description"), descInput),
      schema.length
        ? el("div", null, ...schema.map(def => schemaField(def, settings[def.name])))
        : el("p", {class: "hint"}, "This plugin declares no settings — the profile only carries a name."));
    dialog({title: `${existing ? "Edit" : "New"} profile — ${manifest.name}`, wide: true, bodyEl: form, actions: [
      {label: "Cancel", variant: "secondary", onclick: close => close()},
      {label: existing ? "Save profile" : "Create profile", variant: "primary", onclick: (close) => {
        const name = nameInput.value.trim();
        if (!name) { toast("Profile name is required.", "err"); return; }
        const values = collectSchema(form, schema);
        if (!values) return;
        const body = existing
          ? {plugin: manifest.name, name: existing.name, description: descInput.value, settings: values}
          : {plugin: manifest.name, name, description: descInput.value, settings: values};
        if (existing && name !== existing.name) body.name_new = name;
        post(existing ? "/api/plugins/profiles/update" : "/api/plugins/profiles/create", body)
          .then(() => { close(); loadProfiles(); })
          .catch(e => toast(e.message, "err"));
      }},
    ]});
    nameInput.focus();
  }

  /* ── one registry card ──────────────────────────────────────────────── */
  function pluginCard(manifest) {
    const accounts = storeGet("accounts") || [];
    const card = el("div", {class: "card"});

    /* header: name, version, lifecycle actions */
    const reload = el("button", {class: "btn btn-ghost btn-sm", type: "button",
      title: "Re-import the plugin module and re-attach to accounts"}, "↻ reload");
    reload.onclick = () => busy(reload, async () => {
      try { await post("/api/plugins/reload", {plugin: manifest.name}); await reloadAll(); }
      catch (e) { toast(e.message, "err"); }
    });
    const unload = el("button", {class: "btn btn-ghost btn-sm", type: "button",
      title: "Detach from all accounts (assignments preserved)"}, "unload");
    unload.onclick = async () => {
      const ok = await confirmBox({title: "Unload plugin",
        body: `Unload ${manifest.name} from all accounts? Assignments are preserved — reload restores them.`});
      if (!ok) return;
      try { await busyRun(unload, () => post("/api/plugins/unload", {plugin: manifest.name})); await reloadAll(); }
      catch (e) { toast(e.message, "err"); }
    };
    card.append(el("div", {class: "toolbar"},
      el("strong", {}, manifest.name),
      el("span", {class: "badge badge-dim mono"}, "v" + (manifest.version || "?")),
      el("span", {style: "flex:1;"}),
      reload, unload));
    card.append(el("p", {class: "dim", style: "margin:0;overflow-wrap:anywhere;"},
      manifest.description || "no description"));

    /* per-account enablement (server keys set-enabled on account+plugin) */
    const acctSel = el("select", {"aria-label": "Account for " + manifest.name},
      ...accounts.map(a => el("option", {value: a.name}, a.name)));
    const toggleInput = el("input", {type: "checkbox"});
    const sw = el("span", {class: "toggle"});
    const settingsBtn = el("button", {class: "btn btn-secondary btn-sm", type: "button"}, "Settings…");
    settingsBtn.onclick = () => openSettingsEditor(manifest, accounts);
    if (accounts.length) {
      const loadState = async () => {
        try {
          const states = await api("/api/plugins/state?account=" + encodeURIComponent(acctSel.value)) || [];
          const st = states.find(x => x.name === manifest.name);
          toggleInput.checked = !!(st && st.enabled);
          sw.classList.toggle("on", toggleInput.checked);
          toggleInput.disabled = !st;   /* not assigned to this account */
          toggleInput.title = st ? "" : "not assigned to this account";
        } catch { /* leave the last known state */ }
      };
      acctSel.onchange = loadState;
      loadState();
      toggleInput.addEventListener("change", async () => {
        sw.classList.toggle("on", toggleInput.checked);
        try {
          await post("/api/plugins/set-enabled",
            {account: acctSel.value, plugin: manifest.name, enabled: toggleInput.checked});
          toast(`${manifest.name} ${toggleInput.checked ? "enabled" : "disabled"} on ${acctSel.value}`, "ok");
        } catch (e) {
          toast(e.message, "err");
          toggleInput.checked = !toggleInput.checked;
          sw.classList.toggle("on", toggleInput.checked);
        }
      });
      card.append(el("div", {class: "toolbar"},
        el("span", {class: "hint"}, "on"), acctSel,
        el("label", {style: "display:inline-flex;align-items:center;gap:var(--sp-2);cursor:pointer;"},
          el("span", {class: "hidden"}, toggleInput), sw),
        el("span", {style: "flex:1;"}), settingsBtn));
    } else {
      card.append(el("div", {class: "toolbar"},
        settingsBtn,
        el("span", {class: "hint"}, "add an account to enable plugins per account")));
    }

    /* profile library */
    const profs = profilesOf(manifest.name);
    const lib = el("div", {class: "field"},
      el("label", {}, `Profile library (${profs.length})`));
    if (state.profileFailed)
      lib.append(el("div", {class: "hint"}, "profile library unavailable — see the error toast"));
    if (!profs.length) {
      lib.append(el("div", {class: "hint"},
        "no profiles yet — plugin defaults apply to every account"));
    } else for (const p of profs) {
      const edit = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "edit");
      edit.onclick = () => openProfileEditor(manifest, p);
      const del = el("button", {class: "btn btn-ghost btn-sm", type: "button"}, "delete");
      del.onclick = async () => {
        const ok = await confirmBox({title: `Delete profile ${p.name}`,
          body: `Delete the profile '${p.name}' of plugin '${manifest.name}'? Accounts bound to it fall back to the plugin defaults.`,
          yes: "Delete profile"});
        if (!ok) return;
        try {
          await busyRun(del, () => post("/api/plugins/profiles/delete",
            {plugin: manifest.name, name: p.name}));
          await loadProfiles();
        } catch (e) { toast(e.message, "err"); }
      };
      lib.append(el("div", {class: "kv"},
        el("span", {class: "v"}, p.name),
        el("span", {class: "k"}, p.description || ""),
        el("span", {style: "margin-left:auto;display:inline-flex;gap:4px;"}, edit, del)));
    }
    /* profile CREATION is on hold for removal (operator, Phase A #10):
       existing profiles stay editable/deletable, new ones come later
       through the account MEMORY flow */
    card.append(lib);
    return card;
  }

  function render() {
    const manifests = storeGet("manifests") || [];
    paintGrid();
    grid.textContent = "";
    if (!manifests.length) {
      grid.append(emptyState("No plugins",
        "No plugins discovered in the plugins directory — drop one in and reload."));
      return;
    }
    for (const m of manifests) grid.append(pluginCard(m));
  }

  async function loadProfiles() {
    try { state.profiles = await api("/api/plugins/profiles") || []; state.profileFailed = false; }
    catch (e) { state.profileFailed = true; toast(e.message, "err"); }
    if (state.alive) render();
  }
  async function reloadAll() {
    await Promise.allSettled([loadManifests(), loadProfiles()]);
    if (state.alive) render();
  }

  const unsub = storeSubscribe(key => {
    if (!state.alive) return;
    if (key === "manifests" || key === "accounts") render();
  });
  refreshBtn.onclick = () => busy(refreshBtn, reloadAll);
  render();
  loadProfiles();

  return () => { state.alive = false; unsub(); };
});

/* ═════════════════════════ route 2: builder pages ═══════════════════════ */

async function pluginPageMount(container, params) {
  const plugin = params.plugin || "";
  let pageId = params.pageId || "";
  const state = {alive: true};
  let unsubNav = null;

  /* default page: the plugin's first navigation entry. On a cold load the
     router mounts BEFORE boot's navigation fetch lands — wait for it (the
     skeleton covers the gap) instead of declaring the plugin pageless. */
  if (!pageId && !(storeGet("navigation") || []).length) {
    container.append(el("div", {class: "skel"}, el("div"), el("div"), el("div")));
    await new Promise(resolve => {
      unsubNav = storeSubscribe(key => { if (key === "navigation") resolve(); });
      setTimeout(resolve, 8000);   // never hang the route forever
    });
  }
  if (!pageId) {
    const entry = (storeGet("navigation") || []).find(n => n.plugin === plugin);
    if (!entry) {
      container.textContent = "";
      container.append(emptyState("No dashboard pages",
        `Plugin "${plugin}" registers no dashboard pages.`));
      if (unsubNav) unsubNav();
      return () => {};
    }
    pageId = entry.page_id;
  }

  container.textContent = "";
  container.append(el("div", {class: "view-hdr"},
    el("h1", {}, plugin),
    el("p", {class: "dim"}, `builder page · ${pageId}`),
  ));
  const host = el("div", null,
    el("div", {class: "skel"}, el("div"), el("div"), el("div"), el("div")));
  container.append(host);

  try {
    const tree = await api("/api/dashboard-ui/page?plugin=" + encodeURIComponent(plugin) +
      "&page_id=" + encodeURIComponent(pageId));
    if (!state.alive) return;
    if (!tree || tree.type !== "page") {
      host.textContent = "";
      host.append(emptyState("Page not found", `No page '${pageId}' for plugin '${plugin}'.`));
      return;
    }
    host.textContent = "";
    host.append(renderBuilderTree(tree, plugin));
  } catch (e) {
    if (!state.alive) return;
    host.textContent = "";
    host.append(emptyState("Page unavailable", e.message));
  }
  return () => { state.alive = false; if (unsubNav) unsubNav(); host.textContent = ""; };
}

routerAdd("/plugins/:plugin", "Plugin", pluginPageMount);
routerAdd("/plugins/:plugin/:pageId", "Plugin", pluginPageMount);
