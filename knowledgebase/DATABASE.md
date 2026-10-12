# DATABASE — async SQLite persistence: one aiosqlite connection, queued writes, thirteen repositories

> Source of truth: `database/{db,connection,schema}.py` + `database/repositories/*.py`.
> Per-module reference: `read_only/modules/database.*.md`.

```
                        Database (database/db.py)
       ONE long-lived aiosqlite connection (dedicated worker thread)
       constructor does NO I/O — await connect() opens + pragas + migrations
        │  journal_mode=WAL • busy_timeout=5000 • foreign_keys=ON • synchronous=NORMAL
        │  migrate(): schema_migrations ← MIGRATIONS (ordered, per-migration tx)
        │  writers serialized by ONE asyncio.Lock (SQLite single-writer rule)
        │
        ├─ accounts ─────────► AccountsRepository        (accounts + account_profiles)
        ├─ account_plugins ──► AccountPluginsRepository  (account_plugins + profile bindings — DIRECT assignments, the capability source)
        ├─ account_settings ─► AccountSettingsRepository  (account_settings)
        ├─ profiles ─────────► ProfilesRepository         (profiles + profile_plugins — retained infra)
        ├─ plugin_profiles ──► PluginProfilesRepository   (plugin_profiles — per-plugin setting bundles)
        ├─ plugins ──────────► PluginRepository           (plugins + plugin_settings + plugin_states)
        ├─ policies ─────────► RatePolicyRepository       (rate_limit_policies)
        ├─ audit ────────────► AuditRepository            (audit_logs, pruned)
        ├─ logs ─────────────► LogRepository              (logs, pruned)
        ├─ tasks ────────────► TaskHistoryRepository      (task_history)
        ├─ cache ────────────► CacheRepository            (api_cache)
        ├─ kv ───────────────► KvRepository               (kv — incl. access:* admin rows)
        ├─ credentials ──────► CredentialsRepository      (credentials — FERNET CIPHERTEXT only)
        └─ cooldowns ────────► CooldownRepository         (cooldowns — absolute epoch expiry)
                 all repos stateless views over the owned connection; raise only DatabaseError
```

---

## 1. Engine choice — aiosqlite, and what it does (NOT) buy

`database.connection.DatabaseConnection` opens ONE `aiosqlite` connection.
aiosqlite runs every SQLite operation on a single dedicated worker thread
behind an async queue: `await`-ing a query never blocks the event loop.

**Why the conversion (spec §4):** the framework is asyncio-native; the old
synchronous `sqlite3` calls ran INLINE on the event loop (logging, audit,
manager mutations) — roughly 0.2-0.4 ms of loop unavailability per call,
and under bursts (boot echo storms, dashboard tail polling, audit writes
during paced sends) they compound into multi-tens-of-ms stalls that
compete with pacing timers and websocket heartbeats.

**Honest limits (do not oversell this):**

- **SQLite remains single-writer.** One connection serializes writers
  exactly as before. aiosqlite removes the EVENT-LOOP stall, not the
  single-writer rule. Concurrent writers queue on an `asyncio.Lock`
  (`DatabaseConnection.write()`), which turns `SQLITE_BUSY` races into an
  ordered queue — the 20-concurrent-writers test in
  `tests/unit/test_database.py` exercises exactly this.
- **Throughput is NOT the goal.** End-to-end per-record cost is higher
  than inline sync writes (thread hop + queueing); the benchmark in §7
  shows the trade. What improves is *loop availability*, which is what a
  paced selfbot actually needs.
- **Zero-dependency core is gone.** `aiosqlite` (and `cryptography` for
  the credentials layer) are now runtime dependencies — see
  `requirements.txt`.

What this is NOT: a connection pool, an ORM, or a multi-file store. It is
one file, one connection, twelve repositories.

## 2. Connection ownership + lifecycle

Exactly ONE connection per `Database`, created by
`DatabaseConnection.connect()`, owned by the `Database` service, closed by
`close()`. **No per-query, per-log, or per-dashboard-request connections
exist anywhere in the codebase.**

```python
db = Database(path)        # constructor does NO I/O
await db.connect()         # open + pragmas + migrations (bootstrap does this)
...
await db.close()           # graceful shutdown (idempotent)
```

Lifecycle rules enforced by tests:

- using the database before `connect()` raises `DatabaseError`
  ("database is not connected");
