# Module `core.priority`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/priority.py`

```
Shared task/priority ordering used by BOTH the rate limiter and scheduler.

Lives in its own tiny module so neither component imports the other just for
this enum (avoids a circular dependency while keeping one source of truth).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Priority` **PLUGIN API** | enum | yes | Queue priority: HIGH actions are granted before NORMAL before LOW. |

### `Priority` — PLUGIN API

- `def coerce(cls, value: 'int | str | Priority | None') -> 'Priority'`
  - Best-effort conversion (int value, name string, or None -> NORMAL).
