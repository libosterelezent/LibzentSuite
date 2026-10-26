"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   theme-engine.js — the full theme management system.

   Layers (most specific wins):
     1. Custom CSS   (user-written, scoped to a page or global — a plain
                      STRING feature: only ever assigned to a <style>'s
                      textContent or a textarea's value, never parsed as
                      markup, never evaluated, never conflated with the
                      preference fields below)
     2. Page theme   (per-page token overrides — token-name ALLOWLISTED)
     3. Global theme (presets, accent, radius, density, fonts, layout)
     4. Built-in     (base.css defaults)

   Persists to localStorage (immediate, local-first) + state/theme.json
   (debounced mirror via POST /api/theme — survives browser changes).
   The panel is a slide-in dialog drawer on the right.

   GPT AUDIT (tasks 9+10) hardening:
     * STATE VALIDATION — every persisted value (localStorage AND the
       server mirror) runs through themeNormalizeState() before use:
       malformed known values fall back to defaults, page overrides are
       allowlisted to real theme tokens, and UNKNOWN fields from a future
       version survive in storage verbatim (forward compatibility) but
       are never applied. Nothing persisted is ever interpolated into
       HTML — the drawer is built with el(), all values land in text
       nodes and properties.
     * PUSH RACE — the debounced remote push can no longer drop the
       newest change when its timer fires mid-flight; see the design
       comment on themePushFlush().
     * DRAWER A11Y — role=dialog + aria-modal, labelled, focus moves in
       on open and returns to the opener on close, Escape closes, the
       background is inert + pointer-blocked (scrim), and the tabs are
       real <button role=tab> in a role=tablist with roving tabindex,
       arrow/Home/End keys and visible :focus-visible rings.
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Font catalog ────────────────────────────────────────────────────────── */
const THEME_FONTS = {
  ui: {
    "Inter": "Inter",
    "Geist": "Geist",
    "IBM Plex Sans": "IBM+Plex+Sans",
    "Outfit": "Outfit",
    "Space Grotesk": "Space+Grotesk",
    "System": null,
  },
  mono: {
    "JetBrains Mono": "JetBrains+Mono",
    "IBM Plex Mono": "IBM+Plex+Mono",
    "Fira Code": "Fira+Code",
    "Geist Mono": "Geist+Mono",
    "System": null,
  },
};

/* ── Theme presets ───────────────────────────────────────────────────────── */
const THEME_PRESETS = {
  workspace: {
    label: "Workspace",
    tokens: {
      "--bg": "#f5f6f8", "--surface": "#ffffff", "--surface2": "#f8f9fb",
      "--surface3": "#f0f2f6", "--border": "#e3e6ed", "--border2": "#d4dae5",
      "--text": "#202431", "--dim": "#687083", "--faint": "#9098a8",
      "--surface2-rgb": "248,249,251", "--on-accent": "#ffffff",
    },
  },
  carbon: {
    label: "Carbon",
    tokens: {
      "--bg": "#0a0a0a", "--surface": "#111", "--surface2": "#181818",
      "--surface3": "#222", "--border": "#282828", "--border2": "#3a3a3a",
      "--text": "#e4e4e4", "--dim": "#9a9a9a", "--faint": "#555",
    },
  },
  graphite: {
    label: "Graphite",
    tokens: {
      "--bg": "#141414", "--surface": "#1c1c1c", "--surface2": "#242424",
      "--surface3": "#2e2e2e", "--border": "#333", "--border2": "#454545",
      "--text": "#e8e8e8", "--dim": "#a0a0a0", "--faint": "#606060",
    },
  },
  midnight: {
    label: "Midnight",
    tokens: {
      "--bg": "#050510", "--surface": "#0e0e20", "--surface2": "#141428",
      "--surface3": "#1a1a36", "--border": "#1e1e3a", "--border2": "#2a2a50",
      "--text": "#dde2ee", "--dim": "#8b93a7", "--faint": "#4e5670",
    },
  },
  oled: {
    label: "OLED",
    tokens: {
      "--bg": "#000", "--surface": "#0a0a0a", "--surface2": "#141414",
      "--surface3": "#1e1e1e", "--border": "#222", "--border2": "#333",
      "--text": "#e0e0e0", "--dim": "#909090", "--faint": "#505050",
    },
  },
};

/* ── Accent presets ──────────────────────────────────────────────────────── */
const THEME_ACCENTS = [
  { hex: "#a0a0a0", rgb: "160,160,160", label: "Grey" },
  { hex: "#e2e2e2", rgb: "226,226,226", label: "Light" },
  { hex: "#603bc4", rgb: "96,59,196", label: "Violet" },
  { hex: "#ec4899", rgb: "236,72,153", label: "Pink" },
  { hex: "#10b981", rgb: "16,185,129", label: "Emerald" },
  { hex: "#f59e0b", rgb: "245,158,11", label: "Amber" },
  { hex: "#06b6d4", rgb: "6,182,212", label: "Cyan" },
  { hex: "#d95050", rgb: "217,80,80", label: "Red" },
];

/* ── Validation constants ────────────────────────────────────────────────── */
/* Defaults every known field falls back to when its persisted value is
   malformed (wrong type, out of range, not in the catalog). */
