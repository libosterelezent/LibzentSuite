# Module `core.dashboard_ui.components`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\dashboard_ui\components.py`

```
The dashboard UI component catalog.

Every concrete builder except the Layout root: display parts (Text,
Badge, Metric), the eight input types (Checkbox/Toggle, TextInput and
specializations, Select and variants), data parts (ListView, Table,
Progress), the Button action component, and the containers that hold
them (Form, Section, Card, Row, Column, Tab, Page). Package-internal:
import the public classes from core.dashboard_ui (or
core.dashboard_ui.builders), not from here.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Text` | class | yes | A block of text. |
| `Badge` | class | yes | A small status pill. |
| `Metric` | class | yes | A label/value/sub readout with an optional level. |
| `Separator` | class | yes | A horizontal divider line. |
| `Description` | class | yes | A descriptive paragraph (rendered smaller/muted below a title). |
| `Header` | class | yes | A prominent section header with optional subtitle. |
| `Footer` | class | yes | A muted footer line (small text at the bottom of a section). |
| `Checkbox` | class | yes | A labeled boolean checkbox with an optional change action. |
| `Toggle` | class | yes | Visual variant of Checkbox (switch styling). |
| `TextInput` | class | yes | A single-line text field. |
| `NumberInput` | class | yes | A numeric field with optional bounds and step. |
| `Textarea` | class | yes | A multi-line text field. |
| `Select` | class | yes | A single-choice dropdown. |
| `RadioGroup` | class | yes | Single-choice rendered as a radio group. |
| `MultiSelect` | class | yes | Multi-choice select; the value is a list. |
| `RowAction` | class | yes | One per-row button on a :class:`Table`. |
| `BulkAction` | class | yes | One bulk-operation button for a selectable Table/TileView. |
| `_Selectable` | class | no | Shared selection machinery for Table and TileView (mixin). |
| `ListView` | class | yes | A dynamic list fed by a server-side data source callable. |
| `Table` | class | yes | A paginated table fed by a server-side data source callable. |
| `Progress` | class | yes | A 0-100 progress indicator. |
| `Button` | class | yes | A clickable action with an optional confirmation and loading state. |
| `Form` | class | yes | A labeled group of input fields with a submit action. |
| `Container` | class | yes | A neutral, untitled grouping box — the alignment primitive. |
| `TileView` | class | yes | A responsive grid of data-driven cards (tiles). |
| `Toolbar` | class | yes | A header bar: optional title on the left, children on the right. |
| `Grid` | class | yes | A structured column grid — THE layout primitive for tidiness. |
| `Alert` | class | yes | A message box for errors, warnings and notices. |
| `DisplayText` | class | yes | Read-only presentation text with tone, size, weight and alignment. |
| `Section` | class | yes | A titled block inside a Page/Tab holding any content component. |
| `Card` | class | yes | A boxed content container with an optional title. |
| `Row` | class | yes | Horizontal layout container (no nested rows). |
| `Column` | class | yes | Vertical layout container (no nested rows). |
| `Tab` | class | yes | A tab page inside a Page; holds sections. |
| `Modal` | class | yes | A dialog overlay rendered from the page tree. |
| `Page` | class | yes | A dashboard page holding sections and/or tabs (plus modals). |

### `Text`

- `def set_value(self, text: str) -> 'Text'`
- `def set_variant(self, variant: str) -> 'Text'`
- `def set_data_source(self, data_source) -> 'Text'`
  - Live text: a provider returning ``{"value": str}`` (or a str).

### `Badge`

- `def set_value(self, text: str) -> 'Badge'`
- `def set_variant(self, variant: str) -> 'Badge'`

### `Metric`

- `def set_label(self, label: str) -> 'Metric'`
- `def set_value(self, value: str) -> 'Metric'`
- `def set_sub(self, sub: str) -> 'Metric'`
- `def set_level(self, level: str) -> 'Metric'`
- `def set_data_source(self, data_source) -> 'Metric'`
  - Live values: a provider returning ``{value, sub?, level?}``.

### `Separator`

- `def set_variant(self, variant: str) -> 'Separator'`
  - 'solid' (default) or 'dashed'.

### `Description`

- `def set_value(self, text: str) -> 'Description'`

### `Header`

- `def set_subtitle(self, text: str) -> 'Header'`

### `Footer`

- `def set_value(self, text: str) -> 'Footer'`

### `Checkbox`

- `def set_value(self, value: bool) -> 'Checkbox'`

### `TextInput`

- `def set_max_length(self, max_length: int) -> 'TextInput'`

### `NumberInput`

- `def set_min_value(self, min_value) -> 'NumberInput'`
- `def set_max_value(self, max_value) -> 'NumberInput'`
- `def set_step(self, step) -> 'NumberInput'`

### `Textarea`

- `def set_rows(self, rows: int) -> 'Textarea'`

### `Select`

- `def set_options_provider(self, provider) -> 'Select'`
  - DYNAMIC options: a callable returning ``{"options": [...]}``
- `def set_options(self, options) -> 'Select'`
  - Accepts a list of strings or {'value','label'} dicts; both are

### `MultiSelect`

- `def set_value(self, value) -> 'MultiSelect'`

### `RowAction`


### `BulkAction`


### `_Selectable`

- `def set_selectable(self, selectable: bool)`
  - Render a checkbox per row/tile; selected keys feed the bulk
