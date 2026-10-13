# Module `core.logging_service`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/logging_service.py`

```
Centralized logging service (spec §10).

Every component (core, account, plugin, scheduler, ratelimiter, adapter)
records STRUCTURED entries through LogService instead of scattering print()
calls or raw logger text. Each entry carries:

    timestamp, severity, source, account, plugin, task_id, event, message

Consumers:
  * stderr mirror (human-readable, one line per record),
  * the dashboard Logs page (live tail over WebSocket — no text parsing),
  * the database `logs` table (bounded, best-effort — via an ASYNC drain
    worker fed by a thread-safe queue, spec §29: the record path NEVER
    touches SQLite synchronously).

Persistence model (async DB layer): ``record()`` only appends to the ring,
bumps counters, mirrors to stdlib, notifies subscribers and ENQUEUES a dict
on a ``queue.SimpleQueue`` (thread-safe; callable from any thread, never
awaits, never blocks). ``await start_persistence(repo)`` spawns one asyncio
drain worker that awaits ``repo.add(entry)`` per dequeued item; failures are
counted in ``stats()['db_failures']`` and never raise (one raw stderr
one-liner per failure burst at most). The queue is capped at
``_MAX_INFLIGHT`` in-flight items — beyond that records are DROPPED from
persistence (ring/counters unaffected) and counted in ``stats()['dropped']``.

STARTUP ORDER: ``await start_persistence(...)`` BEFORE heavy logging begins
(early records otherwise never persist). SHUTDOWN ORDER: ``await
stop_persistence(...)`` BEFORE ``await database.close()`` so the drain
finishes against a live connection. With no persistence started, record()
does zero extra work and never warns (simulator/headless boots).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `LogEntry` | dataclass | yes | One structured log record (JSON-safe via as_dict). |
| `LogService` | class | yes | Ring buffer + live subscribers + queued-async-DB sink for logs. |
| `ServiceLogger` | class | yes | Stdlib-logger-shaped adapter over LogService (shared convention shim). |
| `StdlibBridge` | class | yes | Bridges stdlib logger records into the structured LogService. |

### `LogEntry`

- `def as_dict(self) -> Dict[str, Any]`

### `LogService`

- `def ingest_context(self)`
  - Mark this thread as inside a bridge ingestion (mirror suppressed).
- `def attach_db(self, repository: Any) -> None`
  - Configure the persistence repository — CONFIGURATION ONLY.
- `async def start_persistence(self, repo: Any, *, batch: int=50, interval: float=0.25) -> Optional['asyncio.Task']`
  - Spawn the async drain worker (call BEFORE heavy logging).
- `async def stop_persistence(self, timeout: float=5.0) -> None`
  - Stop the drain worker (idempotent); call BEFORE ``db.close()``.
- `def record(self, severity: str, source: str, message: str, *, account: Optional[str]=None, plugin: Optional[str]=None, task_id: Optional[int]=None, event: Optional[str]=None) -> Dict[str, Any]`
  - Store one structured entry; returns its JSON-safe dict.
- `def info(self, source: str, message: str, **ctx: Any) -> Dict[str, Any]`
- `def warning(self, source: str, message: str, **ctx: Any) -> Dict[str, Any]`
- `def error(self, source: str, message: str, **ctx: Any) -> Dict[str, Any]`
- `def tail(self, n: int=100, *, account: Optional[str]=None, plugin: Optional[str]=None, severity: Optional[str]=None, source: Optional[str]=None) -> List[Dict[str, Any]]`
  - Most recent entries (oldest first) after applying the filters.
- `def subscribe(self, callback: Callable[[LogEntry], None]) -> Callable[[], None]`
  - Live-tail subscription (dashboard WS); returns unsubscribe.
- `def stats(self) -> Dict[str, Any]`
  - Counters by severity + drop/failure counters for the dashboard.

### `ServiceLogger`

- `def debug(self, msg: Any, *args: Any, **ctx: Any) -> None`
- `def info(self, msg: Any, *args: Any, **ctx: Any) -> None`
- `def warning(self, msg: Any, *args: Any, **ctx: Any) -> None`
- `def warn(self, msg: Any, *args: Any, **ctx: Any) -> None`
- `def error(self, msg: Any, *args: Any, **ctx: Any) -> None`
- `def exception(self, msg: Any, *args: Any, **ctx: Any) -> None`
- `def log(self, level: int, msg: Any, *args: Any, **ctx: Any) -> None`
- `def record(self, severity: str, source: str, message: str, **ctx: Any) -> dict`
  - LogService-style call passes through tagged with THIS source.

### `StdlibBridge`

- `def emit(self, record: logging.LogRecord) -> None`

## Functions

- `def stop_stderr_pipeline() -> None`
  - Stop the stderr listener thread and detach its queue handler.
- `def configure_stderr(level: int=logging.INFO) -> None`
  - Attach one human-readable stderr handler to the logger tree (idempotent).
- `def attach_stdlib_bridge(service: 'LogService', logger_name: str='slaughtermudae') -> StdlibBridge`
  - Attach (idempotently) the stdlib->structured bridge; returns the handler.
- `def detach_stdlib_bridge(logger_name: str='slaughtermudae') -> int`
  - Remove every StdlibBridge from the tree (shutdown/teardown).
