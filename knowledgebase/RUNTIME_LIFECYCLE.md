# LibzentSuite — Runtime Lifecycle

This file documents process lifecycle as implemented: the exact boot order
of `core.bootstrap.build()` and `main.run_app` (verified line by line
against source), the account lifecycle (add → start → supervisor → stop →
emergency stop → resume), the shutdown order and the reasoning behind it,
and what can be reconfigured at runtime versus what requires a restart.
Topology and contracts: [ARCHITECTURE.md](ARCHITECTURE.md); per-component
state: [COMPONENT_MAP.md](COMPONENT_MAP.md); traffic flows:
[DATA_FLOW.md](DATA_FLOW.md).

## Boot order — `core.bootstrap.build()` (verified, line by line)

`build(*, env_file=".env", config_dir="config", db_path="state/libzent.db",
plugin_dir="plugins", client_factory=None, simulation_runner=None,
credentials=None, database=None, quiet=False) -> Framework`:

1. **`configure_stderr()`** (skipped when `quiet=True`) — one human-readable
   stderr handler on the `libzent` logger tree, idempotent.
2. **`Credentials.from_file(env_file)`** — .env parsing; a missing file
   yields an EMPTY credential set (warning) — boot never dies on it.
3. **`load_config(config_dir)`** — global settings layer, rate policies
   (defaults 2.0 s + 0.0–0.5 s jitter when the file is absent), lane config,
   account bootstrap files. Malformed pieces fail LOUD (typo protection).
4. **`Database(db_path)`** — opens/creates the file (0600 before first
   write, parents auto-created, WAL), then **runs migrations in the
   constructor** (`Database.migrate`: each pending migration of
   `database.schema.MIGRATIONS` in its own transaction, recorded in
   `schema_migrations`; `:memory:` works fully).
5. **`LogService()` + `logs.attach_db(database.logs)`** — structured logs
   now persist (best-effort; failures counted, never raised).
6. **`attach_stdlib_bridge(logs)`** — plugin/stdlib logger records under
   `libzent.*` are ingested into the structured service (loop-proof
   via the `_sm_from_service` marker + `ingest_context`).
7. **`EventBus()` + `_wire_event_logging(bus, logs)`** — lifecycle events in
   `_LOGGED_EVENTS` (Account*, Plugin*, Task terminal, RateLimitTriggered,
   Scenario*) become structured INFO lines; `MessageReceived` is excluded
   as too chatty.
8. **`RateLimitManager(bus, ServiceLogger(logs,"ratelimit"),
   database.policies, kv_repo=database.kv, lane_enabled=…, lane_prefixrate=…)`**
   — constructor loads stored policies (bad rows logged+skipped) and the
   lane config from kv (kv wins over constructor defaults; first boot
   persists the baseline). Then **`_apply_policies(limiter, config)`** pushes
   the config file's `global` and `accounts` policies into the manager —
   the floor still applies to everything.
9. **`SettingsManager(bus, ServiceLogger(logs,"settings"),
   plugin_repo=database.plugins, account_settings_repo=
   database.account_settings, global_layer=config.global_settings)`** — the
   three-layer merge is lazy-loaded on first use.
10. **`AuditRecorder(database.audit)`** — ring + DB sink + live subscribers.
11. **The late-bound sender seam**: `sender_box = {"send": None}` and the
    `_sender(task)` closure that raises a structured, non-retryable
    `RuntimeError("send hook not wired (framework still booting)")` until
    the box is filled — this breaks the scheduler↔accounts constructor
    cycle.
12. **`TaskScheduler(bus, ServiceLogger(logs,"scheduler"), limiter, _sender,
    audit=audit, history_repo=database.tasks)`**.
13. **`PluginManager(bus, ServiceLogger(logs,"plugins"), settings,
    plugin_repo=database.plugins, plugin_dir=plugin_dir)` +
    `plugins.discover()`** — scans now; metadata upserted to the repo (the
    Plugins page works before any account attaches).
 14. **REMOVED (2026-10-22): `ProfileManager`** — the legacy profile
     manager is deleted; nothing is constructed here. Capabilities are
     direct account-to-plugin assignments (`database.account_plugins`);
     per-plugin settings presets live in `plugin_profiles`
     (see the plugin manager section).
