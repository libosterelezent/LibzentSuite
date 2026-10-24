# Primitive Components

## Button

88×36px, radius `md` (6px), flex row centered, Inter 500/13.

### Variants

| Variant | Fill | Border | Text Color | Text Weight | Opacity |
|---|---|---|---|---|---|
| Primary | `surface-active` (#262626) | — | `text-primary` | 500 | 100% |
| Secondary | `surface` (#101010) | `border` 1px inner | `text-secondary` | 400 | 100% |
| Ghost | none | — | `text-secondary` | 400 | 100% |
| Disabled | `surface` (#101010) | `border` 1px inner | `icon-muted` | 400 | 50% |

### States (applied per variant)

| State | Change |
|---|---|
| Hover | Primary→`surface-hover` fill; Secondary/Ghost→`text-primary` text |
| Active (pressed) | Primary→`border-strong` fill; opacity 80% |
| Focus | 2px `focus-ring` border replaces default border |
| Disabled | opacity 50%, `icon-muted` text |

### Library component name
`Button` with `Variant` property: Primary / Secondary / Ghost / Disabled.

---

## IconButton

36×36px, radius `md`, flex row centered. Icon: 16px, stroke-width 2.

### Variants

| Variant | Fill | Border | Icon Color |
|---|---|---|---|
| Default | none | — | `icon-default` |
| Hover | `surface-hover` | — | `icon-active` |
| Focus | none | `focus-ring` 2px inner | `icon-active` |
| Disabled | `surface` | — | `icon-muted` (opacity 50%) |

### Library component name
`IconButton` with `State` property: Default / Hover / Focus / Disabled.

---

## TextInput

200×36px, radius `md`, flex row align-center, padding-left `spacing/3` (12px), padding-right 12px.

### Variants

| Variant | Fill | Border | Border Width | Text Color | Placeholder |
|---|---|---|---|---|---|
| Default | `canvas` | `border` | 1px inner | `text-secondary` | "Placeholder…" |
| Focus | `canvas` | `focus-ring` | 2px inner | `text-primary` | — |
| Filled | `canvas` | `border` | 1px inner | `text-primary` | — |
| Disabled | `surface` | none | — | `icon-muted` | "disabled" |
| Error | `canvas` | `border` | 1px inner | `text-secondary` | "invalid input" |

### Library component name
`TextInput` with `State` property: Default / Focus / Filled / Disabled / Error.

---

## SearchField

200×36px, radius `md`, flex row align-center, padding-left `spacing/2` (10px? actually `spacing/2`=8px; use `spacing/3`=12px to match TextInput), gap `spacing/2`.

- Icon: search magnifier, `icon/sm` (14px), `icon-muted`
- Text: "Search…" Inter 400/13, `text-secondary`
- Fill: `canvas`, border: `border` 1px inner, radius `md`

### Library component name
`SearchField` (single variant).

---

## Checkbox

16×16px, radius `sm` (3px).

### Variants

| Variant | Fill | Border | Content |
|---|---|---|---|
| Unchecked | none | `border` 1px inner | — |
| Checked | `text-primary` | none | "✓" 10px weight 600, `text-inverse` |

### Library component name
`Checkbox` with `State` property: Unchecked / Checked.

---

## Switch

36×18px, radius 9px (pill).

### Variants

| Variant | Track Fill | Knob |
|---|---|---|
| Off | `border` | 14×14 circle, `canvas`, left-aligned (2px inset) |
| On | `text-primary` | 14×14 circle, `canvas`, right-aligned (2px inset) |

### Library component name
`Switch` with `State` property: Off / On.

---

## Select

200×36px, radius `md`, flex row align-center, padding-left 12, padding-right 10.

- Fill: `canvas`, border: `border` 1px inner
- Text: selected value, Inter 400/13, `text-primary`
- Chevron: down arrow, 14px, `icon-default`, right-aligned

### Library component name
`Select` (single variant — closed state; the dropdown panel is a separate overlay pattern).

---

## Badge

Auto-width × `control-sm` (28px→20px visually), radius `sm`, flex row centered, padding 4×8.

### Variants

| Variant | Fill | Border | Text Color | Text Size |
|---|---|---|---|---|
| Active | `surface-active` | — | `text-primary` | 10px 400 |
| Default | `surface` | — | `text-secondary` | 10px 400 |
| Muted | `surface` | — | `icon-muted` | 10px 400 |

### Library component name
`Badge` with `Label` text override and `Tone` property: Active / Default / Muted.

---

## Divider

Rectangle: 300×1px, fill `border`.

Create as component `Divider` (horizontal). Duplicate + rotate 90° for vertical.

---

# Composite Components

## NavigationItem

**Already built in Stage 1.** Library: `NavItem / Home` (variant set: Default + Active).

Board: 228×36px, radius `md`, flex row align-center, gap 10, padding-left 10.
Icon: `icon/md` (16px). Label: Inter 13px.

### States (add Hover + Focus as variants to the existing set)

| State | Fill | Border | Icon | Label Weight |
|---|---|---|---|---|
| Default | none | — | `icon-default` | `text-secondary` / 400 |
| Hover | `surface-hover` | — | `icon-active` | `text-primary` / 400 |
| Active | `surface-active` | `border` 1px inner | `icon-active` | `text-primary` / 500 |
| Focus | none | `focus-ring` 2px | `icon-default` | `text-secondary` / 400 |

---

## Card

280×120px (auto-height), radius `md`, fill `surface`, border `border` 1px inner, padding `spacing/4`.

Content: Title (14/500 `text-primary`) + Description (13/400 `text-secondary`), gap `spacing/2`.

---

## Section

Header row: title (14/500 `text-primary`) + optional action link (13/400 `accent`) + bottom border `border`.
Body: content with `spacing/4` padding.

---

## FormField

Flex column, gap `spacing/1` (4px).
Label (12/500 `text-secondary`) → TextInput instance → Hint (11/400 `icon-muted`).

---

## Dialog

480px wide, radius `lg`, fill `surface`, border `border` 1px.

Header (padding `spacing/4`, title 14/500 `text-primary`, ✕ button 26×26) → Body (padding `spacing/4`) → Footer (padding `spacing/3`, flex row justify-end, gap `spacing/2`: Cancel Button/Secondary + Confirm Button/Primary).

---

## EmptyState

Board: 280×120px, radius `md`, border `border` 1px **dashed**, flex column centered, gap `spacing/2`.
Icon (24px, `icon-muted`) + Title (13/500 `text-secondary`) + Description (12/400 `icon-muted`).

---

## DataTable

Header row: `#101010` fill, 13/500 `text-secondary`, bottom border `border`.
Data rows: 36px height, bottom border `border`, 13/400 `text-primary`, padding `spacing/3`.
Empty state: EmptyState instance inside the table wrapper.

---

# LibzentSuite Domain Components

## AccountIdentity

Flex row align-center, gap 10. Avatar (32×32 circle, `surface-active` fill, initial letter 13/500) + Name (14/500 `text-primary`) + Status dot (7px circle: `text-primary` running / `icon-muted` created / `border-strong` stopped).

---

## AccountStatus

Badge instance with text override: "created" / "running" / "stopped" / "failed". Tone maps: running→Active fill, created→Default, failed→Muted.

---

## PluginAssignment

Flex row align-center, gap 8. Plugin name (13/400 `text-primary`) + Switch instance (On/Off).

---

## PresetSelector

Select instance with preset names + "clear binding" Ghost Button. Shows the active preset name + scope summary.

---

## ActivityEntry

Flex row align-start, gap 8. Timestamp (10/400 `text-muted`, mono) + Description (13/400 `text-primary`).

---

## BulkActionToolbar

Toolbar: auto-width, `height/control-md`, fill `surface`, border `border` 1px, radius `md`, padding `spacing/2`.
Flex row, gap `spacing/2`: action Buttons (Start / Stop / Remove), visible only when accounts are selected.
