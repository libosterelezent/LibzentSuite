# SCHEDULER — the ONE place outbound work is dispatched

> Source of truth: `core/task_scheduler/{tasks,scheduler}.py`.
> Per-module reference: `read_only/modules/core.task_scheduler.*.md`.

`TaskScheduler` owns the entire lifecycle of every outbound action: plugins
never call Discord directly — they submit a `Task`, and the scheduler grants,
sends, retries, cancels, audits, and records it.

```
submit() ──validate──> per-account priority heap ──> TaskQueued event
                              |
              per-account worker LANE (concurrency = 1 per account;
              a task fully terminates before the next one for that
              account starts — never two in-flight requests/account)
                              |
                 _GrantGate.enter(priority, seq)          ← cross-account
                              |                              priority turnstile
                 limiter.acquire(action, account, plugin, priority)
                              |
                 checkpoint 1 (cancel while parked on grant?)
                              |
                 sender(task)  under asyncio.wait_for(timeout_s)
                   ├─ ok ──────────────> checkpoint 2 → _finish(COMPLETED)
                   ├─ TimeoutError ─────┐
                   ├─ ServerRateLimit ──┤ report_server_limit to limiter
                   ├─ ConnectionError ──┤ backoff sleep → retry loop
                   ├─ TimeoutError ─────┘ (attempts budget)
                   └─ FlowBlocked / other → _finish(FAILED) immediately
                              |
                 _finish(status) — exactly-once terminal transition:
                 memory rows + counters + task_history repo + audit row
                 (gated) + TaskCompleted/TaskFailed/TaskCancelled event
```

---

## 1. The Task model (`core.task_scheduler.tasks`)

```python
TaskStatus = Literal["queued", "running", "completed", "failed", "cancelled", "timed_out"]
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
```

Status flow: `queued -> running -> completed | failed | cancelled | timed_out`
(`Task.is_terminal` is True inside `TERMINAL_STATUSES`).

| Field | Type / default | Meaning |
|---|---|---|
| `id` | `int` | scheduler sequence id (starts at 1); preserved in the history repo |
| `name` | `str` | human label; defaults to `f"{plugin}:{action}"` at submit |
| `account` / `plugin` / `action` | `str` | who wants what done; `action` is opaque to the scheduler (not only `message`/`dm`) |
| `channel_id` | `Optional[int]` | message/reaction/button target channel |
| `user_id` | `Optional[int]` | dm target user |
| `content` | `Optional[str]` | message text, or the emoji (`reaction`) / `custom_id` (`button`) |
| `message_id` | `Optional[int]` | target message for reaction/button actions (emoji/custom_id ride in `content`; the scheduler treats both opaquely) |
| `priority` | `int` | `core.priority.Priority` value (HIGH=0 < NORMAL=1 < LOW=2) |
| `submitted_at` / `started_at` / `finished_at` | `float` | wall-clock timestamps |
| `attempts` | `int = 0` | **retries USED** — incremented only when a retry is scheduled, not on final failure. First-try success leaves 0; success on the second try leaves 1. The audit note contract `attempts={attempts + 1}` then renders the total number of sends; retry budget is `1 + max_retries` tries |
| `max_retries` | `int = 0` | retries allowed after the first failure |
| `timeout_s` | `float = 30.0` | per-attempt `asyncio.wait_for` budget for the sender |
| `status` | `TaskStatus` | current status |
| `error` | `Optional[str]` | human-readable failure reason (None unless failed/timed_out) |
| `result_summary` | `Optional[str]` | short (< 80 chars) success description |
| `grant_wait_ms` / `grant_jitter_ms` | `float = 0.0` | pacing wait + jitter the rate limiter applied for the (latest) grant |
| `cancelled` | `bool = False` | cooperative cancel flag, checked at the checkpoints |
| `sender_ran` | `bool = False` | True once the sender was actually invoked for the last attempt — discriminates CANCELLED tasks that touched the API from those cancelled while still queued/parked. **Audit integrity field** (see §5) |

`Task.as_dict()` is the JSON-safe snapshot (floats rounded) used for task
listings, the history repo row, and stats; it also derives `duration_s` from
`finished_at - started_at` (0.0 when unset) — but note the scheduler overrides
`duration_s` in `_finish` with the monotonic measurement.

---

