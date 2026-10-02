# Hardening pass — Phase 1 of N

Scope of Phase 1: **database layer, logging/audit persistence, boot &
shutdown, credentials/config, event-bus + scheduler cancellation, adapter
task hygiene.** Phases 2+ (below) have NOT been audited yet.

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
