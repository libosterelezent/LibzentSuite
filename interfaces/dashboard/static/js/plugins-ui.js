"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   plugins-ui.js — the plugin-profile model's two surfaces:
     * Plugins tab — per-plugin profile library (create/edit/delete) plus a
       quick per-account binding row for every account the plugin is
       assigned to.
     * Profiles tab — account-centric manager: one section per ASSIGNED
       plugin with its bound profile, bind/unbind and the shared editor.
   The shared profile editor is schema-driven (one input per manifest
   settings entry, extras as raw JSON) and owns the channel-scope section:
   mode radios, a nested guild → channel picker fed by
   GET /api/accounts/channels, a manual guild:channel textarea fallback
   (always available; the default view when the account is offline) and a
   live "N channels across M servers" preview.
   ONLY the Phase-1 endpoints are used: /api/plugins/profiles* for the
   library, /api/accounts/plugin-profile[s] for bindings, plus the
   channels and effective-settings reads. The retained legacy global
   profile API is never called.
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Library state + loaders ──────────────────────────────────────────────── */

async function loadPluginProfiles(thenRender=true) {
  try { pluginProfiles = await api("/api/plugins/profiles")||[]; pluginProfilesFailed = false; }
  catch(e) { pluginProfilesFailed = true; showErr(e.message); }
  if (thenRender) render();
}

/* One coherent post-mutation update (the afterGroupMutation shape):
   library AND account records fresh, then exactly one render. */
async function afterProfileMutation() {
  await Promise.all([loadPluginProfiles(false), refreshAccountsNow()]);
  render();
}

function profilesOf(plugin) { return (pluginProfiles||[]).filter(p=>p.plugin===plugin); }

/* The reserved "channels" setting decides the mode badge; absent/null or
   mode "all" means no restriction. */
function channelModeOf(p) {
  const sc = p && p.settings && p.settings.channels;
  return (sc && (sc.mode==="allow"||sc.mode==="deny")) ? sc.mode : "all";
}
function scopeCounts(scope) {
  let chans = 0, guilds = 0;
  for (const r of (scope&&scope.rules)||[]) {
    const n = (r.channel_ids||[]).length;
    if (n) { chans += n; guilds++; }
  }
  return [chans, guilds];
}
function boundProfileOf(account, plugin) {
  const a = accounts().find(x=>x.name===account);
  const map = (a && a.plugin_profiles) || ppBindings || {};
  return map[plugin] || null;
}

/* ── Plugins tab ───────────────────────────────────────────────────────────── */

