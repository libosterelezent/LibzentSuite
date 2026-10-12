# The Dashboard UI API — plugin author's guide

> How a plugin builds its own dashboard pages with the fluent builder
> API: components, automatic IDs, actions, data providers, visibility
> checks, forms, lifecycle and scope. Source of truth:
> [`core/dashboard_ui/`](../core/dashboard_ui/) (`builders.py`,
> `components.py`, `_base.py`, `registry.py`, `context.py`,
> `errors.py`), [`core/plugin_base.py`](../core/plugin_base.py)
> (the hooks), [`core/plugin_manager/manager.py`](../core/plugin_manager/manager.py)
> (lifecycle integration), [`core/api.py`](../core/api.py) +
> [`interfaces/dashboard/server.py`](../interfaces/dashboard/server.py)
> (the 4 routes) and
> [`interfaces/dashboard/static/js/dashboard-ui.js`](../interfaces/dashboard/static/js/dashboard-ui.js)
> (the generic renderer).
>
> The **working reference example** is
> `tests/fixtures/plugins/config_demo/plugin.py` — every pattern below
> in one runnable plugin, exercised end-to-end by
> `tests/unit/test_dashboard_ui_acceptance.py`. Companion pages:
> [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) (the plugin contract),
> [API_ENDPOINTS.md §5a](API_ENDPOINTS.md) (the routes),
> [DASHBOARD.md](DASHBOARD.md) (the SPA).

The Dashboard UI API is the builder-based successor to the structured
`ui_stats()`/`ui_panels()` hooks (§7b of PLUGIN_SYSTEM.md — still
supported): instead of handing the dashboard fixed-shape data, a
plugin assembles a **component tree** (pages → tabs → sections →
cards → inputs/buttons/data parts) with validated, JSON-serializable
builders. The dashboard renders ANY tree with its generic component
renderer; actions and data flow back through server-side callbacks.
Everything is structured data — a plugin never emits HTML, so its
output is XSS-safe by construction.

---

## 1. Import and the hook

```python
from core.dashboard_ui import DashboardUIToolset, ActionResult
```

`DashboardUIToolset` is the namespace of builder classes (also
re-exported individually: `Layout`, `Page`, `Tab`, `Section`, `Card`,
`Row`, `Column`, `Text`, `Badge`, `Metric`, `Checkbox`, `Toggle`,
`TextInput`, `NumberInput`, `Textarea`, `Select`, `RadioGroup`,
`MultiSelect`, `ListView`, `Table`, `Progress`, `Button`, `Form`).
The context/result types live beside it: `ActionContext`,
`ActionResult`, `CheckContext`.

Declare a UI by overriding ONE optional hook on the plugin class:

```python
def initialize_dashboard_ui(self, ui: DashboardUIToolset) -> None:
    page = ui.Page().set_id("my-plugin").set_title("My Plugin")
    section = ui.Section().set_title("Controls")
    section.add_component(
        ui.Button(component_id="run").set_label("Run")
        .set_action(self._on_run))
    page.add_section(section)
    self.attach_dashboard_ui(ui.Layout().add_page(page))
```

Facts that matter:

* The hook is **SYNC** — attachment is pure in-memory work (validate →
  freeze → index callbacks), no event-loop gymnastics.
* The plugin manager calls it during account activation, AFTER
  `on_load`/`on_enable` (actions may reflect plugin state), and only
  when the class actually OVERRIDES it — plugins without the hook are
  completely unaffected (backward compatible).
* `self.attach_dashboard_ui(layout)` registers the finished tree. It
  raises `UIError` on an invalid tree, a duplicate-ID tree, or when no
  registry is wired (only the managed lifecycle wires one).
* A failing hook NEVER fails the activation: the error is recorded on
  the runtime (`last_error = "ui_init: …"`) and any partial
  registration is detached. Isolation is the framework's job.

---

## 2. Builders: the fluent API

Every builder shares the same contract:

* **fluent setters** — each returns `self`, validates its argument
  IMMEDIATELY (a bad type raises `UIError` before any state changes)
  and refuses to run once the layout is frozen (`FrozenLayoutError`);
* **common setters** — `set_id(str)`, `set_title(str)`,
  `set_check(callable)` and `set_disabled(bool)` exist on EVERY
  component;
* **serialization** — `to_dict()` yields a JSON-safe dict
  (`{"type", "id", …props}` plus `"children"` for containers);
  callables never cross the wire (an action serializes as
  `{"action": true}` — the server maps the component id back to the
  callable).

### 2.1 The tree shape (who may hold whom)

```
Layout
└── Page                       (set_title/description/icon/order)
    ├── Section                (set_title/description — any content)
    │   ├── Card               (boxed content, optional set_title)
    │   ├── Row / Column       (layout; Row may not nest Row)
    │   └── …content components (see 2.2–2.4) and Form
    └── Tab                    (set_title/icon — holds Sections only)
        └── Section → …
```

Typed add/remove methods per container: `Layout.add_page(s)/remove_page`,
`Page.add_section(s)/add_tab(s)/remove_*`, `Tab.add_section(s)`,
`Section/Card/Row/Column.add_component(s)/remove_component`,
`Form.add_field(s)/remove_field`. The generic `children()`,
`get_child(id)`, `clear_children()` and `replace_child(id, comp)` work
on every container. Multi-child adds are **atomic** — every child is
validated before any is attached, so a failing batch leaves the tree
untouched.

### 2.1b Alignment & side-by-side

Every component participates in one small flex vocabulary (whitelisted
keywords and bounded ints only — no style strings cross the wire):

**On any component** (applies when rendered inside a Row/Column/Container):

| Setter | Meaning |
|---|---|
| `set_grow(0–100)` | how eagerly this child takes FREE space side-by-side (equal weights split evenly; 2 vs 1 doubles up). A fixed `set_width` wins while space lasts; grow governs the surplus |
| `set_shrink(0–100)` | squeeze allowance when space runs short (0 = never) |
| `set_align_self("auto"\|"start"\|"center"\|"end"\|"stretch"\|"baseline")` | override the parent's cross-axis alignment for this child only |

**On every content container** (Section, Card, Row, Column, Container):

| Setter | Meaning |
|---|---|
| `set_align(...)` | CROSS-axis of the children — vertical inside a Row, horizontal inside a Column/vertical Container |
| `set_justify("start"\|"center"\|"end"\|"between"\|"around"\|"evenly")` | MAIN-axis distribution — horizontal in a Row, vertical in a Column |

```python
# 1 : 2 : 1 side-by-side split
ui.Row().add_component(
    ui.Container().set_grow(1),
    ui.Container().set_grow(2),   # double share
    ui.Container().set_grow(1))

# fixed value beside flowing description
ui.Container().set_direction("horizontal").set_gap("12px").add_component(
    ui.DisplayText().set_value("99.2%").set_size("xl")
        .set_tone("ok").set_monospace(True).set_width("120px"),
    ui.Container().set_grow(1))
```

### 2.2 Display components

| Builder | Key setters | Purpose |
|---|---|---|
| `Text` | `set_value`, `set_variant("body"\|"muted"\|"code"\|"heading")`, `set_data_source(callable)` (live) | a text block |
| `Badge` | `set_value`, `set_variant("info"\|"ok"\|"warn"\|"err")` | a status pill |
| `Metric` | `set_label`, `set_value` (str), `set_sub`, `set_level(""\|"ok"\|"warn"\|"err")`, `set_data_source(callable)` (live) | a label/value readout |
| `Progress` | `set_value` (number 0–100), `set_label` | a progress bar |
| `DisplayText` | `set_value`, `set_tone("default"\|"dim"\|"ok"\|"warn"\|"err")`, `set_size("xs"\|"sm"\|"md"\|"lg"\|"xl")`, `set_weight("normal"\|"medium"\|"semibold"\|"bold")`, `set_align("left"\|"center"\|"right")`, `set_monospace(bool)` | read-only presentation text — values with visual weight |
| `Alert` | `set_title`, `set_value` (message), `set_variant("info"\|"ok"\|"warn"\|"err")` | the designed error/notice box — also the markup the renderer itself uses when a data provider or page fetch fails |
| `Separator` | `set_variant("solid"\|"dashed")` | a horizontal divider |
| `Description` | `set_value` | a muted descriptive paragraph |
| `Header` | `set_title`, `set_subtitle` | a prominent section header |
| `Footer` | `set_value` | a small muted footer line |