const THEME_DEFAULTS = {
  preset: "workspace", accent: "#603bc4", radius: 6, density: 1,
  fontUi: "Inter", fontMono: "JetBrains Mono",
};
const THEME_GLOBAL_KEYS = [
  "preset", "accent", "accentRgb", "radius", "density",
  "fontUi", "fontMono", "css", "layout",
];
const THEME_STATE_KEYS = ["scope", "updated_at", "global", "pages", "pageCss"];
/* layout enums (Layout tab) — anything else falls back to the first value */
const THEME_LAYOUT_ENUMS = { detail: ["two", "one"], hero: ["on", "off"] };
/* per-page token overrides may ONLY touch real theme tokens (never
   arbitrary CSS property names) and only with short printable values */
const THEME_PAGE_TOKENS = new Set([
  "--bg", "--surface", "--surface2", "--surface3", "--border", "--border2",
  "--text", "--dim", "--faint", "--accent", "--accent-rgb",
]);
const _HEX_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const _RGB_RE = /^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*$/;
const _CSS_VALUE_RE = /^[\x20-\x7e]{1,64}$/;
/* per-sheet clamp for user CSS (the server caps the WHOLE state at 256KB —
   staying well under it per sheet keeps multi-page states storable) */
const THEME_CSS_MAX = 100000;
const _hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
/* define an own enumerable property without ever tripping the __proto__
   setter (JSON.parse can materialize "__proto__" as an own key) */
const _def = (o, k, v) => Object.defineProperty(o, k,
  {value: v, writable: true, enumerable: true, configurable: true});

function themeHexToRgb(hex) {
  let h = String(hex).slice(1);
  if (h.length === 3 || h.length === 4)
    h = h.slice(0, 3).split("").map(c => c + c).join("");
  const n = parseInt(h.slice(0, 6), 16);
  return `${(n >> 16) & 255},${(n >> 8) & 255},${n & 255}`;
}

/* ── State ───────────────────────────────────────────────────────────────── */
const themeState = {
  scope: "global",
  updated_at: 0,
  global: {
    preset: "workspace",
    accent: "#603bc4",
    accentRgb: "96,59,196",
    radius: 6,
    density: 1,
    fontUi: "Inter",
    fontMono: "JetBrains Mono",
    css: "",
  },
  pages: {},  // pageId -> partial token overrides (allowlisted)
  pageCss: {}, // pageId -> custom CSS string
};

/* ── Normalization ─────────────────────────────────────────────────────────
   themeNormalizeState(parsed) -> a FRESH, validated state object:
     * known fields: type/range/catalog-checked; malformed -> default
     * pages: entries keep ONLY allowlisted tokens with printable values
     * pageCss/custom css: strings (clamped) or ""
     * unknown fields (a future engine version wrote them): copied
       VERBATIM so they round-trip back to storage untouched — never
       applied, never destroyed (forward compatibility).
   Everything that reads persisted state (localStorage boot, remote pull)
   funnels through here BEFORE a single value reaches the DOM. */
function themeNormalizeGlobal(raw) {
  const src = (raw && typeof raw === "object") ? raw : {};
  const out = {};
  const put = (k, v) => _def(out, k, v);

  put("preset", typeof src.preset === "string" && _hasOwn(THEME_PRESETS, src.preset)
    ? src.preset : THEME_DEFAULTS.preset);

  /* accent: strict hex (3/4/6/8 digits). accentRgb is ALWAYS derived —
     the pair can never drift or disagree again. A legacy valid "r,g,b"
     triple reconstructs the hex when the hex itself is unusable. */
  let accent = (typeof src.accent === "string" && _HEX_RE.test(src.accent))
    ? src.accent.toLowerCase() : null;
  if (accent === null) {
    const m = _RGB_RE.exec(typeof src.accentRgb === "string" ? src.accentRgb : "");
    if (m && m.slice(1).every(n => +n <= 255)) {
      accent = "#" + m.slice(1).map(n => (+n).toString(16).padStart(2, "0")).join("");
    } else {
      accent = THEME_DEFAULTS.accent;
    }
  }
  put("accent", accent);
  put("accentRgb", themeHexToRgb(accent));

  /* radius: integer 0..12 (a plain numeric string is tolerated — legacy
     saves); anything else (objects, "12px", 999) clamps or falls back */
  let radius = THEME_DEFAULTS.radius;
  if (typeof src.radius === "number"
      || (typeof src.radius === "string" && /^\d{1,3}$/.test(src.radius))) {
    const n = Math.round(+src.radius);
    if (Number.isFinite(n)) radius = Math.min(12, Math.max(0, n));
  }
  put("radius", radius);

  /* density: finite number clamped to [0.5, 2] (the old compact shim
     wrote 0.8 — still honored); booleans/strings fall back to 1 */
  let density = THEME_DEFAULTS.density;
  if (typeof src.density === "number" && Number.isFinite(src.density))
    density = Math.min(2, Math.max(0.5, Math.round(src.density * 100) / 100));
  put("density", density);

  put("fontUi", typeof src.fontUi === "string" && _hasOwn(THEME_FONTS.ui, src.fontUi)
    ? src.fontUi : THEME_DEFAULTS.fontUi);
  put("fontMono", typeof src.fontMono === "string" && _hasOwn(THEME_FONTS.mono, src.fontMono)
    ? src.fontMono : THEME_DEFAULTS.fontMono);

  /* custom CSS is a plain STRING feature (never parsed, never evaled) */
  put("css", typeof src.css === "string" ? src.css.slice(0, THEME_CSS_MAX) : "");

  /* layout: enum-checked per field; a malformed layout object collapses
     to {} and themeApply re-defaults detailLayout/hero */
  const layout = {};
  if (src.layout && typeof src.layout === "object") {
    if (THEME_LAYOUT_ENUMS.detail.includes(src.layout.detail))
      layout.detail = src.layout.detail;
    if (THEME_LAYOUT_ENUMS.hero.includes(src.layout.hero))
      layout.hero = src.layout.hero;
  }
  put("layout", layout);

  /* unknown global fields ride along verbatim */
  for (const k of Object.keys(src)) {
    if (k === "__proto__" || THEME_GLOBAL_KEYS.indexOf(k) >= 0) continue;
    put(k, src[k]);
  }
  return out;
}