function renderPlugins() {
  const grid = $("plugin-grid"); if (!grid) return;
  // Focus guard (same as renderAdmin): a WS tick must not rebuild the
  // quick-bind selects out from under a half-used one — data stays
  // stale only while focused.
  if (grid.querySelector("select:focus, input:focus, textarea:focus")) return;
  grid.innerHTML = "";
  if (manifestsFailed) { grid.innerHTML=emptyState("Plugins Unavailable","The plugins list could not be loaded — see the error toasts. The rest of the dashboard remains usable."); return; }
  if (!manifests.length) { grid.innerHTML=emptyState("No Plugins","No plugins discovered in the plugins directory."); return; }
  for (const m of manifests) {
    // quick binds exist only for accounts WITH the assignment — the
    // assignment itself stays in the account menu's Assign Plugins flow
    const users = accounts().filter(a=>assignedPlugins(a).includes(m.name));
    const profs = profilesOf(m.name);
    const card = document.createElement("div"); card.className="card";
    card.innerHTML = `
      <div class="row" style="margin-bottom:6px">
        <b class="trunc" style="font-size:14px;color:var(--ok);flex-shrink:1;min-width:0">${esc(m.name)}</b>
        <span class="dim mono" style="font-size:11px;flex-shrink:0">v${esc(m.version||"?")}</span>
        <span style="margin-left:auto;display:flex;gap:4px;flex-shrink:0">
          <button class="btn btn-ghost btn-sm" data-pact="reload" title="Re-import the plugin module and re-attach to accounts">↻ reload</button>
          <button class="btn btn-ghost btn-sm" style="color:var(--err)" data-pact="unload" title="Detach from all accounts (assignments preserved)">unload</button>
        </span>
        <span class="dim" style="font-size:12px;flex-shrink:0">${users.length} account${users.length!==1?"s":""}</span>
      </div>
      <p class="sub" style="margin-bottom:var(--sp-2);overflow-wrap:anywhere">${esc(m.description||"")}</p>
      <div class="pp-sec">
        <span class="pp-sec-title">Profile library</span>
        ${pluginProfilesFailed?'<span class="faint" style="font-size:11px">profile library unavailable — see the error toasts</span>':""}
        <div class="pp-list">
          ${profs.length ? profs.map(p=>`
            <div class="pp-row" data-prof="${esc(p.name)}">
              <span class="pp-name trunc" title="${esc(p.name)}">${esc(p.name)}</span>
              <span class="pp-mode pp-mode-${channelModeOf(p)}" title="Channel scope: ${channelModeOf(p)}">${channelModeOf(p)==="all"?"all channels":channelModeOf(p)+" list"}</span>
              <span class="pp-desc trunc" title="${esc(p.description||"")}">${esc(p.description||"")}</span>
              <span class="pp-row-actions">
                <button class="btn btn-ghost btn-sm" data-pact="edit">edit</button>
                <button class="btn btn-ghost btn-sm" style="color:var(--err)" data-pact="del">delete</button>
              </span>
            </div>`).join("")
            : '<span class="faint" style="font-size:11px">no profiles yet — plugin defaults apply to every account</span>'}
        </div>
        <button class="btn btn-secondary btn-sm" data-pact="new" style="align-self:flex-start">+ New Profile</button>
      </div>
      <div class="pp-sec">
        <span class="pp-sec-title">Per-account profile</span>
        ${users.length ? users.map(a=>{
          const bound = ((a.plugin_profiles||{})[m.name])||"";
          return `<div class="pp-qb" data-qb="${esc(a.name)}">
            <span class="pp-qb-name trunc" title="${esc(a.name)}">${esc(a.name)}</span>
            <select data-qb-sel title="Profile used by ${esc(a.name)} for ${esc(m.name)}">
              <option value="">(default)</option>
              ${profs.map(p=>`<option value="${esc(p.name)}"${p.name===bound?" selected":""}>${esc(p.name)}</option>`).join("")}
            </select>
          </div>`;}).join("")
          : '<span class="faint" style="font-size:11px">not assigned to any account — assign it from the account menu first</span>'}
      </div>`;
    wirePluginCard(card, m);
    grid.appendChild(card);
  }
}

function wirePluginCard(card, m) {
  card.querySelectorAll("[data-pact]").forEach(b=>b.onclick=()=>{
    const act = b.dataset.pact;
    if (act === "reload") {
      b.disabled = true;
      post("/api/plugins/reload", {plugin: m.name})
        .then(r => { showHeaderPrompt(`${m.name} reloaded (${r.restored_accounts} account(s) re-attached)`); loadManifests().then(renderPlugins); })
        .catch(e => { setFb(card, "error", e.message); b.disabled = false; });
      return;
    }
    if (act === "unload") {
      confirmAction("Unload Plugin", `Unload ${m.name} from all accounts? Assignments are preserved — reload restores them.`,
        () => post("/api/plugins/unload", {plugin: m.name})
          .then(r => { showHeaderPrompt(`${m.name} unloaded from ${r.detached_accounts} account(s)`); loadManifests().then(renderPlugins); })
          .catch(e => setFb(card, "error", e.message)));
      return;
    }
    // the channel tree needs an account's live cache — any assigned one
    const chanAccount = accounts().find(x=>assignedPlugins(x).includes(m.name));
    if (act==="new") return openProfileEditor(m.name, null, {account: chanAccount?chanAccount.name:null});
    const row = b.closest(".pp-row");
    const pname = row ? row.dataset.prof : "";
    if (act==="edit") return openProfileEditor(m.name, pname, {account: chanAccount?chanAccount.name:null});
    if (act==="del") return confirmAction(`Delete Profile ${pname}`,
      `Delete the profile '${pname}' of plugin '${m.name}'? Accounts bound to it fall back to the plugin defaults.`,
      ()=>post("/api/plugins/profiles/delete",{plugin:m.name, name:pname})
        .then(afterProfileMutation).catch(e=>toast(e.message)),
      "Delete Profile");
  });
  card.querySelectorAll(".pp-qb").forEach(qb=>{
    const sel = qb.querySelector("[data-qb-sel]");
    sel.onchange = async ()=>{
      clearFb(qb);
      const profile = sel.value || null;
      try {
        await post("/api/accounts/plugin-profile",{account:qb.dataset.qb, plugin:m.name, profile});
        flashFb(qb, profile?`bound to ${profile}`:"using plugin defaults");
        await afterProfileMutation();
      } catch(e){ setFb(qb,"error",e.message); }
    };
  });
}