- `def set_bulk_actions(self, actions) -> 'Table'`
  - Bulk-operation buttons shown while rows are selected (list of

### `ListView`

- `def set_data_source(self, data_source) -> 'ListView'`
- `def set_empty_text(self, text: str) -> 'ListView'`
- `def set_columns(self, columns) -> 'ListView'`
  - Declare RECORDS mode: column labels for structured rows.
- `def set_key_column(self, column: str) -> 'ListView'`
  - Which cell value identifies a record (row actions/selection).
- `def set_row_actions(self, actions) -> 'ListView'`

### `Table`

- `def set_columns(self, columns) -> 'Table'`
- `def set_data_source(self, data_source) -> 'Table'`
- `def set_empty_text(self, text: str) -> 'Table'`
- `def set_page_size(self, page_size: int) -> 'Table'`
- `def set_key_column(self, column: str) -> 'Table'`
  - Which column identifies a row for row-action dispatch.
- `def set_row_actions(self, actions) -> 'Table'`
  - Declare per-row buttons (list of :class:`RowAction`).

### `Progress`

- `def set_value(self, value) -> 'Progress'`
- `def set_label(self, label: str) -> 'Progress'`

### `Button`

- `def set_label(self, label: str) -> 'Button'`
- `def set_description(self, description: str) -> 'Button'`
- `def set_variant(self, variant: str) -> 'Button'`
- `def set_confirm(self, confirm: str) -> 'Button'`
  - Require the user to confirm with this message before firing.
- `def set_open_modal(self, modal_id) -> 'Button'`
  - Open a Modal client-side on click (no server round trip).
- `def set_loading(self, loading: bool) -> 'Button'`

### `Form`

- `def set_submit_action(self, action, require_account: bool=False) -> 'Form'`
  - Register the submit callable (see set_action for the
- `def set_submit_label(self, label: str) -> 'Form'`
- `def set_close_on_submit(self, close: bool=True) -> 'Form'`
  - Close the containing Modal after a successful submit (no
- `def set_fields_provider(self, provider) -> 'Form'`
  - DYNAMIC fields: a callable returning ``{"fields": [def, …]}``
- `def set_columns(self, columns) -> 'Form'`
  - Lay the FIELDS out in a grid (int 1–4, or "auto") instead of
- `def set_description(self, description: str) -> 'Form'`
- `def set_client_action(self, name: str) -> 'Form'`
  - Handle submission CLIENT-side instead of a server action.
- `def add_field(self, field) -> 'Form'`
- `def add_fields(self, *fields) -> 'Form'`
- `def remove_field(self, component_id: str) -> 'Form'`
- `def fields(self) -> list`

### `Container`

- `def set_direction(self, direction) -> 'Container'`
  - Which way children flow ("vertical" default | "horizontal").

### `TileView`

- `def set_data_source(self, data_source) -> 'TileView'`
- `def set_empty_text(self, text: str) -> 'TileView'`
- `def set_key_column(self, column: str) -> 'TileView'`
- `def set_row_actions(self, actions) -> 'TileView'`

### `Grid`

- `def set_columns(self, columns) -> 'Grid'`
  - Column count (int 1–6) or "auto" (with set_min_child).
- `def set_min_child(self, min_child) -> 'Grid'`
  - Minimum child width in auto mode (CSS size; default 240px).

### `Alert`

- `def set_value(self, value) -> 'Alert'`
  - The message body.
- `def set_variant(self, variant: str) -> 'Alert'`
  - Severity look + icon ("info"|"ok"|"warn"|"err").

### `DisplayText`

- `def set_value(self, value) -> 'DisplayText'`
- `def set_tone(self, tone: str) -> 'DisplayText'`
  - Color intent ("default"|"dim"|"ok"|"warn"|"err").
- `def set_size(self, size: str) -> 'DisplayText'`
  - Type scale ("xs"|"sm"|"md"|"lg"|"xl").
- `def set_weight(self, weight: str) -> 'DisplayText'`
  - Font weight ("normal"|"medium"|"semibold"|"bold").
- `def set_align(self, align: str) -> 'DisplayText'`
  - Self alignment of the text ("left"|"center"|"right").
- `def set_monospace(self, monospace: bool) -> 'DisplayText'`

### `Section`

- `def set_description(self, description: str) -> 'Section'`

### `Card`

- `def set_tone(self, tone) -> 'Card'`
  - Decorative accent border ("info"|"ok"|"warn"|"err").

### `Tab`

- `def set_icon(self, icon: str) -> 'Tab'`
  - Set the sidebar icon (SVG path data, narrow charset).
- `def add_section(self, section) -> 'Tab'`
- `def add_sections(self, *sections) -> 'Tab'`

### `Modal`

- `def set_description(self, description: str) -> 'Modal'`

### `Page`

- `def set_description(self, description: str) -> 'Page'`
- `def set_icon(self, icon: str) -> 'Page'`
  - Set the sidebar icon (SVG path data, narrow charset).
- `def set_account_scope(self, scoped: bool=True) -> 'Page'`
  - Declare this page ACCOUNT-SCOPED: the dashboard shows an
- `def set_order(self, order: int) -> 'Page'`
- `def add_section(self, section) -> 'Page'`
- `def add_sections(self, *sections) -> 'Page'`
- `def add_modal(self, modal) -> 'Page'`
- `def add_modals(self, *modals) -> 'Page'`
- `def add_tab(self, tab) -> 'Page'`
- `def add_tabs(self, *tabs) -> 'Page'`
- `def remove_section(self, component_id: str) -> 'Page'`
- `def remove_tab(self, component_id: str) -> 'Page'`
- `def sections(self) -> list`
- `def tabs(self) -> list`