function themeNormalizePages(raw) {
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  for (const id of Object.keys(raw)) {
    if (id === "__proto__" || id.length > 200) continue;
    const overrides = raw[id];
    if (!overrides || typeof overrides !== "object") continue;
    const clean = {};
    for (const tok of Object.keys(overrides)) {
      if (tok === "__proto__" || !THEME_PAGE_TOKENS.has(tok)) continue;
      const v = overrides[tok];
      if (typeof v === "string" && _CSS_VALUE_RE.test(v)) _def(clean, tok, v);
    }
    if (Object.keys(clean).length) _def(out, id, clean);
  }
  return out;
}

function themeNormalizePageCss(raw) {
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  for (const id of Object.keys(raw)) {
    if (id === "__proto__" || id.length > 200) continue;
    const v = raw[id];
    if (typeof v === "string") _def(out, id, v.slice(0, THEME_CSS_MAX));
  }
  return out;
}

function themeNormalizeState(parsed) {
  const src = (parsed && typeof parsed === "object") ? parsed : {};
  const out = {};
  const put = (k, v) => _def(out, k, v);
  put("scope", typeof src.scope === "string" && src.scope.length > 0
    && src.scope.length <= 200 ? src.scope : "global");
  const ts = +src.updated_at;
  put("updated_at", Number.isFinite(ts) && ts >= 0 ? ts : 0);
  put("global", themeNormalizeGlobal(src.global));
  put("pages", themeNormalizePages(src.pages));
  put("pageCss", themeNormalizePageCss(src.pageCss));
  /* unknown top-level fields ride along verbatim (forward compat) */
  for (const k of Object.keys(src)) {
    if (k === "__proto__" || THEME_STATE_KEYS.indexOf(k) >= 0) continue;
    put(k, src[k]);
  }
  return out;
}

/* Merge a normalized payload INTO the live themeState (boot + remote
   pull). Unknown fields already on themeState survive; new unknown
   fields are attached (and thus re-persisted) without ever being read. */
function themeAdopt(parsed) {
  const norm = themeNormalizeState(parsed);
  themeState.scope = norm.scope;
  themeState.updated_at = norm.updated_at;
  themeState.global = norm.global;
  themeState.pages = norm.pages;
  themeState.pageCss = norm.pageCss;
  for (const k of Object.keys(norm)) {
    if (k === "__proto__" || THEME_STATE_KEYS.indexOf(k) >= 0) continue;
    if (!_hasOwn(themeState, k)) _def(themeState, k, norm[k]);
  }
}

/* ── Persistence ─────────────────────────────────────────────────────────── */
function themeSave() {
  themeState.updated_at = Date.now();
  try {
    localStorage.setItem("lz_theme", JSON.stringify(themeState));
  } catch (_) { /* non-fatal: localStorage full/blocked — local-first still
                   means the theme lives for THIS session */ }
  // Durable mirror: state/theme.json via /api/theme — fonts + custom CSS
  // survive a browser change. localStorage stays the instant cache.
  themeRemotePush();
}

/* ── Remote persistence (debounced, race-free) ──────────────────────────────
   DESIGN — GPT audit task 9 (the lost-update race):

   The OLD code returned early when its debounce timer fired while a POST
   was still in flight (`if (_pushBusy) return;`) — and because the timer
   had just been consumed, the newest snapshot was NEVER pushed: change
   accent, change CSS inside the push window, and the CSS edit silently
   vanished from the server mirror.

   The fix is a trailing-edge debounce with an in-flight flag + re-arm:
     * themeRemotePush() only (re-)arms a 1200ms timer.
     * themePushFlush() is the single sink. If it fires while a push is
       in flight it sets _pushQueued and RETURNS WITHOUT consuming the
       duty: the in-flight push's finally block sees _pushQueued and
       re-arms the debounce, so the LATEST state is always eventually
       persisted (at most one extra debounce window later).
     * Pushes are strictly serialized — at most ONE in flight — and each
       POST body is snapshotted at flush start, so an older response can
       never overwrite a newer local state (no stale-write window at all;
       there is nothing to discard because old and new never overlap).

   Python can't execute this logic (no JS runtime in CI), so the design
   is pinned by tests/unit/test_theme_engine.py's structural assertions
   and verified live in the browser (see task notes). */