## 2. Worker model — one serialized lane per account

Ported discipline (from the audited `core/send_engine.py`): a rate limiter
paces the *rate* of actions but does not prevent two Discord requests being
IN FLIGHT for one account at the same moment — concurrent API calls are
exactly what flags selfbot accounts. Each account therefore gets ONE worker
lane (`TaskScheduler._lane(account)`): a task is popped from that account's
priority heap — ordered by `(priority, task.id)`, so a HIGH task submitted
after a LOW one still runs first — granted, sent, and only then does the lane
pick the next one.

- Lanes are spawned lazily on the first `submit()` for an account
  (`_ensure_lane`) and live until `stop()`; strong references are kept in
  `_worker_refs` so no worker is ever GC'd mid-flight (the documented
  `create_task` hazard).
- An empty lane parks on its per-account `asyncio.Event` (`_lane_events`);
  `submit()` sets it.
- The lane loop re-checks popped tasks: a task already terminal/cancelled
  while queued is skipped (already finished by `cancel()`).
- Lane containment: sender exceptions never propagate (each `_run_task` call
  is wrapped — `asyncio.CancelledError` re-raises for `stop()`, everything
  else is logged as "contained"); the lane never dies except by cancellation.

### The `_GrantGate` — cross-account priority turnstile

Because each account has its own lane, several lanes can reach
`limiter.acquire` at once. The shared `core.task_scheduler.scheduler._GrantGate`
orders ENTRY into `acquire` globally by `(priority, seq)`:

- `enter(priority, seq)` parks the caller on a future pushed onto a heap and
  kicks a release cycle;
- a release cycle waits ONE event-loop hop (`await asyncio.sleep(0)`) first —
  all lanes that became runnable in the same tick register before the best
  candidate is chosen, which is what lets a late-submitted HIGH task overtake
  an already-registered LOW one;
- exactly ONE waiter is released per cycle; each released waiter re-kicks the
  gate in its `finally`, advancing the chain;
- the gate therefore never serializes the acquires themselves — only their
  STARTING order — keeping per-account pacing independent (two accounts still
  send in parallel);
- cancelled waiters are skipped (their futures are done) and their `finally`
  still kicks, so a stopping scheduler can never wedge the chain;
- release-cycle tasks are strong-referenced in `_cycles` (same GC hazard).

---

## 3. DEEP DIVE — `TaskScheduler.submit`

### Purpose
Validate, queue, and publish ONE outbound task; returns the live `Task`.

### Why
This is the only public entry point for outbound work (plugins go through
`core.plugin_base` services → `AccountPluginServices.submit_*` → here). Doing
ALL validation here means malformed work can never enter the pipeline: bad
tasks are rejected loudly at the boundary with precise, human messages
(`SchedulerError`), never discovered mid-send.

### Inputs

| Parameter | Type | Default | Validation |
|---|---|---|---|
| `account` | `str` | required | non-empty string (stripped before storage) |
| `plugin` | `str` | required | non-empty string (stripped) |
| `action` | `str` | required | non-empty string (stripped); opaque to the scheduler |
| `channel_id` | `Optional[int]` | `None` | `None` or a true int (booleans rejected) |
| `user_id` | `Optional[int]` | `None` | same |
| `message_id` | `Optional[int]` | `None` | same (reaction/button target) |
| `content` | `Optional[str]` | `None` | `None` or a string |
| `priority` | `Priority` | `Priority.NORMAL` | `Priority.coerce` (int / name string / enum; `None`→NORMAL; booleans and unknown names rejected) |
| `name` | `Optional[str]` | `None` | `None` or string; falls back to `f"{plugin}:{action}"` (stripped) |
| `timeout` | `Optional[float]` | `None` | `None` → constructor `default_timeout` (30s); else finite number >= 0 |
| `retries` | `int` | `0` | true int >= 0 (booleans rejected) |

