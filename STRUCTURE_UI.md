Dashboard UI Structure Review.

Intent: Full UI audit and fixes or solution according to the project owner, and in depth discussion on components and elements that were introduced in the project to have a customisation flow for the ui.

_______________________________________________________________________
|           |                                                         |
|           |_____________________Header______________________________|
|           |                                                         |
|           |                                                         |
|           |                                                         |
|           |                                                         |
|           |                                                         |
|  Sidebar  |                                                         |
|           |                                                         |
|           |                                                         |
|           |                  Main Content                           |
|           |                                                         |
|           |                                                         |
|           |                                                         |
|           |                                                         |
|           |                                                         |
|           |                                                         |
|           |                                                         |
|___________|_________________________________________________________|

Layout Intro :-
To have the screen size aware layout we used liquid layout that adjust the UI components and elements according to the devices screen.

Sidebar :- 
side bar expands when the screen has enough space, and collapses when screen is at a midium size, fully hides when the dashboard is opened in mobiles by showing a upper-left corner button, there is a glitch on it and that is when sidebar is opened in phones it unnecessarily occupies the entire screen only icons were shown with no text beside them which makes it ugly. Conclusion: the overall structure is good but improvement is needed in phone screens do not overlap the entire sidebar over the browser screen make it responsive with close x on top and pc-like aligned items.

Main Content :-
this is where components and elements add themeselves through robust building block classes, and extensive features, though there are some that controls sidebar entries vice versa.


# Dashboard UI Component Inventory
Make a plan for how these components should be rendered they owns the ground of main content.

## Universal settings — every component inherits these

| Setter     | Values | Notes |
|------------------------|---|---|
| `set_id(str)`          | `[a-zA-Z0-9_-]{1,64}` | auto-generated UUID-12 if omitted; duplicates rejected tree-wide |
| `set_title(str)`       | any text              | rendered where the component has a title concept                 |
| `set_check(callable)`  | `(CheckContext) → bool` | visibility rule, fail-closed on raise; evaluated with the full ancestor chain |
| `set_disabled(bool)`   | — | **enforced server-side**: disabled components (and descendants) can't dispatch actions or serve data |
| `set_width(v)`         | `"300px"`, `"50%"`, `"12rem"`, `"50vh"`, or number→px | validated CSS size |
| `set_height(v)`        | `"300px"`, `"50%"`, `"12rem"`, `"50vh"`, or number→px | validated CSS size |
| `set_grow(0–100)`      |
| `set_shrink(0–100)`    | int | flex share when side-by-side |
| `set_align_self(...)`  | `auto, start, center, end, stretch, baseline` | per-child cross-axis override |

## Structure components

| Component | Own setters | Allowed children | Purpose |
|---|---|---|---|
| **Layout** | `add_page()`, `add_pages()`, `remove_page(id)`, `pages()` | Page | tree root; one per plugin |
| **Page** | `set_description`, `set_icon`, `set_order(int)`, `set_account_scope(bool)`, `add_section(s)()`, `add_modal(s)()`, `add_tab(s)()` | Section, Tab, Modal | sidebar entry; `account_scope` draws the account picker |
| **Tab** | `set_icon`, `add_section(s)()` | Section | renders as a genuine ARIA tablist with keyboard nav + lazy panel loading |
| **Section** | `set_description`, `set_icon` | all content components | titled block, spacing contract |
| **Card** | `set_tone(info/ok/warn/err)` | all content | boxed summary panel, decorative accent border |
| **Row** | — | content minus Row | horizontal flex (no nested rows) |
| **Column** | — | content minus Row | vertical flex |
| **Container** | `set_direction(vertical/horizontal)` | content + Containers | neutral grouping box, nests opt-in |
| **Grid** | `set_columns(1–6 or "auto")`, `set_min_child(css)` | content minus Row | equal-cell structured columns; auto-fill responsive |
| **Toolbar** | — | content minus Row | header bar: title+icon left, children cluster right, never stretch |
| **Modal** | `set_description` | all content | page-level `<dialog>`; opens via `Button.set_open_modal` or action result `{"modal": id}`; `Form.set_close_on_submit` closes it |

Every container also inherits: `set_gap(size)`, `set_align(start/center/end/stretch/baseline)`, `set_justify(start/center/end/between/around/evenly)`, `set_icon` (via `_ComponentParent`).

## Display components

