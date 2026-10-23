"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   render.js — the generic builder-tree renderer for plugin dashboard pages.
   A plugin declares a Layout through the DashboardUIToolset; the server
   serializes it to a JSON component tree (GET /api/dashboard-ui/page) and
   this module renders ANY component type from it — it never knows about a
   specific plugin.

   Contract (a simplified port of the old dashboard-ui.js):
     * renderBuilderTree(tree, pluginName)  → DOM element for a page tree
     * builderLoadData(plugin, compId, host) → fill a data host (table /
       list-view / live leaf / dynamic form fields) from
       GET /api/dashboard-ui/data — skeleton → data → empty/error states
     * builderAction(plugin, compId, payload) → POST /api/dashboard-ui/action,
       honoring the result: message → toast, field_errors → returned to the
       caller for inline display, refresh: [ids] → re-fetch those hosts,
       modal: id → open that <dialog>.

   Wire format (verified live): every component carries "type" (snake_case,
   e.g. "list-view") and "id", plus optional title/description/label/value/
   variant/level/sub/live/action/confirm/open_modal/options/options_live/
   empty_text/columns/page_size/key_column/row_actions/fields_live/…

   XSS safety: everything is built with el() — dynamic values become text
   nodes and setAttribute values, never parsed HTML.
   ═══════════════════════════════════════════════════════════════════════════ */

/* input leaves — the types a form collects (and dynamic field providers return) */
const B_INPUTS = new Set(["checkbox", "toggle", "text-input", "number-input",
  "textarea", "select", "radio-group", "multi-select", "channel-tree"]);

/* per-rendered-page state: id→comp index, fetch race guard, last provider
   results (for "show all"), expanded tables, dynamic form field defs and
   the account selection for account-scoped pages. */
let bComps = {};
let bSeq = 0;
let bDataCache = {};
let bExpanded = {};
let bFormFields = {};
let bAcct = {};
let bRoot = null;          // element of the currently mounted page
let bPlugin = "";          // plugin name of the currently mounted page

/* Layout props cross the wire server-validated; this whitelist is the
   client's second line of defense (never arbitrary style text). */
function bSafeSize(v) {
  return /^[\w.%()-]+$/.test(String(v || "")) ? String(v) : "";
}
function bFlexStyle(comp) {
  let s = "";
  if (comp.grow != null) s += `flex-grow:${Math.max(0, Math.round(+comp.grow || 0))};`;
  if (comp.shrink != null) s += `flex-shrink:${Math.max(0, Math.round(+comp.shrink || 0))};`;
  const w = bSafeSize(comp.width), h = bSafeSize(comp.height);
  if (w) s += `width:${w};max-width:100%;`;
  if (h) s += `height:${h};`;
  return s;
}
function bGap(comp) {
  const g = bSafeSize(comp.gap);
  return g ? `gap:${g};` : "gap:var(--sp-2);";
}
function bStyle(comp, extra) {
  const s = bFlexStyle(comp) + (extra || "");
  return s ? {style: s} : {};
}

/* ═══════════════════════════ page entry point ═══════════════════════════ */

function renderBuilderTree(tree, pluginName) {
  bComps = {};
  bDataCache = {};
  bFormFields = {};
  bIndex(tree);
  bPlugin = pluginName;

  const page = el("div",
    {style: "display:flex;flex-direction:column;gap:var(--sp-4);min-width:0;"});
  if (tree.description)
    page.append(el("p", {class: "dim", style: "margin:0;"}, tree.description));

  /* account-scoped page: one picker feeds every action/provider on it */
  if (tree.account_scoped) page.append(bAccountBar(pluginName));

  const flow = [], modals = [], tabs = [];
  for (const c of (tree.children || [])) {
    if (c.type === "modal") modals.push(c);
    else if (c.type === "tab") tabs.push(c);
    else flow.push(c);
  }
  for (const c of flow) page.append(bComp(c));
  if (tabs.length) page.append(bTablist(tabs));
  for (const m of modals) page.append(bModal(m));

  bRoot = page;
  /* first fetch of every data host + live leaf (after the element is in
     the DOM — callers append it first, but the hosts carry their ids so
     loading can start immediately) */
  queueMicrotask(() => bLoadAll(page));
  return page;
}

function bIndex(comp) {
  if (!comp || typeof comp !== "object") return;
  if (comp.id) bComps[comp.id] = comp;
  for (const c of (comp.children || [])) bIndex(c);
}

function bLoadAll(root) {
  root.querySelectorAll("[data-bld-body]").forEach(h =>
    builderLoadData(bPlugin, h.dataset.bldBody, h));
  root.querySelectorAll("[data-bld-fields]").forEach(h =>
    bLoadFields(bPlugin, h.dataset.bldFields, h));
  root.querySelectorAll("[data-bld-live]").forEach(h =>
    bLoadLive(bPlugin, h.dataset.bldLive, h));
}

