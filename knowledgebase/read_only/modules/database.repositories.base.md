# Module `database.repositories.base`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `database/repositories/base.py`

```
Shared plumbing for every repository over the async connection.

Repos never touch cursors, commits, or locks directly: reads go through
``_fetch_all``/``_fetch_one`` (WAL readers, no writer lock), writes go
through ``_tx`` (serialized transaction context). SQL stays inside repos;
core services consume repository methods; plugins consume plugin-facing
services — never this layer (spec §30/§31).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `BaseRepository` | class | yes | Base class for all repositories: the shared async connection. |

### `BaseRepository`


## Functions

- `def loads_tolerant(raw: Optional[str], default: Any, context: str) -> Any`
  - ``json.loads`` with a safety net: corrupt/missing values return ``default``.
