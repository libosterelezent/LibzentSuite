"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   ui.js — shared UI components: inline feedback, toasts, badges, empty
   states, select/datalist fillers and the dialog (popup) plumbing.
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Inline feedback ─────────────────────────────────────────────────────────
   Errors live NEXT to the action that caused them: every card, form,
   dialog and action group owns a [data-fb] slot. setFb writes it (one
   message per scope — a second action in another card cannot overwrite
   this one), clearFb removes it. A structural re-render rebuilds the DOM
   without the slot, which is exactly the "stale errors must not survive
   a state refresh" rule. */
const FB_TONE_GLYPH = { error: "✕", warn: "⚠", ok: "✓", busy: "ℹ" };

function setFb(container, level, text, opts) {
  if (!container) return;
  const o = opts || {};
  let f = container.querySelector(":scope [data-fb]");
  if (!f) { f = document.createElement("div"); f.dataset.fb = "1"; container.appendChild(f); }
  if (o.float) {
    // AWARENESS CONTRACT (knowledgebase/DESIGN_AWARENESS.md §1): the
    // message is an ANCHORED OVERLAY under the control — it never
    // pushes layout, caps its width, wraps long text and dismisses
    // itself. The container becomes the positioning anchor.
    container.classList.add("fb-anchor");
    f.classList.add("fb-float");
    // success dismisses itself (4s, industry norm); errors stay until
    // the next action or a click — they must remain actionable
    o.autoMs = o.autoMs !== undefined ? o.autoMs
             : (level === "ok" ? 4000 : (level === "error" ? 8000 : 0));
  }
  f.className = "fb fb-" + level + (o.float ? " fb-float" : "");
  f.setAttribute("role", level === "error" ? "alert" : "status");
  f.textContent = (FB_TONE_GLYPH[level] ? FB_TONE_GLYPH[level] + "  " : "") + text;
  if (o.float && o.autoMs) {
    clearTimeout(f._fbTimer);
    f._fbTimer = setTimeout(() => { if (f.isConnected) f.remove(); }, o.autoMs);
  }
  f.onclick = () => f.remove();   // click-to-dismiss (all levels)
  return f;
}
function clearFb(container) {
  if (!container) return;
  const f = container.querySelector(":scope [data-fb]");
  if (f) f.remove();
}
/* flash a success message that clears itself once the state settles */
function flashFb(container, text) {
  const f = setFb(container, "ok", text);
  if (f) setTimeout(() => { if (f.isConnected) f.remove(); }, 2500);
}
/* FLOAT variants (DESIGN_AWARENESS.md §1): anchored overlays under the
   control — they never push layout. Errors stay 8s (actionable) or
   until clicked; success self-dismisses at 4s. */
function setFbFloat(container, level, text) {
  return setFb(container, level, text, { float: true });
}
function flashFbFloat(container, text) {
  return setFb(container, "ok", text, { float: true });
}
/* busy state for a button while its action runs (.btn.busy shows a spinner) */
function withBusy(btnEl, promise) {
  if (!btnEl || btnEl.disabled) return promise;
  btnEl.classList.add("busy"); btnEl.disabled = true;
  return Promise.resolve(promise).finally(() => { btnEl.classList.remove("busy"); btnEl.disabled = false; });
}

/* ── Toasts — the only global error surface ──────────────────────────────────
   For errors with no natural component (WS drops, loader failures,
   startup faults). Replaces the old header error strip that sandwiched
   the main body. Auto-dismiss; never blocks the UI. */
function toast(msg, level="error") {
  const host = $("toasts"); if (!host) return;
  const t = document.createElement("div");
  t.className = "toast" + (level==="ok"?" toast-ok":level==="info"?" toast-info":"");
  t.textContent = msg;
  host.appendChild(t);
  while (host.children.length > 5) host.removeChild(host.firstChild);
  setTimeout(() => { if (t.isConnected) t.remove(); }, 7000);
}
function showErr(msg) { toast(msg, "error"); }