/* ═══════════════════════════ structural containers ══════════════════════ */

function bComp(comp) {
  if (!comp || typeof comp !== "object") return el("span");
  const kids = comp.children || [];
  const box = (tag, cls, style) => {
    const n = el(tag, Object.assign({class: cls || undefined}, style ? {style} : {}));
    for (const c of kids) n.append(bComp(c));
    return n;
  };
  switch (comp.type) {
    case "page":
    case "tab": return box("div", null, "display:flex;flex-direction:column;" + bGap(comp));
    case "section": {
      const n = el("div", {class: "section"});
      const hdr = el("div", {class: "sec-hdr"});
      if (comp.icon) hdr.append(bIcon(comp.icon));
      if (comp.title) hdr.append(el("span", {}, comp.title));
      if (hdr.childNodes.length) n.append(hdr);
      const body = el("div", {class: "sec-body"});
      if (comp.description) body.append(el("p", {class: "dim"}, comp.description));
      for (const c of kids) body.append(bComp(c));
      n.append(body);
      return n;
    }
    case "card": {
      const tone = {ok: "var(--ok)", warn: "var(--warn)", err: "var(--err)",
                    info: "var(--accent)"}[comp.tone];
      const n = el("div", {class: "card", style: tone ? `border-left:3px solid ${tone};` : ""});
      if (comp.title) n.append(el("h4", {}, comp.title));
      if (comp.description) n.append(el("p", {class: "dim"}, comp.description));
      for (const c of kids) n.append(bComp(c));
      return n;
    }
    case "row": return box("div", null, "display:flex;flex-wrap:wrap;align-items:flex-end;" + bGap(comp));
    case "column": return box("div", null, "display:flex;flex-direction:column;" + bGap(comp));
    case "container":
      return box("div", null, comp.direction === "horizontal"
        ? "display:flex;flex-wrap:wrap;align-items:flex-start;" + bGap(comp)
        : "display:flex;flex-direction:column;" + bGap(comp));
    case "grid": {
      let cols = "";
      if (comp.columns === "auto")
        cols = `repeat(auto-fill,minmax(${bSafeSize(comp.min_child) || "240px"},1fr))`;
      else {
        const n = Math.round(+comp.columns);
        if (Number.isFinite(n) && n >= 1 && n <= 6) cols = `repeat(${n},minmax(0,1fr))`;
      }
      return box("div", null, `display:grid;${cols ? `grid-template-columns:${cols};` : ""}` + bGap(comp));
    }
    case "toolbar": {
      const n = el("div", {class: "toolbar"});
      const t = el("span", {class: "dim"});
      if (comp.icon) t.append(bIcon(comp.icon));
      if (comp.title) t.append(el("span", {}, comp.title));
      if (t.childNodes.length) n.append(t);
      for (const c of kids) n.append(bComp(c));
      return n;
    }
    case "metric": {
      const n = el("div", Object.assign({class: "pacing-item"}, bStyle(comp)));
      const v = el("div", {class: "pv" + (["ok", "warn", "err"].includes(comp.level) ? " " + comp.level : "")},
        comp.value ?? "—");
      if (comp.live) v.dataset.bldLive = comp.id;
      if (comp.label) n.append(el("div", {class: "pl"}, comp.label));
      n.append(v);
      if (comp.sub) n.append(el("div", {class: "hint"}, comp.sub));
      return n;
    }
    case "badge": {
      const cls = {ok: "badge-ok", warn: "badge-warn", err: "badge-err"}[comp.variant] || "badge-dim";
      return el("span", {class: "badge " + cls}, comp.value ?? "");
    }
    case "text": {
      if (comp.variant === "code") {
        const pre = el("pre", {class: "mono dim",
          style: "white-space:pre-wrap;overflow-wrap:anywhere;margin:0;" +
                 "max-height:380px;overflow:auto;"}, comp.value ?? "");
        if (comp.live) pre.dataset.bldLive = comp.id;
        return pre;
      }
      const p = el("p", {class: comp.variant === "muted" ? "dim" : undefined,
                         style: "margin:0;overflow-wrap:anywhere;"}, comp.value ?? "");
      if (comp.live) p.dataset.bldLive = comp.id;
      return comp.variant === "heading" ? el("h4", {style: "margin:0;"}, comp.value ?? "") : p;
    }
    case "display-text": {
      const cls = ["dim", "ok", "warn", "err"].includes(comp.tone) ? " " + comp.tone : "";
      return el("div", Object.assign({class: (comp.monospace ? "mono" : "") + cls}, bStyle(comp)),
        comp.value ?? "");
    }
    case "progress": {
      const val = Math.max(0, Math.min(100, Math.round(+comp.value || 0)));
      const n = el("div", Object.assign({}, bStyle(comp),
        {style: bFlexStyle(comp) + "display:flex;flex-direction:column;gap:4px;"}));
      if (comp.label) n.append(el("div", {class: "hint"},
        el("span", {}, comp.label), el("span", {class: "mono"}, val + "%")));
      n.append(el("div", {role: "progressbar", "aria-valuenow": String(val),
        "aria-valuemin": "0", "aria-valuemax": "100",
        style: "height:8px;border-radius:4px;background:var(--surface3);overflow:hidden;"},
        el("div", {style: `height:100%;width:${val}%;background:var(--accent);border-radius:4px;`})));
      return n;
    }
    case "button": return bButton(comp);
    case "toggle": return bToggle(comp);
    case "checkbox":
    case "text-input":
    case "number-input":
    case "textarea":
    case "select":
    case "radio-group":
    case "multi-select": return bInput(comp);
    case "list-view":
    case "table": {
      const n = el("div", Object.assign({}, bStyle(comp),
        {style: bFlexStyle(comp) + "display:flex;flex-direction:column;gap:var(--sp-2);min-width:0;"}));
      if (comp.title) n.append(el("div", {class: "dim"}, comp.title));
      const host = el("div", {dataset: {bldBody: comp.id}});
      n.append(host);
      return n;
    }
    case "form": return bForm(comp);
    case "modal": return bModal(comp);   /* when nested (rare) */
    default:
      return el("div", {class: "empty"},
        el("div", {class: "empty-msg"}, `unsupported component type "${comp.type || "?"}"`));
  }
}