/* ── Profiles tab (account-centric bindings) ──────────────────────────────── */

function renderProfiles() {
  const sel = $("pp-acct"), host = $("pp-host"); if (!sel||!host) return;
  const names = accounts().map(a=>a.name);
  if (ppAccount && !names.includes(ppAccount)) ppAccount = "";
  fillSelect(sel, names, "— select an account —");
  if (ppAccount) sel.value = ppAccount;
  host.innerHTML = "";
  if (!names.length) { host.innerHTML = emptyState("No Accounts","Add an account first, then manage which profile each of its plugins uses."); return; }
  if (!ppAccount) { host.innerHTML = emptyState("Select an Account","Pick an account above to manage which profile each of its plugins uses."); return; }
  const a = accounts().find(x=>x.name===ppAccount);
  const bindings = (a && a.plugin_profiles) || ppBindings || {};
  const assigned = assignedPlugins(a);
  if (!assigned.length) {
    host.innerHTML = emptyState("No Plugins Assigned",
      "Assign plugins to "+ppAccount+" first — Accounts \u2192 Assign Plugins\u2026");
    return;
  }
  const wrap = document.createElement("div"); wrap.className = "grid grid-2";
  for (const pname of assigned) {
    const m = manifests.find(x=>x.name===pname);
    const profs = profilesOf(pname);
    const bound = bindings[pname] || null;
    const card = document.createElement("div"); card.className="card";
    card.innerHTML = `
      <div class="row" style="margin-bottom:4px">
        <b class="trunc" style="font-size:14px;color:var(--ok)">${esc(pname)}</b>
        <span class="dim mono" style="font-size:11px">${m?`v${esc(m.version||"?")}`:""}</span>
      </div>
      ${m&&m.description?`<p class="sub" style="margin-bottom:var(--sp-2)">${esc(m.description)}</p>`:""}
      <div class="row" style="gap:6px">
        ${bound
          ? `<span class="chip plugin trunc" title="Bound profile">${esc(bound)}</span><span class="faint" style="font-size:11px">bound</span>`
          : '<span class="chip" title="No profile bound — plugin defaults apply">default</span>'}
      </div>
      <div class="row" style="gap:6px;margin-top:var(--sp-2)">
        <button class="btn btn-secondary btn-sm" data-bb="edit">${bound?"Edit Profile":"Set Up Profile"}</button>
        <button class="btn btn-ghost btn-sm" data-bb="unbind" ${bound?"":"disabled"} title="${bound?"":"bind a profile first"}">Unbind</button>
        <button class="btn btn-secondary btn-sm" data-bb="bind">+ Bind Profile</button>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" data-bb="eff" title="Merged settings this account actually runs with">effective\u2026</button>
      </div>
      <div class="faint" style="font-size:11px;margin-top:6px">${profs.length?`${profs.length} profile${profs.length!==1?"s":""} in the library`:"no profiles in the library yet"}</div>`;
    card.querySelectorAll("[data-bb]").forEach(b=>b.onclick=()=>{
      if (b.disabled) return;
      const act = b.dataset.bb;
      // no profile bound yet: Edit first offers the picker (pick or create)
      if (act==="edit") return bound ? openProfileEditor(pname, bound, {account:ppAccount}) : openBindPicker(ppAccount, pname);
      if (act==="bind") return openBindPicker(ppAccount, pname);
      if (act==="eff") return openEffectiveSettings(ppAccount, pname);
      if (act==="unbind") {
        clearFb(card);
        withBusy(b, post("/api/accounts/plugin-profile",{account:ppAccount, plugin:pname, profile:null}))
          .then(async()=>{ flashFb(card,"unbound — plugin defaults apply"); await afterProfileMutation(); })
          .catch(e=>setFb(card,"error",e.message));
      }
    });
    wrap.appendChild(card);
  }
  host.appendChild(wrap);
}