| Component | Setters | Values |
|---|---|---|
| **Text** | `set_value`, `set_variant`, `set_data_source` (live) | body, muted, code, heading |
| **DisplayText** | `set_value`, `set_tone`, `set_size`, `set_weight`, `set_align`, `set_monospace` | tone: default/dim/ok/warn/err · size: xs/sm/md/lg/xl · weight: normal/medium/semibold/bold · align: left/center/right |
| **Badge** | `set_value`, `set_variant` | info, ok, warn, err |
| **Metric** | `set_label`, `set_value`, `set_sub`, `set_level`, `set_data_source` (live: `{value, sub?, level?}`) | level: ""/ok/warn/err |
| **Progress** | `set_value(0–100)`, `set_label` | clamped |
| **Alert** | `set_title`, `set_value`, `set_variant` | info, ok, warn, err — also the renderer's error surface |
| **Separator** | `set_variant` | solid, dashed |
| **Description** | `set_value` | muted paragraph |
| **Header** | `set_title`, `set_subtitle` | prominent heading |
| **Footer** | `set_value` | small muted line |

## Input components (all inherit `set_label`, `set_description`, `set_placeholder`, `set_required`, `set_value`, `set_action` — dispatched `{value}` on change)

| Component | Extra setters |
|---|---|
| **Checkbox** / **Toggle** | bool value |
| **TextInput** | `set_max_length(int)` |
| **NumberInput** | `set_min_value`, `set_max_value`, `set_step` (finite numbers; bools rejected) |
| **Textarea** | `set_rows(int)` |
| **Select** | `set_options(list)` — strings or `{value,label}`; **or** `set_options_provider` (live options, mutually exclusive with static) |
| **RadioGroup** | inherits Select's option setters |
| **MultiSelect** | same; value is a list |

## Data components

| Component | Setters | Contract |
|---|---|---|
| **ListView** | `set_data_source`, `set_empty_text` + **kv mode** (default) OR **records mode**: `set_columns(1–6)`, `set_key_column`, `set_row_actions`, `set_selectable`, `set_bulk_actions` | kv: list/`{label,value}` · records: `{records:[{key, cells}]}` with cell types: string, identity `{primary,secondary}`, status `{text,tone,meta}`, chips `{chips:[{text,kind}]}` |
| **Table** | `set_columns`, `set_data_source`, `set_empty_text`, `set_page_size`, `set_key_column`, `set_row_actions`, `set_selectable`, `set_bulk_actions` | `{columns, rows}` arrays; extra `__key` cell for composite keys; 500-item/list cap |
| **TileView** | `set_data_source`, `set_empty_text`, `set_key_column`, `set_row_actions`, `set_selectable`, `set_bulk_actions` | `{tiles:[{key,title,status,tone,meta,chips,lines}]}` — summaries only, not records |
| **Button** | `set_label`, `set_description`, `set_variant(primary/secondary/danger/ghost)`, `set_confirm(text)`, `set_open_modal(id)`, `set_loading(bool)`, `set_action(cb, require_account=False)` | |
| **Form** | `set_title`, `set_description`, `set_submit_action(cb, require_account=False)`, `set_submit_label`, `set_close_on_submit`, `set_columns(1–4/"auto")` (field grid), `set_fields_provider` (schema-driven dynamic fields), `set_client_action(save-token/save-theme)`, `add_field(s)()`, `remove_field()` | server-side schema validation before the callback; unknown keys rejected on static forms |

## Action helpers (not rendered directly)

| Helper | Constructor | Handler signature |
|---|---|---|
| **RowAction** | `(id, label, callable, variant=secondary, confirm="")` | `action(row_key, context)` — sync or async |
| **BulkAction** | `(id, label, callable, variant, confirm)` | `action(rows_list, context)` |

## Runtime contracts

- **Actions**: return `None` / str / dict / `ActionResult` — normalized to `{success, message, data, field_errors, refresh, modal}`; data validated JSON-safe; `refresh` ids must exist in the layout
- **Callbacks**: bound methods rebind to the live plugin runtime (refuse without one); plain functions run as registered; `require_account=True` demands a validated account + services facade
- **Sync policy**: served stack offloads sync callbacks (4 lanes, 10s timeout)
- **Limits**: ≤1000 components, ≤24 depth per tree; provider lists ≤500; icons = SVG path-data charset only

Also in the live dashboard but **not** builder components: the shell (sidebar, header, search, toasts, WS indicator), onboarding panel, and the confirm dialog — deliberately hardcoded. Want this as a permanent reference page in `knowledgebase/`?