function bIcon(path) {
  return el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
                    "stroke-width": "2", "aria-hidden": "true", width: "14", height: "14"},
    el("path", {d: String(path || "")}));
}

/* busyRun(): invoke ui.busy's wrapped handler directly. ui.busy is a
   factory (for `onclick = busy(btn, fn)` assignment); async handler bodies
   that await the guarded call need this immediate-invocation form.
   Loaded before every view — shared by render.js and all views/*.js. */
function busyRun(btn, fn, ...args) { return busy(btn, fn)(...args); }

/* ═══════════════════════════ action + input leaves ══════════════════════ */

function bResultFieldErrors(res) {
  if (res && res.field_errors && typeof res.field_errors === "object")
    return Object.values(res.field_errors).join("; ");
  return "";
}

function bButton(comp) {
  const variant = ["primary", "secondary", "danger", "ghost"].includes(comp.variant)
    ? comp.variant : "secondary";
  const btn = el("button", {
    class: "btn btn-" + variant, type: "button",
    title: comp.description || undefined,
    disabled: comp.disabled === true || comp.loading === true ? "" : undefined,
  }, comp.label || comp.id);
  btn.onclick = () => {
    if (comp.open_modal) { bOpenModal(comp.open_modal); return; }
    const fire = async () => {
      try {
        const res = await busyRun(btn, () =>
          builderAction(bPlugin, comp.id, {}));
        const fe = bResultFieldErrors(res);
        if (fe) toast(fe, "err");
      } catch (e) { toast(e.message, "err"); }
    };
    if (comp.confirm) confirmBox({title: "Confirm", body: comp.confirm}).then(ok => { if (ok) fire(); });
    else fire();
  };
  const style = bFlexStyle(comp);
  return style
    ? el("div", {style}, btn)
    : btn;
}

/* toggle: a native checkbox driving a skinned .toggle switch (32×18) */
function bToggle(comp) {
  const input = el("input", {type: "checkbox"});
  input.checked = comp.value === true;
  const sw = el("span", {class: "toggle" + (comp.value === true ? " on" : "")});
  const label = el("label", {style: "display:inline-flex;align-items:center;gap:var(--sp-2);cursor:pointer;"},
    el("span", {class: "hidden"}, input), sw, el("span", {}, comp.label || comp.id));
  if (comp.description) label.title = comp.description;
  if (comp.disabled === true) input.disabled = true;
  const wrap = el("div", Object.assign({dataset: {bldField: comp.id}}, bStyle(comp)), label);
  input.addEventListener("change", async () => {
    sw.classList.toggle("on", input.checked);
    if (comp.action !== true) return;
    try {
      const res = await builderAction(bPlugin, comp.id, {value: input.checked});
      const fe = bResultFieldErrors(res);
      if (fe) toast(fe, "err");
      else if (res && res.message && res.success === false) toast(res.message, "err");
    } catch (e) { toast(e.message, "err"); input.checked = !input.checked; sw.classList.toggle("on", input.checked); }
  });
  return wrap;
}

