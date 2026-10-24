# Module `core.dashboard_ui.context`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\dashboard_ui\context.py`

```
Callback context types for the dashboard UI API (Stage C).

Three small frozen dataclasses define the contract between the
framework and plugin-declared callables (visibility checks, action
callbacks, data providers):

  * :class:`CheckContext`  — handed to visibility checks and data
    providers. Deliberately carries NO viewer identity: the current
    dashboard authenticates with ONE shared bearer token, not per-user
    identity, so a "who is looking" field would be a lie plugins could
    not trust. Checks key off plugin/component/account only. When the
    dashboard grows real per-user auth this type gains a viewer field
    and every existing check keeps working (additive change).
  * :class:`ActionContext` — handed to action callbacks; additionally
    carries the plugin's services facade so actions can send, persist
    state and read settings exactly like lifecycle hooks do.
  * :class:`ActionResult`  — the normalized, JSON-safe result every
    action callback returns (or is coerced into by the registry).

Pure stdlib; no imports from adapters/, interfaces/, database/ or the
dashboard itself.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `CheckContext` | dataclass | yes | Trusted context for visibility checks — fail-closed. |
| `ActionContext` | dataclass | yes | Context passed to action callbacks. |
| `ActionResult` | dataclass | yes | Normalized action result — JSON-safe. |

## Functions

- `def clean_view_id(raw: Any) -> Optional[str]`
  - A client-supplied view id if well-formed, else None.
- `def current_view_id() -> Optional[str]`
  - The current request's dashboard view id (None = unknown/legacy).
