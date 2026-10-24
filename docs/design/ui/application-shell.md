# Application Shell Layout

The dashboard application shell at 1440px desktop viewport.

## Expanded Shell (260px sidebar)

```
┌─────────────────────────────────────────────────────────────────┐
│ Sidebar (260×900, #101010)  │  Top Bar (1180×48, #1B1B1B @40%) │
│                             │                                   │
│ ┌─ Brand ─────────────────┐ │  ┌─ Content ────────────────────┐ │
│ │ [L] LibzentSuite        │ │  │                              │ │
│ └─────────────────────────┘ │  │  Page title                  │ │
│ ┌─ Search ────────────────┐ │  │  Content area                │ │
│ │ 🔍 Search…              │ │  │                              │ │
│ └─────────────────────────┘ │  │                              │ │
│                             │  │                              │ │
│ ┌─ Navigation ────────────┐ │  │                              │ │
│ │ 🏠 Home        [ACTIVE] │ │  │                              │ │
│ │ 👤 Accounts             │ │  │                              │ │
│ │ 📦 Fleet                │ │  │                              │ │
│ │ 🔌 Plugins              │ │  │                              │ │
│ │ 📊 Activity             │ │  │                              │ │
│ │ 🛡 Policies             │ │  │                              │ │
│ │ ⚙ Administration        │ │  │                              │ │
│ │ 🔧 Tools                │ │  │                              │ │
│ └─────────────────────────┘ │  │                              │ │
│                             │  │                              │ │
│ ┌─ Footer ────────────────┐ │  │                              │ │
│ │ ─────────────────────── │ │  │                              │ │
│ │ « Collapse              │ │  │                              │ │
│ └─────────────────────────┘ │  │                              │ │
└─────────────────────────────┴──┴──────────────────────────────┘
  ← 260px →                    ←────────── 1180px ──────────────→
```

### Sidebar measurements (expanded)

| Zone | Height | Padding | Notes |
|---|---|---|---|
| Brand | 40px | — | 22×22 mark + "LibzentSuite" 15/600 |
| Divider | 1px | — | `border` fill |
| Search | 36px | 10px horizontal | radius `md`, icon 14px + placeholder |
| Gap | 16px | — | between search and nav |
| Nav items | 8 × 36px | gap 2px between | icon 16px + label 13px |
| (flex spacer) | auto | — | pushes footer to bottom |
| Footer separator | 1px | — | `border` fill |
| Collapse | 32px | 10px left | chevrons icon + label |
| Bottom padding | 16px | — | |

Total fixed: ~497px. Remaining space is the flex spacer.

### Sidebar horizontal padding: 16px each side
### Content width inside sidebar: 228px (260 - 32)

---

## Collapsed Shell (64px sidebar)

```
┌─────────────────────────────────────────────────────────────────┐
│ Sidebar (64×900, #101010)   │  Top Bar (1376×48, #1B1B1B @40%) │
│                             │                                   │
│ ┌────┐                      │  ┌─ Content ────────────────────┐ │
│ │ [L]│                      │  │                              │ │
│ └────┘                      │  │                              │ │
│                             │  │                              │ │
│ ┌────┐                      │  │                              │ │
│ │ 🏠 │ [ACTIVE]             │  │                              │ │
│ ├────┤                      │  │                              │ │
│ │ 👤 │                      │  │                              │ │
│ ├────┤                      │  │                              │ │
│ │ 📦 │                      │  │                              │ │
│ ├────┤                      │  │                              │ │
│ │ 🔌 │                      │  │                              │ │
│ ├────┤                      │  │                              │ │
│ │ 📊 │                      │  │                              │ │
│ ├────┤                      │  │                              │ │
│ │ 🛡 │                      │  │                              │ │
│ ├────┤                      │  │                              │ │
│ │ ⚙  │                      │  │                              │ │
│ ├────┤                      │  │                              │ │
│ │ 🔧 │                      │  │                              │ │
│ └────┘                      │  │                              │ │
│                             │  │                              │ │
│ ┌────┐                      │  │                              │ │
│ │ »  │                      │  │                              │ │
│ └────┘                      │  │                              │ │
└─────────────────────────────┴──┴──────────────────────────────┘
  ← 64px →                    ←────────── 1376px ──────────────→
```

### Sidebar measurements (collapsed)

| Zone | Size | Notes |
|---|---|---|
| Brand mark | 22×22 | centered |
| Nav icons | 8 × 40×36 cells | centered, gap 6px |
| Expand | 40×32 cell | chevrons-right icon |
| Horizontal padding | 12px each side | |

### Icon cell: 40×36px, radius `md`, centered icon 16px
### Active cell: `surface-active` fill, accent 3px left border
### Tooltip on hover: label text (extension-driven in implementation)

---

## Interaction States

| State | Surface | Border | Icon | Label | Weight |
|---|---|---|---|---|---|
| Default | transparent | — | `icon-default` | `text-secondary` | 400 |
| Hover | `surface-hover` | — | `icon-active` | `text-primary` | 400 |
| Active | `surface-active` | `border` 1px | `icon-active` | `text-primary` | 500 |
| Focus | transparent | `focus-ring` 1px ring | — | — | — |
| Disabled | `surface` | — | `icon-muted` | `icon-muted` | 400 |

Focus ring: 1px `#F2F2F2` outline with 2px offset (from the shape edge outward).

---

## Design Decision: Extension-Driven Entries

The sidebar navigation entries are NOT hardcoded in the SPA. They are
rendered from configuration objects served by the FleetService backend.
Each entry is a NavItem component instance driven by a config object:

```json
{
  "id": "home",
  "label": "Home",
  "icon": "home",
  "route": "#/home",
  "section": "workspace"
}
```

This means:
* The SPA builds the sidebar from a `navigation` store key (seeded at boot
  via `/api/dashboard-ui/navigation`)
* Adding a new nav entry requires NO frontend code changes — only a
  new config object from the backend
* The Fleet Hub, Profiles, and future pages are auto-discovered through
  the same mechanism