function bFieldShell(comp, control) {
  const shell = el("div", Object.assign({
    class: "field", dataset: {bldField: comp.id},
  }, bStyle(comp)));
  if (comp.label)
    shell.append(el("label", {}, comp.label,
      comp.required ? el("span", {class: "faint", "aria-hidden": "true"}, " *") : null));
  shell.append(control);
  if (comp.description) shell.append(el("div", {class: "hint"}, comp.description));
  return shell;
}

function bInput(comp) {
  const cur = comp.value ?? "";
  const on = comp.action === true;
  /* standalone inputs with actions dispatch {value} on change */
  const wire = control => {
    if (!on) return control;
    control.addEventListener("change", async () => {
      try {
        const res = await builderAction(bPlugin, comp.id, {value: bFieldValue(comp.id)});
        const fe = bResultFieldErrors(res);
        if (fe) toast(fe, "err");
        else if (res && res.message && res.success === false) toast(res.message, "err");
      } catch (e) { toast(e.message, "err"); }
    });
    return control;
  };

  if (comp.type === "checkbox") {
    const l = el("label", {style: "display:inline-flex;align-items:center;gap:var(--sp-2);"},
      Object.assign(el("input", {type: "checkbox"}), {checked: comp.value === true}),
      el("span", {}, comp.label || comp.id));
    if (comp.description) l.title = comp.description;
    const wrap = el("div", Object.assign({dataset: {bldField: comp.id}}, bStyle(comp)), l);
    if (on) wire(l.querySelector("input"));
    return wrap;
  }
  if (comp.type === "text-input") {
    const i = el("input", {type: "text", value: String(cur),
      placeholder: comp.placeholder || undefined,
      maxlength: comp.max_length ? String(Math.round(+comp.max_length)) : undefined});
    return bFieldShell(comp, wire(i));
  }
  if (comp.type === "number-input") {
    const i = el("input", {type: "number", value: String(cur),
      placeholder: comp.placeholder || undefined,
      min: comp.min_value != null ? String(comp.min_value) : undefined,
      max: comp.max_value != null ? String(comp.max_value) : undefined,
      step: comp.step != null ? String(comp.step) : undefined});
    return bFieldShell(comp, wire(i));
  }
  if (comp.type === "textarea") {
    const i = el("textarea", {rows: String(Math.max(1, Math.round(+comp.rows || 3))),
      placeholder: comp.placeholder || undefined});
    i.value = String(cur);
    return bFieldShell(comp, wire(i));
  }
  if (comp.type === "channel-tree")
    return bFieldShell(comp, bChannelTree(comp));
  if (comp.type === "select" || comp.type === "radio-group" || comp.type === "multi-select") {
    const host = comp.type === "select"
      ? el("select", {"aria-label": comp.label || comp.id})
      : el("div", {style: "display:flex;flex-direction:column;gap:var(--sp-1);"});
    const shell = bFieldShell(comp, host);
    bFillOptions(bPlugin, comp, host);
    if (on && comp.type === "select") wire(host);
    return shell;
  }
  return el("span");
}

/* channel-tree: guild groups of channel checkboxes (profile scope
   picker). The guild head toggles/clears its children and reads
   indeterminate on partial picks; value = the checked channel ids. */
function bChannelTree(comp) {
  const pre = new Set((Array.isArray(comp.value) ? comp.value : []).map(String));
  const tree = el("div", {class: "ctree"});
  for (const g of (Array.isArray(comp.groups) ? comp.groups : [])) {
    const chans = (Array.isArray(g.channels) ? g.channels : [])
      .filter(c => c && c.id);
    if (!chans.length) continue;
    const boxes = [];
    const head = el("input", {type: "checkbox", class: "ctree-head",
      "aria-label": String(g.name || g.id)});
    const list = el("div", {class: "ctree-chans"});
    const sync = () => {
      const n = boxes.filter(b => b.checked).length;
      head.checked = n === boxes.length;
      head.indeterminate = n > 0 && n < boxes.length;
    };
    head.addEventListener("change", () => {
      for (const b of boxes) b.checked = head.checked;
    });
    for (const c of chans) {
      const box = el("input", {type: "checkbox", class: "ctree-chan"});
      box.value = String(c.id);
      box.checked = pre.has(String(c.id));
      box.addEventListener("change", sync);
      boxes.push(box);
      list.append(el("label", {class: "ctree-row"},
        box, el("span", {}, "#" + (c.name || c.id))));
    }
    sync();
    tree.append(el("div", {class: "ctree-group"},
      el("label", {class: "ctree-guild"}, head,
        el("span", {}, g.name || g.id)), list));
  }
  return tree;
}

/* option list fill: static comp.options now, or an options provider when
   comp.options_live (async replace, current selection kept). */