/* Authoritative bindings for the selected account (the WS snapshot field
   covers re-renders; this fetch drives the selector-change refresh). */
async function loadAccountBindings() {
  if (!ppAccount) { render(); return; }
  try { ppBindings = await api("/api/accounts/plugin-profiles?account="+encodeURIComponent(ppAccount))||{}; }
  catch(e) { showErr(e.message); }
  render();
}

/* ── Bind picker (existing profile, default, or create one) ───────────────── */

function openBindPicker(account, plugin) {
  const profs = profilesOf(plugin);
  const current = boundProfileOf(account, plugin);
  const body = openDetailsPopup(`Bind profile — ${plugin} \u00b7 ${account}`);
  const list = document.createElement("div"); list.className = "picklist";
  const row = (label, value, hint, active, isNew) => {
    const l = document.createElement("label");
    // structured pick-row: fixed radio column | non-shrinking name | hint
    l.className = "pick-row" + (active ? " active" : "");
    l.innerHTML = `<input type="radio" name="pppick" data-val="${esc(value||"")}"${isNew?' data-new="1"':""}${active?" checked":""}>
      <span class="pl-name" title="${esc(label)}">${esc(label)}</span>
      ${hint?`<span class="pl-desc" title="${esc(hint)}">${esc(hint)}</span>`:""}`;
    list.appendChild(l);
  };
  row("Default (no profile)", "", "plugin defaults for every setting", !current);
  for (const p of profs) row(p.name, p.name, p.description||"", current===p.name);
  row("New profile\u2026", "__new__", "create a profile and bind it", false, true);
  const actions = document.createElement("div"); actions.className = "form-actions";
  const cancel = btn("Cancel", null, "btn btn-secondary"); cancel.dataset.dlgClose = "1";
  const bind = btn("Bind", null, "btn btn-primary");
  bind.onclick = async () => {
    const selOpt = list.querySelector('input[name="pppick"]:checked');
    if (!selOpt) return;
    if (selOpt.dataset.new) return openProfileEditor(plugin, null, {account, bind:true});
    clearFb(list);
    try {
      // null = clear the binding (plugin defaults); string = bind
      await withBusy(bind, post("/api/accounts/plugin-profile",{account, plugin, profile: selOpt.dataset.val || null}));
      $("dlg-details").close();
      await afterProfileMutation();
    } catch(e){ setFb(list, "error", e.message); }
  };
  actions.append(cancel, bind);
  list.appendChild(actions);
  body.appendChild(list);
}

/* ── Effective settings viewer (defaults <- profile <- overrides) ─────────── */

