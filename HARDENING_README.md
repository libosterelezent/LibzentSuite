# Hardening pass — Phase 1 of N

Scope of Phase 1: **database layer, logging/audit persistence, boot &
shutdown, credentials/config, event-bus + scheduler cancellation, adapter
task hygiene.** Phase 2 (below) is complete; see 'Post-rebase fixes' for
the upstream-sync addendum and 'Audit rounds 1–2' at the bottom for the
two post-Phase-3 sweeps.

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

## Post-rebase fixes (upstream sync with master `4a67b08`)
Upstream moved to DB-only credentials, HTTP/1.1 keep-alive, first-boot-only
config policy defaults and its own knowledgebase gitignore. Rebasing kept all
phase-1/2 fixes intact (704 tests) and fixed one bug the keep-alive commit
introduced:

* **HTTP/1.1 keep-alive + early rejects corrupted connections.** A 401/403/
  400/413 answered BEFORE the POST body was read left the body in the
  socket; the reused keep-alive connection then parsed it as the next
  request line. Every pre-body reject now sends ``Connection: close`` and
  tears the connection down; the websocket upgrade path sets
  ``close_connection`` before hijacking the socket so the stdlib loop never
  reads from the detached file object. Regression:
  ``test_keepalive_survives_unread_body_reject`` (705 total).

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
* Run: `python3 -m unittest discover -s tests -t .` (needs `aiosqlite` and `cryptography`, per `requirements.txt`).
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
   guard semantics; README test count (docs say 550, now 705).
8. Re-run the suite on real `aiosqlite` + your target Python versions.


# Hardening pass — Phase 3 (full-system audit, 17-audit sweep)

17 parallel audits (event bus, scheduler, limiter, account manager, dashboard
+ SPA, DB layer, credentials/settings, bootstrap/lifecycle, CLI/remote,
live adapter, simulator, plugins, persistence, performance, tests, docs,
cross-cutting invariants) → ~90 findings fixed. **708 tests pass**
(705 + 3 new; several strengthened).

## Critical / high
* **Estop still sent messages**: the stop-sweep awaited repo writes while a
  lane popped the NEXT queued task and ran it — a `_sweeping` barrier now
  gates lane pops + run checkpoints 0/1 (regression: sends cannot start
  after estop begins).
* **Persisted lane config never loaded** (kv get returns a decoded dict;
  `json.loads(dict)` TypeError was swallowed) — every reboot silently
  disabled the sequential send lane. Fixed + real-kv round-trip regression.
* **Cleared config-default policies resurrected on restart**: clear now
  writes a kv tombstone consulted by first-boot seeding; explicit set
  removes it.
* **Safety floor was never persisted** — a raised floor silently reverted
  to 1.5s on restart. Now stored under `ratelimit:floor`.
* **start/stop races**: stop-during-connect left a CONNECTED zombie client
  invisible to every later stop; the start exception path misclassified the
  race as INVALID_CREDENTIAL and published AccountFailed after
  AccountDisconnected; the reconnect loop could resurrect RUNNING after a
  stop. All windows closed (client teardown on abort, stop_requested
  checkpoints, hardened stop() early-return).
* **REPL blocked the whole event loop at the prompt** (dashboard 504s, WS
  stalls, gateway heartbeats starved): input now runs on a daemon reader
  thread feeding an asyncio queue. SIGTERM/SIGINT also end REPL sessions
  gracefully (teardown always runs).
* **`send_typing` called a nonexistent API** (typing indicators never fired
  live); connect now has a 90s READY timeout (black-holed gateways used to
  hang boot forever); 5xx maps to retryable ConnectionError; stop-during-
  connect is no longer misread as a bad token.
* **remove_account cascade completed**: plugin hooks now fire for FAILED
  accounts, api_cache rows dropped, SettingsManager memory layers forgotten,
  the parked scheduler lane torn down (new `forget_account`).
* **A second Ctrl-C/dashboard-exit during teardown skipped database.close**:
  the shutdown and build-cleanup loops now tolerate KeyboardInterrupt per
  step (re-raised after the DB closes).
* **Limiter waiter CPU burn** (67–98% of a core at fleet scale, starved the
  configured send rate by 32%): the acquire loop services only its own
  pacing key, cancelled heads wake successors, non-head backstop poll
  0.25s→2s. Measured 4–37× CPU reduction at the same grant rate.

## Medium / low (selected)
* kv `case_sensitive_like=ON` (prefix cascades no longer cross case-variant
  account names); migration 4 (per-account trail indexes, dead ts indexes
  dropped); task_history capped at 10k rows.
* stop_all/start_all bounded-parallel (estop latency, fleet boot minutes→
  seconds); scheduler stop single-flight hardened (owner-cancel no longer
  fakes completion; joiner timeout raises instead of lying).