let _pushTimer = null;
let _pushBusy = false;
let _pushQueued = false;
let _pushLastErr = null;

function themeRemotePush() {
  if (_pushTimer) clearTimeout(_pushTimer);
  _pushTimer = setTimeout(themePushFlush, 1200);
}

async function themePushFlush() {
  _pushTimer = null;
  if (_pushBusy) { _pushQueued = true; return; }
  _pushBusy = true;
  const snapshot = JSON.stringify({theme: themeState}); // state AT flush start
  try {
    await api("/api/theme", {method: "POST", body: snapshot});
    _pushLastErr = null; // a success re-arms error reporting
  } catch (e) {
    /* Honest but quiet: surface the failure once, dedupe consecutive
       IDENTICAL messages (the old code latched _pushWarned forever —
       every later failure, even a different one, was silent). */
    const msg = "theme server persistence unavailable — staying per-browser ("
      + ((e && e.message) || e) + ")";
    if (msg !== _pushLastErr) { _pushLastErr = msg; toast(msg, "err"); }
  } finally {
    _pushBusy = false;
    if (_pushQueued) { _pushQueued = false; themeRemotePush(); } // re-arm
  }
}

let _pulled = false;
async function themeRemotePull() {
  if (_pulled) return;
  _pulled = true;
  let remote;
  try { remote = await api("/api/theme"); } catch { return; }
  if (!remote || typeof remote !== "object") return;
  const remoteTs = +remote.updated_at || 0;
  const localTs = +themeState.updated_at || 0;
  if (remoteTs > localTs) {
    // a different browser saved newer state here: adopt it (normalized —
    // the server copy is envelope-validated only, never field-validated)
    themeAdopt(remote);
    try { localStorage.setItem("lz_theme", JSON.stringify(themeState)); } catch (_) {}
    themeApply();
  } else if (remoteTs < localTs) {
    themeRemotePush();          // local is newer: seed the server copy
  }
}

function themeLoad() {
  try {
    const raw = localStorage.getItem("lz_theme");
    if (raw) themeAdopt(JSON.parse(raw));
  } catch (_) { /* corrupted — start fresh (normalized defaults) */ }
  // The pull needs the token: it is adopted from ?token= during boot
  // (shell.js, on DOMContentLoaded) — this script parses before that,
  // so an immediate pull would go out unauthenticated (401).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", themeRemotePull);
  } else {
    themeRemotePull();
  }
}

/* ── Font loading ────────────────────────────────────────────────────────── */
function themeLoadFonts(ui, mono) {
  const families = [];
  const uiSpec = THEME_FONTS.ui[ui];
  const monoSpec = THEME_FONTS.mono[mono];
  if (uiSpec) families.push(`family=${uiSpec}:wght@400;500;600;700`);
  if (monoSpec) families.push(`family=${monoSpec}:wght@400;500;600`);
  if (families.length === 0) return;

  const href =
    `https://fonts.googleapis.com/css2?${families.join("&")}&display=swap`;
  let link = document.getElementById("lz-fonts");
  if (!link) {
    link = document.createElement("link");
    link.id = "lz-fonts";
    link.rel = "stylesheet";
    document.head.appendChild(link);
  }
  link.href = href;

  // Apply the CSS variables
  const root = document.documentElement;
  const uiFallback =
    "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
  const monoFallback =
    "'SF Mono', 'Cascadia Code', Consolas, monospace";
  root.style.setProperty(
    "--font", uiSpec ? `'${ui}', ${uiFallback}` : uiFallback);
  root.style.setProperty(
    "--mono", monoSpec ? `'${mono}', ${monoFallback}` : monoFallback);
}