async function openEffectiveSettings(account, plugin) {
  let eff;
  try { eff = await api("/api/accounts/effective-settings?account="+encodeURIComponent(account)+"&plugin="+encodeURIComponent(plugin)); }
  catch(e) { toast(e.message); return; }
  const scope = eff ? eff.channels : null;
  const rest = Object.assign({}, eff||{});
  delete rest.channels;
  const body = openDetailsPopup(`Effective settings — ${plugin} \u00b7 ${account}`);
  const [chans, guilds] = scopeCounts(scope);
  body.innerHTML = `
    <p class="sub" style="margin-bottom:var(--sp-2)">Merge order, highest wins: plugin defaults \u2190 bound profile \u2190 per-account overrides.</p>
    <div class="card2" style="margin-bottom:var(--sp-3)"><b style="font-size:11px;color:var(--dim);text-transform:uppercase">Channel scope</b>
      <div class="sub">${scope
        ? `${scope.mode==="allow"?"Allow list":"Deny list"} — ${chans} channel${chans!==1?"s":""} across ${guilds} server${guilds!==1?"s":""}`
        : "no restriction — every channel the account can see"}</div></div>
    <b style="font-size:11px;color:var(--dim);text-transform:uppercase">Settings</b>
    ${Object.keys(rest).length
      ? `<div class="tbl-wrap" style="margin-top:var(--sp-2)"><table><tbody>${Object.keys(rest).sort().map(k=>`<tr><td class="mono">${esc(k)}</td><td class="mono" style="overflow-wrap:anywhere">${esc(JSON.stringify(rest[k]))}</td></tr>`).join("")}</tbody></table></div>`
      : '<p class="sub">This plugin declares no settings.</p>'}`;
  $("dlg-details").showModal();
}

/* ── Profile editor (shared by both tabs) ─────────────────────────────────── */

function normalizeScope(raw) {
  if (!raw || typeof raw!=="object") return {mode:"all", rules:[]};
  const mode = raw.mode==="allow"||raw.mode==="deny" ? raw.mode : "all";
  const rules = [];
  for (const r of Array.isArray(raw.rules)?raw.rules:[]) {
    if (r && typeof r==="object" && r.guild_id && Array.isArray(r.channel_ids) && r.channel_ids.length)
      rules.push({guild_id:String(r.guild_id), channel_ids:r.channel_ids.map(String)});
  }
  return {mode, rules};
}

function rulesToText(rules) {
  return rules.map(r=>r.guild_id+":"+r.channel_ids.join(",")).join("\n");
}

/* one rule per line: "guild_id:channel_id[,channel_id ...]" — all-or-nothing */
function parseScopeText(text) {
  const rules = [], bad = [];
  for (const lineRaw of String(text||"").split("\n")) {
    const line = lineRaw.trim();
    if (!line) continue;
    const i = line.indexOf(":");
    if (i<=0) { bad.push(line); continue; }
    const gid = line.slice(0,i).trim();
    const chans = line.slice(i+1).split(",").map(s=>s.trim()).filter(Boolean);
    if (!gid || !chans.length) { bad.push(line); continue; }
    const ex = rules.find(r=>r.guild_id===gid);
    if (ex) { for (const c of chans) if (!ex.channel_ids.includes(c)) ex.channel_ids.push(c); }
    else rules.push({guild_id:gid, channel_ids:chans});
  }
  return [rules, bad];
}

function toggleScopeChannel(scope, gid, cid, on) {
  let rule = scope.rules.find(r=>r.guild_id===gid);
  if (on) {
    if (!rule) { rule = {guild_id:gid, channel_ids:[]}; scope.rules.push(rule); }
    if (!rule.channel_ids.includes(cid)) rule.channel_ids.push(cid);
  } else if (rule) {
    rule.channel_ids = rule.channel_ids.filter(x=>x!==cid);
    if (!rule.channel_ids.length) scope.rules = scope.rules.filter(r=>r!==rule);
  }
}

/* One input per schema entry; the value comes from the profile when the
   key exists there, else the schema default. */
function schemaFieldHTML(s, settings) {
  const cur = (s.name in settings) ? settings[s.name] : s.default;
  const val = cur==null ? "" : String(cur);
  const head = `<div class="field"><label>${esc(s.name)} <span class="faint">(${esc(s.type)})</span></label>`;
  let ctrl;
  if (s.type==="bool") {
    ctrl = `<label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" data-set="${esc(s.name)}" data-t="bool"${cur?" checked":""}> ${cur?"enabled":"disabled"}</label>`;
  } else if (s.type==="int"||s.type==="float") {
    const mm = (s.min!=null?` min="${esc(s.min)}"`:"") + (s.max!=null?` max="${esc(s.max)}"`:"");
    ctrl = `<input type="number" step="${s.type==="int"?"1":"any"}"${mm} data-set="${esc(s.name)}" data-t="${esc(s.type)}" value="${esc(val)}">`;
  } else {
    ctrl = `<input data-set="${esc(s.name)}" data-t="str" value="${esc(val)}" placeholder="${esc(s.default==null?"":String(s.default))}">`;
  }
  return head + ctrl + (s.description?`<span class="hint">${esc(s.description)}</span>`:"") + "</div>";
}