* Remote control: commands ALWAYS execute (estop was swallowed inside the
  reply cooldown), reply-only throttle, internals never leak to the channel,
  `surface="remote"` audit labels; OWNER_USER_IDS validated at boot.
* Dashboard: chunked Transfer-Encoding rejected (keep-alive stream
  poisoning), NaN-safe WS serialization, reserved-opcode/fragmented-close
  protocol failures, `_register` post-stop guard, DatabaseError→500.
* SPA: 7 mis-attributed error sections fixed, dropdowns no longer rebuilt
  every second, account cards render-gated on structural change, dead ENV
  chip removed, forget-token reloads.
* Soulcopy: in-flight re-trigger guard (overlapping flows cross-collected
  each other's pages), UTF-16 chunk budget (emoji lists no longer produce
  API-rejected oversized chunks), broad clean-abort handling; roll: same
  abort contract + fixed dead confirm_reply.
* Plugins teach the cache from inbound traffic (post-READY channels no
  longer fail validation forever); DM re-ingest heals stale deleted flags.
* PersistQueue: outer cancellation propagates (was swallowed), failure
  burst notification resets on batch success, counted drops drain the
  queue, single-flight stop; LogService caps 16k messages and never raises.
* Engine: `POLL_INTERVAL` semantics, lane `held_off` accuracy, jitter
  capped at effective interval, `_account_cooldowns`/`_last_fire` pruning,
  invalid config-lane values fail loud, below-floor config defaults clamp
  instead of bricking boot.
* Cooldown lazy-clear is expiry-scoped (a second process's extension can no
  longer be deleted); `Setting.validate` rejects bools; `import-env`
  re-reads .env from disk; reserved account name `all` rejected;
  event-bus tail drain step + plugin-manager teardown step in shutdown.
* Simulator: synthetic owner/mudae ids overlaid on fleet credentials
  (dashboard-run scenarios were dead against real ids), bridge detach on
  every exit path, mid-assembly DB leaks closed, edit events push frozen
  DTOs, 429/consumer guard order matches live, reaction rebuild keeps id/me.
* Docs: credentials story (DB-encrypted, not .env), dependency list,
  705→708 counts, SM_MASTER_KEY documented, first-boot policy semantics,
  dashboard resume note — see README / docs/ diffs.

# Audit rounds 1–2 (post-Phase-3 sweeps)

**Round 1** (commit `cb082b0`, "10-agent sweep + coordinator fixes",
738 → 784 tests):

* Critical: account start/stop race left plugins active on stopped
  accounts; bootstrap policy-clamp crashed on an unimported name +
  property-as-call; deleted-reply sentinel + unbounded reply-chain
  recursion in the rich model.
* Major: scheduler `stop()` latch stranding (30s hangs); unvalidated
  persisted rate floor; LIKE-wildcard policy cascade deleting OTHER
  accounts' rows; dashboard broadcast/handler serialization crashes;
  simulator one-shot 429 consumed by TargetGone, subscriber isolation,
  scenario timeout, assembly teardown; REPL Ctrl-C hang; roll live-state
  flip; soulcopy zombie collector; live-client post-disconnect mapping.
* Coordinator: lane-prefixrate + str-bool parsing in bootstrap, post-stop
  persist-queue drops counted, credential `resolve()` cross-process
  refresh, 8 dead symbols removed,
  `asyncio.iscoroutine` → `inspect.iscoroutine`, INTEGRATION.md contract
  fixes.

**Round 2** (commit `c32f9c2`, specialist + coordinator pass, 784 → **1094+
tests**):

* Dashboard/WS hardening: stats serialization moved inside the failure
  guard (a hostile snapshot no longer kills the broadcast loop), NaN-safe
  WS serialization, stalled-client write handling, header-flush and lying
  `Content-Length` edge cases; new SPA regression suite
  (`tests/unit/test_dashboard_spa.py`).
* `PluginServices` protocol completed: framework-managed cooldowns
  (`cooldown_set/remaining/active/clear/list`) and persistent per-plugin
  state (`state_get/set/delete`) on the protocol AND the
  `AccountPluginServices` implementation; matching `cooldowns` CLI group.
* Rate safety: `MAX_INTERVAL = 86400.0` cap — intervals AND the floor are
  validated against it (below-floor config defaults still clamp).
* Dead public symbols removed (`credentials_summary`, `setting_defaults`,
  `effective_token_ref`, `deliveries_since`, `delete_global`,
  `delete_plugin`, `subscriber_count`, `_spawn_maybe_async`, …).
* Documentation re-synced against HEAD (test counts, CLI surface,
  knowledgebase provenance, INTEGRATION signatures); scripts made
  venv-aware (`run_tests.sh`) and drift-check tolerant
  (`gen_knowledgebase.py --check` degrades gracefully when the gitignored
  `knowledgebase/` is absent); hot-path micro-benchmark harness added
  (`scripts/bench_hot.py`).