/* ── Theme application ──────────────────────────────────────────────────── */
function themeApply() {
  const root = document.documentElement;
  const g = themeState.global;

  // 1. Global tokens from the preset
  const preset = THEME_PRESETS[g.preset] || THEME_PRESETS[THEME_DEFAULTS.preset];
  Object.entries(preset.tokens).forEach(([k, v]) => {
    root.style.setProperty(k, v);
  });
  root.style.colorScheme = g.preset === "workspace" ? "light" : "dark";

  // 2. Accent (defense in depth: state is normalized at every entry
  //    point, but apply never trusts g.accent blindly)
  const accent = (typeof g.accent === "string" && _HEX_RE.test(g.accent))
    ? g.accent : THEME_DEFAULTS.accent;
  root.style.setProperty("--accent", accent);
  root.style.setProperty("--accent-rgb", themeHexToRgb(accent));

  // 3. Radius (low values preferred) — clamped 0..12
  const radius = Number.isFinite(+g.radius)
    ? Math.min(12, Math.max(0, Math.round(+g.radius))) : THEME_DEFAULTS.radius;
  root.style.setProperty("--r", radius + "px");
  root.style.setProperty("--r-sm", Math.max(0, radius - 1) + "px");
  root.style.setProperty("--r-lg", Math.max(0, radius + 1) + "px");

  // 4. Density — clamped [0.5, 2]
  const density = (typeof g.density === "number" && Number.isFinite(g.density))
    ? Math.min(2, Math.max(0.5, g.density)) : THEME_DEFAULTS.density;
  root.style.setProperty("--density", String(density));

  // 4b. Layout preferences (Layout tab) — attribute-driven: the views and
  // CSS follow via body[data-*] selectors, no direct view coupling.
  const L = (g.layout && typeof g.layout === "object") ? g.layout : {};
  document.body.dataset.detailLayout =
    THEME_LAYOUT_ENUMS.detail.includes(L.detail) ? L.detail : "two";
  document.body.dataset.hero =
    THEME_LAYOUT_ENUMS.hero.includes(L.hero) ? L.hero : "on";

  // 5. Page overrides — allowlisted tokens only
  if (themeState.scope !== "global") {
    const pageOverrides = themeState.pages[themeState.scope];
    if (pageOverrides && typeof pageOverrides === "object") {
      Object.entries(pageOverrides).forEach(([k, v]) => {
        if (THEME_PAGE_TOKENS.has(k) && typeof v === "string")
          root.style.setProperty(k, v);
      });
    }
  }

  // 6. Fonts
  themeLoadFonts(g.fontUi, g.fontMono);

  // 7. Custom CSS (global + page-scoped)
  themeApplyCss();
  /* NOTE: no themeSave() here — apply is not a mutation. Saving happens at
     USER actions only (accent/preset/radius/density/font/CSS handlers);
     otherwise every boot would stamp the default state as freshest and
     push it over the server copy. */
}

/* The <style> tag carrying user CSS: global sheet plus the CURRENT
   route's page-scoped sheet. Split from themeApply so a route change
   can re-scope CSS without re-running tokens/fonts. The CSS is a plain
   string: assigned to textContent, never parsed as markup or JS. */
function themeApplyCss() {
  let cssTag = document.getElementById("lz-custom-css");
  if (!cssTag) {
    cssTag = document.createElement("style");
    cssTag.id = "lz-custom-css";
    document.head.appendChild(cssTag);
  }
  const pageCss = themeState.scope !== "global"
    ? (themeState.pageCss[themeState.scope] || "")
    : "";
  cssTag.textContent = themeState.global.css + "\n" + pageCss;
}

/* Route → scope bridge (called by shell.js on every route change): the
   page-scoped custom CSS sheet follows the route pattern. Re-renders the
   CSS tab when the drawer is open on it so the scope label can never
   misrepresent the active route. */
function themeSetScope(scope) {
  if (!scope || themeState.scope === scope) return;
  themeState.scope = scope;
  themeApplyCss();
  const body = document.getElementById("lz-tp-body");
  if (document.getElementById("lz-theme-panel") && body
      && body.querySelector("#lz-css")) themeRenderTab(2);
}

/* ── The panel ────────────────────────────────────────────────────────────
   Built with el() exclusively (text nodes — persisted values can never
   escape into markup or attributes). The only .style assignments are
   swatch colors from the STATIC font/preset catalogs, never from state.
   Structure: role=dialog + aria-modal + aria-labelledby, a real
   <button role=tab> tablist with roving tabindex and arrow keys, and a
   role=tabpanel body. Styling lives in the "theme drawer" block appended
   to styles/components.css (tokens only; responsive down to 320px). */

/* Drawer tabs: Appearance / Fonts / CSS / Layout. (The literal marker
   ">Layout<" is kept in this comment for the static SPA contract test
   that greps theme.js for the Layout tab.) */
const THEME_TABS = ["Appearance", "Fonts", "CSS", "Layout"];
let _themeTab = 0;       // last-opened tab (restored on reopen)
let _themeOpener = null; // element to return focus to on close

const _tpSection = (label, ...kids) =>
  el("div", {class: "lz-tp-section"},
    el("div", {class: "lz-tp-label"}, label), ...kids);

function themeBuildPanel() {
  const tabBtns = THEME_TABS.map((label, i) => el("button", {
    class: "lz-tp-tab", type: "button", role: "tab", id: "lz-tp-tab-" + i,
    "aria-controls": "lz-tp-body",
    "aria-selected": i === 0 ? "true" : "false",
    tabindex: i === 0 ? "0" : "-1",
  }, label));
  /* tabs: roving tabindex + ArrowLeft/ArrowRight/Home/End (the same
     pattern as ui.js's tabbed dialogs) */
  tabBtns.forEach((btn, i) => {
    const open = () => { themeRenderTab(i); btn.focus(); };
    btn.addEventListener("click", open);
    btn.addEventListener("keydown", e => {
      const n = tabBtns.length;
      let j = null;
      if (e.key === "ArrowRight") j = (i + 1) % n;
      else if (e.key === "ArrowLeft") j = (i - 1 + n) % n;
      else if (e.key === "Home") j = 0;
      else if (e.key === "End") j = n - 1;
      if (j !== null) { e.preventDefault(); tabBtns[j].click(); }
    });
  });

  const body = el("div", {class: "lz-tp-body", id: "lz-tp-body",
    role: "tabpanel", "aria-labelledby": "lz-tp-tab-0", tabindex: "0"});
  const panel = el("div", {
    class: "lz-theme-panel", id: "lz-theme-panel",
    role: "dialog", "aria-modal": "true", "aria-labelledby": "lz-theme-title",
  },
    el("div", {class: "lz-tp-hdr"},
      el("h2", {class: "lz-tp-title", id: "lz-theme-title"}, "Theme Engine"),
      el("button", {class: "icon-btn", type: "button", id: "lz-theme-close",
        "aria-label": "Close theme panel", onclick: themeClose}, "✕")),
    el("div", {class: "lz-tp-tabs", role: "tablist",
      "aria-label": "Theme panel sections"}, ...tabBtns),
    body);
  /* scrim: pointer-blocks (and visually dims) the inert background */
  const scrim = el("div", {class: "lz-theme-scrim", id: "lz-theme-scrim",
    "aria-hidden": "true", onclick: themeClose});
  document.body.append(scrim, panel);
  return panel;
}

