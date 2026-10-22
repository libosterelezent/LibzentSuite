# Module `database.repositories.tasks`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\repositories\tasks.py`

```
TaskHistoryRepository — persistent finished-task archive (spec §6).

Rows live in ``task_history``; the scheduler writes one row per finished
task (any terminal status) via ``add`` and may amend it via ``update``
(e.g. a late timeout reclassification). The scheduler's live Task ids are
preserved: an entry carrying its own "id" is stored under that id.

Rows map to/from plain dicts in the Task.as_dict() shape; unknown keys are
ignored, and "result_summary" is accepted as an alias for "summary"
(both spellings circulate in the contract).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `TaskHistoryRepository` | class | yes | add/update/recent for the task history archive (bounded: newest |

### `TaskHistoryRepository`

- `async def add(self, entry: Dict[str, Any]) -> int`
  - Insert one history row; returns its id.
- `async def max_id(self) -> int`
  - Highest stored task id (0 when the archive is empty).
- `async def update(self, task_id: int, **fields: Any) -> None`
  - Update ONLY the provided columns of one history row.
- `async def recent(self, limit: int=50, account: Optional[str]=None) -> List[Dict[str, Any]]`
  - The newest ``limit`` rows (oldest-first), optionally per-account.
