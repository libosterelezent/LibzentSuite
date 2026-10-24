# LibzentSuite — Design System Specification

Design tokens and component specifications for manual Penpot construction.
All values are authoritative — match these exactly when building in Penpot.

**Font**: Inter (weights: 400 regular, 500 medium, 600 semibold)

---

## Color Palette

| Token | Hex | Usage |
|---|---|---|
| `color/canvas` | `#090909` | Application canvas background |
| `color/surface` | `#101010` | Sidebar and panel background |
| `color/surface-hover` | `#1B1B1B` | Hover surface |
| `color/surface-active` | `#262626` | Active / selected surface |
| `color/surface-raised` | `#1B1B1B` | Cards, dialogs |
| `color/border` | `#303030` | Default border |
| `color/border-strong` | `#454545` | Strong border (emphasis) |
| `color/text-primary` | `#F2F2F2` | Primary text |
| `color/text-secondary` | `#A0A0A0` | Secondary text |
| `color/text-muted` | `#858585` | Muted icons and hints |
| `color/focus-ring` | `#F2F2F2` | Focus ring (monochrome) |

---

## Spacing Scale

| Token | Value |
|---|---|
| `spacing/1` | 4px |
| `spacing/2` | 8px |
| `spacing/3` | 12px |
| `spacing/4` | 16px |
| `spacing/5` | 24px |
| `spacing/6` | 32px |

---

## Corner Radii

| Token | Value | Usage |
|---|---|---|
| `radius/sm` | 3px | Badges |
| `radius/md` | 6px | Buttons, inputs, nav items |
| `radius/lg` | 8px | Cards, dialogs |

---

## Typography (Inter)

| Style | Size | Weight | Color |
|---|---|---|---|
| Page Title | 20px | 600 | `color/text-primary` |
| Section Heading | 16px | 600 | `color/text-primary` |
| Component Heading | 14px | 500 | `color/text-primary` |
| Body Text | 13px | 400 | `color/text-primary` |
| Supporting Text | 11px | 400 | `color/text-secondary` |
| Form Label | 12px | 500 | `color/text-secondary` |
| Compact Metadata | 10px | 400 | `color/text-muted` |

---

## Icon Sizes

| Token | Value | Usage |
|---|---|---|
| `icon/sm` | 14px | Search, collapse, inline icons |
| `icon/md` | 16px | Navigation items |

---

## Control Heights

| Token | Value | Usage |
|---|---|---|
| `height/control-sm` | 28px | Badges, compact controls |
| `height/control-md` | 36px | Buttons, inputs, nav items |
| `height/control-lg` | 44px | Dialog headers, large targets |
| `height/row` | 36px | Table rows, list items |

---
---

# Primitive Components

## Button

Board: 88×36px, radius 6 (`radius/md`), flex row centered.

| Variant | Fill | Border | Text Color | Text Weight |
|---|---|---|---|---|
| Primary | `#262626` | — | `#F2F2F2` | 500 |
| Secondary | `#101010` | `#303030` 1px | `#A0A0A0` | 400 |
| Ghost | none | — | `#A0A0A0` | 400 |
| Disabled | `#101010` | `#303030` 1px | `#858585` | 400 (opacity 50%) |

Create one component `Button` with a `Variant` property: Primary / Secondary / Ghost / Disabled.

---

## IconButton

Board: 36×36px, radius 6 (`radius/md`), flex row centered. Icon: 16px, gear glyph.

| Variant | Fill | Border | Icon Color |
|---|---|---|---|
| Default | none | — | `#858585` |
| Hover | `#1B1B1B` | — | `#F2F2F2` |
| Focus | none | `#F2F2F2` 2px | `#F2F2F2` |
| Disabled | `#101010` | `#303030` 1px | `#858585` (opacity 50%) |

Create component `IconButton` with a `State` property: Default / Hover / Focus / Disabled.

---

## TextInput

Board: 200×36px, radius 6 (`radius/md`), flex row align-center, padding-left 12.

| Variant | Fill | Border | Text | Border Width |
|---|---|---|---|---|
| Default | `#090909` | `#303030` 1px | `#A0A0A0` ("Placeholder…") | 1px |
| Focus | `#090909` | `#F2F2F2` 2px | `#F2F2F2` | 2px |
| Filled | `#090909` | `#303030` 1px | `#F2F2F2` ("filled value") | 1px |
| Disabled | `#101010` | none | `#858585` ("disabled") | 0 |
| Error | `#090909` | `#303030` 1px | `#A0A0A0` ("invalid input") | 1px |

Create component `TextInput` with a `State` property: Default / Focus / Filled / Disabled / Error.

---

## SearchField

Board: 200×36px, radius 6, flex row align-center, padding-left 10, gap 8.

- Fill: `#090909`, border: `#303030` 1px
- Icon: search magnifier, 14px, `#858585`
- Text: "Search…" Inter 400/13, `#A0A0A0`

Create component `SearchField`.

---

## Checkbox

16×16px, radius 3 (`radius/sm`).

