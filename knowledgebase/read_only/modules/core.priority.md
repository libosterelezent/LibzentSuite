# Module `core.priority`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\priority.py`

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
