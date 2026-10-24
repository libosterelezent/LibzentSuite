# Module `core.dashboard_ui._base`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\dashboard_ui\_base.py`

```
Dashboard UI builder primitives.

Argument-check helpers plus the UIComponent / UIContainer contract and
the interactive/input mixins every concrete builder derives from.
Package-internal: import the public classes from core.dashboard_ui (or
core.dashboard_ui.builders), not from here.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `UIComponent` | class | yes | Common contract for every UI builder. |
| `UIContainer` | class | yes | A component that can hold children. |
| `_ComponentParent` | class | no | Shared add/remove API for the free-form content containers |
| `_Interactive` | class | no | Base for components that register server-side actions. |
| `_InputField` | class | no | Shared field scaffolding: label, description, placeholder, required, |

### `UIComponent`

- `def set_id(self, component_id: str) -> 'UIComponent'`
  - Change the id. Validated first; releases the old id with the
- `def set_title(self, title: str) -> 'UIComponent'`
- `def set_check(self, check) -> 'UIComponent'`
  - Set the visibility check callable (evaluated server-side).
- `def set_disabled(self, disabled: bool) -> 'UIComponent'`
- `def set_width(self, width) -> 'UIComponent'`
  - Set the component width (CSS size string or px number).
- `def set_height(self, height) -> 'UIComponent'`
  - Set the component height (CSS size string or px number).
- `def set_grow(self, grow) -> 'UIComponent'`
  - Side-by-side share: how eagerly this child takes free space.
- `def set_shrink(self, shrink) -> 'UIComponent'`
  - Squeeze allowance (flex-shrink weight, 0–100; 0 = never).
- `def set_align_self(self, align) -> 'UIComponent'`
  - Override the PARENT's cross-axis alignment for this child only.
- `def get_id(self) -> str`
- `def get_check(self)`
- `def get_parent(self)`
- `def to_dict(self) -> dict`
  - JSON-safe dict: type, id, props (and children for containers).

### `UIContainer`

- `def get_child(self, component_id: str)`
- `def children(self) -> list`
- `def clear_children(self) -> 'UIContainer'`
- `def replace_child(self, component_id: str, comp) -> 'UIContainer'`
  - Atomically swap the child with `component_id` for `comp`.
- `def to_dict(self) -> dict`

### `_ComponentParent`

- `def set_gap(self, gap) -> '_ComponentParent'`
  - Spacing between children (CSS size string or px number).
- `def set_icon(self, icon) -> '_ComponentParent'`
  - Decorative SVG path drawn beside the title (24×24 viewBox
- `def set_align(self, align) -> '_ComponentParent'`
  - Cross-axis alignment of the children ("start"|"center"|"end"|
- `def set_justify(self, justify) -> '_ComponentParent'`
  - Main-axis distribution of the children ("start"|"center"|"end"|
- `def add_component(self, comp) -> '_ComponentParent'`
- `def add_components(self, *comps) -> '_ComponentParent'`
- `def remove_component(self, component_id: str) -> '_ComponentParent'`

### `_Interactive`

- `def set_action(self, action, require_account: bool=False) -> '_Interactive'`
  - Register the action callable.

### `_InputField`

- `def set_label(self, label: str) -> '_InputField'`
- `def set_description(self, description: str) -> '_InputField'`
- `def set_placeholder(self, placeholder: str) -> '_InputField'`
- `def set_required(self, required: bool) -> '_InputField'`
- `def set_value(self, value: str) -> '_InputField'`

## Functions