/* ── Badges / empty states / fillers ──────────────────────────────────────── */
function statusBadge(s) { return `<span class="st st-${esc(s)}">${esc(String(s||"").replace(/_/g," "))}</span>`; }
function scopeBadge(s) { return `<span class="scope scope-${esc(s)}">${esc(s)}</span>`; }
function emptyState(title, body, cta) {
  return `<div class="empty"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M9 9h6v6H9z"/></svg><h3>${esc(title)}</h3><p>${esc(body)}</p>${cta?`<button class="btn btn-primary">${esc(cta)}</button>`:""}</div>`;
}
function fillSelect(sel, names, ph) {
  if (!sel) return; const cur = sel.value;
  sel.innerHTML = `<option value="">${esc(ph)}</option>` + names.map(n=>`<option>${esc(n)}</option>`).join("");
  if (cur) sel.value = cur;
}
function fillDatalist(id, names) { const dl = $(id); if (dl) dl.innerHTML = names.map(n=>`<option>${esc(n)}</option>`).join(""); }

/* ── View-mode selector ──────────────────────────────────────────────────────
   One segmented control per page (Accounts / Groups). The buttons carry
   data-mode; renderViewSel() syncs the .active state after every render
   so switching views, WS refreshes and navigation all stay consistent. */
function renderViewSel(selId, mode) {
  const sel = $(selId); if (!sel) return;
  sel.querySelectorAll(".vm-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
}
function bindViewSel(selId, setter, rerender) {
  const sel = $(selId); if (!sel) return;
  sel.querySelectorAll(".vm-btn").forEach(b => b.onclick = () => {
    if (b.dataset.mode === (selId==="vm-acct" ? acctViewMode : groupViewMode)) return;
    setter(b.dataset.mode);
    rerender();
  });
}

/* ── Dialog (popup) plumbing ────────────────────────────────────────────────
   All popups are native <dialog>s, viewport-centered via CSS (fixed +
   inset:0 + margin:auto) with a 32px close button in the header's upper
   right. ONE delegated listener (app.js wire()) handles every
   [data-dlg-close] (close ✕ and Cancel buttons); Escape is native. */
function confirmAction(title, msg, fn, yesLabel) {
  $("cf-title").textContent = title;
  $("cf-msg").textContent = msg;
  const dlg = $("dlg-confirm");
  $("cf-yes").textContent = yesLabel || "Confirm";
  $("cf-yes").onclick = () => { dlg.close(); fn(); };
  dlg.showModal();
}

/* Shared host for the dynamic popups (details, pickers, editors): opens
   with a title, and the close-cleanup handler in wire() clears the body
   so no stale popup DOM survives navigation. */
function openDetailsPopup(title) {
  $("dt-title").textContent = title;
  const body = $("dt-body");
  body.innerHTML = "";               // fresh content every open
  clearFb(body);
  const dlg = $("dlg-details");
  if (!dlg.open) dlg.showModal();    // re-entrant opens (picker from the
  return body;                       // details card) just swap the body
}

/* ═══════════════════════════════════════════════════════════════════════════
   Decorative theme layer — customization that can NEVER touch structure.
   Accent hue + density swap CSS variables on <html>; everything else
   (layout, components, spacing scale) stays the framework's. Persisted in
   localStorage (non-sensitive, unlike the session token).
   ═══════════════════════════════════════════════════════════════════════════ */
const DASH_ACCENTS = {
  indigo: ["#818cf8", "#5b66e6"],
  blue:   ["#5ba3f5", "#3d7fd0"],
  violet: ["#a78bfa", "#7c5ce0"],
  green:  ["#34d399", "#059669"],
  amber:  ["#fbbf24", "#d97706"],
  rose:   ["#fb7185", "#e11d48"],
  cyan:   ["#22d3ee", "#0891b2"],
};
/* "#rrggbb" → "r, g, b" for the rgba(var(--accent-rgb), a) tints */
function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(", ");
}

function applyDashTheme(accent, density) {
  const root = document.documentElement;
  const name = DASH_ACCENTS[accent] ? accent
    : (localStorage.getItem("dash_accent") || "indigo");
  const pair = DASH_ACCENTS[name];
  root.style.setProperty("--accent", pair[0]);
  root.style.setProperty("--accent2", pair[1]);
  root.style.setProperty("--accent-rgb", hexToRgb(pair[0]));
  localStorage.setItem("dash_accent", name);
  const dens = ["comfortable", "compact"].includes(density) ? density
    : (localStorage.getItem("dash_density") || "comfortable");
  root.dataset.density = dens;
  localStorage.setItem("dash_density", dens);
}
