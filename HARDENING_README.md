# Hardening pass — Phase 1 of N

Scope of Phase 1: **database layer, logging/audit persistence, boot &
shutdown, credentials/config, event-bus + scheduler cancellation, adapter
task hygiene.** Phases 2+ (below) have NOT been audited yet.

## Phase 1 verification update (post-recovery)
* Recovered zip was validated on **real aiosqlite** (the original run used a
  stand-in): one genuine defect surfaced and was fixed — `TaskScheduler.stop()`
  exited its drain loop the moment `_tasks` emptied, which happens BEFORE the
  awaited history write lands, so the lane cancel cut the write mid-flight and
  silently dropped the final task row (`[1, 2]` vs `[1, 2, 3]`). `stop()` now
  drains in-flight finish tails too, and the history-write failure log no
  longer raises `NameError` (missing `as exc`).

# Hardening pass — Phase 2

Scope: **account manager lifecycle, scheduler/rate-limiter races, dashboard
HTTP+WS surface, CLI/remote-control/bootstrap, persist queue, simulator
fidelity.** Five parallel audits; every fix below has a regression test in
`tests/unit/test_hardening_phase2.py` where testable.

## Verification
* **704 tests pass** (689 after phase 1 + 15 new).
* Run: `python3 -m unittest discover -s tests -t .` (needs `aiosqlite` +
  `cryptography`, per `requirements.txt`).

## Bugs fixed

### Event-loop wedges / duplicate events (high)
| # | Problem | Fix |
|---|---------|-----|
| 1 | **Supervisor busy-spun with the whole loop frozen** when reconnect was disabled while an account was down (`_reconnect_loop` returned without awaiting; `_supervise` re-entered forever). | `_supervise` yields every iteration; the disabled branch settles the account FAILED and clears `lost`. |
| 2 | **Zombie live client on reconnect**: the old client was never torn down while the library auto-resumed it → two gateways, every plugin event fired twice, unbounded client leak; callbacks from replaced clients kept publishing. | `_connect_client` disconnects the previous client first; every wired callback is staleness-guarded (`rt.client is not client`). |
| 3 | **Ghost dashboard WS client**: registration timeout (busy main loop) left a permanently-registered dead client whose queue grew forever. | `_register` skips dead clients; the timeout path marks dead + schedules an explicit unregister. |

### Races / correctness (medium)
| # | Problem | Fix |
|---|---------|-----|
| 4 | `remove_account` check-then-act window: a concurrent `start` could leave a live untracked client; double-remove raised raw `KeyError`. | Registry pop FIRST (atomic), refusal restores it; also cancels the account's scheduler tasks and disconnects a leftover client. |
| 5 | `stop()` on one account could run twice concurrently (duplicate events, double hooks). | Per-account `stop_lock` (single-flight). |
| 6 | Scheduler `stop()` was not concurrency-safe: an estop racing a drain could kill a scheduler that `start()` had re-armed. | Single-flight stop latch; joiners await the in-flight teardown. |
| 7 | Send-ack timeout raised a failure while the task stayed queued — the message still landed minutes later (duplicate-send hazard). | `wait_for_task` best-effort cancels the task on timeout. |
| 8 | Guild-channel cache never healed on `TargetGone` (deleted channel failed forever until restart). | Message + reaction paths invalidate the channel on `TargetGone`. |
| 9 | `accounts remove` left kv-backed plugin state behind; a same-named new account inherited the old one's counters/markers. | `PluginStateService.cascade_account` + `KvRepository.delete_prefix`. |
| 10 | `ControlAPI.shutdown()` closed the DB out of order — REPL/dashboard exits drained log/audit queues into a dead DB (silent record loss). | The facade no longer closes the DB (Framework owns the ordered teardown); dashboard shutdown routes process exit through main.py's graceful signal path (opt-in `exit_signal=`). |
| 11 | Global remote-control `estop` reply could never be sent (scheduler already stopped) and the failure was invisible. | Undeliverable replies are logged at ERROR with the full text. |
| 12 | `services.settings` forked from the live view at activate (fresh dict rebind). | `_activate_one` updates the runtime dict in place. |
| 13 | `PersistQueue.stop()` drain was unbounded per batch and silently discarded leftovers. | Every batch is bounded by the remaining budget; undrained items are counted in `dropped`; per-item fallback no longer re-persists committed rows after a mid-batch cancel. |

### Dashboard / input surface
* Cross-site protection: loopback-bound servers refuse non-loopback `Host`
  (DNS rebinding) and browser `Origin` must match `Host` (CSRF / WS hijack).
