# Module `database.schema`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `database/schema.py`

```
Ordered SQL migrations. Append-only: NEVER edit an applied migration.

Each entry: (version, name, sql). The runner (database/db.py) records applied
versions in `schema_migrations` and applies pending ones in order inside a
transaction per migration.
```

## Functions

- `def latest_version() -> int`
  - Highest migration version defined (0 when none).
