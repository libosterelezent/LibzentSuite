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
    // itself. The container becomes the positioning anchor, or — when
    // the caller passes anchorEl — a SPECIFIC element (the triggering
    // button's row): a form-level anchor drops the message hundreds of
    // pixels below the button the user actually clicked (user-reported
    // fix directive #1: errors belong under the component).
    const anchorRaw = (o.anchorEl && o.anchorEl !== container) ? o.anchorEl : container;
    // a BUTTON must never parent the chip (invalid HTML, click bleed,
    // clipped painting) — route button anchors through the dedicated
    // after-button slot instead
    const anchor = (anchorRaw && anchorRaw.tagName === "BUTTON"
                    && typeof fbAnchorFor === "function")
      ? fbAnchorFor(anchorRaw) : anchorRaw;
    if (f.parentElement !== anchor) {
      container.querySelectorAll(":scope [data-fb]").forEach(el => el.remove());
      anchor.querySelectorAll(":scope [data-fb]").forEach(el => el.remove());
      f = document.createElement("div"); f.dataset.fb = "1"; anchor.appendChild(f);
    }
    // USER FIX DIRECTIVE #3: exactly ONE float visible at any moment —
    // a new prompt removes every other floating chip (two rapid clicks
    // on different buttons must never stack overlapping prompts; the
    // most recent message wins). Inline field errors are unaffected.
    document.querySelectorAll(".fb-float[data-fb]").forEach(el => {
      if (el !== f) el.remove();
    });
    anchor.classList.add("fb-anchor");
    f.classList.add("fb-float");
    // USER FIX DIRECTIVE #1 (alignment): pin the chip's left edge under
    // the triggering element — right-aligned buttons (Save/Apply sit at
    // the row's right) otherwise leave the chip ~570px to their left.
    if (o.alignEl && o.alignEl !== anchor) {
      const ar = anchor.getBoundingClientRect();
      const er = o.alignEl.getBoundingClientRect();
      f.style.left = Math.max(0, Math.round(er.left - ar.left)) + "px";
    } else {
      f.style.left = "";
    }
    // USER FIX: the chip sizes to its text (max-content) but must NEVER
    // overflow the card it lives in — clamp the left edge into the
    // nearest card and cap the width so long messages wrap instead.
    requestAnimationFrame(() => {
      if (!f.isConnected) return;
      const card = anchor.closest(".dui-section, .card, .card2, .dui-container")
                   || document.body;
      const cr = card.getBoundingClientRect();
      const maxRight = cr.right - 8;
      const measure = () => f.getBoundingClientRect();
      let fr = measure();
      if (fr.left + fr.width <= maxRight) return;
      const minLeft = Math.max(cr.left + 8, 8);
      // NaN-safe: track the offset as a NUMBER (style.left may be ""
      // before the first alignment pass)
      let leftPx = parseFloat(f.style.left) || 0;
      const shift = Math.min(fr.left - minLeft, fr.left + fr.width - maxRight);
      if (shift > 0) {
        leftPx = Math.max(0, leftPx - shift);
        f.style.left = Math.round(leftPx) + "px";
      }
      fr = measure();
      const cap = maxRight - fr.left;
      if (cap > 60 && fr.width > cap) f.style.maxWidth = Math.round(cap) + "px";
    });
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
   until clicked; success self-dismisses at 4s. ``anchorEl`` pins the
   overlay under one specific element (the triggering button's row). */
function setFbFloat(container, level, text, anchorEl, alignEl) {
  return setFb(container, level, text, { float: true, anchorEl: anchorEl, alignEl: alignEl });
}
function flashFbFloat(container, text, anchorEl, alignEl) {
  return setFb(container, "ok", text, { float: true, anchorEl: anchorEl, alignEl: alignEl });
}
/* Resolve the FLOAT anchor for an action button: its own row when one
   exists (.form-actions/.dui-row/.dui-toolbar), otherwise a dedicated
   zero-size slot inserted right AFTER the button — the chip then sits
   directly under the control, aligned, in ANY layout (user fix
   directive #1: prompts belong under the component, never a card's
   bottom edge). */
function fbAnchorFor(btnEl) {
  const row = btnEl.closest(".dui-row, .form-actions, .dui-toolbar");
  if (row) return row;
  let slot = btnEl.nextElementSibling;
  if (!slot || slot.dataset.fbSlot !== "1") {
    slot = document.createElement("span");
    slot.dataset.fbSlot = "1";
    slot.style.cssText = "position:relative;display:block;width:0;height:0;";
    btnEl.after(slot);
  }
  // shift the slot back over the button's left edge so the chip appears
  // LEFT-ALIGNED under the control (the slot naturally sits at the
  // button's right edge; layout-neutral since the box is 0x0)
  slot.style.left = -btnEl.offsetWidth + "px";
  return slot;
}
/* HEADER PROMPT (user directive): add/remove and other high-priority
   outcomes surface ON TOP OF THE HEADER for 5s — ONE opaque, tone-
   railed banner, shared by every tab, independent of the component
   chips (which stay anchored under their controls). */
function showHeaderPrompt(text, level, ms) {
  level = level || "ok";
  const dur = ms === undefined ? 5000 : ms;
  let el = document.getElementById("hdr-prompt");
  if (!el) {
    el = document.createElement("div");
    el.id = "hdr-prompt";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.addEventListener("click", () => el.remove());
    document.body.appendChild(el);
  }
  clearTimeout(el._hdrTimer);
  el.className = "hdr-prompt hdr-prompt-" + level;
  el.textContent = (FB_TONE_GLYPH[level] ? FB_TONE_GLYPH[level] + "  " : "") + text;
  // restart the entrance animation when a repeat message replaces one
  el.style.animation = "none"; void el.offsetWidth; el.style.animation = "";
  if (dur > 0) el._hdrTimer = setTimeout(() => { if (el.isConnected) el.remove(); }, dur);
  return el;
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
