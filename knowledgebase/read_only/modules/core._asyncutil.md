# Module `core._asyncutil`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\_asyncutil.py`

```
Small asyncio helpers shared by the event bus and the task scheduler.
```

## Functions

- `def cancellation_attributable(task: Optional['asyncio.Task']=None) -> bool`
  - True when this interpreter can TELL an outer ``Task.cancel()`` apart
- `def outer_cancel_pending(task: Optional['asyncio.Task']=None, *, unknown: bool=False) -> bool`
  - True when ``task`` (default: the current one) has a PENDING cancel
