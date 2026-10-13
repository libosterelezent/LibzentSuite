# Module `interfaces`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `interfaces/__init__.py`

```
User-facing interface packages: `cli` (REPL + one-shot) and `dashboard`.

Interfaces are thin surfaces over `core.api.ControlAPI` ONLY — parity between
the CLI and the dashboard is guaranteed because both call the exact same
facade methods (spec: "One Core API" design goal).
```
