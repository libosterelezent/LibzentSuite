# Module `core.dashboard_ui.registry`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/dashboard_ui/registry.py`

```
DashboardUIRegistry — the central registry for plugin dashboard UIs.

Stage C of the dashboard UI API. Manages the whole server-side lifetime
of a plugin's UI:

  * layout attachment  — validate the tree, freeze it, extract the
    action callbacks / visibility checks / data providers, store the
    JSON-safe snapshot;
  * action dispatch    — visibility-checked, fail-closed invocation of
    component callbacks with normalized :class:`ActionResult` output;
  * data providers     — visibility-checked, JSON-safe, bounded results;
  * visibility         — walks the component's FULL ancestor chain,
    evaluating every check (sync or async); any False or any raised
    error hides the component (fail-closed). ``visible_tree`` applies
    the same verdicts to the SERIALIZED page tree, so denied subtrees
    are pruned from what the frontend renders, not merely blocked at
    dispatch;
  * lifecycle cleanup  — detach unfreezes the tree and drops every
    registration; idempotent.

Plugin-scoped: two plugins can use the same component id safely — every
map is keyed ``plugin_name -> component_id -> ...``.

SYNC/ASYNC SPLIT (design decision): ``attach``/``detach``/``get_layout``/
``list_plugins`` are plain SYNCHRONOUS methods — they only validate,
freeze and index in-memory structures, no I/O — so the sync
``PluginBase.initialize_dashboard_ui`` hook can call them directly via
``attach_dashboard_ui`` with no event-loop gymnastics. The dispatch
surface (``dispatch_action``/``get_data``/``check_visibility``) is async
because it invokes PLUGIN callables, which may be coroutine functions.

Pure core: stdlib + the builder package only. Nothing here imports from
adapters/, interfaces/, database/ or the dashboard itself (Stage D's
routes call INTO this registry).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `DashboardUIRegistry` | class | yes | Central registry for plugin-attached dashboard UIs. |

### `DashboardUIRegistry`

- `def attach(self, plugin_name: str, layout: Layout) -> dict`
  - Validate, freeze and register ``layout`` for ``plugin_name``.
- `def detach(self, plugin_name: str) -> bool`
  - Unfreeze and remove every registration for ``plugin_name``.
- `def get_layout(self, plugin_name: str) -> Optional[dict]`
  - The stored JSON-safe snapshot (a copy — callers may mutate it);
- `def list_plugins(self) -> List[str]`
  - Plugin names with an active UI (sorted for determinism).
- `async def dispatch_action(self, plugin_name: str, component_id: str, payload: dict, context: ActionContext) -> ActionResult`
  - Invoke one component's action callback (visibility-checked).
- `async def get_data(self, plugin_name: str, component_id: str, context: CheckContext) -> Any`
  - Fetch one component's data (visibility-checked, JSON-safe).
- `async def check_visibility(self, plugin_name: str, component_id: str, context: CheckContext) -> bool`
  - Evaluate the component's FULL ancestor chain of checks.
- `async def visible_tree(self, plugin_name: str, node: dict, account: Optional[str]=None) -> Optional[dict]`
  - A copy of a serialized ``node`` with every visibility-denied
