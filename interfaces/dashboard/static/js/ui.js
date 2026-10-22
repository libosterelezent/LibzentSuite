"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   ui.js — DOM helpers, formatting, toasts, dialogs, confirm boxes.
   Pure presentation utilities: no fetching, no routing, no state.
   ═══════════════════════════════════════════════════════════════════════════ */

const $ = id => document.getElementById(id);
const $$ = sel => Array.from(document.querySelectorAll(sel));

/* esc(): HTML-escape ANY dynamic value before it enters markup — text,
   attribute values, URLs rendered into tags. All interpolated data goes
   through this (audit rule: no raw ${value} from API data in templates). */
function esc(s) {
  const d = document.createElement("span");
  d.textContent = String(s ?? "");
  return d.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* el(): namespaced DOM builder — attrs.class, attrs.dataset, attrs.onclick
   and any on* handler, everything else via setAttribute. Returns a real
   element (for wiring), never a string. */
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === "class") n.className = v;
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k === "html") n.innerHTML = v;          // trusted, static markup ONLY
    else n.setAttribute(k, v);
  }
  for (const c of children.flat(9)) {
    if (c == null || c === false) continue;
    n.append(c.nodeType ? c : document.createTextNode(c));
  }
  return n;
}

/* ── formatting ───────────────────────────────────────────────────────────── */
function fmtDur(s) {
  s = Math.max(0, Math.round(+s || 0));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}
function fmtLatency(ms) {
  if (ms == null) return "—";
  const n = +ms;
  return Number.isFinite(n) ? Math.round(n) + "ms" : "—";
}
function fmtAge(ts) { return ts ? fmtDur((Date.now() - ts * 1000) / 1000) : "—"; }
function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

/* account status → tone class (hero borders, dots, badges) */
const STATUS = {
  running: {label: "running", tone: "ok"},
  connected: {label: "connected", tone: "ok"},
  idle: {label: "idle", tone: "dim"},
  stopped: {label: "stopped", tone: "dim"},
  disabled: {label: "disabled", tone: "warn"},
  error: {label: "error", tone: "err"},
  err: {label: "error", tone: "err"},
};
function statusTone(s) { return (STATUS[s] || {tone: "dim"}).tone; }

/* dot(): status dot element — .dot.on/.off/.err (see components.css) */
function dot(state) {
  const tone = state === true || state === "on" || state === "ok" ? "on"
    : state === false || state == null || state === "off" || state === "dim" ? "off"
    : "err";
  return el("span", {class: "dot " + tone, "aria-hidden": "true"});
}

/* ico(): sprite-based icon — names come from /assets/icons.svg
   (home, users, activity, shield, admin, tools, bolt, db, clock, pulse,
   plug, refresh, x, plus, gear, trash, eye, search, chevron-left, menu).
   New UI code uses THIS instead of inline path svgs. */
function ico(name, cls = "ico") {
  return el("svg", {class: cls, "aria-hidden": "true"},
    el("use", {href: "/assets/icons.svg#" + name}));
}

/* iconBtn(): 26×26 ghost icon button with an aria-label (never unlabeled).
   Accepts an inline svg path OR a sprite name (see ico()). */
function iconBtn(svgOrName, label, onclick, title) {
  const inner = svgOrName.startsWith("#") || !svgOrName.includes(" ")
    ? ico(svgOrName.replace("#", ""), "ico")
    : el("svg", {viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
                 "stroke-width": "2", "aria-hidden": "true"},
        el("path", {d: svgOrName}));
  return el("button", {
    class: "icon-btn", type: "button", "aria-label": label,
    title: title || label, onclick,
  }, inner);
}

/* ── toasts ───────────────────────────────────────────────────────────────── */
function toastRoot() {
  let r = $("lz-toasts");
  if (!r) { r = el("div", {id: "lz-toasts", "aria-live": "polite"}); document.body.append(r); }
  return r;
}
function toast(msg, kind = "info") {
  const t = el("div", {class: "toast toast-" + kind, role: "status"}, String(msg));
  toastRoot().append(t);
  requestAnimationFrame(() => t.classList.add("in"));
  setTimeout(() => { t.classList.remove("in"); setTimeout(() => t.remove(), 250); }, 4200);
}
const showErr = msg => toast(msg, "err");

