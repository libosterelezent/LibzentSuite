# Module `core.theme_store`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\theme_store.py`

```
Theme store — the dashboard theme engine's durable state.

The theme engine (interfaces/dashboard/static/js/theme.js) keeps its state
in localStorage for instant paint, and mirrors it here so fonts, accent,
custom CSS and presets survive a browser change. This is a DASHBOARD
PRESENTATION concern: it deliberately does NOT go through ControlAPI (the
CLI has no theme), the dashboard server owns the two routes that read and
write it.

Shape (owned by the frontend engine; this module only validates the
envelope, never the semantics):

    {
      "scope": "global",
      "global": {...},
      "pages": {...},
      "pageCss": {...},
      "updated_at": 1730000000000
    }

Atomic write (tmp + os.replace) so a crash mid-save can never leave a
truncated file behind.
```

## Functions

- `def load() -> dict`
  - The persisted theme state, or {} when absent/corrupt (a broken
- `def save(state: dict) -> None`
  - Persist the theme state atomically. Raises ValueError on an
