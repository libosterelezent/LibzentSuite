# ERROR_HANDLING — the domain exception taxonomy and what handles what

> Source of truth: `core/errors.py` + every raise/catch site that uses it.
> Per-module reference: `read_only/modules/core.errors.md`.

Grouping rules (module docstring):

- **`FlowBlocked` family** — a plugin flow for ONE account hit a dead end;
  the account's flow aborts with a human reason while everything else runs on
  (isolation is the dispatcher's job).
- **`ServerRateLimit`** — Discord itself said WAIT (429 / Retry-After);
  raised by adapters, consumed by the scheduler which reports it to the
  limiter.
- **Config/validation** — `ValueError` subclasses with precise, human
  messages; both interfaces render them as plain `error: ...` lines / honest
  400 responses.

```
ValueError                                        RuntimeError
 ├─ ProfileError                                  ├─ FlowBlocked ──┬─ CacheInvalid ──┬─ TargetGone
 ├─ SettingsValidationError                      │                └─ AccessDenied, FetchRequired
 ├─ RatePolicyError ── UnsafePolicyError         ├─ ServerRateLimit
 └─ UsageError (interfaces.cli.commands)          ├─ AccountError, PluginError, SchedulerError,
                                                     DatabaseError, CredentialsError, SimulationError
```

---

## 1. ERROR MATRIX

| Error | Source module | Raised when | Expected handling | Retry? | Fatal? | Logged where |
|---|---|---|---|---|---|---|
| `FlowBlocked` | `core/errors.py`; reconstructed in `core.account_manager.services.TaskAckRegistry._on_failed` | a plugin flow for one account cannot continue (base class; also the family's catch type) | plugin catches it around its WHOLE flow, logs one clean abort line, returns — no re-raise, no partial output | no (scheduler fails the task fast) | no — one account's flow only | plugin abort line (LogService); scheduler logs the task failure; `TaskFailed` event → bootstrap event logging |
| `CacheInvalid` | `core/errors.py` | the cached state needed for this action is gone or stale | as `FlowBlocked` | no | no | same as FlowBlocked |
| `TargetGone` (`CacheInvalid` subclass) | `core.account_manager.cache.DiscordCache.validate_channel` / `resolve_dm_channel`; `adapters.simulator.client.SimClient` (vanished channel/message/custom_id); `adapters.discord.client.RealClient._map_send_error` (HTTP 404/NotFound) | the cached channel/target no longer exists | abort the flow; `send_hook` additionally invalidates the DM cache row on the `dm` path so it heals on the next resolution | no | no | scheduler failure line + audit-note path; cache logs |
| `AccessDenied` | `adapters.discord.client.RealClient._map_send_error` (HTTP 403/Forbidden) | Discord refused access (permissions revoked, banned from channel) | abort the flow (catch `FlowBlocked`) | no | no | same as FlowBlocked |
| `FetchRequired` | `core/errors.py` (family member; surfaces from cache policy) | target not in cache and fetching is disabled/unavailable | abort the flow | no | no | same as FlowBlocked |
| `ServerRateLimit` | `adapters.discord.client.RealClient._map_send_error` (HTTP 429: Retry-After float-parsed, fallback 1.0s; global flag from `X-RateLimit-Global`) | Discord's own rate limit said WAIT | scheduler catches, calls `limiter.report_server_limit(account, action, retry_after, global_)`, retries (ALWAYS ≥1) | **yes — always at least one retry** | no | limiter warning "server rate limit on ... cooling account ..."; `RateLimitTriggered` event on the retry's wait |
| `AccountError` | `core.account_manager.{manager,services}`, `adapters.discord.client` (startup failures) | invalid account configuration/state: duplicate name, unknown account, wrong status transition, client failed to start, bad submit args from services | caller surfaces as usage error (CLI `error:` line / dashboard 400); `send_hook`'s variant fails the task (no retry) | no | no | AccountManager/ServiceLogger lines |
| `PluginError` | `core.plugin_manager.{loader,manager}` | plugin loading/lifecycle problem: bad module, invalid name, duplicate name, no attached plugins, bad context | discovery skips/raises loudly at load; runtime load/unload surfaces to the operator | no | no — one plugin | plugins source lines |
| `SettingsValidationError` (`ValueError`) | `core.settings_manager` (Setting construction + set paths) | a setting value failed schema validation (path included in the message) | usage error rendering; the setting stays at its previous value | no | no | settings source lines |
| `RatePolicyError` (`ValueError`) | `core.rate_limit_manager.{policies,engine,lane}` | malformed rate policy (bad interval/jitter/level/owner, bad acquire args, bad prefixrate, bad floor argument) | usage error rendering; nothing changes | no | no | ratelimit source lines |
| `UnsafePolicyError` (`RatePolicyError` subclass) | `RateLimitManager._store` | a policy would break the minimum-pacing safety floor (interval < floor; hard minimum 1.5s/action) | rejected at SET time — the policy is never stored (memory or db) | n/a | no | ratelimit source lines |
| `SchedulerError` | `core.task_scheduler.scheduler` (submit validation, stopped/stopping state, off-loop submit); `core.account_manager.services` (ack seam: task failed/cancelled/timeout wrap); `send_hook` (unsupported action) | invalid task/scheduler operation | caller renders; submit rejections leave no partial state | no | no | scheduler source lines |
| `DatabaseError` | `database/{db,repositories/*}` — the ONLY error that escapes persistence (any `sqlite3.Error` re-raised; friendly duplicates; migration failures; repo-level validation) | persistence-layer failure or invalid database operation | callers treat persistence as best-effort where designed (policies, lane, history, audit all swallow + log); loud where correctness demands (account add) | no | no | repo warnings ("history write failed (continuing)", "audit record failed (continuing)"); `database` logger |
| `CredentialsError` | `core.credentials.Credentials` (missing/malformed required values); `adapters.discord.client` (missing token at connect) | a required credential is missing or malformed | boot/connect fails fast with the exact key named | no | **at boot: yes for hard requirements** | accounts/client lines |
| `SimulationError` | `adapters.simulator.runner` | scenario/runner problem (unknown scenario, broken script/stack) | the scenario report carries the failure; run() returns `passed: False` | no | no | sim report + failures list |
| `UsageError` (`ValueError`) | `interfaces.cli.commands` | unparseable CLI input, wrong argument counts, bad numbers, unknown subcommand (contextual usage text attached) | CLI renders bare (`error: ...`); never retried | no | no | CLI stderr |
| `KeyboardInterrupt` / `SystemExit` | anywhere | process-level signals | **always propagate** — the event bus explicitly re-raises them out of handler isolation; REPL ends the session cleanly | no | yes | — |

Retry/fatal semantics for the transport-level builtins inside the scheduler:
`ConnectionError` and `TimeoutError`/`asyncio.TimeoutError` are retryable
(within `max_retries`; a timeout's final status is `"timed_out"`); everything
not in the table's retry column fails the task immediately with
`"TypeName: message"`.

---

## 2. Retry semantics in the scheduler

`core.task_scheduler.scheduler.TaskScheduler._run_task` classifies every
sender exception (full table in [SCHEDULER.md](SCHEDULER.md) §4):

- **`ServerRateLimit`** → `self._limiter.report_server_limit(task.account,
  task.action, exc.retry_after, exc.global_)` (a reporting failure is logged
  and swallowed — the retry still happens), then retry with
  `retry_budget = max(task.max_retries, 1)` — **server-provided limits ALWAYS
  earn at least one retry** (the framework respects server rate-limit
  information; the reported limiter cooldown paces the retry after
  Retry-After seconds).
- **`ConnectionError` / `TimeoutError` / `asyncio.TimeoutError`** → retry
  within `task.max_retries` (`asyncio.TimeoutError` marks the final status
  `"timed_out"`).
- **`FlowBlocked` + everything else** → fail immediately with the reason (the
  flow is dead; retrying would just re-abort).
- Backoff: `min(retry_backoff * 2 ** (attempts - 1), max_backoff)` through the
  `_sleep` seam; attempts capped at `1 + max_retries` total tries.
- `asyncio.CancelledError` from the sender is a FAILURE unless the scheduler
  itself is stopping (`_outer_cancelled()` — worker cancellation vs
  sender-internal cancellation).
- Failure containment (ported from the audited scheduler): workers swallow
  every sender exception into a task failure — the scheduler NEVER crashes;
  history-repo and audit failures are likewise isolated (try/except + log).

---

## 3. `error_kind` — FlowBlocked reconstruction across the task-ack seam

Plugins await their sends through `AccountPluginServices.submit_*` +
`TaskAckRegistry.wait_for_task`, but the send itself runs in the scheduler's
lane worker — the only channel back is the `TaskFailed` bus event (a plain
string). The flow-abort contract must survive that seam: **a plugin waiting
on `send_message` receives `TargetGone`, not an opaque `SchedulerError`.**

The chain:

1. `_finish` derives `error_kind` from the failure string via
   `TaskScheduler._error_kind`: `re.match(r"^([A-Z][A-Za-z0-9_]+): ", error)`
   extracts the exception CLASS NAME from the `"ClassName: message"` shape
   every failure site produces (`""` when it doesn't match).
2. `TaskFailed(account, task_id, error, error_kind)` is emitted.
3. `TaskAckRegistry._on_failed` looks `error_kind` up in `_FLOW_CLASSES`
   (`FlowBlocked`, `CacheInvalid`, `TargetGone`, `AccessDenied`,
   `FetchRequired`) and re-raises the REAL class on the waiting future:
   `blocked(event.account, self._flow_reason(event.error))`.
4. `_flow_reason` unwraps the message: drops the leading `"ClassName: "`,
   then the `"[account] flow blocked: "` wrapper `FlowBlocked.__init__` adds —
   so the reconstructed exception's `reason` is the original, curated one.
5. Any other `error_kind` → `SchedulerError(f"task {id} failed: {error}")`;
   `TaskCancelled` → `SchedulerError(f"task {id} was cancelled")`.

That is why every scheduler failure site formats errors as
`f"{type(exc).__name__}: {exc}"` — the class name must round-trip.

---

## 4. User-visible rendering

### CLI — `interfaces.cli.repl.render_error`
One-line error text; keeps the type name for NON-domain exceptions so bugs
stay identifiable:

```python
if isinstance(exc, _DOMAIN_ERRORS) or not message:
    return f"error: {message}" if message else f"error: {name}"
return f"error: {name}: {message}"
```

`_DOMAIN_ERRORS = (UsageError, AccountError, PluginError, ProfileError,
SettingsValidationError, RatePolicyError, UnsafePolicyError, SchedulerError,
CredentialsError, ValueError, RuntimeError)` — the families that already
carry a human-readable message are rendered bare. Multi-line messages are
collapsed to ONE line (whitespace-joined). `FlowBlocked`/`ServerRateLimit`
inherit `RuntimeError`'s bare rendering through the family; the REPL loop
catches everything per command (a failing command never kills the session),
and Ctrl-C/EOF end it cleanly.

### Dashboard — `interfaces.dashboard.server` (error semantics ported from the audited original, 3 audit rounds clean)
- missing/invalid fields → **400** with an actionable message naming the field
  and the route (`require_field`/`require_str`/`require_number` — the old
  bare-`KeyError`-text leak is fixed);
- strict bools → `parse_json_bool`: `{"enabled": "yes"}` is a 400, never a
  silent truthy cast (the error-audit fix for "opposite action" bugs);
- `ValueError`/`RuntimeError` from the api (the whole domain family in
  `core/errors.py`) → honest **400** carrying the message;
- anything unexpected → **500** with the traceback logged;
- main-loop hop timeout (`ROUTE_TIMEOUT_S = 10.0` via
  `run_coroutine_threadsafe`) → **504** with a real message (fixed from an
  empty 500 error string in the error-audit round);
- body > 1MB (`MAX_BODY_BYTES`) → **413** after a bounded drain;
- auth: missing/invalid `X-Dashboard-Token`/`?token=` → **401** on both REST
  and the WebSocket handshake (the static index itself is served without
  auth; the SPA prompts for the token).

---

## 5. Shutdown failure containment

Shutdown paths are ordered so one failing stage cannot strand the others:

- **`core.bootstrap.Framework.shutdown`**: `remote_control.stop()` →
  `scheduler.stop(drain=True)` → `accounts.stop_all()` →
  `logs.attach_db(None)` → `detach_stdlib_bridge()` → `database.close()` —
  logging sinks detach BEFORE the database closes (a bridge feeding a dead
  sink silently fails forever). See [LOGGING.md](LOGGING.md) §5.
- **`TaskScheduler.stop`** never hangs (drain bounded by `timeout`), never
  leaves a task non-terminal (survivors are cancelled), reaps lanes with
  `gather(..., return_exceptions=True)`.
- **Event-bus handler isolation** keeps one subscriber's exception from
  blocking shutdown broadcasts; `KeyboardInterrupt`/`SystemExit` still
  propagate (see [EVENT_BUS.md](EVENT_BUS.md) §isolation).
- **`Repl.run`'s finally** wraps `api.shutdown()` in `try/except: pass` —
  "never let a shutdown failure eat the goodbye".
- **Never-log-inside-the-write-path / never-audit-inside-the-send-path**:
  `LogService` failure counters, `AuditRecorder.record` isolation, the
  scheduler's history/audit try/excepts, and the rate engine's
  logger-swallowing `_log` all enforce the same rule — observability and
  persistence must never break the pipeline they observe.
- **Simulation containment**: a broken stack or crashing scenario becomes a
  failed REPORT (`failures` list entries like `"stack phase failed:
  TypeName: msg"`), never an exception out of `SimulationRunner.run`; every
  teardown step (account stop, scheduler stop, db close, unsubscribes) is
  individually guarded and appends to `failures` on error.

### Addendum (2026-10-25): `contextlib.suppress(Exception)` is not `except (CancelledError, Exception)` at await points

Rewriting `try/except (asyncio.CancelledError, Exception): pass` around an
`await task` into `with contextlib.suppress(Exception):` silently WIDENS the
propagated exception set: since Python 3.8, `asyncio.CancelledError` derives
from `BaseException`, so `except Exception` does not catch it — but the
explicit two-element tuple does. Awaiting a task that has been `.cancel()`ed
re-raises `CancelledError` inside the awaiting coroutine, so the suppression
set must name it explicitly.

Observed consequence (2026-10-25): the ruff SIM105 baseline pass rewrote
`benchmark/sampler.py::Sampler.stop` this way; every benchmark scenario then
crashed at teardown with `CancelledError` propagating out of
`session.stop()` (8 test errors on Linux, where the subsystem is importable;
Windows never reached the code because `import resource` fails first).
Fixed by restoring the explicit tuple:
`contextlib.suppress(asyncio.CancelledError, Exception)`.

Rule of thumb for this codebase: when a `try/except` block wraps an `await`,
preserve the exception tuple verbatim through any suppression rewrite, and
check whether `CancelledError` was named on purpose (task teardown usually
names it deliberately).