/* ── dialog service ─────────────────────────────────────────────────────────
   ui.dialog builds a <dialog> on demand (Carbon structure: dlg-hdr /
   dlg-body / dlg-foot), wires ONE delegated close listener per dialog, and
   returns {close, body, dialog}. Escape works natively. */
function dialog({title, bodyEl, actions = [], wide = false, onClose} = {}) {
  const dlg = el("dialog", {class: wide ? "dlg wide" : "dlg"});
  const close = () => { if (dlg.open) dlg.close(); };
  const hdr = el("div", {class: "dlg-hdr"},
    el("span", {class: "dlg-title"}, title || ""),
    el("button", {class: "dlg-close", type: "button", "aria-label": "Close",
                  onclick: close}, "✕"));
  const body = el("div", {class: "dlg-body"});
  if (bodyEl) body.append(bodyEl);
  const foot = actions.length
    ? el("div", {class: "dlg-foot"},
        ...actions.map(a => el("button", {
          class: "btn btn-" + (a.variant || "secondary"), type: "button",
          onclick: () => a.onclick ? a.onclick(close, body) : close(),
        }, a.label)))
    : null;
  dlg.append(hdr, body, foot || "");
  dlg.addEventListener("close", () => { dlg.remove(); if (onClose) onClose(); });
  document.body.append(dlg);
  dlg.showModal();
  return {close, body, dialog: dlg};
}

/* confirmBox(): promise-based confirm — the ONLY confirm surface (never
   window.confirm, which the prompt system replaced). */
function confirmBox({title = "Confirm", body = "", yes = "Confirm", danger = true} = {}) {
  return new Promise(resolve => {
    const d = dialog({
      title,
      bodyEl: el("p", {class: "confirm-msg"}, body),
      actions: [
        {label: "Cancel", variant: "secondary", onclick: close => { close(); resolve(false); }},
        {label: yes, variant: danger ? "danger" : "primary",
         onclick: close => { close(); resolve(true); }},
      ],
      onClose: () => resolve(false),
    });
    return d;
  });
}

/* spinner(): inline busy indicator for buttons awaiting a mutation */
function spinner() {
  return el("span", {class: "spinner", "aria-hidden": "true"});
}

/* busy(): double-submit guard wrapper — disables the button, swaps in a
   spinner, re-enables in finally. Returns the wrapped handler. */
function busy(btnEl, fn) {
  return async (...args) => {
    if (btnEl.disabled) return;
    btnEl.disabled = true;
    btnEl.dataset.label = btnEl.textContent;
    btnEl.textContent = "";
    btnEl.append(spinner(), " " + btnEl.dataset.label);
    try { return await fn(...args); }
    finally {
      btnEl.disabled = false;
      btnEl.textContent = btnEl.dataset.label;
    }
  };
}

/* emptyState(): styled empty block — empty states are designed, not blank. */
function emptyState(msg, hint) {
  return el("div", {class: "empty"},
    el("div", {class: "empty-msg"}, msg),
    hint ? el("div", {class: "empty-hint dim"}, hint) : null);
}

/* collapseWrap(host, threshold): when a list container exceeds threshold
   children, hide the overflow behind a "+N more / show less" toggle.
   Returns host. Used for id chips, perms, member chips (Phase A #9). */
function collapseWrap(host, threshold = 8) {
  const items = Array.from(host.children);
  if (items.length <= threshold) return host;
  items.forEach((c, i) => { if (i >= threshold) c.classList.add("hidden"); });
  const more = el("button", {class: "btn btn-ghost btn-sm", type: "button",
    "aria-expanded": "false"},
    "+" + (items.length - threshold) + " more");
  let open = false;
  more.onclick = () => {
    open = !open;
    items.forEach((c, i) => {
      if (i >= threshold) c.classList.toggle("hidden", !open);
    });
    more.textContent = open ? "show less" : "+" + (items.length - threshold) + " more";
    more.setAttribute("aria-expanded", open ? "true" : "false");
  };
  host.append(more);
  return host;
}