async function bFillOptions(plugin, comp, host) {
  const isSelect = comp.type === "select";
  const current = String(comp.value ?? "");
  const render = opts => {
    const norm = (opts || []).map(o => {
      const value = (o && typeof o === "object") ? String(o.value ?? o) : String(o);
      const label = (o && typeof o === "object") ? String(o.label ?? o.value ?? o) : String(o);
      return {value, label};
    });
    if (isSelect) {
      host.textContent = "";
      for (const o of norm)
        host.append(el("option", {value: o.value, selected: o.value === current ? "" : undefined}, o.label));
      return;
    }
    host.textContent = "";
    if (!norm.length) { host.append(el("span", {class: "hint"}, "no options")); return; }
    for (const o of norm) {
      const input = el("input",
        Object.assign({value: o.value}, comp.type === "radio-group"
          ? {type: "radio", name: "bld-rg-" + comp.id}
          : {type: "checkbox"}));
      if (comp.type === "radio-group" && o.value === current) input.checked = true;
      if (comp.type === "multi-select" && Array.isArray(comp.value) && comp.value.map(String).includes(o.value))
        input.checked = true;
      host.append(el("label", {style: "display:inline-flex;align-items:center;gap:var(--sp-2);"}, input,
        el("span", {}, o.label)));
    }
  };
  render(comp.options);
  if (!comp.options_live) return;
  try {
    const data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(plugin) +
      "&component_id=" + encodeURIComponent(comp.id));
    if (Array.isArray(data && data.options)) {
      render(data.options);
      if (isSelect && data.value != null) host.value = String(data.value);
    }
  } catch { /* keep the static list */ }
}

/* ═══════════════════════════ forms ══════════════════════════════════════ */

function bForm(comp) {
  const form = el("form", {class: "card", dataset: {bldForm: comp.id}});
  form.addEventListener("submit", e => e.preventDefault());
  if (comp.title) form.append(el("h4", {}, comp.title));
  if (comp.description) form.append(el("p", {class: "dim"}, comp.description));
  /* dynamic fields host (schema-driven editors) sits above static fields */
  if (comp.fields_live)
    form.append(el("div", {dataset: {bldFields: comp.id}},
      el("div", {class: "skel"}, el("div"), el("div"))));
  for (const c of (comp.children || [])) form.append(bComp(c));
  const submit = el("button", {class: "btn btn-primary", type: "submit"},
    comp.submit_label || "Submit");
  form.append(el("div", {style: "display:flex;gap:var(--sp-2);"}, submit));
  submit.onclick = () => bSubmitForm(comp, form, submit);
  return form;
}

async function bSubmitForm(comp, form, submit) {
  form.querySelectorAll("[data-bld-err]").forEach(n => n.remove());
  const payload = {};
  const defs = [...(comp.children || []), ...(bFormFields[comp.id] || [])];
  for (const f of defs) {
    if (!B_INPUTS.has(f.type) || !f.id) continue;
    const value = bFieldValue(f.id);
    const err = bValidate(f, value);
    if (err) {
      const slot = form.querySelector(`[data-bld-field="${CSS.escape(f.id)}"]`);
      const msg = el("div", {class: "hint", dataset: {bldErr: "1"}}, "⚠ " + err);
      if (slot) slot.append(msg);
      else toast(err, "err");
      return;
    }
    payload[f.id] = value;
  }
  try {
    const res = await busyRun(submit, () => builderAction(bPlugin, comp.id, payload));
    const fe = res && res.field_errors;
    if (fe && typeof fe === "object")
      for (const fid of Object.keys(fe)) {
        const slot = form.querySelector(`[data-bld-field="${CSS.escape(fid)}"]`);
        if (slot) slot.append(el("div", {class: "hint", dataset: {bldErr: "1"}}, "⚠ " + fe[fid]));
      }
    if (comp.close_on_submit && res && res.success !== false &&
        !(fe && Object.keys(fe).length)) {
      const dlg = form.closest("dialog");
      if (dlg) dlg.close();
    }
  } catch (e) { toast(e.message, "err"); }
}

/* one field's current value from the DOM */
function bFieldValue(id) {
  const field = bRoot && bRoot.querySelector(`[data-bld-field="${CSS.escape(id)}"]`);
  if (!field) return null;
  const comp = bComps[id] || {};
  if (comp.type === "multi-select")
    return [...field.querySelectorAll('input[type="checkbox"]:checked')].map(i => i.value);
  if (comp.type === "channel-tree")
    return [...field.querySelectorAll(".ctree-chan:checked")].map(i => i.value);
  if (comp.type === "radio-group") {
    const r = field.querySelector('input[type="radio"]:checked');
    return r ? r.value : null;
  }
  const input = field.querySelector("input, select, textarea");
  if (!input) return null;
  if (comp.type === "checkbox" || comp.type === "toggle") return input.checked;
  if (comp.type === "number-input") {
    if (input.value === "") return null;
    const n = parseFloat(input.value);
    return Number.isFinite(n) ? n : null;
  }
  return input.value;
}