- `close()` is idempotent and safe during shutdown;
- an unclosed connection keeps a non-daemon worker thread alive — every
  test closes its database (the hanging-suite bug this caused once is
  documented in DISCREPANCIES.md).

### Pragmas — each one justified (`database/connection.py`)

| Pragma | Why |
|---|---|
| `journal_mode=WAL` | readers never block the writer; the audit/logs read paths (dashboard tail polling) stay responsive while writes continue |
| `busy_timeout=5000` | the ONLY second connection allowed (rare offline inspection with the framework stopped) waits instead of failing `BUSY` |
| `foreign_keys=ON` | new tables (credentials/cooldowns) declare FKs; legacy tables keep their proven MANUAL cascades (reworking them buys nothing) |
| `synchronous=NORMAL` | WAL default; durable-enough for derived state (cooldowns/plugin state) with far fewer fsyncs than FULL. Secrets are tiny rows |

### File hygiene

- parent dirs auto-created; `chmod 0o600` best-effort after first creation
  (secrecy: restrict before anything else reads it);
- `:memory:` works fully (simulations/scenarios) — pragmas that need a
  file are skipped;
- `row_factory = aiosqlite.Row` (dict-like rows everywhere).

### Error model (`database.repositories.base`)

Reads go through `_fetch_all`/`_fetch_one` (WAL readers, no writer lock);
writes through `_tx` (serialized transaction context: COMMIT on success,
ROLLBACK on any exception). Any exception escaping either is re-raised as
`DatabaseError` with a human message — callers above the persistence layer
only ever see that one error type. Repositories catch *expected*
`sqlite3.IntegrityError` duplicates INSIDE the transaction and re-raise as
`DatabaseError` with a precise message. JSON columns are parsed tolerantly
(`loads_tolerant`: corrupt/missing → logged warning + default, never a
crash; the next write replaces the bad payload).

> Two `DatabaseError` classes exist — `core.errors.DatabaseError` and
> `database.connection.DatabaseError` (both `RuntimeError` subclasses).
> See DISCREPANCIES.md; code is the truth for which call sites raise which.

---

## 3. Migration runner

`Database.migrate()` (invoked by `connect()`):

1. `CREATE TABLE IF NOT EXISTS schema_migrations (...)`;
2. reads the applied version set;
3. for each pending `(version, name, sql)` in `database.schema.MIGRATIONS`
   (ordered list — append-only, NEVER edit an applied migration): one
   `async with conn.write()` transaction — `executescript(sql)` + the
   `schema_migrations` row commit together;
4. failure rolls back and raises
   `DatabaseError(f"migration {version} ({name}) failed: ...")`.

Re-running is a no-op (returns 0) — the file + `:memory:` idempotence is
test-pinned. A crash between migration body and version row would re-apply
that migration on next boot; harmless because every statement is
`CREATE ... IF NOT EXISTS` (idempotent by construction).

- **Migration 1 — "initial schema"**: thirteen core tables + three
  indexes (§4).
- **Migration 2 — "persistent api cache"**: `api_cache` — per-account
  cache of resolved Discord entities so restarts never re-fetch them (one
  `open_dm` per `(account, user)` EVER, not per boot; rows scoped to ONE
  account/profile only).
- **Migration 3 — "secure credentials + persistent cooldowns"**:
  `credentials` (Fernet ciphertext ONLY; key lives OUTSIDE the database —
  `state/credentials.key` 0600 or `SM_MASTER_KEY` env: a stolen DB never
  yields usable tokens) and `cooldowns` (ABSOLUTE epoch expiry so a
  restart restores the REMAINING time, never restarts the window).
- **Migration 4 — "per-account trail indexes"**: account-keyed indexes on
  `audit_logs`/`logs`; the unused `ts` indexes are dropped (the hottest
  write paths stop paying for them).
- **Migration 5 — "first-class sync groups"**: `sync_groups` (identity +
  metadata; MEMBERSHIP stays on `accounts.sync_group`) + the one-time
  legacy conversion of distinct group names.
- **Migration 6 — "account plugin assignments"**: `account_plugins` — the
  DIRECT account→plugin assignment table (the capability source since the
  profile-mediated model was removed). The profile tables are left
  untouched (retained future infrastructure) and NO data is migrated from
  `account_profiles`: assignments start empty and are managed by the
  account manager.