**Live leaves** — `Metric` and `Text` accept a data provider
(`{"value", "sub?", "level"?}` for metrics, `{"value": str}` or a bare
`str` for text). The frontend refreshes them in place on every stats
tick without rebuilding the DOM, so forms the user is typing in
elsewhere on the page survive. This is the builder-based replacement
for hand-rolled log tails and report viewers.

### 2.3 Input components (the eight field types)

All share `set_label`, `set_description`, `set_placeholder`,
`set_required(bool)`, `set_value` (the initial value) — and being
interactive, `set_action(callable)` (dispatched when the field
changes, payload `{"value": <current>}`).

| Builder | Extra setters | Value type |
|---|---|---|
| `Checkbox` | — | `bool` |
| `Toggle` | — (switch-styled checkbox) | `bool` |
| `TextInput` | `set_max_length(int)` | `str` |
| `NumberInput` | `set_min_value`, `set_max_value`, `set_step` (numbers) | `str` (the field text; bounds are hints) |
| `Textarea` | `set_rows(int)` | `str` |
| `Select` | `set_options(list)` — strings or `{"value","label"}` dicts | `str` |
| `RadioGroup` | (select rendering) | `str` |
| `MultiSelect` | (select rendering) | `list` |

### 2.4 Data + action components

| Builder | Key setters | Purpose |
|---|---|---|
| `Container` | `set_direction("vertical"\|"horizontal")`, `set_gap(size)`, `set_align`, `set_justify` | a neutral untitled grouping box — the alignment primitive; nests (explicit opt-in) |
| `Grid` | `set_columns(1–6 \|"auto")`, `set_min_child(size)` (auto mode), `set_gap(size)` | STRUCTURED columns — children slot into equal cells that wrap as whole rows (the tidiness primitive; auto-fill is inherently responsive) |
| `Toolbar` | `set_title`, `set_icon`, `set_gap`, `set_align` | the section header bar: title left, actions right — refresh/add buttons sit IN the bar instead of floating above the content |
| `TileView` | `set_data_source`, `set_key_column`, `set_row_actions`, `set_selectable`, `set_bulk_actions`, `set_empty_text` | a responsive card grid fed by a provider (`{tiles: [{key, title, tone, status, lines, chips}]}`) — same row-action and bulk-selection contracts as Table |
| `Modal` | `set_title`, `set_description`, `add_component` | a dialog overlay in the page tree (`page.add_modal(...)`); opens client-side via `Button.set_open_modal(id)` or server-side when an action result carries `modal: id` (the context-modal pattern) |
| `ListView` | `set_data_source(callable)`, `set_empty_text` | a list fed by a server-side provider |
| `Table` | `set_columns([...])`, `set_data_source(callable)`, `set_page_size(int)`, `set_empty_text`, `set_key_column(str)`, `set_row_actions([RowAction, ...])` | a paginated table fed by a provider, with optional per-row buttons |
| `Button` | `set_label`, `set_description`, `set_variant("primary"\|"secondary"\|"danger"\|"ghost")`, `set_confirm(str)`, `set_loading(bool)`, `set_action(callable)` | dispatches an action on click |
| `Form` | `set_title`, `set_description`, `set_submit_label`, `set_submit_action(callable)`, `add_field(...)` | groups the eight input types behind ONE submit |
| `RowAction` | `RowAction(action_id, label, callable, variant="secondary", confirm="")` | one per-row button for a `Table` |