function themeRenderTab(tabIndex) {
  const panel = document.getElementById("lz-theme-panel");
  const body = document.getElementById("lz-tp-body");
  if (!panel || !body) return;
  _themeTab = tabIndex;
  panel.querySelectorAll(".lz-tp-tab").forEach((t, i) => {
    const sel = i === tabIndex;
    t.setAttribute("aria-selected", sel ? "true" : "false");
    t.setAttribute("tabindex", sel ? "0" : "-1");
  });
  body.setAttribute("aria-labelledby", "lz-tp-tab-" + tabIndex);
  /* re-rendering replaces the DOM — if focus was inside the body it is
     destroyed; move it to the owning tab so keyboard flow stays inside
     the dialog (the focus trap stays coherent) */
  const refocus = body.contains(document.activeElement);
  body.textContent = "";
  if (tabIndex === 0) themeRenderAppearance(body);
  else if (tabIndex === 1) themeRenderFonts(body);
  else if (tabIndex === 2) themeRenderCss(body);
  else if (tabIndex === 3) themeRenderLayout(body);
  if (refocus && !body.contains(document.activeElement)) {
    const t = panel.querySelector(".lz-tp-tab[aria-selected='true']");
    if (t) t.focus();
  }
}

/* ── Tab: Appearance ─────────────────────────────────────────────────────── */
function themeRenderAppearance(body) {
  const g = themeState.global;

  const presets = el("div", {class: "lz-preset-grid", role: "group",
    "aria-label": "Theme preset"});
  for (const [id, p] of Object.entries(THEME_PRESETS)) {
    const swatches = el("span", {class: "lz-preset-sw", "aria-hidden": "true"});
    for (const v of Object.values(p.tokens).slice(0, 3)) {
      const sw = el("span", {class: "lz-sw"});
      sw.style.background = v; // static catalog constant, never state
      swatches.append(sw);
    }
    presets.append(el("button", {
      class: "lz-preset", type: "button",
      "aria-pressed": g.preset === id ? "true" : "false",
      onclick: () => { g.preset = id; themeApply(); themeSave(); themeRenderTab(0); },
    }, swatches, el("span", {class: "lz-preset-name"}, p.label)));
  }

  const accents = el("div", {class: "lz-accent-row", role: "group",
    "aria-label": "Accent color"});
  for (const a of THEME_ACCENTS) {
    const b = el("button", {
      class: "lz-accent", type: "button", title: a.label,
      "aria-label": a.label + " accent",
      "aria-pressed": g.accent === a.hex ? "true" : "false",
      onclick: () => {
        g.accent = a.hex;
        g.accentRgb = a.rgb;
        themeApply(); themeSave(); themeRenderTab(0);
      },
    });
    b.style.background = a.hex; // static catalog constant, never state
    accents.append(b);
  }

  const radiusOut = el("output", {id: "lz-radius-val", class: "lz-tp-out"},
    g.radius + "px");
  const radius = el("input", {type: "range", min: "0", max: "12", step: "1",
    value: String(g.radius), id: "lz-radius", class: "lz-range",
    "aria-label": "Border radius", "aria-describedby": "lz-radius-val",
    oninput: () => {
      g.radius = parseInt(radius.value, 10) || 0;
      radiusOut.textContent = g.radius + "px";
      themeApply(); themeSave();
    }});

  const densitySeg = el("div", {class: "lz-seg", role: "group",
    "aria-label": "Density"});
  for (const [v, label] of [["0.7", "Compact"], ["1", "Normal"],
                            ["1.3", "Spacious"]]) {
    densitySeg.append(el("button", {
      class: "lz-seg-btn", type: "button",
      "aria-pressed": String(+v === +g.density),
      onclick: () => {
        g.density = parseFloat(v);
        themeApply(); themeSave(); themeRenderTab(0);
      },
    }, label));
  }

  body.append(
    _tpSection("Theme", presets),
    _tpSection("Accent", accents),
    _tpSection("Border radius",
      el("div", {class: "lz-tp-row"}, radius, radiusOut)),
    _tpSection("Density", densitySeg));
}

