# Module `core.framework_ui`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\framework_ui.py`

```
The framework's OWN dashboard pages, built with the plugin UI API.

The main dashboard tabs (Home, Plugins, Activity, Policies, Admin, Tools)
are defined here through the SAME builder toolset plugins use
(:mod:`core.dashboard_ui`) and registered in the
:class:`~core.dashboard_ui.registry.DashboardUIRegistry` under the
reserved plugin name ``$framework``. The SPA renders them with the same
generic renderer as plugin pages — the dashboard dogfoods its own UI API.

Design rules:
  * pages are BUILT ONCE at boot (frozen snapshots); anything dynamic is
    a data provider (tables, list-views, live metrics/texts) or an
    action closure over the ControlAPI — never a rebuilt layout;
  * actions call ControlAPI methods only (audit + validation included)
    and return ``refresh`` lists so the renderer re-fetches exactly the
    data components the mutation touched;
  * the reserved ``$framework`` plugin name cannot collide with a real
    plugin (the loader rejects ``$`` in plugin names), and the generic
    sidebar nav filters it out — these pages ARE the main tabs.

Row keys: tables whose rows need composite or display-cleaned keys emit
an extra ``__key`` cell past the visible columns; the renderer resolves
``key_column`` against the columns first, then the ``__key`` cell.

Accounts & Groups and Profiles stay on the legacy panels until the
builder API grows selection/modals; everything here is fully covered by
components that exist today.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `_ViewState` | class | no | Dict-like modal context, scoped to the requesting dashboard VIEW. |

### `_ViewState`

- `def get(self, key: str, default: Any=None) -> Any`

## Functions

- `def attach_framework_ui(registry: DashboardUIRegistry, api: Any) -> int`
  - Build and register the framework's main-tab pages.
