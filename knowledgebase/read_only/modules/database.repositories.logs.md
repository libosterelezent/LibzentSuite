# Module `database.repositories.logs`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\repositories\logs.py`

```
AuditRepository + LogRepository — the two bounded append-only trails.

* ``audit_logs`` — the interaction trail (spec §6): every inbound/outbound
  message with wait/jitter numbers, pruned to ``audit_keep`` newest rows on
  every insert (ported SQL from the audited old implementation).
* ``logs`` — the structured log sink LogService attaches to, pruned to
  ``log_keep`` newest rows the same way.

Both return rows as plain dicts with float timestamps; ``recent`` fetches
newest-first then reverses, so callers always see OLDEST-FIRST (tail)
ordering — matching the old audit code and LogService.tail.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AuditRepository` | class | yes | Interaction audit trail (bounded, newest-kept pruning). |
| `LogRepository` | class | yes | Structured log persistence (bounded, newest-kept pruning). |

### `AuditRepository`

- `async def add(self, entry: Dict[str, Any]) -> None`
  - Append one interaction record, then prune to the newest ``keep``.
- `async def add_many(self, entries: List[Dict[str, Any]]) -> int`
  - Append a batch in ONE transaction with ONE prune (the queued
- `async def recent(self, limit: int=100, account: Optional[str]=None) -> List[Dict[str, Any]]`
  - The newest ``limit`` audit rows, returned oldest-first.

### `LogRepository`

- `async def add(self, entry: Dict[str, Any]) -> None`
  - Append one structured log entry, then prune to the newest ``keep``.
- `async def add_many(self, entries: List[Dict[str, Any]]) -> int`
  - Append a batch in ONE transaction with ONE prune (all-or-nothing).
- `async def recent(self, limit: int=100, *, account: Optional[str]=None, severity: Optional[str]=None, source: Optional[str]=None) -> List[Dict[str, Any]]`
  - The newest ``limit`` log rows (oldest-first), optionally filtered.

## Functions