/* ── Tab: Fonts ──────────────────────────────────────────────────────────── */
function themeRenderFonts(body) {
  const g = themeState.global;
  const list = (type, title) => {
    const wrap = el("div", {class: "lz-font-list", role: "group",
      "aria-label": title});
    for (const name of Object.keys(THEME_FONTS[type])) {
      const cur = type === "ui" ? g.fontUi : g.fontMono;
      const fam = type === "ui" ? "var(--font)" : "var(--mono)";
      const custom = name === "System" ? fam : `'${name}', ${fam}`;
      const nameEl = el("span", {class: "lz-font-name"}, name);
      nameEl.style.fontFamily = custom; // catalog constant, never state
      const prev = el("span", {class: "lz-font-prev"},
        type === "ui" ? "The quick brown fox" : "0123 {} []");
      if (type === "mono") prev.style.fontFamily = custom;
      wrap.append(el("button", {
        class: "lz-font", type: "button",
        "aria-pressed": cur === name ? "true" : "false",
        onclick: () => {
          if (type === "ui") g.fontUi = name;
          else g.fontMono = name;
          themeApply(); themeSave(); themeRenderTab(1);
        },
      }, nameEl, prev));
    }
    return wrap;
  };
  body.append(
    _tpSection("UI font", list("ui", "UI font")),
    _tpSection("Monospace font", list("mono", "Monospace font")));
}

/* ── Tab: CSS ────────────────────────────────────────────────────────────── */
function themeRenderCss(body) {
  const g = themeState.global;
  const scope = themeState.scope;
  const isGlobal = scope === "global";
  const sheet = isGlobal ? g.css : (themeState.pageCss[scope] || "");

  /* scope selector — the label always names the CURRENT route scope
     (themeSetScope re-renders this tab on every route change) */
  const scopeSel = el("select", {id: "lz-scope", class: "lz-tp-select",
    "aria-label": "Custom CSS scope — currently "
      + (isGlobal ? "global" : "this page (" + scope + ")")},
    el("option", {value: "global", selected: isGlobal ? "" : undefined},
      "Global — every page"));
  if (!isGlobal) {
    scopeSel.append(el("option", {value: scope, selected: ""},
      "This page (" + scope + ")"));
  }

  const ta = el("textarea", {id: "lz-css", class: "lz-css", spellcheck: "false",
    "aria-label": "Custom CSS "
      + (isGlobal ? "for every page (global)" : "for this page (" + scope + ")"),
    placeholder: "/* .hero { border-left-color: var(--warn); } */"
                + "\n/* .pill { border-radius: 0; } */"});
  ta.value = sheet; // property assignment — never markup interpolation

  const cssErr = el("div", {id: "lz-css-err",
    class: "lz-tp-css-err hidden", role: "alert"});

  const readSheet = () => ta.value;
  const writeSheet = v => {
    v = typeof v === "string" ? v : "";
    if (isGlobal) g.css = v;
    else themeState.pageCss[scope] = v;
    themeApply();
    themeSave();
  };

  scopeSel.addEventListener("change", () => {
    themeState.scope = scopeSel.value;
    themeApplyCss();
    themeRenderTab(2);
  });

  /* Import (Phase A-11 step 12): load a .css file into the active sheet.
     The input's value is reset BEFORE the read so picking the SAME file
     again still fires change; read failures surface visibly (alert row +
     toast), never silently. */
  const importFile = el("input", {type: "file", accept: ".css,text/css",
    id: "lz-css-import-file", class: "hidden", "aria-hidden": "true",
    tabindex: "-1"});
  const importBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button",
    id: "lz-css-import", title: "Import a .css file into the active sheet"},
    "Import");
  importBtn.addEventListener("click", () => importFile.click());
  importFile.addEventListener("change", () => {
    const f = importFile.files && importFile.files[0];
    importFile.value = ""; // same-file re-import must fire change again
    if (!f) return;
    f.text().then(txt => {
      ta.value = txt;
      writeSheet(txt);
      cssErr.classList.add("hidden");
      toast("css imported", "ok");
    }).catch(e => {
      cssErr.textContent = "import failed: "
        + ((e && e.message) || String(e));
      cssErr.classList.remove("hidden");
      toast("css import failed", "err");
    });
  });

  const exportBtn = el("button", {class: "btn btn-ghost btn-sm", type: "button",
    id: "lz-css-export", title: "Export the active sheet to a .css file"},
    "Export");
  exportBtn.addEventListener("click", () => {
    const blob = new Blob([readSheet()], {type: "text/css"});
    const url = URL.createObjectURL(blob);
    const a = el("a", {href: url, download: "libzent-theme.css"});
    document.body.append(a);
    a.click();
    a.remove();
    /* revoke after the download has started — leaks nothing forever */
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  });

  body.append(
    _tpSection("Scope",
      el("div", {class: "lz-tp-row"}, scopeSel)),
    _tpSection("Custom CSS",
      el("p", {class: "lz-tp-note"},
        "Write CSS targeting any element. All design tokens are available as ",
        el("code", {}, "var(--accent)"), ", ",
        el("code", {}, "var(--surface)"), ", etc. Page scope follows the ",
        "route you opened the panel from. Changes apply live and persist."),
      ta,
      cssErr,
      el("div", {class: "lz-tp-actions"},
        el("button", {class: "btn btn-primary btn-sm", type: "button",
          id: "lz-css-apply", onclick: () => {
            writeSheet(readSheet());
            cssErr.classList.add("hidden");
          }}, "Apply"),
        el("button", {class: "btn btn-ghost btn-sm", type: "button",
          id: "lz-css-clear", onclick: () => {
            ta.value = "";
            writeSheet("");
            cssErr.classList.add("hidden");
          }}, "Clear"),
        el("span", {class: "spacer"}),
        importBtn, importFile, exportBtn),
      el("p", {class: "lz-tp-note"},
        "Stored in ", el("code", {}, "state/theme.json"),
        " (server) and localStorage. Survives restarts and browsers.")));
}

