# LOGGING — the two-pipeline structured logging architecture

> Source of truth: `core/logging_service.py`; attach paths in
> `core/bootstrap.py`, `interfaces/dashboard/server.py`, and
> `adapters/simulator/runner.py`.
> Per-module reference: `read_only/modules/core.logging_service.md`.

Every component (core, account, plugin, scheduler, ratelimiter, adapter)
records STRUCTURED entries through `LogService` instead of scattering `print()`
or raw logger text. Each `LogEntry` carries:

```
timestamp, severity, source, account, plugin, task_id, event, message
```

Consumers: the stderr mirror (human-readable, one line per record), the
dashboard Logs page (live tail over WebSocket — no text parsing), and the
database `logs` table (bounded, best-effort).

## The two pipelines (and the wall between them)

```
OUTBOUND (framework-native)                          INBOUND (stdlib world)
┌────────────────────────────────┐                   ┌──────────────────────────────────┐
│ manager.info(source, msg, ...) │                   │ logging.getLogger("libzent.*")
│  (ServiceLogger / LogService   │                   │  (plugin contexts, loader,        │
│   convenience methods)         │                   │   event_bus, dashboard, 3rd-party)│
└───────────┬────────────────────┘                   └───────────────┬──────────────────┘
            ▼                                                        ▼
   LogService.record()  ◄──────────── StdlibBridge.emit() ────────────┘
   (severity normalized)              (source from logger name: "alpha:x"
            │                          or "ext:libname"; severity mapped,
            │                          CRITICAL/FATAL → ERROR)
            ├── ring buffer append (deque maxlen=capacity, under lock)
            ├── counters[severity] += 1
            │
            ├── OUTBOUND MIRROR — ONLY when NOT inside ingest_context():
            │     logging.getLogger(f"libzent.{source}").log(
            │         level, msg, extra={"_sm_from_service": True})
            │        └──► StdlibBridge skips records carrying the
            │             _sm_from_service marker (guard #1)
            │
            ├── db sink (best-effort): LogRepository.add(entry) — a failure
            │     increments _db_failures; NEVER logs inside the write path
            │
            └── subscribers (dashboard WS "log" frames): a raising callback
                  increments _subscriber_failures; never recursive
```

THE INVARIANT — **one source event → one canonical `LogEntry`**: each thing
that happens in the framework produces exactly one structured entry, no matter
which pipeline it arrived through. The mirror exists so humans on stderr see
framework-native lines; the bridge exists so stdlib-world lines reach the
structured store. The two structural guards below are what stop those flows
from feeding each other.

---

## 1. The FEEDBACK-LOOP postmortem (measured)