15. **`client_factory`** — the injected one, or `RealClientFactory(
    credentials)` (lazy import of `adapters.discord`; tests/sims never do).
16. **`AccountManager(bus, ServiceLogger(logs,"accounts"),
    database.accounts, plugins, profiles, scheduler, settings, audit,
    client_factory, credentials, limiter=limiter,
    api_cache_repo=database.cache)`** — the constructor **restores persisted
    accounts** (`_load_from_repo`: rebuilds `Account`s, attaches plugins,
    registers sync-group scope; repository failures never break
    construction) and subscribes the inbound audit trail.
17. **`sender_box["send"] = accounts.send_hook`** — the seam is filled
    BEFORE the scheduler can run anything.
18. **config-dir account files** are registered via `accounts.add_account(...)`
    (persist-first; a broken file logs and continues).
19. **`scheduler.start()`** — idempotent arming; lanes spawn lazily on first
    submit per account.
20. **`simulation_runner`** — injected, or best-effort
    `SimulationRunner(plugin_dir, credentials)` (import failure → `None`).
21. **`ControlAPI(accounts=…, plugins=…, profiles=…, settings=…, limiter=…,
    scheduler=…, logs=…, audit=…, simulation=…, credentials=…, database=…,
    started_at=…)`**.
22. **`COMMAND_HUB.api_provider = lambda: api`** — the self-command
    layer's Framework verbs bridge to the ONE ControlAPI. (The retired
    `RemoteControl` bus handler is no longer wired here; the class
    survives only in the simulator stack, for `control_mention`.)
23. **`Framework(...)`** returned — a plain holder of api/bus/logs/limiter/
    scheduler/accounts/plugins/profiles/settings/audit/database/credentials/
    config.

## `main.run_app` — surfaces and their lifecycle

```
build(...) ──► framework = Framework
   │
   ├─ args.start_all ─► api.accounts_start()            (every ENABLED account;
   │                                                     failure prints "live mode unavailable", rc 2)
   ├─ args.dashboard ─► DashboardServer(api, token=api.dashboard_token,
   │                      port=args.port, bus=framework.bus)
   │                      .start().attach_logs(api.logs_service())
   │                      + attach_audit(api.audit_recorder()) when present
   ├─ argv?            ─► run_once(api, argv)           (one-shot; NO shutdown of its own)
   ├─ args.headless    ─► await asyncio.Event().wait()  (daemon; Ctrl-C ends)
   └─ else             ─► Repl(api).run()               (api.shutdown() on EVERY exit path)
   finally: dashboard.stop() → await framework.shutdown()
```

Argparse surface (verified): `--sim [NAME]`, `--sim-list`, `--config`,
`--plugins`, `--env`, `--start-all`, `--dashboard`, `--headless`, `--port`
(default 8070), `--db` (default `state/libzent.db`); trailing argv becomes
a one-shot command. `--sim`/`--sim-list` short-circuit into
`run_sim` (offline; never touches the live stack). There is **no** `--tui`
flag despite the module docstring (discrepancy list).

## Account lifecycle

### add (`AccountManager.add_account`)

Validate name (`^[a-z0-9][a-z0-9_-]{0,31}$`) and duplicate, then **persist
FIRST** (`accounts_repo.add` — a failed write leaves memory untouched),
build the in-memory `Account`, store the DIRECT plugin assignments
(`plugins` → `account_plugins` rows; passing the legacy `profiles=` kwarg
raises an explanatory `ValueError`), attach exactly those plugins, register
the sync-group scope with the limiter. The token VALUE never enters the DB
— only the `.env` key ref (`token_ref`, convention fallback
`DISCORD_TOKEN_<NAME>`).

### start (`AccountManager.start`)

1. Refuse when already running/connecting/reconnecting; clear `lost`,
   `last_error`; status `CONNECTING`.
