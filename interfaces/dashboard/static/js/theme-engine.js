"use strict";
/* ═══════════════════════════════════════════════════════════════════════════
   theme-engine.js — the full theme management system.

   Layers (most specific wins):
     1. Custom CSS   (user-written, scoped to a page or global)
     2. Page theme   (per-page token overrides)
     3. Global theme (presets, accent, radius, density, fonts)
     4. Built-in     (base.css defaults)

   Persists to localStorage (immediate) + state/theme.json (server-side,
   survives browser clears). The panel is a slide-in drawer on the right.
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
  { hex: "#8b5cf6", rgb: "139,92,246", label: "Violet" },
  { hex: "#ec4899", rgb: "236,72,153", label: "Pink" },
  { hex: "#10b981", rgb: "16,185,129", label: "Emerald" },
  { hex: "#f59e0b", rgb: "245,158,11", label: "Amber" },
  { hex: "#06b6d4", rgb: "6,182,212", label: "Cyan" },
  { hex: "#d95050", rgb: "217,80,80", label: "Red" },
];

/* ── State ───────────────────────────────────────────────────────────────── */
const themeState = {
  scope: "global",
  global: {
    preset: "carbon",
    accent: "#a0a0a0",
    accentRgb: "160,160,160",
    radius: 3,
    density: 1,
    fontUi: "Inter",
    fontMono: "JetBrains Mono",
    css: "",
  },
  pages: {},  // pageId -> partial token overrides
  pageCss: {}, // pageId -> custom CSS string
};

/* ── Persistence ─────────────────────────────────────────────────────────── */
function themeSave() {
  try {
    localStorage.setItem("lz_theme", JSON.stringify(themeState));
  } catch (_) { /* non-fatal */ }
  // Server-side persistence (best-effort — localStorage is the primary)
  if (typeof fetch === "function") {
    fetch("/api/settings/set", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Dashboard-Token":
          typeof token === "function" ? (token() || "") : "",
      },
      body: JSON.stringify({
        key: "__theme", value: JSON.stringify(themeState),
      }),
    }).catch(() => {});
  }
}

function themeLoad() {
  try {
    const raw = localStorage.getItem("lz_theme");
    if (raw) {
      const parsed = JSON.parse(raw);
      Object.assign(themeState.global, parsed.global || {});
      themeState.pages = parsed.pages || {};
      themeState.pageCss = parsed.pageCss || {};
    }
  } catch (_) { /* corrupted — start fresh */ }
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
  const preset = THEME_PRESETS[g.preset] || THEME_PRESETS.carbon;
  Object.entries(preset.tokens).forEach(([k, v]) => {
    root.style.setProperty(k, v);
  });

  // 2. Accent
  root.style.setProperty("--accent", g.accent);
  root.style.setProperty("--accent-rgb", g.accentRgb);

  // 3. Radius (low values preferred)
  root.style.setProperty("--r", g.radius + "px");
  root.style.setProperty("--r-sm", Math.max(0, g.radius - 1) + "px");
  root.style.setProperty("--r-lg", Math.max(0, g.radius + 1) + "px");

  // 4. Density
  root.style.setProperty("--density", String(g.density));

  // 5. Page overrides
  const pageOverrides = themeState.pages[themeState.scope];
  if (pageOverrides && themeState.scope !== "global") {
    Object.entries(pageOverrides).forEach(([k, v]) => {
      root.style.setProperty(k, v);
    });
  }

  // 6. Fonts
  themeLoadFonts(g.fontUi, g.fontMono);

  // 7. Custom CSS (global + page-scoped)
  let cssTag = document.getElementById("lz-custom-css");
  if (!cssTag) {
    cssTag = document.createElement("style");
    cssTag.id = "lz-custom-css";
    document.head.appendChild(cssTag);
  }
  const pageCss = themeState.scope !== "global"
    ? (themeState.pageCss[themeState.scope] || "")
    : "";
  cssTag.textContent = g.css + "\n" + pageCss;

  themeSave();
}