### Expected Result
The new `Task` (status `queued`, id from the scheduler's `itertools.count(1)`)
— registered in `self._tasks`, pushed onto `self._queues[account]` as
`(priority, id, task)`, its lane ensured, logged, and announced with a
`core.event_bus.TaskQueued(account, task_id, plugin, action)` event.

### Side Effects
1. State check: rejects with `SchedulerError("scheduler is stopped; new tasks
   are rejected")` when stopped or stopping.
2. Argument validation (the table above) — each failure raises
   `SchedulerError` naming the field and the received value.
3. `Task(...)` construction; `self._tasks[task.id] = task`;
   `heapq.heappush(self._queues.setdefault(account, []), ...)`.
4. `_ensure_lane(account)`: reuses a live lane (setting its wake event) or
   spawns `loop.create_task(self._lane(account),
   name=f"scheduler-lane-{account}")`, strong-referenced.
   Outside a running loop this raises `SchedulerError("submit() must be called
   from a running event loop ...")`.
5. `_log("info", "scheduler", f"queued task {id} ({name}) account=...
   priority=... retries=...")`.
6. `_emit(TaskQueued(...))` — `publish_nowait`, fire-and-forget; a bus
   failure is logged ("event publish failed") and never breaks submission.

### Errors
`core.errors.SchedulerError` for every rejected input (message names the
field) and for the stopped/stopping state. No partial state survives a
rejected submit — the task is only registered after all checks pass.

### Concurrency
Must run inside the event loop (lanes are asyncio tasks). Safe to call while
other tasks run: the heap push and lane ensure are synchronous; the lane wakes
via its event. Multiple concurrent submits order by `(priority, id)` in the
account heap.

### Callers
- `core.account_manager.services.AccountPluginServices` (`send_message`,
  `send_dm`, reaction/button submits — the plugin-facing surface);
- the self-command layer (`core/plugin_manager/commands.py` — framework
  verb replies flow through the paced pipeline);
- tests (`tests/unit/test_task_scheduler.py`) and simulator scenarios.

### Callees
`Priority.coerce`, `Task`, `heapq.heappush`, `_ensure_lane`, `_log`,
`_emit` → `EventBus.publish_nowait`.

### Config
Constructor knobs that shape submitted tasks: `default_timeout` (used when
`timeout=None`), plus `retry_backoff`/`max_backoff` (used later, at retry
time). All three are validated at construction (finite numbers >= 0).

### Related
[RATE_LIMITING.md](RATE_LIMITING.md) (the acquire the lane performs),
[EVENT_BUS.md](EVENT_BUS.md) (`TaskQueued` et al.),
[DATABASE.md](DATABASE.md) (`TaskHistoryRepository`).

---

## 4. Execution loop and retry classification (`_run_task`)

`TaskScheduler._run_task(task)` drives one task from grant to terminal status
and never raises. Attempt loop:

1. **checkpoint 0** — cancelled (or no longer queued/running) before even
   asking for a grant → `_finish(CANCELLED)`. A retry iteration legitimately
   re-enters here while RUNNING.
2. `await self._gate.enter(priority, task.id)` → `await
   self._limiter.acquire(task.action, task.account, task.plugin,
   task.priority)`.
   - `CancelledError`: if `_outer_cancelled()` (the scheduler is stopping or
     the worker task has a pending outer cancellation) → re-raise (the outer
     handler finishes the task as cancelled); else → `_finish(FAILED, "rate
     limiter acquire was cancelled")`.
   - any other exception → `_finish(FAILED, "rate limiter error:
     TypeName: ...")`.
3. Grant timings recorded: `task.grant_wait_ms = grant.wait_s * 1000`,
   `task.grant_jitter_ms = grant.jitter_s * 1000`.
4. **checkpoint 1** — cancelled while waiting for the grant →
   `_finish(CANCELLED)`.
5. First attempt transition: `queued -> running`, `started_at = time.time()`,
   monotonic anchor set, `TaskStarted(account, task_id, action)` emitted.
6. `task.sender_ran = True` — stamped immediately before the API call goes
   out — then `await asyncio.wait_for(self._sender(task), task.timeout_s)`.
   Exception classification:

| Exception | Handling | Retry? | Terminal status / note |
|---|---|---|---|
| `asyncio.CancelledError` | `_outer_cancelled()`? → re-raise (stop sweep) : `_finish(FAILED, "sender was cancelled before completing")` | no | CANCELLED via outer path, else FAILED |
| `asyncio.TimeoutError` | `timed_out = True` | **yes** | final status `"timed_out"` with `"timed out after Xs (N attempt(s))"` |
| `core.errors.ServerRateLimit` | `self._limiter.report_server_limit(task.account, task.action, exc.retry_after, exc.global_)` (failure logged, swallowed) then `retryable = exc` | **yes — ALWAYS at least one retry** (`retry_budget = max(max_retries, 1)`): the framework respects server rate-limit information; the reported limiter cooldown paces the retry after Retry-After seconds | else FAILED with `"ServerRateLimit: ..."` |
| `ConnectionError`, `TimeoutError` | `retryable = exc` | yes (within `max_retries`) | else FAILED |
| `core.errors.FlowBlocked` | fail fast | no | `_finish(FAILED, "TypeName: [acct] flow blocked: ...")` — the error_kind survives for the ack seam |
| any other `Exception` | fail fast | no | `_finish(FAILED, "TypeName: message")` |

7. Success path → **checkpoint 2**: cancelled while the sender ran → discard
   the result, `_finish(CANCELLED)`; else `_finish(COMPLETED, summary)` where
   the summary is `_summarize(result, task)` (result type name + `id=` +
   `ch=`, truncated to 79 chars).
8. Retryable failure: cancelled meanwhile → `_finish(CANCELLED)`. Budget:
   `retry_budget = task.max_retries`, raised to `max(task.max_retries, 1)`
   for `ServerRateLimit`. If `task.attempts < retry_budget`:
   `task.attempts += 1` (retries used — drives the audit note and backoff),
   `delay = min(retry_backoff * 2 ** (attempts - 1), max_backoff)` —
   exponential backoff through the `_sleep` seam (patched to instant in unit
   tests) — then `continue` the attempt loop. A `CancelledError` during the
   backoff sleep follows the same outer-cancelled discrimination.
9. Retries exhausted → `timed_out` ? `_finish(TIMED_OUT, ...)` :
   `_finish(FAILED, "TypeName: message")`.

`_outer_cancelled()` distinguishes worker cancellation (scheduler stopping, or
the current asyncio task has `cancelling() > uncancel()`, Python 3.11+) from a
sender that raises `CancelledError` on its own — the latter is a FAILURE, the
former propagates so `stop()` sweeps finish as CANCELLED. The outermost
`except asyncio.CancelledError` of `_run_task` guarantees a terminal status
even if the sweep has not reached the task yet, then re-raises.

The sender itself is `core.account_manager.AccountManager.send_hook` (late
bound in `core.bootstrap.build` through a holder box to break the
accounts↔scheduler construction cycle): one task → one validated, audited
Discord send supporting `message`, `dm`, `reaction`, `button` (anything else
raises `SchedulerError("unsupported action ...")`).

---

## 5. DEEP DIVE — `TaskScheduler._finish`

### Purpose
Apply exactly ONE terminal transition to a task: fields, memory structures,
history repository row, audit record, and terminal event.

### Why
Every exit path (success, every failure kind, every cancel checkpoint, the
stop sweep, the outer CancelledError handler) funnels through here, which is
what makes terminal semantics *exactly-once by construction* and keeps the
audit trail truthful — the pacing/simulation layers read audit timestamps as
REAL send times, so phantom rows corrupt their math.

### Inputs

| Parameter | Type | Meaning |
|---|---|---|
| `task` | `Task` | the task to finish (live or already popped from its heap) |
| `status` | `str` | one of `STATUS_COMPLETED` / `STATUS_FAILED` / `STATUS_CANCELLED` / `STATUS_TIMED_OUT` |
| `error` | `Optional[str] = None` | failure reason; stored verbatim on the task and the row |
| `summary` | `Optional[str] = None` | success summary (COMPLETED only in practice) |
| `started_mono` | `Optional[float] = None` | monotonic anchor captured at the queued→running transition (0.0 duration when None, e.g. cancelled while queued) |

### Expected Result
The task holds the terminal status; `task.finished_at` set; the row is in the
in-memory finished tail (`_finished_rows` + `_finished_tasks`, both bounded
deques of `_FINISHED_KEEP = 500`); the per-status counter advanced; the
history row persisted (when a repo is attached); the audit row recorded (when
the audit gate passes); the matching terminal event emitted. Returns `None`.

### Side Effects (in order)
1. **Exactly-once guard**: `if task.status in TERMINAL_STATUSES: return` — a
   second call is a no-op.
2. `task.status`, `task.error`, `task.result_summary`, `task.finished_at =
   time.time()`; `duration = time.monotonic() - started_mono` (monotonic —
   immune to wall-clock jumps).
3. `row = task.as_dict()`; `row["duration_s"] = round(duration, 3)` (overrides
   the wall-clock-derived value).
4. Memory: `self._tasks.pop(task.id, None)` (the exactly-once registry
   removal), `_finished_rows.append(row)`, `_finished_tasks.append(task)`,
   `self._counts[status] += 1`.
5. History repo (best-effort): `self._history_repo.add(row)` inside
   try/except — a failure logs `"task {id} history write failed (continuing)"`
   and never breaks the pipeline.
6. **Audit gate** (see below): when `self._audit` is attached AND the task is
   auditable AND `task.action in _AUDITED_ACTIONS`:
   `audit.record(account, direction="out", action, channel_id, content,
   wait_ms=grant_wait_ms, jitter_ms=grant_jitter_ms, note=f"plugin={plugin}
   task={id}[ attempts={attempts + 1}]")` — wrapped in try/except ("audit
   record failed (continuing)").
   `_AUDITED_ACTIONS = ("message", "dm", "reaction", "button")`.
7. Terminal event via `_emit` (publish_nowait):
   - COMPLETED → `TaskCompleted(account, task_id, duration_s, summary or "")`
   - FAILED / TIMED_OUT → `TaskFailed(account, task_id, error or status,
     error_kind=_error_kind(error or ""))`
   - CANCELLED → `TaskCancelled(account, task_id)`

### Errors
Never raises for recording failures (history/audit exceptions swallowed +
logged). Cannot "fail" a terminal transition itself — the only early exit is
the exactly-once no-op.

### Concurrency
Runs on the event loop inside lane workers, `cancel()`, and the `stop()`
sweep. The TERMINAL_STATUSES guard + the `_tasks` registry removal make
double-finish impossible even when the stop sweep races a worker's own
transition (whoever gets there first wins; the other call no-ops).

### Callers
`_run_task` (every terminal path), `cancel` (queued cancels),
`cancel_matching`, `stop` (the sweep: `for task in list(self._tasks.values()):
self._finish(task, STATUS_CANCELLED)`).

### Callees
`Task.as_dict`, `TaskHistoryRepository.add`, `AuditRecorder.record`,
`_error_kind`, `_emit` → `EventBus.publish_nowait`, `_log`.

### Config
`_AUDITED_ACTIONS` (module constant) defines which actions are mirrored into
the audit trail; the audit recorder and history repo are injected
(`core.bootstrap.build` passes `audit=AuditRecorder(database.audit)` and
`history_repo=database.tasks`).

### Related
`core.audit.AuditRecorder`, [DATABASE.md](DATABASE.md),
[ERROR_HANDLING.md](ERROR_HANDLING.md) (error_kind reconstruction),
[LOGGING.md](LOGGING.md) (never-log-inside-write-path discipline).

### The audit-integrity gate (phantom-row bug, fixed in commit `1e2a050`)

**The bug:** `_finish` originally wrote an outbound audit row for EVERY
terminal of `message`/`dm` tasks — so a task cancelled while still queued or
parked on its grant produced a phantom `'out'` row for a message that never
touched Discord. Audit timestamps are consumed as real send times (pacing
checks, sim inspection) — phantom rows corrupt that math.

**The fix (two parts):**
1. `Task.sender_ran` is stamped `True` immediately before the sender
   invocation (step 6 of the attempt loop) — the precise "the API call is
   going out NOW" marker.
2. The gate in `_finish`:

```python
auditable = status != STATUS_CANCELLED or task.sender_ran
if self._audit is not None and auditable and task.action in _AUDITED_ACTIONS:
```

COMPLETED/FAILED/TIMED_OUT attempts all invoked the sender (by construction —
those statuses only occur after `sender_ran = True`); a CANCELLED task is
audited only when it was cancelled mid-send (checkpoint 2, after the sender
ran). A cancel while still queued or parked on the grant NEVER sent and must
not appear in the trail.

### `error_kind` derivation (`_error_kind`)

`re.match(r"^([A-Z][A-Za-z0-9_]+): ", error)` extracts the exception CLASS
NAME from the `"ClassName: message"` shape every failure site produces
(e.g. `"TargetGone: [alpha] flow blocked: channel 500 vanished"` →
`"TargetGone"`). `""` when the shape does not match. The services layer uses
it to faithfully re-raise `FlowBlocked` subclasses across the task-ack seam
(see [ERROR_HANDLING.md](ERROR_HANDLING.md) §ack-seam).

---

## 6. Cancel, stop, and the re-armable resume path

### `cancel(task_id) -> bool`
- `queued` → `_finish(CANCELLED)` immediately (no sender call) + TaskCancelled;
- `running` → sets the cooperative `task.cancelled` flag; the worker aborts at
  its next checkpoint (0/1/2 or the backoff sleep) and finishes CANCELLED;
- unknown / already terminal → `False`.

### `cancel_matching(*, account=None, plugin=None, action=None) -> int`
Cancels every live task matching ALL given filters (no filters = every live
task); counts immediate (queued) + cooperative (running) cancels. Used by the
global emergency stop and the account-scoped estop.

### `stop(*, drain=True, timeout=10.0)` (idempotent)
- `drain=True`: waits (bounded by `timeout`, polling every 10ms) for
  queued+running tasks to finish normally, then tears the lanes down;
- `drain=False`: cancels everything immediately — every still-live task gets a
  terminal `cancelled` status + TaskCancelled event, every lane task is
  cancelled;
- tasks still alive when a drain times out are cancelled as well — `stop()`
  never hangs and never leaves a task non-terminal;
- sets `_stopped`/`_stopping` (rejections begin immediately).

### `start()` — RE-ARMS after `stop()` (the resume path)
Marked ready; idempotent; raises `SchedulerError("scheduler is stopping; wait
for stop() to finish")` only during an in-flight stop. Lanes are lazy, so
there is no central loop to launch. Re-arming after `stop()` is the resume
path for the global emergency stop: `stop()` already drove every task to a
terminal state and reaped every lane, so a fresh arm starts from clean state
(finished-task history preserved; attempts, counters, audit state are
per-task — nothing stale carries over).

History (commit `1e2a050`, BUG 2): `emergency_stop_all` reports "restart
manually" but no restart path existed — `start()` refused to re-arm after
`stop()`, leaving the fleet permanently unable to send until process restart.
The fix chain: `core.api.ControlAPI.system_resume` re-arms + returns status,
exposed as `estop resume` (CLI) and `POST /api/resume` (dashboard) — both
surfaces, one method. Accounts stay stopped on purpose: restart the ones you
want with `accounts_start`.

---

## 7. Queries

- **`get(task_id)`** — live Task first, then the recently-finished tail
  (`_finished_tasks`, newest-last scan); None when unknown.
- **`tasks(*, status=None, account=None)`** — live queued/running `as_dict()`
  rows (id order) + the finished tail, optionally filtered.
- **`history(limit=50, account=None)`** — finished tasks, newest LAST:
  repo-backed (`TaskHistoryRepository.recent`) when attached, else the
  in-memory tail; a repo failure logs a warning and falls back to memory.
- **`stats()`** — the dashboard Overview shape:

```python
{"queued": int, "running": int, "completed": int, "failed": int,
 "cancelled": int, "timed_out": int, "avg_duration_s": float,
 "workers": int}   # workers = lanes not done
```

Counters come from the in-memory `_counts` tallies; `avg_duration_s` averages
`duration_s` over COMPLETED rows in the finished tail (rounded, 0.0 when
none).

---

## DB worker telemetry + pressure observability

> Source of truth: `database/telemetry.py`, `database/connection.py`,
> `core/task_scheduler/scheduler.py`. Verified at commit `487dc53`.

**DbTelemetry** — boundary DB worker timing (default OFF, zero overhead;
benchmark or `LZ_DB_TELEMETRY=1` enable it): TOTAL_DB_LATENCY,
COMMIT_TIME, TRANSACTION_TIME histograms from the connection-layer seams
+ worker-thread CPU via `/proc/self/task/<tid>/stat`. Honest about the
seam's limits: per-statement in-transaction timings and the queue-wait
vs sqlite-execution split are NOT visible (documented, not faked).

**Pressure signal** — see [RATE_LIMITING.md](RATE_LIMITING.md) § pressure.
`stats()` stays a pure observer (calling it never mutates tasks).
