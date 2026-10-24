# Design Tokens

Source of truth for every color, dimension, and typography value in the LibzentSuite UI.

## Color Palette

| Token | Hex | Usage |
|---|---|---|
| `color/canvas` | `#090909` | Application canvas background |
| `color/surface` | `#101010` | Sidebar, panel, card background |
| `color/surface-hover` | `#1B1B1B` | Hover surface (rows, nav items, buttons) |
| `color/surface-active` | `#262626` | Active / selected surface (nav item, toggle on) |
| `color/surface-raised` | `#1B1B1B` | Raised surfaces: cards, dialogs, dropdowns |
| `color/border` | `#303030` | Default border (inputs, cards, dividers) |
| `color/border-strong` | `#454545` | Strong border (focus emphasis) |
| `color/text-primary` | `#F2F2F2` | Primary text (titles, active nav labels, values) |
| `color/text-secondary` | `#A0A0A0` | Secondary text (descriptions, inactive nav labels) |
| `color/text-muted` | `#858585` | Muted icons, hints, placeholders |
| `color/text-inverse` | `#090909` | Inverse text (on light fills, e.g. checked checkbox) |
| `color/icon-default` | `#858585` | Default icon color |
| `color/icon-active` | `#F2F2F2` | Active icon color |
| `color/focus-ring` | `#F2F2F2` | Keyboard focus ring |
| `color/scrim` | `rgba(0,0,0,0.62)` | Modal backdrop |

## Spacing Scale

| Token | Value |
|---|---|
| `spacing/1` | 4px |
| `spacing/2` | 8px |
| `spacing/3` | 12px |
| `spacing/4` | 16px |
| `spacing/5` | 24px |
| `spacing/6` | 32px |

## Corner Radii

| Token | Value | Usage |
|---|---|---|
| `radius/sm` | 3px | Badges |
| `radius/md` | 6px | Buttons, inputs, nav items, switches |
| `radius/lg` | 8px | Cards, dialogs |

## Border Widths

| Token | Value | Usage |
|---|---|---|
| `borderWidth/hairline` | 1px | Default borders, dividers |
| `borderWidth/focus` | 2px | Focus ring borders |

## Typography (Inter)

| Style | Size | Weight | Color | Usage |
|---|---|---|---|---|
| Page Title | 20px | 600 | `text/primary` | Page `h1` |
| Section Heading | 16px | 600 | `text/primary` | Section headers |
| Component Heading | 14px | 500 | `text/primary` | Card titles, dialog titles |
| Body Text | 13px | 400 | `text/primary` | Main content |
| Supporting Text | 11px | 400 | `text/secondary` | Descriptions, hints |
| Form Label | 12px | 500 | `text/secondary` | Input labels |
| Compact Metadata | 10px | 400 | `text/muted` | Timestamps, counts |

## Icon Sizes

| Token | Value |
|---|---|
| `icon/sm` | 14px |
| `icon/md` | 16px |

## Control Heights

| Token | Value | Usage |
|---|---|---|
| `height/control-sm` | 28px | Badges, compact controls |
| `height/control-md` | 36px | Buttons, inputs, nav items |
| `height/control-lg` | 44px | Dialog headers, large targets |
| `height/row` | 36px | Table rows, list items |

## Sidebar Dimensions

| Token | Value |
|---|---|
| `width/sidebar-expanded` | 260px |
| `width/sidebar-collapsed` | 64px |
