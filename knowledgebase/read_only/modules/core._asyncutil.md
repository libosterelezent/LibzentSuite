# Module `core._asyncutil`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/_asyncutil.py`

```
Small asyncio helpers shared by the event bus and the task scheduler.
```

## Functions

- `def outer_cancel_pending(task: Optional['asyncio.Task']=None, *, unknown: bool=False) -> bool`
  - True when ``task`` (default: the current one) has a PENDING cancel