| Variant | Fill | Border | Content |
|---|---|---|---|
| Unchecked | none | `#303030` 1px | — |
| Checked | `#F2F2F2` | none | "✓" 10px 600 `#090909` |

Create component `Checkbox` with a `State` property: Unchecked / Checked.

---

## Switch

Board: 36×18px, radius 9 (pill).

| Variant | Fill | Knob |
|---|---|---|
| Off | `#303030` | 14×14 circle `#090909`, left-aligned |
| On | `#F2F2F2` | 14×14 circle `#090909`, right-aligned |

Create component `Switch` with a `State` property: Off / On.

---

## Badge

Board: auto-width (~64×20px), radius 3 (`radius/sm`), flex row centered, padding 4×8.

| Variant | Fill | Border | Text Color | Text |
|---|---|---|---|---|
| Active | `#262626` | — | `#F2F2F2` 10px 400 | "active" |
| Default | `#101010` | — | `#A0A0A0` 10px 400 | "default" |
| Muted | `#101010` | — | `#858585` 10px 400 | "muted" |

Create component `Badge` with a `Label` text override and a `Tone` property.

---

## Divider

Rectangle: 300×1px, fill `#303030`.

Create component `Divider` (horizontal). Duplicate and rotate 90° for vertical use.

---

# Composite Components

## NavigationItem

**Already built** (Stage 1, `NavItem / Home` variant set).

Board: 228×36px, radius 6, flex row align-center, gap 10, padding-left 10.
Icon: 16px. Label: Inter 13px.

| State | Fill | Border | Icon | Label |
|---|---|---|---|---|
| Default | none | — | `#858585` | `#A0A0A0` / 400 |
| Hover | `#1B1B1B` | — | `#A0A0A0` | `#F2F2F2` / 400 |
| Active | `#262626` | `#303030` 1px | `#F2F2F2` | `#F2F2F2` / 500 |
| Focus | none | `#F2F2F2` 1px | `#858585` | `#A0A0A0` / 400 |

---

## Card

Board: 280×120px (auto-height), radius 6, fill `#101010`, border `#303030` 1px, padding 16.

Content: Title (14/500 `#F2F2F2`) + Description (13/400 `#A0A0A0`), gap 8.

---

## Section

Board: auto-width/height. Header row (title 14/500 `#F2F2F2` + optional action link) + body (gap 12). Header has a bottom border `#303030`.

---

## FormField

Board: auto-width/height, gap 4. Label (12/500 `#A0A0A0`) + TextInput instance + Hint (11/400 `#858585`).

---

## Dialog

Board: 480px wide, radius 8, fill `#101010`, border `#303030` 1px.

Structure: Header (padding 16, title 14/500 `#F2F2F2`, ✕ button) + Body (padding 16) + Footer (padding 16, Cancel + Confirm buttons, flex row justify-end).

---

## EmptyState

Board: 280×120px, radius 6, border `#303030` 1px dashed, flex column centered, gap 6.

Icon (24px, `#858585`) + Title (13/500 `#F2F2F2`) + Description (12/400 `#A0A0A0`).

---

# LibzentSuite Domain Components

## AccountIdentity

Flex row align-center, gap 10. Avatar (32×32 circle, `#262626` fill) + Name (14/500 `#F2F2F2`) + Status dot (7px circle: `#F2F2F2` on / `#858585` off / `#F2F2F2` err).

---

## AccountStatus

Badge instance with text override: "created" / "running" / "stopped" / "failed". Badge tone maps to state: running=active fill, created=default, failed=error.

---

## PluginAssignment

Flex row align-center, gap 8. Plugin name (13/400 `#F2F2F2`) + Switch instance (On/Off) — the toggle assigns the plugin to the account.

---

## PresetSelector

Select instance with profile names as options + "clear binding" ghost button. Shows the active preset name.

---

## ActivityEntry

Flex row align-start, gap 8. Timestamp (10/400 `#858585`, mono) + Description (13/400 `#F2F2F2`).

---

## BulkActionToolbar

Toolbar board (auto-width, 36px height, fill `#101010`, border `#303030` 1px, radius 6, padding 8). Flex row, gap 8: action buttons (Start / Stop / Remove), visible only when accounts are selected.

---

# Penpot Component Organization

Create these frames on the `Design System` page:

```
Design System (page)
├── Workspace / Desktop (1440×900)     ← existing, preserved
├── Workspace / Collapsed (1440×900)   ← existing, preserved
├── Components / NavItem               ← existing, preserved
├── States Spec / NavItem              ← existing, preserved
├── Foundations                        ← NEW: color swatches, spacing scale, typography specimens
├── Primitives                         ← NEW: Button set, IconButton set, TextInput set, etc.
├── Composites                         ← NEW: Card, Dialog, EmptyState, DataTable
└── LibzentSuite                       ← NEW: AccountIdentity, PresetSelector, ActivityEntry
```

Each frame: fill `#0D0D0D`, border `#1B1B1B` 1px, radius 8, padding 24, gap 16, auto-sizing.
Arrange frames in a 2×2 grid on the canvas: Foundations top-left, Primitives top-right,
Composites bottom-left, LibzentSuite bottom-right.