- **Migration 7 — "plugin profiles"**: `plugin_profiles` (per-plugin named
  setting bundles, UNIQUE `(plugin, name)`; `settings_json` may carry the
  reserved `"channels"` scope) + `ALTER TABLE account_plugins ADD COLUMN
  profile` — the binding lives ON the assignment row (NULL = plugin
  defaults). Schema validation of `settings` is the ControlAPI's job,
  never the repository's.

`database.schema.latest_version()` reports the highest defined version.

---

## 4. Every table

| Table | Columns | Keys / indexes | Purpose |
|---|---|---|---|
| `accounts` | `id` INTEGER PK AUTOINCREMENT, `name` TEXT NOT NULL UNIQUE, `token_ref` TEXT, `enabled` INTEGER NOT NULL DEFAULT 1, `sync_group` TEXT, `settings_json` TEXT NOT NULL DEFAULT '{}', `created_at` REAL NOT NULL | PK `id`; UNIQUE `name` | the persistent account registry. `token_ref` is the legacy **.env key** (credentials themselves now live encrypted in `credentials`) |
| `account_settings` | `account` TEXT NOT NULL, `key` TEXT NOT NULL, `value_json` TEXT NOT NULL | PK `(account, key)` | per-account framework settings (reconnect policy...) — the `SettingsManager` framework layer |
| `plugins` | `name` TEXT PK, `version`, `description`, `settings_json` | PK `name` | discovered-plugin metadata cache (manifest for the UI) |
| `plugin_settings` | `account`, `plugin`, `key`, `value_json` | PK `(account, plugin, key)` | per-account plugin setting overrides (JSON values) |
| `plugin_states` | `account`, `plugin`, `enabled` | PK `(account, plugin)` | per-account plugin enabled flag (absent row = caller default) — note: ENABLEMENT, not assignment |
| `account_plugins` | `account`, `plugin`, `profile` (nullable, migration 007) | PK `(account, plugin)` | DIRECT account→plugin assignments (migration 006) — the CAPABILITY source: one row = "this account runs this plugin". `profile` binds the assignment to a plugin setting profile (NULL = plugin defaults). No FK/cascade; cleanup on account delete belongs to the account-delete path |
| `plugin_profiles` | `id` INTEGER PK AUTOINCREMENT, `plugin`, `name`, `description` (default `''`), `settings_json` (default `'{}'`), `created_at`, `updated_at` | UNIQUE `(plugin, name)` | per-plugin named setting bundles (migration 007); `settings_json` is validated against the plugin's Setting schema by the ControlAPI and may carry the reserved `channels` scope (`core.plugin_scopes`) |
| `profiles` | `name` TEXT PK, `description` | PK `name` | profile records — RETAINED future infrastructure |
| `profile_plugins` | `profile`, `plugin` | PK `(profile, plugin)` | plugin list per profile (retained infra) |
| `account_profiles` | `account`, `profile` | PK `(account, profile)` | profile assignment bookkeeping (account NAME keyed; retained infra — no capability effect) |
| `rate_limit_policies` | `level`, `owner` (TEXT NOT NULL DEFAULT ''), `action`, `interval`, `jitter_min`, `jitter_max` | PK `(level, owner, action)` | the 4-level rate policy store (`owner` '' = global; mapped to/from `RatePolicyRecord.owner=None` at the boundary). See [RATE_LIMITING.md](RATE_LIMITING.md) |
| `audit_logs` | `id` PK AUTOINCREMENT, `ts`, `account`, `direction`, `action`, `channel_id`, `content`, `wait_ms`, `jitter_ms`, `note` | PK `id`; index `audit_ts (ts DESC)` | interaction audit trail (see [SCHEDULER.md](SCHEDULER.md) §audit-gate) |
| `logs` | `id` PK AUTOINCREMENT, `ts`, `severity`, `source`, `account`, `plugin`, `task_id`, `event`, `message` | PK `id`; index `logs_ts (ts DESC)` | structured log sink for `LogService` (see [LOGGING.md](LOGGING.md)) |
| `task_history` | `id` INTEGER PK (the scheduler's Task id preserved), `name`, `account`, `plugin`, `action`, `status`, `priority`, `submitted_at`, `started_at`, `finished_at`, `attempts`, `duration_s`, `error`, `summary` | PK `id`; index `task_history_account (account, id DESC)` | finished-task archive — one row per terminal task |
| `kv` | `key` TEXT PK, `value` TEXT NOT NULL | PK `key` | generic key/value: the admin-access rows (`access:admins` = `[int]`, `access:mudae_bot_id` = `int`, `access:account:<name>` = the access row), the rate floor + cleared-policy tombstones (`ratelimit:floor`, `ratelimit:removed`), namespaced plugin state (`pstate:<scope>:...`) |
| `api_cache` | `account`, `kind` ('dm'\|'user'\|'channel'), `key` (str entity id), `value_json`, `cached_at` | PK `(account, kind, key)` | per-account persistent cache of resolved Discord entities |
| `credentials` | `account` TEXT PK, `ciphertext` BLOB NOT NULL, `created_at` REAL, `updated_at` REAL | PK `account` | **Fernet ciphertext only** — encrypted account tokens (spec §12); plaintext NEVER enters this layer |
| `cooldowns` | `id` PK AUTOINCREMENT, `account`, `plugin`, `cooldown_key`, `started_at`, `expires_at`, `source`, `reason`, `metadata_json`, `created_at`, `updated_at` | UNIQUE `(account, plugin, cooldown_key)`; index `cooldown_expiry` | persistent cooldowns with ABSOLUTE expiry — restart-correct remaining time |
| `schema_migrations` | `version` INTEGER PK, `name`, `applied_at` | PK `version` | migration bookkeeping |

### Secrets policy (strengthened)

Token VALUES never enter the database as plaintext. Two supported
locations: **encrypted at rest** in `credentials` (Fernet ciphertext; key
outside the DB — `state/credentials.key` chmod 0600 or `SM_MASTER_KEY`
env), or the **legacy `.env`** file (`accounts.token_ref` keeps the env
KEY for back-compat). Resolution precedence: DB credential → `.env`
fallback → `MISSING_CREDENTIAL`. See
[ACCOUNT_SYSTEM.md](ACCOUNT_SYSTEM.md) and [SECURITY.md](SECURITY.md).

---

## 5. Repository catalog

All methods are **async**. Transaction behavior legend: **tx** = one
all-or-nothing `_tx` transaction (multi-statement bodies commit or roll
back together); **read** = SELECT (WAL reader, no writer lock); all
failures raise `DatabaseError`.

### `BaseRepository` (`database/repositories/base.py`) — shared plumbing
`_fetch_all`/`_fetch_one` (guarded SELECTs) · `_tx` context manager
(serialized write transaction) · `_prune_newest(table, keep)` (subquery
delete; MUST be called inside an open `_tx`) · `loads_tolerant` (JSON with
a safety net).

### `AccountsRepository` (`accounts`)
| Method | Purpose / behavior |
|---|---|
| `add(name, *, token_ref=None, enabled=True, sync_group=None)` | insert one account; **tx** (UNIQUE violation → friendly `DatabaseError`); empty name rejected; returns the persisted `AccountRecord` |
| `get(name)` | one account + its sorted profiles (retained-infra bookkeeping); **read**; corrupt `settings_json` degrades to `{}` |
| `list()` | all accounts by name, each with sorted profiles; **read** |
| `set_enabled(name, enabled)` | flip the persisted flag (False = skipped by `start_all`); **tx**; 0 rows → `DatabaseError` |
| `set_settings(name, settings)` | replace the whole framework-settings JSON blob; **tx**; validates dict + serializability |
| `delete(name)` | **cascade delete, one atomic tx**: `accounts` + `account_settings` + `plugin_settings` + `plugin_states` + `account_profiles` + `api_cache` + `credentials` + `cooldowns`; returns existence. (`account_plugins` rows are cleared by the account manager's remove path — `AccountPluginsRepository.set_plugins(name, [])` — same-named per-account convention as the other tables.) |
| `assign_profile(name, profile)` / `unassign_profile(name, profile)` | attach/detach; **tx**; assign checks account existence, idempotent |
| `profiles_of(name)` | sorted assigned profile names; **read** |

### `AccountPluginsRepository` (`account_plugins`) — the capability source
`assign_plugin(account, plugin)` (idempotent insert) ·
`unassign_plugin(account, plugin)` (returns the pre-state — `False` when
the row was already gone) · `plugins_of(account)` (sorted) ·
`set_plugins(account, plugins)` (REPLACE the whole set atomically, returns
the stored sorted list) · `all_assignments()` (bulk boot load:
`{account: [plugins]}`). One row = "this account runs this plugin";
mutated only through the account manager, which re-attaches immediately.
**Profile bindings (migration v7)** — kept separate so the assignment
methods' return shapes stay frozen: `set_profile(account, plugin,
profile)` (UPDATE-only, idempotent; `False` when the assignment row
does not exist — this layer never creates assignments) ·
`bindings(account)` → `{plugin: profile or None}` · `all_bindings()` →
`{account: {plugin: profile or None}}` (bulk boot load; accounts with
zero plugins are OMITTED — read with `.get(account, {})`).

### `PluginProfilesRepository` (`plugin_profiles`) — NEW (v7)
`create(plugin, name, description="", settings=None)` (UNIQUE violation →
friendly `DatabaseError`) · `get(plugin, name)` · `list(plugin=None)`
(plugin-then-name order) · `update(plugin, name, *, name_new=None,
description=None, settings=None)` (field-wise; a RENAME rewrites every
`account_plugins.profile` pointer that named the old profile in the SAME
transaction) · `delete(plugin, name)` (returns existence; CASCADE:
every `account_plugins.profile` reference to the profile is NULLed in
the same transaction — a deleted profile means "plugin defaults" again,
never a dangling binding). Persistence ONLY: no discovery check, no
schema validation (both are the ControlAPI's job); anything
JSON-serializable stores verbatim.

### `AccountSettingsRepository` (`account_settings`)
`get(name)` · `set(name, key, value)` · `delete(name, key)` ·
`list_accounts()` (spine-compat helper probed via `hasattr` by
SettingsManager to rebuild the framework layer after restart).

### `ProfilesRepository` (`profiles`)
`upsert(name, plugins, description="")` (canonical deduped+sorted plugin
list on disk) · `delete(name)` (**cascade tx**: `profiles` +
`profile_plugins` + `account_profiles`) · `get(name)` · `list()`.

### `PluginRepository` (`plugins`)
`upsert_metadata` (idempotent, every discovery pass) · `metadata()` ·
`metadata_for(name)` · `state(account, plugin)` (None = no row ≠ False) ·
`set_state` · `setting` / `settings` / `set_setting` ·
`all_account_settings()` (whole override layer, one-query preload).

### `RatePolicyRepository` (`policies`)
Deliberate validation split: data integrity here (finite numbers,
`0 <= jitter_min <= jitter_max`, valid level/owner shape); the 1.5s safety
floor is the rate manager's job (`UnsafePolicyError`), not a storage
invariant.
`set(level, owner, action, interval, jitter_min, jitter_max)` ·
`delete(level, owner, action)` (lenient) · `get` (owner forms normalized) ·
`list()` (corrupt rows skipped with a warning).

### `AuditRepository` (`audit`) and `LogRepository` (`logs`)
`add(entry)` — append + prune in **one tx**; missing fields default to
`'?'` markers; audit content truncated to 500 chars; log severity
upper-cased. `recent(limit, ...)` — newest `limit` rows OLDEST-FIRST (DESC
fetch + reverse); audit filters by `account`, logs by
`account`/`severity`/`source` (AND together).

### `TaskHistoryRepository` (`tasks`)
`add(entry)` (whitelisted `_TASK_COLUMNS`; `"result_summary"` →
`"summary"` alias; own-id preservation; id collision → `DatabaseError`) ·
`update(task_id, **fields)` (unknown field LOUD; `"id"` immutable; no row
→ `DatabaseError`) · `recent(limit, account)` (tail contract as above).

### `CacheRepository` (`cache`)
`get(account, kind, key)` · `set` · `delete` ·
`drop_account(account, kind=None)` (stale-target maintenance) ·
`counts()` (diagnostics).

### `KvRepository` (`kv`)
`get(key, default)` (corrupt JSON falls back to the RAW STRING — old-file
tolerance) · `set` (JSON-encoded) · `delete` · `prefix(p)` (LIKE wildcards
escaped). Also the storage substrate for `PluginStateService` namespaced
keys (`pstate:<scope>:...`).

### `CredentialsRepository` (`credentials`) — NEW
| Method | Purpose / behavior |
|---|---|
| `store(account, ciphertext)` | insert/replace the Fernet ciphertext blob; **tx**; plaintext NEVER enters this layer |
| `load(account)` | stored ciphertext or None; **read** |
| `delete(account)` | remove; **tx**; returns existence |
| `accounts_with_credential()` | account names with a stored credential (status display); **read** |

### `CooldownRepository` (`cooldowns`) — NEW
| Method | Purpose / behavior |
|---|---|
| `upsert(*, account, plugin, cooldown_key, started_at, expires_at, source, reason, metadata)` | insert/replace one cooldown (identity = account+plugin+key); **tx** |
| `get(account, plugin, cooldown_key, now)` | one NON-EXPIRED row or None; **read** |
| `active_for(account, plugin=None, now)` | non-expired cooldowns of one account, soonest-expiring first; **read** |
| `clear(account, plugin, cooldown_key)` | remove one; **tx** |
| `clear_expired(now)` | boot cleanup — delete every expired row, return count; **tx** |
| `delete_account(account)` | account-deletion cascade; **tx** |

---

## 6. Queue persistence for logs/audit (application paths never touch SQLite)

The highest-frequency writers — `LogService` records and `AuditRecorder`
entries — do NOT await the database on the application/event path (spec
§29):

```
record()/audit path                drain worker (one per service)
      │ enqueue (thread-safe             │
      │ SimpleQueue, never blocks)       ▼
      └──────────────► queue ──► await repo.add(entry) per item
                                       (aiosqlite worker thread)
```

- `await service.start_persistence(repo)` spawns the drain worker BEFORE
  heavy logging begins; `await service.stop_persistence(timeout)` drains
  what remains, then cancels — called BEFORE `db.close()` in shutdown.
- Failures are counted (`stats()['db_failures']`), never raised into the
  application path; overflow drops are counted too.
- `attach_db(repo)` alone is CONFIGURATION ONLY — no write happens until
  the worker spawns (the old foot-gun where a sink wrote a closed DB is
  structurally gone).

The scheduler's task-history write is queued the same way (the terminal
`_archive` path awaits the repo off the application path). Everything
else (account add/remove, policy set, settings writes) is a low-frequency
direct `await` — a few per human interaction, not per message.

---

## 7. Benchmark — inline sqlite3 vs queued aiosqlite (spec §40)

`scripts/bench_db.py` writes **1_000 records into the same `logs` schema**
two ways and measures event-loop availability with a ~1 ms ticker task:

- **legacy inline** — synchronous `sqlite3` execute+commit per record
  executed INLINE on the event loop (the pre-conversion LogService sink
  pattern);
- **queued aiosqlite** — `LogService.record()` → queue → one async drain
  worker (today's production path).

Measured on the development machine (Linux, Python 3.13, aiosqlite 0.22,
local SSD; 3 runs of `python3 scripts/bench_db.py --records 1000 --repeat 3`;
**numbers vary by machine/disk/load — run it yourself**):

```
harness                records     wall  max stall  med stall  per-rec
----------------------------------------------------------------------
legacy inline             1000  63-72ms   63-72ms    ~0.18ms  ~0.07ms
queued aiosqlite          1000  0.74-0.8s   5-7ms     ~0.12ms  ~0.8ms
```

Reading it honestly:

- legacy max stall ≈ the ENTIRE write phase (63-72 ms for 1000 records,
  one continuous block — the loop serves nobody meanwhile; per-write
  fsync spikes would make individual stalls worse on slower disks);
- queued max stall 5-7 ms (thread hop + queue wakeup), median ≈ 0.1 ms —
  the loop stays available to pacing timers and heartbeats while 1000
  records persist;
- wall-clock end-to-end is SLOWER queued (~0.8 ms/record including full
  drain vs ~0.07 ms inline): the queue trades throughput for loop
  availability. Write volume here is bounded (audit/log pruning caps the
  tables), so availability wins.

## 8. Models crossing the boundary (`database.models`)

Plain dataclasses, dependency-free (database/ stays independently
testable): `AccountRecord` (persistent account data — runtime data lives
in `core.account_manager.AccountRuntime` and is NEVER persisted),
`ProfileRecord`, `PluginMetadata`, `RatePolicyRecord` (with the
`POLICY_LEVELS`-validation `__post_init__`), and the constant
`POLICY_LEVELS = ("global", "account", "plugin", "account_plugin")`.