function collectProfileSettings(form, schema) {
  const out = {};
  for (const s of schema) {
    const el = form.querySelector('[data-set="'+s.name+'"]');
    if (!el) continue;
    if (s.type==="bool") { out[s.name] = el.checked; continue; }
    if (s.type==="int"||s.type==="float") {
      const n = Number(el.value);
      if (!Number.isFinite(n) || (s.type==="int" && !Number.isInteger(n))) {
        setFb(el.closest(".field"),"error",`${s.name} must be ${s.type==="int"?"an integer":"a finite number"}.`);
        return null;
      }
      out[s.name] = n; continue;
    }
    out[s.name] = el.value;
  }
  const ex = form.querySelector('[data-pf="extras"]');
  if (ex && ex.value.trim()) {
    let parsed;
    try { parsed = JSON.parse(ex.value); }
    catch { setFb(ex.closest(".field"),"error","Extra settings must be valid JSON."); return null; }
    if (typeof parsed!=="object"||parsed===null||Array.isArray(parsed)) {
      setFb(ex.closest(".field"),"error","Extra settings must be a JSON object."); return null;
    }
    Object.assign(out, parsed);
  }
  return out;
}

async function openProfileEditor(plugin, name, opts={}) {
  const m = manifests.find(x=>x.name===plugin);
  if (!m) { toast("unknown plugin: "+plugin); return; }
  const prof = name ? (pluginProfiles||[]).find(p=>p.plugin===plugin&&p.name===name) : null;
  if (name && !prof) { toast("unknown profile: "+name); return; }
  const creating = !prof;
  const settings = (prof && prof.settings) || {};
  const schema = m.settings || [];
  const known = new Set(schema.map(s=>s.name));
  const extras = {};
  for (const k of Object.keys(settings)) if (k!=="channels" && !known.has(k)) extras[k]=settings[k];
  const scope = normalizeScope(settings.channels);
  const account = opts.account || null;

  const body = openDetailsPopup((creating?"New profile — ":"Edit profile — ")+plugin+(prof?` \u00b7 ${esc(prof.name)}`:""));
  const form = document.createElement("div"); form.className = "form";
  form.innerHTML = `
    <div class="field"><label>Profile name</label>
      <input data-pf="name" value="${esc(prof?prof.name:"")}" placeholder="e.g. farm-fast">
      <span class="hint">Unique within this plugin — lowercase letters, digits, dashes</span></div>
    <div class="field"><label>Description <span class="faint">(optional)</span></label>
      <input data-pf="desc" value="${esc(prof?(prof.description||""):"")}" placeholder="what this bundle is for"></div>
    ${schema.length
      ? '<span class="pp-sec-title">Settings</span>' + schema.map(s=>schemaFieldHTML(s, settings)).join("")
      : '<p class="hint">This plugin declares no settings — the profile only carries the channel scope.</p>'}
    ${Object.keys(extras).length?`
    <div class="field"><label>Extra settings <span class="faint">(keys outside the plugin schema — raw JSON)</span></label>
      <textarea data-pf="extras" rows="3" class="mono" style="width:100%">${esc(JSON.stringify(extras,null,2))}</textarea>
      <span class="hint">The server rejects unknown keys on save — remove stale entries first.</span></div>`:""}
    <span class="pp-sec-title">Channels</span>
    <div data-pf="modes" class="picklist">
      ${["all","allow","deny"].map(md=>`
        <label class="pick-row${scope.mode===md?" active":""}">
          <input type="radio" name="ppmode" value="${md}"${scope.mode===md?" checked":""}>
          <span class="pl-name">${md==="all"?"All channels":md==="allow"?"Allow list":"Deny list"}</span>
          <span class="pl-desc">${md==="all"?"no restriction — act everywhere the account can see":md==="allow"?"only the selected channels may be used":"the selected channels are blocked, all others allowed"}</span>
        </label>`).join("")}
    </div>
    <div data-pf="scope" class="pp-scope"></div>`;
  body.appendChild(form);
  const actions = document.createElement("div"); actions.className = "form-actions";
  const cancel = btn("Cancel", null, "btn btn-secondary"); cancel.dataset.dlgClose = "1";
  const save = btn(creating?"Create Profile":"Save Profile", null, "btn btn-primary");
  actions.append(cancel, save);
  form.appendChild(actions);

  // ── channel scope section: tree from the account's live cache, manual
  //    textarea fallback, live N/M preview. `scope` is the local model.
  const scopeBox = form.querySelector('[data-pf="scope"]');
  let chanGuilds = [];
  const rerenderScope = ()=>renderScopeBox(scopeBox, scope, chanGuilds, account);
  (async()=>{
    if (account) {
      try { chanGuilds = await api("/api/accounts/channels?account="+encodeURIComponent(account))||[]; }
      catch { chanGuilds = []; }  // offline/unstarted -> manual fallback
    }
    rerenderScope();
  })();
  form.querySelectorAll('input[name="ppmode"]').forEach(r=>r.onchange=()=>{
    scope.mode = r.value;
    form.querySelectorAll('[data-pf="modes"] .pick-row').forEach(l=>l.classList.toggle("active", l.querySelector("input").checked));
    rerenderScope();
  });

  save.onclick = async () => {
    clearFb(form);
    const nameEl = form.querySelector('[data-pf="name"]');
    const newName = nameEl.value.trim();
    if (!newName) { setFb(nameEl.closest(".field"),"error","Profile name is required."); return; }
    const settingsOut = collectProfileSettings(form, schema);
    if (!settingsOut) return;  // the offending field already carries its message
    // full settings payload: schema values + extras + the scope (null = all)
    settingsOut.channels = scope.mode==="all"
      ? null
      : {mode:scope.mode, rules:scope.rules.map(r=>({guild_id:r.guild_id, channel_ids:r.channel_ids.slice()}))};
    const description = form.querySelector('[data-pf="desc"]').value;
    try {
      if (creating) {
        await withBusy(save, post("/api/plugins/profiles/create",{plugin, name:newName, description, settings:settingsOut}));
        if (opts.bind && account) await post("/api/accounts/plugin-profile",{account, plugin, profile:newName});
      } else {
        const payload = {plugin, name:prof.name, description, settings:settingsOut};
        if (newName!==prof.name) payload.name_new = newName;
        await withBusy(save, post("/api/plugins/profiles/update", payload));
      }
      $("dlg-details").close();
      await afterProfileMutation();
    } catch(e){ setFb(form,"error",e.message); }  // dialog stays open
  };
  $("dlg-details").showModal();
  form.querySelector('[data-pf="name"]').focus();
}