* Constant-time token compare; empty-string tokens normalized; `?token=`
  redacted from DEBUG request logs; security headers on every response.
* Slowloris guard (`Handler.timeout`), negative `Content-Length` → 400,
  504-timeout catch fixed for Python 3.10, `require_number` rejects bools,
  `NaN` can no longer produce invalid-JSON responses.
* WS inbound strictness per RFC 6455: unexpected continuation/binary frames,
  fragmented control frames and invalid UTF-8 fail the connection;
  `refresh` flood debounced; `stop()` tears live WS sockets down.

### Simpler fixes
* Drop without a supervisor now marks the account FAILED (start() stays
  usable) instead of wedging RECONNECTING forever.
* `CredentialService.delete` clears plaintext memory caches BEFORE the DB
  call (a failed delete no longer leaves the token cached).
* Scheduler: `timeout=0` now means "no per-attempt budget" (was instant
  timeout after burning a pacing grant); cooperative-cancel checkpoint added
  to the non-retryable failure path (estop raced failures now report
  CANCELLED); lane `held_off_seconds` actually accumulates; `"lane"` added
  to the documented Grant/reason vocabulary.
* Bootstrap: a CancelledError inside one shutdown step no longer skips the
  rest (the DB is always closed; the cancellation re-raises at the end).
* Settings/cooldown boot loads are best-effort as documented (a locked read
  no longer fails the whole boot).
* Remote-control `accounts info` no longer crashes formatting a stopped
  account (`None` uptime/latency); CLI `accounts start ""` is a usage error
  instead of starting every account.
* Simulator fidelity: `add_reaction` raises `TargetGone` for vanished
  messages (parity with live), reactions rebuild frozen DTOs instead of
  mutating them, self-authored edit/delete events are filtered like live,
  `ScenarioFinished` is published after teardown (agrees with the report),
  dead scenario check branch removed.

## Not changed (deliberately)
* No DB schema change, no public API signature change.
* Internal exception text still reaches dashboard clients (loopback admin
  tool; no stack traces leak).

## Backlog for Phase 3+
1. Docs: ARCHITECTURE.md / docs/ for the account-manager lifecycle changes
   (staleness guards, single-flight stop), dashboard auth model, and the
   `/api/shutdown` exit-signal contract.
2. Re-audit `core/account_manager/manager.py` send-hook paths under the new
   cache-invalidation behavior (integration-level, with the simulator).
3. Load/soak: long-running simulation with reconnect storms to validate the
   zombie-client and supervisor fixes at scale.


## Verification
* Original suite: **658 tests pass** (baseline, before any change).
* Now: **689 tests pass** — the 658 originals untouched + 31 new in
  `tests/unit/test_hardening_phase1.py`.
* Run: `python3 -m unittest discover -s tests -t .` (needs `aiosqlite`,
  as in `requirements.txt`).
* Caveat: this sandbox had no network and no `aiosqlite` wheel, so the
  suite was run against a small stand-in I wrote (outside the project,
  not shipped). It wraps stdlib `sqlite3` on a worker thread. Please run the
  suite once with the real `aiosqlite` on your machine.

## Bugs fixed

### Data loss / security
| # | Problem | Fix |
|---|---------|-----|
| 1 | **Task history lost after every restart**: task ids restarted at 1 each boot, collided with old `task_history` rows, and the failed insert was swallowed (a 2nd boot archived 0 of 3 tasks — reproduced in a test). | `TaskHistoryRepository.max_id()` + `TaskScheduler.restore_sequence()`, called from `bootstrap`. The swallowed warning now includes the exception. |
| 2 | Removing an account left its **encrypted token** (`credentials`) and `cooldowns` behind. | `AccountsRepository.delete` cascades to both, same transaction. |
| 3 | DB file was briefly world-readable on first run (chmod after create). | Created `0600` before SQLite opens it. |
| 4 | Master key file written then chmod'ed (world-readable window; two processes could overwrite each other's key). | Created atomically `0600` with `O_EXCL`; a corrupt/blank key now raises `CredentialsError` instead of a raw `ValueError`. |