**Row actions** — a table's per-row buttons dispatch through the table's
own action channel: `payload = {"action": <row action id>, "row": <key>}`.
The callable runs as `callable(row_key, context)` (sync or async) and may
return anything an action callback may. The key comes from the declared
`set_key_column("name")` — or, for composite or display-cleaned keys,
from an extra `"__key"` cell the provider appends PAST the visible
columns (the renderer never displays it):

```python
rows.append([visible, cells, ..., f"{account}|{plugin}"])   # __key rides last
```

**Rich tiles** — a tile dict may carry `status` (rendered as the same
`.st` pill the legacy cards used, colored by `tone`), `meta`
(`[[icon, value], …]` — compact icon-prefaced row: 🔑 db · ⏱ 1h · ⚡
88ms), `chips` (plain strings or `{text, kind}` with the legacy chip
colors: group/plugin/ok/warn/err) and `lines` (label/value grid).

**Bulk selection** — `Table`/`TileView` `.set_selectable(True)` renders
a checkbox per row/tile; `.set_bulk_actions([BulkAction(...)])` adds the
bulk bar (shown while rows are selected). BulkAction handlers receive
the selected key LIST: `action(rows, context)`.

**Dynamic form fields** — `Form.set_fields_provider(callable)`: the
provider returns `{"fields": [{id, type, label, value?, placeholder?,
description?, required?, options?, min?, max?, step?}]}` (types:
text/number/textarea/select/checkbox) and the renderer builds REAL
inputs, pre-filled — the schema-driven editor pattern (the profile
editor's fields come from the plugin's setting schema, values from the
profile being edited). May combine with static identity fields; a
refresh naming the form re-fetches the fields.

**Dynamic options** — `Select.set_options_provider(callable)` (also on
RadioGroup/MultiSelect): the provider returns `{"options": [...],
"value": [...]}` and the renderer fetches it lazily — choices track
live state (accounts, groups, scenarios) while the tree stays frozen.
Cannot be combined with static `set_options`.

**The modal action channel** — an action returning `{"modal": id}` (or
`{"open_modal": id}`) opens that Modal after the refresh list runs:
record which row the modal is about in your own state, name the modal's
components in `refresh`, return the modal id. `Form.set_close_on_submit
(True)` closes the containing dialog on success.

**Client-action forms** — `Form.set_client_action(name)` handles the
submit entirely in the browser. Supported names: `"save-token"` (the
dashboard's session-token editor — the token must never round-trip a
server action) and `"save-theme"` (the decorative theme picker).
Server plugins have no use for either.

**Form field grids** — `Form.set_columns(3)` (or `"auto"`) lays the
fields out in a compact grid instead of the default vertical stack; the
submit row stays full-width below. The difference between a form and a
field tower.

**The decorative layer** — customization that can NEVER touch
structure: `Card.set_tone("info"|"ok"|"warn"|"err")` adds an accent
border (emphasis only), `Section.set_icon(<svg path>)` draws a marker
beside the title, and the dashboard-level theme (accent hue + density,
Tools → Appearance) re-pins CSS variables on `<html>`. Tone never
changes meaning, order or layout — only look.

---

## 3. Automatic IDs and explicit IDs

With no explicit id every component generates one:
`uuid.uuid4().hex[:12]` — 12 hex chars, collision-practically-never.
You pass an explicit id as a constructor kwarg or `set_id()`:

```python
ui.Button(component_id="run")           # at construction
ui.Button().set_id("run")               # later (releasing the old id)
```

**When to use explicit ids:** whenever the plugin needs to TARGET the
component afterwards — an id you will quote in `ActionResult.refresh`,
look up in logs, or reference from other plugin code. Pure decoration
(cards, labels, layout scaffolding) can stay automatic. Rules:
1–64 chars, `[A-Za-z0-9_-]` only (`InvalidComponentIdError` otherwise).

## 4. Duplicate IDs — rejected twice

* **At add time** (same parent): `_check_child` compares against the
  parent's registered child ids and raises
  `DuplicateComponentIdError` carrying the type and tree PATH of both
  the existing and the conflicting component — the collision is
  reported precisely, and the tree is untouched (validation runs
  before any mutation).