/* ── Channel scope section ─────────────────────────────────────────────────── */

function renderScopeBox(box, scope, guilds, account) {
  if (scope.mode==="all") {
    box.innerHTML = `<p class="hint">No restriction — the plugin may act in every channel the account can see.</p>`;
    return;
  }
  const offline = !guilds.length;
  const verb = scope.mode==="allow"?"allowed":"denied";
  const [chans, guildsN] = scopeCounts(scope);
  box.innerHTML = `
    <div class="hint" data-pf="preview" style="font-weight:600;color:var(--dim)">${chans} channel${chans!==1?"s":""} ${verb} across ${guildsN} server${guildsN!==1?"s":""}</div>
    ${offline
      ? `<div class="fb fb-busy" style="border-style:dashed">${account?"account offline — enter channel IDs manually":"no account with this plugin assigned — enter channel IDs manually"}</div>`
      : `<div class="hint">Browsing ${esc(account||"")} channel cache — channels missing here can be added manually.</div>`}
    <div class="pp-tree" data-pf="tree"></div>
    <div class="row" style="gap:6px;margin-top:4px">
      ${offline?"":'<button class="btn btn-ghost btn-sm" data-pf="madd" type="button">Add manually\u2026</button>'}
    </div>
    <div class="field${offline?"":" hidden"}" data-pf="manual">
      <textarea data-pf="mtext" rows="3" class="mono" style="width:100%" placeholder="guild_id:channel_id&#10;guild_id:channel_id,channel_id"></textarea>
      <div class="row" style="gap:6px">
        <button class="btn btn-secondary btn-sm" data-pf="apply" type="button">Apply</button>
        <span class="hint">one rule per line — replaces the list above</span>
      </div>
    </div>`;
  const mtext = box.querySelector('[data-pf="mtext"]');
  mtext.value = rulesToText(scope.rules);
  const tree = box.querySelector('[data-pf="tree"]');
  if (!offline) renderChannelTree(tree, scope, guilds, box);
  const madd = box.querySelector('[data-pf="madd"]');
  if (madd) madd.onclick = ()=>box.querySelector('[data-pf="manual"]').classList.toggle("hidden");
  box.querySelector('[data-pf="apply"]').onclick = ()=>{
    clearFb(box);
    const [rules, bad] = parseScopeText(mtext.value);
    if (bad.length) { setFb(box,"error","unreadable line(s): "+bad.slice(0,3).join(" | ")); return; }
    scope.rules = rules;
    refreshScopeUI(box, scope, guilds, true);
  };
}

