# Module `core._persist_queue`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\_persist_queue.py`

```
Shared queued-persistence engine for the log and audit sinks.

``LogService`` and ``AuditRecorder`` previously carried ~100 duplicated
lines each of drain-worker machinery. This is the one implementation.

Contract (spec §29 — the record path never touches SQLite):

* :meth:`put` is thread-safe, never blocks, never raises. Above
  ``max_inflight`` queued items new ones are DROPPED from persistence only
  (and counted) so a wedged database can't balloon memory.
* ONE asyncio drain worker pulls up to ``batch`` items and writes them with
  the repository's ``add_many`` (ONE transaction + ONE prune per batch)
  when it has one, else item-by-item ``add``. If a batch write fails it is
  retried item-by-item so a single bad row cannot take its neighbours down;
  failures are counted and surface as one raw stderr line per burst
  (never through the logging machinery itself — that is the feedback loop
  this design exists to avoid).
* :meth:`stop` finishes the in-flight batch (bounded join), cancels a
  stuck worker, then drains whatever is left INLINE in original order
  (bounded by ``timeout``). A batch interrupted by cancellation is kept
  and written first, so shutdown loses nothing it had already dequeued.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `PersistQueue` | class | yes | Thread-safe producer queue + one async batching drain worker. |

### `PersistQueue`

- `def active(self) -> bool`
  - True while a live worker is accepting records.
- `def stopped(self) -> bool`
  - True once a stop() completed: puts are counted dropped from then.
- `def put(self, item: Dict[str, Any]) -> None`
  - Enqueue one dict; check+put run under the lock so the cap and
- `def stats(self) -> Dict[str, int]`
- `async def start(self, repo: Any, *, batch: int=50, interval: float=0.25) -> Optional['asyncio.Task']` — side effects: `creates_tasks`
  - Spawn the worker (idempotent: a live worker is returned as-is).
- `async def stop(self, timeout: float=5.0) -> None` — side effects: `publishes_event`
  - Stop accepting, finish the in-flight batch, drain the rest.