* **At attach time** (tree-wide): `Layout.validate()` BFS-walks the
  whole tree and collects duplicate ids ACROSS branches (plus wrong
  child types, orphaned parent pointers, shared parents/cycles and
  non-callable actions). `attach()` raises `TreeValidationError`
  listing every problem; the previously attached layout (if any)
  survives a failed replacement untouched.

Avoid duplicates by namespacing explicit ids with a plugin prefix
(`"myplugin-run"`) — the registry keys everything by
`(plugin_name, component_id)`, so two PLUGINS may safely reuse the
same id, but two components in ONE layout may not.

---

## 5. Actions — direct Python callables

```python
ui.Button(component_id="run").set_action(self._on_run)

async def _on_run(self, context: ActionContext, payload: dict) -> ActionResult:
    ...
```

* **No manual registration** — `set_action(callable)` IS the
  registration. The registry extracts bound methods/functions while
  walking the tree at attach time and maps
  `(plugin, component_id) → callable`.
* **Signature**: `(context, payload) → result`. `context` is an
  `ActionContext` (`plugin`, `component_id`, `account` — the dashboard's
  selected account, `services` — the SAME paced facade lifecycle hooks
  use, so an action can send/persist like any hook; `None` on
  stripped-down stacks — degrade gracefully). `payload` is the JSON
  body the frontend sent: `{}` for buttons, `{"value": …}` for
  standalone inputs, `{field_id: value}` for form submits.
* **Sync or async** — both first-class (awaitables are awaited).
* **Return values are normalized** into `ActionResult`:
  `None` → plain success; `str` → success + message; `dict` → either a
  result-shaped dict (`success`/`message`/`data`/`field_errors`/
  `refresh`) or a data payload; anything else becomes `data`.
* `ActionResult(success, message="", data=None, field_errors={},
  refresh=[])` — `message` is the one-line toast, `data` any
  JSON-safe payload, `field_errors` the inline form errors (§8),
  `refresh` the component ids whose data the frontend should re-fetch.
* **Failures are contained**: an exception inside the callback is
  logged and surfaces as a plain `UIError` ("action … failed: …") at
  the route — never a traceback, never a crash, never other plugins.

Buttons additionally support `set_confirm("Are you sure?")` (the
frontend asks before dispatching) and `set_loading(True)` (busy state).

## 6. Data providers

```python
ui.Table(component_id="events")
  .set_columns(["kind", "count"])
  .set_data_source(self._recent_events)

def _recent_events(self, context: CheckContext) -> list[dict]:
    return [{"kind": "claim", "count": 3}, ...]
```

* `set_data_source(callable)` — called with a `CheckContext` pinned to
  the component (`plugin`, `component_id`, `component_type`,
  `account`); sync or async.
* The return value MUST be **JSON-safe**: `dict` (str keys) / `list` /
  `str` / `int` / `float` / `bool` / `None`, nesting ≤ 8, lists capped
  at 500 items (truncated with a warning). Anything else (a datetime,
  a dataclass, a set…) raises `UIError`.
* Table shapes: a list of row dicts (columns inferred or declared via
  `set_columns`) or `{"columns": [...], "rows": [[...]]}`. List-views
  render lists of `{label, value}` (or plain values) and `{key:
  value}` objects.
* Fetches are **visibility-checked** first (§7) and lazy — the
  renderer only fetches when the page is shown, and re-fetches the ids
  an action names in `refresh`.
* `set_empty_text("…")` customizes the empty state.

## 7. Visibility checks — fail-closed

```python
ui.Tab(component_id="advanced").set_check(self._advanced_visible)

def _advanced_visible(self, context: CheckContext) -> bool:
    return True   # the demo has no gate — but the API is proven
```

* `set_check(callable)` exists on **every** component. The callable
  receives a `CheckContext` (`plugin`, `component_id`,
  `component_type`, `account`) and returns a truthy/falsy value; sync
  or async.