function refreshScopeUI(box, scope, guilds, syncManual) {
  const verb = scope.mode==="allow"?"allowed":"denied";
  const [n, g] = scopeCounts(scope);
  const pv = box.querySelector('[data-pf="preview"]');
  if (pv) pv.textContent = `${n} channel${n!==1?"s":""} ${verb} across ${g} server${g!==1?"s":""}`;
  const tree = box.querySelector('[data-pf="tree"]');
  if (tree && guilds.length) renderChannelTree(tree, scope, guilds, box);
  if (syncManual) { const mt = box.querySelector('[data-pf="mtext"]'); if (mt) mt.value = rulesToText(scope.rules); }
}

/* Nested picker: collapsible guild groups, one checkbox per channel,
   pre-checked from scope.rules; toggles update the scope model in place. */
function renderChannelTree(tree, scope, guilds, box) {
  const prevOpen = new Set(Array.from(tree.querySelectorAll(".pp-guild.open")).map(el=>el.dataset.gid));
  tree.innerHTML = "";
  for (const g of guilds) {
    const gid = String(g.guild_id);
    const chans = g.channels||[];
    if (!chans.length) continue;
    const rule = scope.rules.find(r=>r.guild_id===gid);
    const sel = new Set(rule?rule.channel_ids:[]);
    const grp = document.createElement("div"); grp.className="pp-guild"; grp.dataset.gid = gid;
    const head = document.createElement("button"); head.type="button"; head.className="pp-ghead";
    head.innerHTML = `<span class="pp-car">\u25b8</span>
      <span class="pp-gname trunc" title="${esc(g.guild_name||gid)}">${esc(g.guild_name||gid)}</span>
      <span class="dim mono" style="font-size:10px;flex-shrink:0">${sel.size}/${chans.length}</span>`;
    head.onclick = ()=>grp.classList.toggle("open");
    const list = document.createElement("div"); list.className="pp-chans";
    for (const c of chans) {
      const cid = String(c.id);
      const l = document.createElement("label"); l.className="pick-row";
      l.innerHTML = `<input type="checkbox" data-cid="${esc(cid)}"${sel.has(cid)?" checked":""}>
        <span class="pl-name trunc" title="${esc(c.name||cid)}">${esc(c.name||cid)}</span>
        <span class="pl-desc mono">${esc(cid)}${c.type?` \u00b7 ${esc(c.type)}`:""}</span>`;
      l.querySelector("input").onchange = e=>{
        toggleScopeChannel(scope, gid, e.target.dataset.cid, e.target.checked);
        refreshScopeUI(box, scope, guilds, false);
      };
      list.appendChild(l);
    }
    grp.append(head, list);
    grp.classList.toggle("open", prevOpen.size ? prevOpen.has(gid) : sel.size>0);
    tree.appendChild(grp);
  }
}
