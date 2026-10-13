# Module `core.names`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/names.py`

```
The three namespace validators (accounts, profiles, plugin slugs).

One source of truth for all three: PluginStateService key-scoping
correctness DEPENDS on every name class forbidding ':' and '/' — keep
that separator invariant here, in one place, if these ever change.
```