* **Semantics: fail-closed.** A falsey return OR a raised error hides
  the component — errors are logged, never surfaced. Unknown plugin or
  component id → hidden.
* **Ancestor chains compose**: the registry walks from the component
  UP to the layout root; every ancestor with a check AND the
  component's own check must allow (a denying ancestor
  short-circuits). A check on a Tab/Section guards its whole subtree.
* Applied in BOTH places: the rendered page tree AND every
  server-side request (`dispatch_action`, `get_data`,
  `check_visibility`) — a hidden component cannot be poked over the
  API ("component … is not available").
* **Deliberate limitation**: `CheckContext` carries NO viewer
  identity — the dashboard authenticates with one shared bearer token,
  so per-viewer visibility would be a lie plugins could not trust.
  Checks express STATE conditions (a feature flag, an account being
  linked, a flow being idle), not WHO is asking. When per-user auth
  arrives, the dataclass gains a viewer field and existing checks keep
  working.

## 8. Forms and field_errors

```python
form = (ui.Form(component_id="rule-form")
        .set_title("Add a threshold rule")
        .set_submit_label("Add rule")
        .set_submit_action(self._on_submit_rule))
form.add_field(ui.TextInput(component_id="rule-name").set_required(True))
form.add_field(ui.NumberInput(component_id="rule-threshold")
               .set_min_value(0).set_max_value(100))

async def _on_submit_rule(self, context, payload) -> ActionResult:
    # payload = {"rule-name": <str>, "rule-threshold": <float|None>, ...}
    errors = {}
    if not str(payload.get("rule-name") or "").strip():
        errors["rule-name"] = "the rule name is required"
    if errors:
        return ActionResult(success=False, message="fix the fields",
                            field_errors=errors)
    return ActionResult(success=True, message="saved",
                        refresh=["events-table"])
```

A Form may ONLY hold the eight input types (`add_field` rejects
anything else with `InvalidChildError`). The submit fires ONCE per
click with every field's current value collected client-side, keyed by
**field component id** — the same keys `field_errors` uses, so the
renderer can pin each error under its input. `set_required(True)` is a
rendering hint (the `*` marker); the server-side action decides what
is actually mandatory.

## 9. Disabled — UI state, NOT authorization

```python
ui.Button(component_id="export").set_label("Export").set_disabled(True)
```

`set_disabled(bool)` exists on every component (a disabled container
renders its whole subtree inert). It is a **frontend presentation
hint**: the server still dispatches the action if asked. Never treat
it as an access control — gate actions on real state inside the
callback (or hide the component with `set_check`). The acceptance test
pins exactly this: a disabled button serializes `"disabled": true` and
still dispatches.

## 10. Layout lifecycle: attach → frozen → explicit update

* **attach** — `registry.attach(plugin_name, layout)` validates the
  whole tree, FREEZES it (every setter/add now raises
  `FrozenLayoutError`), extracts the callbacks into the dispatch
  indexes and stores the JSON-safe snapshot. Re-attaching replaces:
  the NEW tree validates first, only then are the old registrations
  dropped — a failed replacement keeps the old UI alive.
* **frozen means stable** — the serialized page every dashboard client
  sees is immutable; plugin code cannot corrupt a live UI.
* **explicit update** — there is no live mutation. To change a
  rendered page: build a FRESH tree and `attach_dashboard_ui(new)` (or
  `detach` + re-`attach`); the next navigation/page fetch picks it up.
  Detaching unfreezes the tree, so an OLD builder object can be
  mutated and re-attached — but never re-attach a still-frozen live
  layout (that raises: build fresh instead).
* **reload/unload cleanup** — the plugin manager detaches the plugin's
  UI when the last account deactivates it (`detach` is idempotent;
  dispatch/data/visibility for a detached plugin fail closed/404), and
  re-registers on the next activation. Stale registrations cannot
  survive a reload.

## 11. Plugin-wide scope — one UI per plugin

