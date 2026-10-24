# Module `database.telemetry`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `database\telemetry.py`

```
FINAL-PHASE (G1/R1): boundary DB worker telemetry — R1 mitigation.

cProfile (``benchmark --profile``) covers MAIN-THREAD Python functions
only; the aiosqlite worker thread's cost shows up as process cpu_sys
with no attribution. This module adds WORKER_THREAD_TIMING measured at
the database/connection.py boundary — deliberately NOT a per-statement
sqlite profiler.

What the seams CAN see (and record):
  * TOTAL_DB_LATENCY — submit→result per read/standalone-execute op,
    exactly the latency the caller experienced (includes the worker
    queue wait, sqlite execution, the threadsafe hop and loop wakeup).
  * COMMIT_TIME — the centralized ``await conn.commit()`` calls
    (connection.py is the only committer in the codebase).
  * TRANSACTION_TIME — outermost ``write()`` body incl. its commit.
  * GUARD_WAIT — caller-side cross-task queuing on the connection.

What they CANNOT (documented, not faked): the queue-wait vs
sqlite-execution split per statement, and per-statement timings inside
a transaction (repositories submit directly through the yielded conn).
A phase-2 ``set_trace_callback`` hook could add true worker-side
statement instants — deliberately out of scope here.

Honesty contract: enabled==False means ZERO work (one attribute load +
bool test per op — no perf_counter, no contextmanager). No per-record
logging, ever; ``snapshot()`` is the only reader surface.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `DbTelemetry` | class | yes | Cumulative + ring telemetry for DB boundary timings (thread-safe: |

### `DbTelemetry`

- `def set_worker_thread(self, thread: Any) -> None`
  - Capture the aiosqlite worker's kernel TID (thread CPU source).
- `def record(self, kind: str, seconds: float, ok: bool=True) -> None`
- `def reset(self) -> None`
- `def snapshot(self) -> Dict[str, Any]`
