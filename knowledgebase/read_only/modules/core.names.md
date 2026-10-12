# Module `core.names`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/names.py`

```
The three namespace validators (accounts, profiles, plugin slugs).

One source of truth for all three: PluginStateService key-scoping
correctness DEPENDS on every name class forbidding ':' and '/' — keep
that separator invariant here, in one place, if these ever change.
```