2. `_connect_client`: resolve the token (raises `CredentialsError` when the
   `.env` ref is missing) → build a client via the factory → **wire
   callbacks BEFORE connect** (`_wire_client`: `on_message`,
   `on_disconnect`, `on_reaction`, `on_message_edit`, `on_message_delete`)
   → `client.connect()` → read `user_id` → **ingest the client's cached
   snapshot** into the per-account `DiscordCache` (zero API calls).
3. Honor a `stop()` that raced mid-connect; status `RUNNING`,
   `started_at = time.monotonic()`.
4. Re-attach plugins with the CURRENT direct assignments
   (`account_plugins` rows — every assignment mutation also re-attaches
   immediately, so this is a refresh, not the only apply point) and
   `activate_account` with the services
   factory (settings resolve; on_load/on_enable fire; failure disables THAT
   plugin, the account keeps running).
5. Publish `AccountConnected`.
6. Spawn the **reconnect supervisor** when the account's
   `reconnect_enabled` setting is true (`asyncio.create_task`, tracked).

Failures: status `FAILED` + `last_error` + `AccountFailed` in every case;
`AccountError`/`CredentialsError` re-raise as-is; process-level signals are
never swallowed.

### supervisor (`_supervise` / `_reconnect_loop`)

Polls the per-account `lost` event every `supervisor_tick` (0.2 s). On an
UNEXPECTED drop (the disconnect callback sets `lost` and flips the status to
`RECONNECTING` — deliberate stops set `stop_requested` first and are
ignored), reconnect with exponential backoff
(`initial·multiplier^attempt`, capped at `max`; defaults 1.0/2.0/60.0 from
`ACCOUNT_SETTINGS`), a **FRESH client per attempt** (factory + callback
rewiring), cache re-ingest, another `AccountConnected`. Settings are re-read
per attempt so live changes apply immediately. `KeyboardInterrupt`/
`SystemExit` mark the account failed and stand down — they never kill the
process.

### stop (`AccountManager.stop` — idempotent, order pinned)

1. `stop_requested = True`; `lost.set()` (wake the supervisor NOW).
2. **Deactivate plugins FIRST** — hooks still see the cache.
3. `await client.disconnect()` (None-safe; failures logged).
4. Reap the supervisor: bounded 1 s `wait_for(shield(...))`, then cancel +
   1 s reap; a caller cancellation also cancels the supervisor (no leaks).
5. Status `STOPPED`, `lost.clear()`, drop per-plugin services.
6. Publish `AccountDisconnected(reason="stopped")` only when the account was
   live.

`stop_all` iterates sorted names, logging and continuing per account.
`remove_account` refuses a running/reconnecting/connecting account (before
any mutation), cancels the supervisor, detaches plugins, deletes the row.

### emergency stop

* **Account-scoped** (`AccountManager.emergency_stop`): SYNCHRONOUS —
  immediately `stop_requested`/`lost.set()`,
  `scheduler.cancel_matching(account=name)`, then fire the full `stop()` as
  a tracked background task; returns
  `{"account", "cancelled_tasks", "stop_initiated"}`. Poll
  `runtime.status` / watch `AccountDisconnected` for completion. Needs a
  running event loop.
* **Global** (`ControlAPI.emergency_stop_all`):
  `scheduler.cancel_matching()` → `await scheduler.stop(drain=False)` →
  `await accounts.stop_all()` → error log "GLOBAL EMERGENCY STOP executed".
* **Resume** (`ControlAPI.system_resume`): re-arms the scheduler
  (`scheduler.start()` — `stop()` already drove every task terminal, so
  nothing stale carries over) and returns fresh status. Accounts stay
  stopped on purpose: restart the ones you want with `accounts_start`.

## Shutdown — `Framework.shutdown` and why this order

