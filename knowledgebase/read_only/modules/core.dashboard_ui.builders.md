# Module `core.dashboard_ui.builders`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\dashboard_ui\builders.py`

```
Dashboard UI builder core: fluent, validated, JSON-serializable components.

Stage B of the dashboard UI API. Pure core — stdlib only, no imports from
adapters/, interfaces/, database/ or the dashboard itself. This module is
the canonical public surface of the package: it defines the Layout root
and the DashboardUIToolset namespace and re-exports the whole catalog
(implementation lives in _base.py and components.py).

Design guarantees:

  * fluent builders for the whole catalog (Layout -> Page/Tab ->
    Section/Card/Row/Column -> display, input, data and action parts);
  * id management: uuid4-hex[:12] generated ids, validated explicit ids
    ([A-Za-z0-9_-]{1,64}), parent-level duplicate detection on add and
    layout-level tree validation for everything else;
  * cycle prevention on every add (ancestor walk) and atomic multi-child
    adds (all-or-nothing);
  * type-checked setters that never corrupt state on failure;
  * JSON-safe to_dict() serialization — actions surface as
    {"action": True} (never the callable; the server-side registry maps
    component ids back to callables) and no parent/internal refs leak.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Layout` | class | yes | Root of a dashboard UI tree; holds pages. |
| `DashboardUIToolset` | class | yes | The public namespace handed to initialize_dashboard_ui(). |

### `Layout`

- `def add_page(self, page) -> 'Layout'`
- `def add_pages(self, *pages) -> 'Layout'`
- `def remove_page(self, component_id: str) -> 'Layout'`
- `def pages(self) -> list`
- `def validate(self) -> list[str]`
  - Validate the whole tree; returns a list of error strings
