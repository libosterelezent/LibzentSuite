# Module `core.dashboard_ui.components`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/dashboard_ui/components.py`

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
| `Checkbox` | class | yes | A labeled boolean checkbox with an optional change action. |
| `Toggle` | class | yes | Visual variant of Checkbox (switch styling). |
| `TextInput` | class | yes | A single-line text field. |
| `NumberInput` | class | yes | A numeric field with optional bounds and step. |
| `Textarea` | class | yes | A multi-line text field. |
| `Select` | class | yes | A single-choice dropdown. |
| `RadioGroup` | class | yes | Single-choice rendered as a radio group. |
| `MultiSelect` | class | yes | Multi-choice select; the value is a list. |
| `ListView` | class | yes | A dynamic list fed by a server-side data source callable. |
| `Table` | class | yes | A paginated table fed by a server-side data source callable. |
| `Progress` | class | yes | A 0-100 progress indicator. |
| `Button` | class | yes | A clickable action with an optional confirmation and loading state. |
| `Form` | class | yes | A labeled group of input fields with a submit action. |
| `Section` | class | yes | A titled block inside a Page/Tab holding any content component. |
| `Card` | class | yes | A boxed content container with an optional title. |
| `Row` | class | yes | Horizontal layout container (no nested rows). |
| `Column` | class | yes | Vertical layout container (no nested rows). |
| `Tab` | class | yes | A tab page inside a Page; holds sections. |
| `Page` | class | yes | A dashboard page holding sections and/or tabs. |

### `Text`

- `def set_value(self, text: str) -> 'Text'`
- `def set_variant(self, variant: str) -> 'Text'`

### `Badge`

- `def set_value(self, text: str) -> 'Badge'`
- `def set_variant(self, variant: str) -> 'Badge'`

### `Metric`

- `def set_label(self, label: str) -> 'Metric'`
- `def set_value(self, value: str) -> 'Metric'`
- `def set_sub(self, sub: str) -> 'Metric'`
- `def set_level(self, level: str) -> 'Metric'`

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

- `def set_options(self, options) -> 'Select'`
  - Accepts a list of strings or {'value','label'} dicts; both are

### `MultiSelect`

- `def set_value(self, value) -> 'MultiSelect'`

### `ListView`

- `def set_data_source(self, data_source) -> 'ListView'`
- `def set_empty_text(self, text: str) -> 'ListView'`

### `Table`

- `def set_columns(self, columns) -> 'Table'`
- `def set_data_source(self, data_source) -> 'Table'`
- `def set_empty_text(self, text: str) -> 'Table'`
- `def set_page_size(self, page_size: int) -> 'Table'`

### `Progress`

- `def set_value(self, value) -> 'Progress'`
- `def set_label(self, label: str) -> 'Progress'`

### `Button`

- `def set_label(self, label: str) -> 'Button'`
- `def set_description(self, description: str) -> 'Button'`
- `def set_variant(self, variant: str) -> 'Button'`
- `def set_confirm(self, confirm: str) -> 'Button'`
  - Require the user to confirm with this message before firing.
- `def set_loading(self, loading: bool) -> 'Button'`

### `Form`

- `def set_submit_action(self, action) -> 'Form'`
- `def set_submit_label(self, label: str) -> 'Form'`
- `def add_field(self, field) -> 'Form'`
- `def add_fields(self, *fields) -> 'Form'`
- `def remove_field(self, component_id: str) -> 'Form'`
- `def fields(self) -> list`

### `Section`

- `def set_description(self, description: str) -> 'Section'`

### `Tab`

- `def set_icon(self, icon: str) -> 'Tab'`
- `def add_section(self, section) -> 'Tab'`
- `def add_sections(self, *sections) -> 'Tab'`

### `Page`

- `def set_description(self, description: str) -> 'Page'`
- `def set_icon(self, icon: str) -> 'Page'`
- `def set_order(self, order: int) -> 'Page'`
- `def add_section(self, section) -> 'Page'`
- `def add_sections(self, *sections) -> 'Page'`
- `def add_tab(self, tab) -> 'Page'`
- `def add_tabs(self, *tabs) -> 'Page'`
- `def remove_section(self, component_id: str) -> 'Page'`
- `def remove_tab(self, component_id: str) -> 'Page'`
- `def sections(self) -> list`
- `def tabs(self) -> list`