/* client-side mirror of the server's schema validation */
function bValidate(f, value) {
  if (f.required) {
    const empty = (f.type === "checkbox" || f.type === "toggle")
      ? value !== true
      : value == null || value === "" || (Array.isArray(value) && !value.length);
    if (empty) return `${f.label || f.id} is required.`;
  }
  if (f.type === "number-input" && value != null) {
    if (f.min_value != null && value < +f.min_value) return `${f.label || f.id} must be at least ${f.min_value}.`;
    if (f.max_value != null && value > +f.max_value) return `${f.label || f.id} must be at most ${f.max_value}.`;
  }
  if ((f.type === "text-input" || f.type === "textarea") && f.max_length &&
      String(value ?? "").length > +f.max_length)
    return `${f.label || f.id} must be at most ${f.max_length} characters.`;
  return "";
}

/* dynamic form fields: a fields provider returns [{id,label,type,value,…}] */
async function bLoadFields(plugin, id, host) {
  let data = null;
  try {
    data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(plugin) +
      "&component_id=" + encodeURIComponent(id));
  } catch (e) {
    host.textContent = "";
    host.append(el("div", {class: "empty"}, el("div", {class: "empty-msg"}, "fields unavailable: " + e.message)));
    return;
  }
  const defs = Array.isArray(data && data.fields) ? data.fields : null;
  if (!defs) { host.textContent = ""; host.append(el("span", {class: "hint"}, "no fields")); return; }
  bFormFields[id] = defs;
  host.textContent = "";
  const TYPE_MAP = {text: "text-input", number: "number-input", textarea: "textarea",
    select: "select", checkbox: "checkbox", multiselect: "multi-select", radio: "radio-group",
    "channel-tree": "channel-tree"};
  for (const d of defs) {
    if (!d || !d.id) continue;
    const c = {
      type: TYPE_MAP[d.type] || "text-input", id: String(d.id),
      label: d.label != null ? String(d.label) : String(d.id),
      value: d.value, placeholder: d.placeholder, description: d.description,
      required: d.required === true, options: Array.isArray(d.options) ? d.options : [],
      groups: Array.isArray(d.groups) ? d.groups : [],
      min_value: d.min, max_value: d.max, step: d.step,
    };
    bComps[c.id] = c;   /* value collection + validation read the comp by id */
    host.append(bInput(c));
  }
}

/* ═══════════════════════════ modals + tabs ══════════════════════════════ */

function bModal(comp) {
  const dlg = el("dialog", {dataset: {bldModal: comp.id || ""}});
  const close = () => { if (dlg.open) dlg.close(); };
  const hdr = el("div", {class: "dlg-hdr"},
    el("span", {class: "dlg-title"}, comp.title || ""),
    el("button", {class: "dlg-close", type: "button", "aria-label": "Close", onclick: close}, "✕"));
  const body = el("div", {class: "dlg-body"});
  if (comp.description) body.append(el("p", {class: "dim"}, comp.description));
  for (const c of (comp.children || [])) body.append(bComp(c));
  dlg.append(hdr, body);
  return dlg;
}

function bOpenModal(modalId) {
  const dlg = bRoot && bRoot.querySelector(`dialog[data-bld-modal="${CSS.escape(String(modalId))}"]`);
  if (dlg && !dlg.open) dlg.showModal();
  return !!(dlg && dlg.open);
}