Historical bug, structurally fixed in commit `def72a4` ("eliminate the
mirror→bridge feedback loop", 10-agent investigation).

**The loop:**

```
LogService.record(msg)
   └─► mirror → logging.getLogger("libzent.x").log(...)   (no marker)
          └─► StdlibBridge.emit (attached to the same tree)
               └─► LogService.record(msg)                        (re-ingested)
                    └─► mirror → stdlib ...                      (repeat)
```

`record()` mirrored every entry to a stdlib child logger; `StdlibBridge`
(attached to the same tree) re-ingested those mirrors back into `record()`,
which mirrored them again. **The only bound was Python's recursion limit** —
the terminal `RecursionError` was swallowed by the bridge's `except: pass`.

**Measured amplification (from the commit, confirmed by measurement):**

| Metric | Value |
|---|---|
| echoes per source line | **~143** |
| boot lines | ~400 |
| per-echo cost | ~0.4ms (sqlite commit + logging overhead) |
| observed boot time | **~23 seconds** |
| ring inserts + DB rows | ~57k + ~57k |
| stderr | flooded ~143× per line |

### The two structural guards

1. **The `_sm_from_service` marker (defense-in-depth).** Every OUTBOUND mirror
   record carries `extra={"_sm_from_service": True}`; `StdlibBridge.emit`
   returns immediately for any record carrying the marker — "this record IS
   LogService's own stdlib mirror — the entry already exists; re-ingesting it
   would re-mirror it and recurse". This closes the recursion for EVERY path,
   even if a mirror somehow reaches a different bridge instance.
2. **`ingest_context()` mirror suppression (the primary fix).** While a
   `StdlibBridge` ingests a raw stdlib record ON THIS THREAD, `record()`
   suppresses the outbound mirror for that record: the record came FROM stdlib
   (stderr already printed it once via the original stdlib call), so mirroring
   it back would double-print and re-enter the tree. Thread-local
   (`threading.local`): other threads keep their mirrors. This also killed the
   residual double-print the investigation found — every raw stdlib line
   (loader, plugin contexts, event_bus, dashboard) previously printed to
   stderr twice.

Either guard alone breaks the cycle; together they make the invariant
structural rather than positional.

---

## 2. `LogService` — ring, sinks, subscribers, counters

```python
LogService(capacity=4000)
```

| Member | Behavior |
|---|---|
| `record(severity, source, message, *, account=None, plugin=None, task_id=None, event=None) -> dict` | the ONE write path: normalizes severity (unknown → `"INFO"`), builds a `LogEntry`, appends to the ring + bumps counters under `threading.Lock`, mirrors (unless suppressed), persists, notifies. **Never raises** — logging must not break the caller's flow. Returns the JSON-safe dict (`ts` rounded to 3). |
| `info/warning/error(source, message, **ctx)` | severity convenience wrappers |
| `tail(n=100, *, account=None, plugin=None, severity=None, source=None)` | most recent entries, OLDEST-FIRST, after filters (severity upper-cased) |
| `subscribe(callback) -> unsubscribe` | live-tail (dashboard WS `attach_logs`); callbacks run outside the lock, one-by-one |
| `attach_db(repository)` | attach/detach the `LogRepository` sink (`attach_db(None)` detaches) |
| `stats()` | `{"entries", "by_severity", "db_failures", "subscriber_failures"}` for the dashboard |

`LogEntry` is a frozen-shape dataclass (`ts, severity, source, message,
account, plugin, task_id, event`); `source` names the component
(`"core"`, `"account:alpha"`, `"scheduler"`, `"events"`, `"ratelimit"`, ...).
`SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR")` — there is no CRITICAL
level in the structured store.

### Failure counters and the never-log-inside-the-write-path rule

`_db_failures` and `_subscriber_failures` increment silently on every sink /
callback exception. **Nothing inside the write path may log** — a log call
inside `record()`'s failure handling is exactly what fed the original feedback
loop (a failing DB write would log the failure, which writes to the DB, which
fails...). Dead sinks must not be silent: the counters surface through
`stats()` so the dashboard shows them, observably, without recursion.

---

## 3. `ServiceLogger` — the convention shim

`ServiceLogger(service, source)` is a stdlib-logger-shaped adapter over
`LogService` (written by agent A7 for the simulation runner, promoted to the
spine at integration). Managers historically mixed two call conventions:

- stdlib style `info("fmt %s", arg)`, `exception(...)` — accepted, rendered via
  `_format` (`text % args`, falling back to `"text arg1 arg2"` so a
  LogService-style call through the shim never loses information);
- LogService style `info(source, message)` — accepted too; a `.record(...)`
  passthrough tags entries with the shim's OWN source.

Every method is exception-proof (logging must never break a manager). The
`.exception()` contract is honored: when `sys.exc_info()` is active, the
exception one-liner `[TypeName: message]` is appended. Bootstrap wraps each
manager's logger: `ServiceLogger(logs, "ratelimit")`, `"scheduler"`,
`"plugins"`, `"profiles"`, `"accounts"`, `"settings"`, `"remote-control"`.

---

## 4. Stdlib boundary functions

### `configure_stderr(level=logging.INFO)`
Attaches ONE human-readable stderr handler to the `libzent` logger
tree (idempotent via the module `_STDERR_CONFIGURED` flag): format
`"%(asctime)s %(levelname)-7s %(name)s: %(message)s"` (`%H:%M:%S`), sets the
tree level, and sets `propagate = False` — framework records must not leak to
the global root logger (whose `lastResort` handler would print them raw).

### `attach_stdlib_bridge(service, logger_name="libzent") -> StdlibBridge`
Attaches (idempotently — an existing bridge on the tree is returned) the
stdlib→structured bridge so plugin-context and third-party records under the
tree reach the dashboard Logs page WITHOUT changing plugin call sites. Beyond
attaching it:

- ensures the tree accepts INFO records even when `configure_stderr()` was
  never called (headless/sim boots) — otherwise raw plugin INFO logs are
  filtered at the logger level before reaching the bridge;
- **`propagate = False` quiet-mode hole fix**: builds with `quiet=True` never
  call `configure_stderr()`; without this, mirrors and raw WARNING+ records
  leaked to the global root and printed raw.

`StdlibBridge.emit(record)` (a `logging.Handler`): skips `_sm_from_service`
records (guard #1); derives `source` from the logger name —
`libzent.*` → the suffix with `.` → `:` (e.g. `account:alpha`),
bare tree root → `"core"`, foreign loggers → `ext:<name>`; maps severity via
`_SEVERITY_BY_LEVELNAME` where **CRITICAL/FATAL → ERROR** (the structured
store has no crash level, and INFO would hide it — the loop-fix commit also
corrected this mapping, which previously sent CRITICAL to INFO); unknown
level names fall back by `record.levelno` thresholds; then calls
`service.record(...)` **inside `ingest_context()`** (guard #2). The whole body
is `try/except: pass` — a logging bridge must never raise.

### `detach_stdlib_bridge(logger_name="libzent") -> int`
Removes every `StdlibBridge` from the tree (shutdown/teardown), returns how
many. After this, raw stdlib records under the tree print only (no structured
ingestion) — exactly the pre-attach behavior. Idempotent; safe to call from
`Framework.shutdown` BEFORE `database.close()`.

---

## 5. Shutdown ordering (logging first, database last)

`core.bootstrap.Framework.shutdown` — ORDER MATTERS:

```
remote_control.stop()
await scheduler.stop(drain=True)
await accounts.stop_all()
self.logs.attach_db(None)      # 1) stop persisting first
detach_stdlib_bridge()         # 2) then stop ingesting stdlib
self.database.close()          # 3) only now close the sink
```

If the bridge were removed after `database.close()` (or never), the bridge
would keep ingesting stdlib lines into a service whose sink is a closed
database — every write silently failing forever (each bumping `_db_failures`
with no one watching). The same discipline applies in
`adapters.simulator.runner.SimulationRunner.run`'s teardown.

---

## 6. Dashboard + simulator attach paths

### Dashboard (`interfaces/dashboard/server.py`)
- `DashboardServer.attach_logs(log_service)` — subscribes `_on_entry`, which
  broadcasts `{"type":"log", **entry}` frames to every WebSocket client. The
  push hops to the main loop via `call_soon_threadsafe` before touching
  clients (safe from any thread AND from the loop itself). Entries arrive as
  dicts or `LogEntry`-like objects (`entry_as_dict` normalizes both).
- `ControlAPI.logs_service()` exposes the live service (the subscription
  point); `logs_tail` reads `LogService.tail` for the REST Logs page.

### Simulator bridge attach/detach + the documented cross-leak
`adapters.simulator.runner.SimulationRunner.run` builds a COMPLETE ephemeral
stack per scenario, with its own in-memory `LogService` (`logger`) and
subscriptions (`logger.subscribe(_on_log)` for the report's timed log lines,
`audit.subscribe(_on_audit)` for interactions) attached BEFORE any traffic can
flow. For the stdlib boundary:

- **attach**: if the `libzent` tree has NO `StdlibBridge`
  (`bridge_attached_here`), it attaches one bound to the RUN's service —
  standalone `--sim` runs previously DROPPED plugin/loader INFO lines (tree
  effective level WARNING with no bridge).
- **documented cross-leak**: when the LIVE stack's bridge is already present
  (dashboard-sim), it is KEPT — swapping a process-global handler mid-run
  would misroute the MAIN stack's lines. The run then records an explicit INFO
  line: "shared stdlib bridge detected: raw stdlib lines from this run land in
  the main log service (documented cross-leak)". The leak is one-directional
  and bounded: raw stdlib lines from the ephemeral stack surface in the main
  log; the ephemeral report still captures its own structured lines via its
  subscriber.
- **detach**: in the run's cleanup, `detach_stdlib_bridge()` runs only when
  `bridge_attached_here` ("never leak a bridge to a dead service"), before
  `db.close()` and the unsubscribe calls in the `finally` block.

---

## 7. Event → log wiring (what the Logs page shows)

`core.bootstrap._wire_event_logging(bus, logs)` subscribes ONE handler to the
base `Event` type. Only whitelisted lifecycle events are recorded (as
structured INFO lines, `source="events"`, `event=<name>`, `account=` when
present, every other dataclass field rendered `key=value!r`):

`_LOGGED_EVENTS = ("AccountConnected", "AccountDisconnected", "AccountFailed",
"PluginLoaded", "PluginUnloaded", "PluginEnabledChanged", "TaskCompleted",
"TaskFailed", "TaskCancelled", "RateLimitTriggered", "ScenarioStarted",
"ScenarioFinished")`

`TaskQueued` / `TaskStarted` / message events are deliberately excluded (far
too chatty — see [EVENT_BUS.md](EVENT_BUS.md) §catalog).