A plugin's UI registers ONCE, no matter how many accounts run the
plugin: the FIRST account to activate it wins, later activations of
the same plugin see the registration and skip. One dashboard page per
plugin, not per account — the dashboard is a fleet console, and
per-account variation belongs in the ACTION callbacks (which receive
`context.account`) and data providers, not in N copies of the layout.
Consequence worth internalizing: the registered callbacks are bound to
the first-activated instance's state. Keep that state account-agnostic
or key it off `context.account` (the config_demo event log does).

## 12. The complete example

`tests/fixtures/plugins/config_demo/plugin.py` (fixture — never
discovered by a production boot; assign it to a test account to try
it). The shape:

* **Page** "Config Demo" (`set_id` for stable `?page_id=` targeting,
  title from resolved settings, `set_description`, `set_order(10)`)
* **Tab "General"** — Section → two Cards: `Checkbox` WITH an action
  (`{"value": bool}` payloads), `TextInput` (`set_max_length`,
  `set_required`), `NumberInput` (`set_min_value(5)` /
  `set_max_value(300)` / `set_step(5)`), `Select` with labeled
  `{"value", "label"}` options (fast/normal/slow), `Toggle`,
  `RadioGroup`, `MultiSelect`, `Textarea` — the full input catalog.
* **Tab "Advanced"** — carries `set_check(self._advanced_visible)`
  (returns `True` — always visible, but proves the API); a `Form`
  (name/threshold/note) whose submit action validates and returns
  `field_errors` per field; a `Button` with `set_confirm("Are you
  sure?")`; a `set_disabled(True)` button whose action STILL
  dispatches (UI state only); a status `Badge`.
* **Tab "Data"** — a `Table` fed by a provider returning row dicts
  (`set_columns`, `set_page_size`, `set_empty_text`), a `Row`/`Column`
  pair holding a `Metric` + `Progress`, a `ListView` fed by a second
  provider, and a muted `Text`.
* **Actions** return full `ActionResult`s — messages with the
  `context.account`, `data` payloads, and `refresh=["recent-events",
  "status-lines"]` naming the provider-backed components to re-fetch.

`tests/unit/test_dashboard_ui_acceptance.py` drives it through the
REAL lifecycle: discovery → activation → hook → attach → serialize →
dispatch (checkbox, form valid + invalid, purge, disabled button) →
data providers → visibility check → deactivation cleanup.

---

## 13. The four routes (dashboard side)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/dashboard-ui/navigation` | sidebar entries `[{plugin, page_id, title, icon, order}]` (sorted by `(order, plugin)`) — reads frozen snapshots only, no plugin code runs |
| GET | `/api/dashboard-ui/page?plugin=&page_id=` | one page's serialized component tree (404 unknown plugin/page) |
| POST | `/api/dashboard-ui/action` | `{plugin, component_id, payload}` → visibility-checked dispatch → the normalized `ActionResult` |
| GET | `/api/dashboard-ui/data?plugin=&component_id=` | visibility-checked provider fetch (JSON-safe, bounded; 404 unknown/hidden/provider-less) |

Full request/response detail: [API_ENDPOINTS.md §5a](API_ENDPOINTS.md).

---

## 14. The framework's own pages ($framework)

The dashboard's MAIN TABS — Home, Plugins, Activity, Policies, Admin,
Tools — are built with this same API (`core/framework_ui.py`) and
registered under the reserved plugin name `"$framework"` at boot
(`attach_framework_ui(registry, api)` from the bootstrap). The SPA
renders them into the standard tab panels through the same generic
renderer (`js/framework-ui.js`); the sidebar filters the reserved name
out because those pages ARE the tabs.

What this gives plugin authors: the component set is exercised by the
dashboard itself — anything the main tabs do (row actions, live
metrics, form validation with `field_errors`, refresh lists) is a
working reference. Accounts & Groups and Profiles stay on the legacy
panels until the builder API grows selection/modals.

Framework actions close over the ControlAPI exclusively (audit and
validation included) and return `refresh` lists naming the data
components a mutation touched — the pattern every plugin should follow.