/* ── The panel ──────────────────────────────────────────────────────────── */
function themeBuildPanel() {
  const panel = document.createElement("div");
  panel.id = "lz-theme-panel";
  panel.style.cssText = `
    position:fixed;top:0;right:-420px;width:400px;height:100vh;
    background:var(--surface);border-left:1px solid var(--border2);
    z-index:200;transition:right .25s ease;display:flex;
    flex-direction:column;box-shadow:-10px 0 40px rgba(0,0,0,.4);
    font-family:var(--font);
  `;

  panel.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid var(--border)">
      <span style="font-size:15px;font-weight:700;flex:1;color:var(--text)">Theme Engine</span>
      <div style="width:28px;height:28px;display:flex;align-items:center;justify-content:center;border-radius:var(--r-sm);color:var(--dim);cursor:pointer" onclick="themeClose()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </div>
    </div>
    <div style="display:flex;border-bottom:1px solid var(--border);padding:0 12px">
      <div class="lz-tp-tab" data-tab="0" style="padding:10px 12px;font-size:12px;font-weight:550;color:var(--accent);border-bottom:2px solid var(--accent);cursor:pointer">Appearance</div>
      <div class="lz-tp-tab" data-tab="1" style="padding:10px 12px;font-size:12px;font-weight:550;color:var(--dim);border-bottom:2px solid transparent;cursor:pointer">Fonts</div>
      <div class="lz-tp-tab" data-tab="2" style="padding:10px 12px;font-size:12px;font-weight:550;color:var(--dim);border-bottom:2px solid transparent;cursor:pointer">CSS</div>
    </div>
    <div style="flex:1;overflow-y:auto;padding:16px 20px" id="lz-tp-body"></div>
  `;

  document.body.appendChild(panel);
  return panel;
}

function themeRenderTab(tabIndex) {
  const body = document.getElementById("lz-tp-body");
  if (!body) return;
  const g = themeState.global;

  // Tab switching
  document.querySelectorAll(".lz-tp-tab").forEach((t, i) => {
    t.style.color = i === tabIndex ? "var(--accent)" : "var(--dim)";
    t.style.borderBottomColor =
      i === tabIndex ? "var(--accent)" : "transparent";
  });

  if (tabIndex === 0) {
    body.innerHTML = `
      <div style="font-size:11px;font-weight:600;color:var(--dim);margin:0 0 6px;letter-spacing:.03em">Theme</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:14px">
        ${Object.entries(THEME_PRESETS).map(([id, p]) => `
          <div class="lz-preset" data-id="${id}" style="border:1px solid ${g.preset === id ? "var(--accent)" : "var(--border)"};border-radius:var(--r);padding:8px 6px;cursor:pointer;text-align:center;transition:all .12s;${g.preset === id ? "background:rgba(var(--accent-rgb),.05)" : ""}">
            <div style="display:flex;gap:3px;justify-content:center;margin-bottom:5px">
              ${Object.values(p.tokens).slice(0, 3).map(v =>
                `<div style="width:12px;height:12px;border-radius:2px;background:${v};border:1px solid rgba(255,255,255,.08)"></div>`
              ).join("")}
            </div>
            <div style="font-size:9px;font-weight:600;color:var(--dim)">${p.label}</div>
          </div>
        `).join("")}
      </div>

      <div style="font-size:11px;font-weight:600;color:var(--dim);margin:0 0 6px;letter-spacing:.03em">Accent</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
        ${THEME_ACCENTS.map(a => `
          <div class="lz-accent" data-hex="${a.hex}" data-rgb="${a.rgb}"
            style="width:26px;height:26px;border-radius:var(--r);cursor:pointer;background:${a.hex};border:2px solid ${g.accent === a.hex ? "#fff" : "transparent"};transition:all .12s"
            title="${a.label}"></div>
        `).join("")}
      </div>

      <div style="font-size:11px;font-weight:600;color:var(--dim);margin:0 0 6px;letter-spacing:.03em">Border Radius</div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
        <input type="range" min="0" max="12" value="${g.radius}"
          id="lz-radius" style="flex:1;accent-color:var(--accent);height:4px">
        <span style="font-family:var(--mono);font-size:11px;color:var(--dim);min-width:36px;text-align:right" id="lz-radius-val">${g.radius}px</span>
      </div>

      <div style="font-size:11px;font-weight:600;color:var(--dim);margin:0 0 6px;letter-spacing:.03em">Density</div>
      <div style="display:flex;background:var(--surface2);border:1px solid var(--border);border-radius:var(--r-sm);overflow:hidden">
        <button class="lz-density" data-v="0.7" style="flex:1;padding:6px 12px;background:none;border:none;color:${g.density === 0.7 ? "var(--accent)" : "var(--dim)"};font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)">Compact</button>
        <button class="lz-density" data-v="1" style="flex:1;padding:6px 12px;background:none;border:none;color:${g.density === 1 ? "var(--accent)" : "var(--dim)"};font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)">Normal</button>
        <button class="lz-density" data-v="1.3" style="flex:1;padding:6px 12px;background:none;border:none;color:${g.density === 1.3 ? "var(--accent)" : "var(--dim)"};font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)">Spacious</button>
      </div>
    `;

    // Wire up
    body.querySelectorAll(".lz-preset").forEach(el => {
      el.addEventListener("click", () => {
        g.preset = el.dataset.id;
        themeApply();
        themeRenderTab(0);
      });
      el.addEventListener("mouseenter", () => {
        if (el.style.borderColor !== "var(--accent)")
          el.style.borderColor = "var(--border2)";
      });
      el.addEventListener("mouseleave", () => {
        if (g.preset !== el.dataset.id)
          el.style.borderColor = "var(--border)";
      });
    });
    body.querySelectorAll(".lz-accent").forEach(el => {
      el.addEventListener("click", () => {
        g.accent = el.dataset.hex;
        g.accentRgb = el.dataset.rgb;
        themeApply();
        themeRenderTab(0);
      });
    });
    const radius = body.querySelector("#lz-radius");
    radius.addEventListener("input", () => {
      g.radius = parseInt(radius.value);
      document.getElementById("lz-radius-val").textContent = g.radius + "px";
      themeApply();
    });
    body.querySelectorAll(".lz-density").forEach(el => {
      el.addEventListener("click", () => {
        g.density = parseFloat(el.dataset.v);
        themeApply();
        themeRenderTab(0);
      });
    });

  } else if (tabIndex === 1) {
    body.innerHTML = `
      <div style="font-size:11px;font-weight:600;color:var(--dim);margin:0 0 6px;letter-spacing:.03em">UI Font</div>
      <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:14px">
        ${Object.keys(THEME_FONTS.ui).map(name => `
          <div class="lz-font" data-type="ui" data-name="${name}"
            style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid ${g.fontUi === name ? "var(--accent)" : "var(--border)"};border-radius:var(--r-sm);cursor:pointer;transition:all .12s;${g.fontUi === name ? "background:rgba(var(--accent-rgb),.04)" : ""}">
            <span style="font-size:12px;font-weight:550;color:var(--text);font-family:${name === "System" ? "var(--font)" : `'${name}', var(--font)`}">${name}</span>
            <span style="margin-left:auto;font-size:10px;color:var(--faint)">The quick brown fox</span>
          </div>
        `).join("")}
      </div>

      <div style="font-size:11px;font-weight:600;color:var(--dim);margin:0 0 6px;letter-spacing:.03em">Monospace Font</div>
      <div style="display:flex;flex-direction:column;gap:4px">
        ${Object.keys(THEME_FONTS.mono).map(name => `
          <div class="lz-font" data-type="mono" data-name="${name}"
            style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid ${g.fontMono === name ? "var(--accent)" : "var(--border)"};border-radius:var(--r-sm);cursor:pointer;transition:all .12s;${g.fontMono === name ? "background:rgba(var(--accent-rgb),.04)" : ""}">
            <span style="font-size:12px;font-weight:550;color:var(--text);font-family:${name === "System" ? "var(--mono)" : `'${name}', var(--mono)`}">${name}</span>
            <span style="margin-left:auto;font-size:10px;color:var(--faint);font-family:${name === "System" ? "var(--mono)" : `'${name}', var(--mono)`}">0123 {} []</span>
          </div>
        `).join("")}
      </div>
    `;

    body.querySelectorAll(".lz-font").forEach(el => {
      el.addEventListener("click", () => {
        const type = el.dataset.type;
        const name = el.dataset.name;
        if (type === "ui") g.fontUi = name;
        else g.fontMono = name;
        themeApply();
        themeRenderTab(1);
      });
    });

  } else if (tabIndex === 2) {
    body.innerHTML = `
      <div style="font-size:11px;font-weight:600;color:var(--dim);margin:0 0 6px;letter-spacing:.03em">Custom CSS</div>
      <div style="font-size:10px;color:var(--faint);margin-bottom:8px;line-height:1.5">
        Write CSS targeting any element. All design tokens available as
        <code style="font-family:var(--mono)">var(--accent)</code>,
        <code style="font-family:var(--mono)">var(--surface)</code>, etc.
        Changes apply live and persist.
      </div>
      <textarea id="lz-css" spellcheck="false"
        style="width:100%;min-height:180px;background:var(--bg);border:1px solid var(--border2);border-radius:var(--r-sm);color:var(--text);font-family:var(--mono);font-size:11px;line-height:1.6;padding:12px;resize:vertical;outline:none;tab-size:2;white-space:pre;overflow:auto"
        placeholder="/* .hero { border-left-color: var(--warn); } */&#10;/* .pill { border-radius: 0; } */">${g.css}</textarea>
      <div style="display:flex;gap:6px;margin-top:8px;align-items:center">
        <button id="lz-css-apply" style="padding:8px 14px;border-radius:var(--r-sm);font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--accent);background:rgba(var(--accent-rgb),.1);color:var(--accent);font-family:var(--font)">Apply</button>
        <button id="lz-css-clear" style="padding:8px 14px;border-radius:var(--r-sm);font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--border2);background:var(--surface2);color:var(--text);font-family:var(--font)">Clear</button>
        <div style="flex:1"></div>
        <button id="lz-css-export" style="padding:8px 14px;border-radius:var(--r-sm);font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--border2);background:var(--surface2);color:var(--text);font-family:var(--font)">Export</button>
      </div>
      <div style="font-size:10px;color:var(--faint);margin-top:8px;line-height:1.5">
        Stored in <code style="font-family:var(--mono)">state/theme.json</code>.
        Survives restarts. Right-click any element to inspect its class name.
      </div>
    `;

    body.querySelector("#lz-css-apply").addEventListener("click", () => {
      g.css = body.querySelector("#lz-css").value;
      themeApply();
    });
    body.querySelector("#lz-css-clear").addEventListener("click", () => {
      g.css = "";
      body.querySelector("#lz-css").value = "";
      themeApply();
    });
    body.querySelector("#lz-css-export").addEventListener("click", () => {
      const blob = new Blob([g.css], { type: "text/css" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "libzent-theme.css";
      a.click();
    });
  }
}

/* ── Open/close ──────────────────────────────────────────────────────────── */
function themeOpen() {
  let panel = document.getElementById("lz-theme-panel");
  if (!panel) panel = themeBuildPanel();
  panel.style.right = "0";
  themeRenderTab(0);
}

function themeClose() {
  const panel = document.getElementById("lz-theme-panel");
  if (panel) panel.style.right = "-420px";
}

// singleton handler (assignment, not addEventListener — the listener-leak
// regression test requires addEventListener only inside wire())
document.onkeydown = function(e) {
  if (e.key === "Escape") themeClose();
};

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
