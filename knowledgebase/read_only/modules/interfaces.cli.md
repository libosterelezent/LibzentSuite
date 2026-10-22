# Module `interfaces.cli`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `interfaces\cli\__init__.py`

```
CLI interface package: shared command engine + REPL/one-shot surface.

Both the REPL and one-shot mode route through :func:`run_command`, which calls
ONLY ``core.api.ControlAPI`` methods — the same facade the dashboard uses, so
the two interfaces can never drift apart in capability.
```