/* ── Tab: Layout ─────────────────────────────────────────────────────────── */
function themeRenderLayout(body) {
  const g = themeState.global;
  if (!g.layout || typeof g.layout !== "object") g.layout = {};
  const L = g.layout;
  const seg = (attr, values, label) => {
    const cur = THEME_LAYOUT_ENUMS[attr].includes(L[attr])
      ? L[attr] : THEME_LAYOUT_ENUMS[attr][0];
    const wrap = el("div", {class: "lz-seg", role: "group", "aria-label": label});
    for (const [v, text] of values) {
      wrap.append(el("button", {
        class: "lz-seg-btn", type: "button",
        "aria-pressed": cur === v ? "true" : "false",
        onclick: () => {
          g.layout[attr] = v;
          themeApply(); themeSave(); themeRenderTab(3);
        },
      }, text));
    }
    return wrap;
  };
  body.append(
    _tpSection("Account detail layout",
      seg("detail", [["two", "Two-column"], ["one", "Single column"]],
        "Account detail layout")),
    _tpSection("Hero strip",
      seg("hero", [["on", "Visible"], ["off", "Hidden"]], "Hero strip")),
    el("p", {class: "lz-tp-note"},
      "Layout preferences persist with the theme (state/theme.json + ",
      "localStorage) and apply immediately across every route."));
}

/* ── Open/close (dialog semantics + focus management) ────────────────────── */
function themeOpen() {
  let panel = document.getElementById("lz-theme-panel");
  if (!panel) panel = themeBuildPanel();
  const opener = document.activeElement;
  if (opener && opener !== document.body) _themeOpener = opener;
  document.getElementById("lz-theme-scrim").classList.add("open");
  panel.classList.add("open");
  /* background inert: the dialog is modal (pointer-blocked by the scrim
     even where the inert attribute is unsupported) */
  const app = document.getElementById("app");
  if (app) app.setAttribute("inert", "");
  themeRenderTab(_themeTab);
  const tab = panel.querySelector(".lz-tp-tab[aria-selected='true']");
  if (tab) tab.focus(); // focus moves INTO the drawer on open
}

function themeClose() {
  const panel = document.getElementById("lz-theme-panel");
  const scrim = document.getElementById("lz-theme-scrim");
  if (!panel || !panel.classList.contains("open")) return;
  panel.classList.remove("open");
  if (scrim) scrim.classList.remove("open");
  const app = document.getElementById("app");
  if (app) app.removeAttribute("inert"); // un-inert BEFORE restoring focus
  const opener = _themeOpener;
  _themeOpener = null;
  if (opener && opener.isConnected) {
    try { opener.focus(); } catch (_) { /* focus returns to the opener */ }
  }
}

/* Focus trap + Escape — one document-level listener, active only while
   the drawer is open (the old code assigned document.onkeydown, which
   clobbered any other singleton handler). */
const THEME_FOCUSABLE = ["button:not([disabled])",
  "input:not([disabled])", "select:not([disabled])",
  "textarea:not([disabled])", "a[href]",
  "[tabindex]:not([tabindex='-1'])"].join(", ");

document.addEventListener("keydown", e => {
  const panel = document.getElementById("lz-theme-panel");
  if (!panel || !panel.classList.contains("open")) return;
  if (e.key === "Escape") { e.preventDefault(); themeClose(); return; }
  if (e.key !== "Tab") return;
  const nodes = Array.from(panel.querySelectorAll(THEME_FOCUSABLE))
    .filter(n => n.getClientRects().length > 0);
  if (!nodes.length) { e.preventDefault(); return; }
  const first = nodes[0], last = nodes[nodes.length - 1];
  const active = document.activeElement;
  if (e.shiftKey && (active === first || !panel.contains(active))) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && (active === last || !panel.contains(active))) {
    e.preventDefault(); first.focus();
  }
});

/* ── Init ────────────────────────────────────────────────────────────────── */
themeLoad();
themeApply();

/* ── Compatibility shim for the old applyDashTheme ─────────────────────── */
if (typeof applyDashTheme === "function") {
  const _old = applyDashTheme;
  applyDashTheme = function(accent, density) {
    // Map old accent names to the engine's hex
    if (accent && typeof DASH_ACCENTS !== "undefined" && DASH_ACCENTS[accent]) {
      themeState.global.accent = DASH_ACCENTS[accent][0];
      // Convert hex to rgb
      const n = parseInt(DASH_ACCENTS[accent][0].slice(1), 16);
      themeState.global.accentRgb =
        `${(n >> 16) & 255},${(n >> 8) & 255},${n & 255}`;
    }
    if (density === "compact") themeState.global.density = 0.8;
    else if (density) themeState.global.density = 1;
    themeApply();
  };
}
