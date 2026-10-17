# DESIGN AWARENESS — component behavior dataset

*Research-derived rules for the "awareness" layer the dashboard lacks:
sizes, alignment, overflow, and action feedback. Sources: Discord client
mod ecosystem (BetterDiscord, nightcord — exact toast palettes), SAP
Fiori (wrap-vs-truncate decision tables), Elastic UI (table column
contracts), GitHub Primer (accessible truncation), Atlassian / Fluent 2 /
QLD / Zeta / Appetite (spacing ramps + 4/8px grids), NN/g + Smashing +
Northbase 10-system error-state study, Vortex + uipotion + Greenhouse +
Wayflyer (toast specs). Compiled 2026-10-14.*

---

## 1. FEEDBACK PLACEMENT — the "button error bloats the layout" bug

### The decision tree (ask two questions, in order)

1. **Does the message still matter after a page reload?**
   - No → **toast** (transient, auto-dismisses, nothing lost)
   - Yes → banner or inline (never a toast)
2. **Does the problem belong to exactly one control?**
   - One control (a field, a button's action) → **inline, attached to that
     control**, clears when the cause is fixed
   - Whole page/section → **banner** (persists until the condition lifts)

**Never** deliver a field/action error as a toast: Northbase's 10-system
study found toasts carry a recovery action only 19% of the time (worst of
all mechanisms) and users cannot act before dismissal. Smashing/NN/g:
"stay away from error messages as toasts, even persistent ones."

### The layout contract (fixes our expansion bug)

Our current `setFb` injects the message as a NEW inline block — the
container grows and shoves the layout. The industry contract:

| Rule | Recipe |
|---|---|
| Reserve the space OR overlay it — never reflow neighbors | `position: absolute` popover anchored to the control, or a fixed-height reserved slot |
| Cap the message | Toasts: 2 lines / ~80 chars (Wayflyer); longer → detail disclosure |
| Fixed width, content wraps inside | Desktop toast 380px; max-width on the message, `overflow-wrap: anywhere` |
| Error lives beside its cause | Validation: red text directly under the field, `border-color: var(--err)`, clear on valid input |
| Preserve user input on error | Never clear the form on failure |
| Recovery action | "Retry"/"Undo" CTAs — toasts with actions skip auto-dismiss |
| Accessibility | `role="alert"` for error/warn, `role="status"` for success; debounce identical messages (500ms) |

### Concrete spec (adopted for the dashboard)

```
Inline action feedback (per-button):
  container: position relative (the button's wrapper)
  message:   position absolute, top: 100%, left 0; min-width 100%;
             max-width 340px; z-index above siblings;
             background var(--surface2); border 1px var(--err|--ok);
             padding var(--sp-1-5) var(--sp-2); font-size var(--fs-xs);
             overflow-wrap anywhere; box-shadow var(--shadow);
  lifetime:  success 4s auto-dismiss; error 8s or click-to-dismiss
  a11y:      role="alert" (error) / role="status" (success)

Form field validation:
  message directly under the field, red, border highlight on the input,
  cleared the moment the field is valid; multi-field failures also get a
  summary ABOVE the submit button (never below it).
```

---

## 2. OVERFLOW & TRUNCATION — wrap vs truncate decision table

Fiori's decision table, condensed:

| Content | Behavior |
|---|---|
| Critical info, consent text, labels, statuses, links | **wrap** (always) |
| Numbers, UUIDs, monetary values | **never wrap, never truncate** (`white-space: nowrap`) |
| Secondary info (table cells, list secondary lines) | **truncate with …**, full text one interaction away |
| Teasers / long free text in tight tiles | **wrap N lines then clamp** (`line-clamp`) |
| Button labels | **never wrap** (buttons don't wrap by contract) |
| Error messages | **never truncate** (they explain a fix) |

### The CSS recipes

```css
/* truncate visually, keep full text in the DOM (screen readers read it) */
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            min-width: 0; }        /* min-width:0 lets flex/grid children shrink */

/* multi-line clamp (2 lines then …) */
.clamp2 { display: -webkit-box; -webkit-line-clamp: 2;
          -webkit-box-orient: vertical; overflow: hidden; }

/* reveal paths — truncation MUST have one (Primer/Material):
   title attr is NOT sufficient for keyboard users */
.truncate:hover .full, .truncate:focus-within .full { display: block; }
```

### The rules we adopt

1. `min-width: 0` on every flex/grid child that holds text (the #1 cause
   of blowouts — flex children refuse to shrink without it)
2. Full text stays in the DOM; reveal via focus-visible tooltip, expand
   control, or the detail view — never a hover-only tooltip
3. Truncating columns are declared as truncating in the component
   definition, not ad-hoc CSS
4. Never truncate text containing focusable elements (links/buttons)
5. Numeric/boolean/ID columns: `nowrap` always
6. Long names: wrap up to 2 lines, clamp after

---

## 3. SIZING & ALIGNMENT — the spacing ramp

**Consensus across Atlassian / Fluent 2 / Material / QLD / Zeta /
Appetite: a 4px base unit; 8px steps for layout; 4px sub-grid inside
small components.** Half-steps (2, 6, 10) exist for icon-to-label gaps
and chip padding — which is exactly the shape of our `--sp-1-5`/`--sp-2-5`.

### The ramp (ours, already in base.css)

| Token | px | Industry use |
|---|---|---|
| `--sp-1` | 4 | icon↔label gap, tight internal gaps |
| `--sp-1-5` | 6 | chip padding, dense row padding |
| `--sp-2` | 8 | label→control, small component padding |
| `--sp-2-5` | 10 | compact rows |
| `--sp-3` | 12 | field padding, card internal gap |
| `--sp-4` | 16 | card padding, screen side padding |
| `--sp-5` | 20 | row gaps |
| `--sp-6` | 24 | card-to-card |

### The grouping rule (Gestalt proximity — the part grids don't teach)

> **Internal spacing < external spacing.** Padding inside a card < margin
> between cards. Label→input gap < field→field gap. Heading→body gap <
> space above the heading. Same-size gaps everywhere = no visual grouping
> even when perfectly on-grid.

### Sizing rules

- **Touch targets ≥ 44×44** (Apple HIG / WCAG 2.5.5); Material says 48 —
  the visible control may be smaller, pad the bounding box
- **Element-first sizing** for predictable elements (buttons, inputs:
  fixed height, padding derives); **content-first** for unpredictable
  containers (tables, cards: padding fixed, size follows content)
- Buttons never wrap; long labels truncate with a title, or the action
  becomes a menu item
- Line-heights on the 4px sub-grid so baselines align
- 1px borders/hairlines are exempt from the grid (strokes, not spacing)

### Our token mapping (already swept)

`--sp-1..6` + `--sp-1-5`/`--sp-2-5` + `--ctrl-h` + `--fs-2xs..4xl` — the
sweep landed every padding/gap/font-size on these. Compact density now
re-pins all of them, so density mode is global.

---

## 4. DISCORD REFERENCE PALETTE (measured from client mods)

For feedback surfaces specifically (BetterDiscord/nightcord sampled the
real client):

| Tone | icon/text | background |
|---|---|---|
| info | `#5A88CE` | `#181F2C` |
| success | `#3AC172` | `#182724` |
| warn | `#FBBF24` | `#2C261C` |
| error | `#C13A3A` | `#26191E` |
| default | `rgba(255,255,245,.86)` | `#21252C` |

Lesson from their iterations: don't tint the icon/text and background
with the same hue at low opacity — sample purpose-built pairs (above).
Also: toasts always dismissible; empty-content toasts must never render
(a "safety guard" against empty black bubbles).

---

## 5. AUDIT CHECKLIST (per component class)

**Buttons** — never wrap; truncating label gets a title; feedback is an
anchored overlay/slot, not a layout push; success/error visually distinct
(icon + tone, never color alone); busy state keeps width (spinner inside,
no reflow); ≥44px touch target bounding box.

**Inputs** — label above, hint under, validation message under the field
in red with border highlight; error clears on valid input; user value
preserved on failed submit; never validate on first keystroke (blur or
submit).

**Tables** — numeric/ID columns `nowrap`; name/primary columns wrap ≤2
lines; secondary columns truncate with reveal; min-width per column from
content; horizontal scroll as the last resort; row height stable.

**Cards** — internal padding (--sp-3/--sp-4) < card-to-card margin
(--sp-6); title truncates, body wraps; tone = left accent border (our
existing pattern ✓); actions row pinned to the card bottom.

**Toasts** — fixed width 380px desktop / full-width-minus-margins mobile;
max 3 stacked; auto-dismiss 4s success / 7s error; pause on hover;
role=alert(status); always a dismiss button; debounce identical messages;
never the sole carrier of a must-act error.

**Modals** — always include ≥1 recovery action (93% industry norm);
max-height with internal scroll; ESC + backdrop-click close.

---

## 6. MAPPING TO OUR DEFECTS

| Defect (user-visible) | Dataset rule | Fix location |
|---|---|---|
| Button error expands the layout | §1 layout contract | `setFb` → anchored overlay (JS) + `.dui-fb` CSS |
| Field errors push forms around | §1 validation rows | `.dui-item` reserved message slot |
| Long names blow out rows/pickers | §2 min-width:0 + wrap≤2 | `.dui-lrow-primary`, `.pl-name` (already has ellipsis ✓) |
| Numbers/IDs wrap mid-value | §2 nowrap class | `.dui-lrow-secondary` (mono, nowrap ✓) |
| Uneven card/table rhythm | §3 internal<external + ramp | token sweep (done) + spacing audit per component |
| Success/error look identical | §1 tone pairs | `.dui-fb-*` tone classes with icon |
| Toasts (if adopted) | §4 palette + caps | `#toasts` exists — restyle to the spec above |

---

## 7. SOURCES

- Northbase — Error States Best Practices (10-enterprise study, 85 instances)
- Smashing Magazine — Designing Better Error Messages (2022)
- NN/g — Error Message Guidelines (2023)
- SAP Fiori — Wrapping and Truncation (decision tables)
- Elastic UI — Table layout guidelines (column contracts)
- GitHub Primer — Accessible truncation
- Material 3 — Text truncation & accessibility
- Atlassian — Spacing (8px base, token ramp)
- Fluent 2 — Layout (4px base + 2/6/10 icon half-steps, breakpoints)
- QLD Design System — 4px baseline / 8px layout grids
- Zeta — 4px mini-unit sizing scale
- Appetite UI — 4pt scale with named half-steps
- Vortex / uipotion / Greenhouse / Wayflyer — toast specs (caps, timers, a11y)
- BetterDiscord #1990 + nightcord — Discord client toast palettes (exact hex)