### Correctness
| # | Problem | Fix |
|---|---------|-----|
| 5 | Two unrelated `DatabaseError` classes; repository errors were re-wrapped ("database add account failed: account already exists"). | One class (`core.errors`), re-exported from `database.connection`/`database.db`. |
| 6 | Same for `CredentialsError` (the Discord client raised a class the account manager did not catch, so missing credentials weren't classified as MISSING_CREDENTIAL). | One class in `core.errors`. |
| 7 | Migrations were not atomic despite the docs. | Each migration + its `schema_migrations` row run in one `BEGIN IMMEDIATE…COMMIT`; failure leaves nothing half-applied and releases the connection. Warns when the DB schema is newer than the code. |
| 8 | A reader could see another task's **uncommitted** rows, and any task's commit committed everyone's work (single shared connection). | All access goes through a per-task re-entrant guard; a task can still read/nest writes inside its own transaction. |
| 9 | `close()` could cut an in-flight write in half. | Waits (bounded, 5s) for the transaction. |
| 10 | `cancelling() > uncancel()` in event bus + scheduler **consumed the cancellation it was checking** (breaks `asyncio.timeout`/TaskGroup) and raised on Python 3.10. | Read-only `core/_asyncutil.outer_cancel_pending()`. |
| 11 | Corrupt `metadata_json` crashed the whole cooldown listing. | Tolerated (warn + `{}`). |
| 12 | `CooldownService.set(inf/nan/"abc")` stored garbage. | Rejected with `ValueError`. |
| 13 | `.env`: a UTF-8 BOM hid the first key; `KEY=value # comment` kept the comment. | Both handled (`#` inside a value without a preceding space is preserved, e.g. tokens). |
| 14 | `config/` JSON: bad shapes gave obscure `AttributeError`/`ValueError`; BOM files failed. | Clear messages; BOM tolerated. |
| 15 | Simulator client used undefined `Any`; Discord client's `create_task` was unreferenced (GC-able) and `disconnect()` never cancelled it. | Import fixed; task kept, cancelled on disconnect and on failed connect. |

### Boot / shutdown
* `build()` failing partway used to leak the DB connection thread, drain
  workers and stdlib bridge (can hang process exit) → everything started so
  far is now released in reverse order.
* `Framework.shutdown()` is fault-tolerant + idempotent: each step runs even
  if an earlier one raised; the database is always closed.
* Log/audit persistence starts **before** boot logging (earlier records were
  never persisted).
* `main.py`: `--start-all` failure and dashboard-start failure now shut down
  cleanly; `--headless` handles **SIGTERM** (and SIGINT) gracefully.

### Performance / cleanup
* One shared queued-persistence engine, `core/_persist_queue.py`,
  replaces ~100 duplicated lines each in `LogService` and `AuditRecorder`.
  **Batched writes**: `add_many` = one transaction + one prune per batch
  (was one per record). Batch failure → retried per item so one bad row
  can't drop its neighbours. Order preserved on shutdown; an interrupted
  batch is written first.
* `as_dict()` for log/audit entries no longer uses `dataclasses.asdict`
  (measured ~3.4x faster); `tail()` stops scanning once `n` matches found.
* `AuditRecorder.record()` can no longer raise on a bad `wait_ms`.
* Cooldown restore: 1 query instead of 1 + N-accounts.
* `SettingsManager.account_settings()` no longer re-validates defaults on
  every call (hot sync path).
* `OWNER_USER_IDS` parsed once (was re-parsed on every inbound message).
* `ServiceLogger.exception()` now appends `@ file.py:LINE in func`.
* Generated `knowledgebase/` refreshed with the project's own
  `scripts/gen_knowledgebase.py` (new modules included; commit hash in the
  manifest left as it was).

## Not changed (deliberately)
* Public APIs, DB schema, file formats, setting names: unchanged. No
  migration added.
* `ARCHITECTURE.md`/`docs/` prose not yet updated for the new modules (see
  backlog).

## Backlog for Phase 2+ (not yet read/audited)
1. `core/account_manager/` (manager.py 1.3k lines, services, cache): lifecycle
   races, reconnect logic, send hook, cascade on delete (in-memory
   cooldown/credential caches).
2. `core/task_scheduler/scheduler.py` internals and
   `core/rate_limit_manager/` (engine 900 lines, lane, policies, warnings).
3. `interfaces/dashboard/` (server 1k lines + hand-rolled websocket layer):
   auth, input limits, frame handling, connection leaks.
4. `interfaces/cli/` (commands 1.1k lines, REPL).
5. `adapters/simulator/*`, `adapters/model.py`, `core/api.py`,
   `core/remote_control.py`, plugin manager/loader/base, profile manager.
6. `plugins/` (roll, soulcopy) and `scripts/`.
7. Docs: update ARCHITECTURE.md / docs for `_persist_queue`, `_asyncutil`,
   guard semantics; README test count (docs say 550, real is now 689).
8. Re-run the suite on real `aiosqlite` + your target Python versions.