/* page-level tab children form ONE tablist (sections around it stay) */
function bTablist(tabs) {
  const wrap = el("div", {style: "display:flex;flex-direction:column;gap:var(--sp-3);"});
  const bar = el("div", {class: "tabbar", role: "tablist"});
  const panels = [];
  tabs.forEach((t, i) => {
    const btn = el("button", {class: "tab" + (i === 0 ? " on" : ""), type: "button",
      role: "tab", "aria-selected": i === 0 ? "true" : "false"}, t.title || t.id);
    const panel = el("div", {role: "tabpanel", class: i === 0 ? undefined : "hidden"}, bComp(t));
    btn.onclick = () => {
      bar.querySelectorAll(".tab").forEach((b, j) => {
        b.classList.toggle("on", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      panels.forEach(p => p.classList.toggle("hidden", p !== panel));
      /* LAZY data: bodies inside a newly shown panel load now */
      panel.querySelectorAll("[data-bld-body]").forEach(h => {
        if (!h.dataset.bldLoaded) builderLoadData(bPlugin, h.dataset.bldBody, h);
      });
      panel.querySelectorAll("[data-bld-live]").forEach(h => bLoadLive(bPlugin, h.dataset.bldLive, h));
    };
    bar.append(btn);
    panels.push(panel);
  });
  wrap.append(bar, ...panels);
  return wrap;
}

/* account-scoped pages: the picker re-runs every provider on the page */
function bAccountBar(plugin) {
  const accounts = storeGet("accounts") || [];
  const sel = el("select", {"aria-label": "Account for " + plugin},
    ...accounts.map(a => el("option", {value: a.name}, a.name)));
  if (!bAcct[plugin] && accounts.length) bAcct[plugin] = accounts[0].name;
  if (bAcct[plugin]) sel.value = bAcct[plugin];
  sel.addEventListener("change", () => {
    bAcct[plugin] = sel.value;
    if (bRoot) bLoadAll(bRoot);
  });
  return el("div", {class: "toolbar"},
    el("span", {class: "dim"}, "Account"), sel);
}

/* ═══════════════════════════ data hosts ═════════════════════════════════ */

async function builderLoadData(plugin, compId, host, quiet) {
  if (!host) return;
  const seq = ++bSeq;
  host.dataset.bldLoaded = "1";
  if (!quiet) {
    host.textContent = "";
    host.append(el("div", {class: "skel"}, el("div"), el("div"), el("div")));
  }
  let data = null;
  try {
    const q = "/api/dashboard-ui/data?plugin=" + encodeURIComponent(plugin) +
      "&component_id=" + encodeURIComponent(compId) +
      (bAcct[plugin] ? "&account=" + encodeURIComponent(bAcct[plugin]) : "");
    data = await api(q);
  } catch (e) {
    if (seq !== bSeq && quiet) return;
    host.textContent = "";
    host.append(emptyState("Data unavailable", e.message));
    return;
  }
  if (seq !== bSeq && quiet) return;
  bDataCache[compId] = data;
  const comp = bComps[compId] || {};
  host.textContent = "";
  if (comp.type === "table") host.append(bTable(plugin, comp, data, host));
  else host.append(bList(plugin, comp, data, host));
}

function bCell(v) {
  if (v && typeof v === "object") return JSON.stringify(v);
  return String(v ?? "");
}

function bRowActionBtns(plugin, comp, key) {
  const span = el("span", {style: "display:inline-flex;gap:4px;justify-content:flex-end;"});
  for (const a of (Array.isArray(comp.row_actions) ? comp.row_actions : [])) {
    const btn = el("button", {class: "btn btn-sm btn-" + (a.variant || "secondary"),
      type: "button", title: a.confirm || undefined}, a.label);
    btn.onclick = () => {
      const fire = async () => {
        try {
          const res = await busyRun(btn, () =>
            builderAction(plugin, comp.id, {action: a.id, row: key}));
          const fe = bResultFieldErrors(res);
          if (fe) toast(fe, "err");
        } catch (e) { toast(e.message, "err"); }
      };
      if (a.confirm) confirmBox({title: "Confirm", body: a.confirm,
        danger: a.variant === "danger"}).then(ok => { if (ok) fire(); });
      else fire();
    };
    span.append(btn);
  }
  return span;
}

/* tables: {columns,rows} | array-of-objects | array-of-arrays; page_size
   hides rows behind a "show all" toggle; row keys resolve via key_column
   (or the "__key" pseudo-cell past the visible columns). */
function bTable(plugin, comp, data, host) {
  let columns = [], rows = [];
  if (data && !Array.isArray(data) && typeof data === "object") {
    columns = Array.isArray(data.columns) ? data.columns.map(String) : [];
    rows = Array.isArray(data.rows) ? data.rows : [];
  } else if (Array.isArray(data) && data.length &&
             data.every(r => r && typeof r === "object" && !Array.isArray(r))) {
    const keys = [];
    for (const row of data) for (const k of Object.keys(row)) if (!keys.includes(k)) keys.push(k);
    columns = (Array.isArray(comp.columns) && comp.columns.length)
      ? comp.columns.map(String) : keys;
    rows = data.map(row => columns.map(k => row[k]));
  } else if (Array.isArray(data)) {
    columns = (Array.isArray(comp.columns) && comp.columns.length)
      ? comp.columns.map(String)
      : (data[0] || []).map((_, i) => "Column " + (i + 1));
    rows = data;
  } else {
    return emptyState("table data must be a list or a {columns, rows} object");
  }
  if (!rows.length) return emptyState(comp.empty_text || "nothing to show yet");

  const pageSize = bExpanded[comp.id] ? 0 : Math.max(0, Math.round(+comp.page_size || 0));
  const visible = pageSize && rows.length > pageSize ? rows.slice(0, pageSize) : rows;
  const keyCol = String(comp.key_column || "");
  let keyIdx = keyCol ? columns.indexOf(keyCol) : -1;
  if (keyCol === "__key" && keyIdx < 0) keyIdx = columns.length;

  const table = el("table", {class: "tbl"});
  const thead = el("thead", {}, el("tr", null,
    ...columns.map(c => el("th", {}, c)),
    ...(Array.isArray(comp.row_actions) && comp.row_actions.length ? [el("th")] : [])));
  const tbody = el("tbody");
  visible.forEach((r, rowIndex) => {
    let key = keyIdx >= 0 && r ? String(r[keyIdx] ?? "") : "";
    if (!key) key = "#" + rowIndex;
    const tr = el("tr", null,
      ...columns.map((_, i) => el("td", {}, bCell(r ? r[i] : ""))),
      ...(Array.isArray(comp.row_actions) && comp.row_actions.length
        ? [el("td", {}, bRowActionBtns(plugin, comp, key))] : []));
    tbody.append(tr);
  });
  table.append(thead, tbody);
  const wrap = el("div");
  wrap.append(el("div", {class: "tbl-wrap"}, table));
  if (pageSize && rows.length > pageSize) {
    const more = el("button", {class: "btn btn-ghost btn-sm", type: "button"},
      `show all ${rows.length} rows`);
    more.onclick = () => {
      bExpanded[comp.id] = true;   /* survives refreshes — no self-collapse */
      host.textContent = "";
      host.append(bTable(plugin, comp, bDataCache[comp.id], host));
    };
    wrap.append(more);
  }
  return wrap;
}

/* list-view: {label,value} objects → .kv rows; records with columns →
   table (reuses the table renderer); plain values → kv rows */
function bList(plugin, comp, data, host) {
  if (data == null || (Array.isArray(data) && !data.length))
    return emptyState(comp.empty_text || "nothing to show yet");
  if (Array.isArray(comp.columns))
    return bTable(plugin, comp, data, host);
  let items = null;
  if (Array.isArray(data)) items = data;
  else if (typeof data === "object") items = Object.entries(data).map(([k, v]) => ({label: k, value: v}));
  if (!items) return emptyState("list data must be a list or an object");
  const box = el("div", {style: "display:flex;flex-direction:column;gap:var(--sp-1);"});
  for (const item of items) {
    if (item && typeof item === "object" && !Array.isArray(item)) {
      for (const [k, v] of Object.entries(item))
        box.append(el("div", {class: "kv"}, el("span", {class: "k"}, k), el("span", {class: "v"}, bCell(v))));
    } else box.append(el("div", {class: "kv"}, el("span", {class: "v"}, bCell(item))));
  }
  return box;
}

/* live leaves (metrics / texts with a data provider): update IN PLACE —
   no DOM rebuild, so refresh cycles never stomp a form mid-typing */
async function bLoadLive(plugin, id, node) {
  if (!node) return;
  let data = null;
  try {
    data = await api("/api/dashboard-ui/data?plugin=" + encodeURIComponent(plugin) +
      "&component_id=" + encodeURIComponent(id));
  } catch { return; }   /* leave the last good value in place */
  if (!data || typeof data !== "object" || Array.isArray(data)) return;
  if (data.value == null && data.level == null) return;
  if (node.classList.contains("pv")) {          /* metric value */
    if (data.value != null) node.textContent = String(data.value);
    node.className = "pv" + (["ok", "warn", "err"].includes(data.level) ? " " + data.level : "");
    if (data.sub != null && node.parentElement) {
      let sub = node.parentElement.querySelector(".hint");
      if (!sub) { sub = el("div", {class: "hint"}); node.after(sub); }
      sub.textContent = String(data.sub);
    }
    return;
  }
  if (data.value != null) node.textContent = String(data.value);   /* text */
}

/* ═══════════════════════════ action dispatch ════════════════════════════ */

async function builderAction(plugin, componentId, payload) {
  const body = {plugin, component_id: componentId, payload: payload || {}};
  if (bAcct[plugin]) body.account = bAcct[plugin];
  const result = await post("/api/dashboard-ui/action", body);
  if (result && typeof result === "object") {
    if (Array.isArray(result.refresh) && result.refresh.length) {
      for (const id of result.refresh) {
        const host = bRoot && bRoot.querySelector(`[data-bld-body="${CSS.escape(String(id))}"]`);
        if (host) { builderLoadData(plugin, id, host, true); continue; }
        const live = bRoot && bRoot.querySelector(`[data-bld-live="${CSS.escape(String(id))}"]`);
        if (live) bLoadLive(plugin, id, live);
      }
    }
    if (result.modal) bOpenModal(result.modal);
    if (result.message)
      toast(result.message, result.success === false ? "err" : "ok");
  }
  return result;
}