```python
async def shutdown(self) -> None:
    if self.remote_control is not None:
        self.remote_control.stop()          # 1. stop ingesting management commands
    await self.scheduler.stop(drain=True)   # 2. drain queued+running tasks (bounded 10s),
                                            #    every task terminal, lanes reaped
    await self.accounts.stop_all()          # 3. plugins deactivated, clients disconnected
    self.logs.attach_db(None)               # 4. stop persisting logs FIRST
    detach_stdlib_bridge()                  # 5. then stop ingesting stdlib records
    self.database.close()                   # 6. only now close the DB
```

**Why:** the docstring pins it — "the db sink detaches and the stdlib bridge
is removed BEFORE `database.close()` — otherwise the bridge keeps ingesting
stdlib lines into a service whose sink is a closed database (every write
silently failing forever)". Steps 4–6 exist purely for that; steps 1–3
quiesce traffic so no new rows are produced while the sinks wind down.

**Caveat (reported for DISCREPANCIES.md):** the REPL calls
`ControlAPI.shutdown()` on EVERY exit path, and that method runs
`scheduler.stop(drain=True)` → `accounts.stop_all()` →
`database.close()` **without** detaching the log sink or the stdlib bridge
first — `main.run_app`'s `finally` then calls `framework.shutdown()`, but
the database is already closed. The documented ordering guarantee therefore
does not hold on the REPL/`quit` path (degraded-but-safe: DB write failures
are swallowed and counted in `LogService.stats()["db_failures"]`).
`ControlAPI.shutdown` also never stops `remote_control`; only
`Framework.shutdown` does.

## Runtime reconfiguration vs restart-required

| Change | Effect | Where |
|---|---|---|
| Plugin setting (`set` / dashboard editor) | validated → persisted → `SettingChanged` → the LIVE settings dict mutated in place; running plugins see it immediately (contexts and services share the dict) | `PluginManager.set_setting` → `SettingsManager.set_plugin_setting` |
| Plugin enable/disable | persisted first; lifecycle hooks fire immediately on an active account (`on_enable`/`on_disable`) | `PluginManager.set_enabled` |
| Framework account setting (reconnect policy, typing_probability) | persisted + `SettingChanged`; reconnect knobs are re-read on every reconnect attempt and every start; `typing_probability` is read per send | `SettingsManager.set_account_setting` |
| Rate policy set/clear | applies to the NEXT acquire; parked waiters are woken and recompute; persists best-effort | `RateLimitManager.set_policy_values`/`clear_policy` (`_wake_waiters`) |
| Safety floor | raise-only; waiters recompute against the new floor | `RateLimitManager.set_floor` |
| Lane enable/prefixrate | immediate; disabling drains every lane waiter; persisted in kv (kv wins over the config file on later boots); prefixrate 0 pauses all outbound | `RateLimitManager.set_lane_*` → `GlobalSendLane` |
| Account `enabled` flag | persisted; affects only `start_all` / `--start-all` (a running account is untouched) | `AccountManager.set_enabled` |
| Account `sync_group` | re-registers the limiter's group scope immediately (`set_account_group`) — pacing composes from the next acquire | `AccountManager._sync_group_scope` |
| Account plugin assignment (`accounts add` plugins / `plugin-add` / `plugin-remove`) | persisted first, then the account's plugins **re-attach IMMEDIATELY** (running or not — no restart needed); an unknown plugin name rejects the call up front | `AccountManager.assign_plugin`/`unassign_plugin`/`set_account_plugins` |
| Plugin code changes / new plugin files | require a process restart (discovery runs once at boot; a fresh `discover()` rescans but nothing re-runs it in production) | `PluginLoader.discover` |
| config/ files (`rate_limits.json`, `global_settings.json`, account files) | read at boot only; DB/kv state and live mutations win afterward | `core.config.load_config` |
| .env (tokens, owner ids) | read at boot; account tokens are resolved at each `start`/reconnect from the boot-time `Credentials` object — a changed token needs a restart (or at least a fresh boot) | `Credentials.from_file`, `AccountManager._connect_client` |

Headless/daemon note: `--headless` waits on a bare `asyncio.Event()`; Ctrl-C
(`KeyboardInterrupt`, rc 130 from `main`) or task cancellation falls into
the same `finally` → `framework.shutdown()` path.
