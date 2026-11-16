# Module `core.names`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\names.py`

```
The three namespace validators (accounts, profiles, plugin slugs).

One source of truth for all three: PluginStateService key-scoping
correctness DEPENDS on every name class forbidding ':' and '/' — keep
that separator invariant here, in one place, if these ever change.
```
