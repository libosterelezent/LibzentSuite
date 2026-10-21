# LibzentSuite — Component Map

This file is the per-component reference: one section per component of the
framework, each with its purpose, the state it owns, its key public symbols
(exact qualified names, cross-checked against
`knowledgebase/read_only/SYMBOL_INDEX.md`), the bus events it emits and
consumes, the persistence it touches, and how it isolates failures. Read
[ARCHITECTURE.md](ARCHITECTURE.md) first for the topology and the rules;
[DATA_FLOW.md](DATA_FLOW.md) shows these components moving real traffic.

---

## core.account_manager (account manager, A6)

* **Modules**: `core.account_manager.manager` (43 symbols),
  `core.account_manager.cache`, `core.account_manager.services`.
* **Purpose**: owns every account — registry, lifecycle (start/stop/reconnect
  supervisor), the outbound `send_hook`, the per-account `DiscordCache`, and
  the plugin-facing services facade. Enforces the audited client-touch
  inventory (see [ARCHITECTURE.md](ARCHITECTURE.md#the-api-boundary-contract)).
* **Owns**: `Account` (persistent `database.models.AccountRecord` +
  runtime `AccountRuntime` — status, client, cache, `lost` event, supervisor
  task, `stop_requested` — plus a cached per-plugin view); the
  `Dict[str, Account]` registry; the single `TaskAckRegistry`; the
  `(account, plugin) -> AccountPluginServices` map (rebuilt per start);
  background-task strong refs.
* **Key symbols**: `core.account_manager.manager.AccountManager`,
  `.Account`, `.AccountRuntime`, `.AccountStatus` (created → connecting →
  running ⇄ reconnecting → stopped/failed); `core.account_manager.cache.DiscordCache`,
  `.ChannelInfo`, `resolve_dm_channel`, `invalidate_dm_channel`;
  `core.account_manager.services.AccountPluginServices`, `.TaskAckRegistry`.
* **Events emitted**: `AccountConnected`, `AccountDisconnected`,
  `AccountFailed` (all via `publish_nowait`). **Consumed**: `MessageReceived`
  (inbound audit `_audit_inbound`); `TaskCompleted`/`TaskFailed`/`TaskCancelled`
  (via the shared ack registry).
* **Persistence**: `database.repositories.accounts` (registry; persist-first
  `add_account`/`set_enabled`), `database.repositories.cache` (`api_cache` —
  DM channels + users per account), direct plugin assignments via
  `database.repositories.account_plugins` (the `account_plugins` table —
  the CAPABILITY source). Token *values* never persisted — only `token_ref`.
* **Failure isolation**: per-account try/except everywhere
  (`start_all`/`stop_all` log-and-continue); reconnect supervisor with
  exponential backoff and a FRESH client per attempt; `KeyboardInterrupt`/
  `SystemExit` never swallowed in start; `stop()` idempotent with bounded
  (1 s) supervisor reap; `send_hook` raises typed errors so the scheduler can
  retry (`ConnectionError`) or fail-fast (`AccountError`, `TargetGone`).
  Runtime import of `adapters.base` at module level — see discrepancy list.

## core.plugin_manager (plugin manager, A2/A3)

* **Modules**: `core.plugin_manager.loader` (discovery),
  `core.plugin_manager.manager` (lifecycle + dispatch + self-command
  execution + UI collection), `core.plugin_manager.commands`
  (the `SelfCommandHub` registry + framework verbs).
* **Purpose**: discovers `core.plugin_base.PluginBase` subclasses under the
  plugin dir; instantiates ONE instance per (account, plugin) filtered by
  the account's DIRECT plugin assignments (`account_plugins` rows —
  profiles are not consulted); drives async lifecycle hooks; fans inbound
  events out to every enabled+active plugin, isolated per plugin;
  publishes each account's self-command surface to the hub and executes
  invocations; collects plugin-contributed UI cards/panels.
* **Owns**: `PluginRuntime` per pair (instance, enabled/loaded/active flags,
  the LIVE settings dict, `last_error`, `error_count`); the
  `account -> (plugin_name) -> services` factories; fire-and-forget dispatch
  task refs. The loader owns plugin records under a synthetic parent package
  (no `sys.path` hacks; relative imports work in package plugins).
* **Key symbols**: `core.plugin_manager.manager.PluginManager`,
  `.PluginRuntime` (`command_entries`, `commands_for`,
  `execute_self_command`, `ui_stats`, `ui_panels`);
  `core.plugin_manager.loader.PluginLoader`,
  `.PluginRecord`; `core.plugin_manager.commands.SelfCommandHub`,
  `.COMMAND_HUB`, `.FRAMEWORK_COMMANDS`, `.RegisteredCommand`,
  `.PacingBackend`, `render_help`; contract in `core.plugin_base.PluginBase`,
  `.PluginContext`, `.PluginServices`, `.CommandSpec`, `.ArgSpec`,
  `.CommandContext`, `.CommandSettingsApi`, `.StatCard`, `.UIPanel`,
  `.PanelSection`.
* **Events emitted**: `PluginLoaded`, `PluginUnloaded`,
  `PluginEnabledChanged`. **Consumed**: `MessageReceived`,
  `MessageEdited`, `MessageDeleted`, `ReactionAdded` (four bus subscriptions
  routed through the one `_on_message` → `_dispatch` path; new event types
  are additive).
* **Persistence**: `database.repositories.plugins` — metadata upsert at
  discover, per-account `plugin_states` (persist-first `set_enabled`),
  `plugin_settings` (via SettingsManager).
* **Failure isolation**: absolute — a failing hook disables/records THAT
  plugin only (`_hook` records + re-raises; `_activate_one` catches);
  dispatch is a fire-and-forget task per event so a slow plugin never blocks
  the publisher; a raising `check` in plugin waits surfaces as
  `AccountError` to the waiter (services layer); self-command execution is
  fully contained (usage errors render the usage line; crashes render an
  error reply); UI providers that crash contribute nothing (skipped +
  logged). Loader raises `core.errors.PluginError` with human messages for
  inconsistent trees.

## core.access_manager (admin access store — NEW)

* **Module**: `core.access_manager`.
* **Purpose**: the DB-backed authorization/prefix/admin store replacing
  the env `OWNER_USER_IDS` model. Single kv rows per concern under the
  `access:` prefix (`admins`, `mudae_bot_id`, per-account rows with the
  admin flag, prefix mode/symbol and the command allowlist).
* **Owns**: nothing beyond the read-through cache of per-account prefix
  configs (`prefix_config_sync` — warmed by the bootstrap and every prefix
  mutation for the client factory's build-time read).
* **Key symbols**: `core.access_manager.AccessManager` (`admin_ids`,
  `set_admin_ids`/`add_admin`/`remove_admin`, `migrate_from_credentials`,
  `account_view`, `set_admin_flag`, `allowed_user_ids`/`set`/`add`/`remove`,
  `prefix_config`/`prefix_config_sync`/`warm_prefix_cache`/`set_prefix_config`,
  `mudae_bot_id`/`set_mudae_bot_id`, `all_account_rows`, `is_admin_id`,
  `set_account_user_id`, `can_invoke`), `PREFIX_MODES`,
  `DEFAULT_ACCOUNT_ROW`, `KEY_ADMINS`, `KEY_MUDAE_ID`, `account_key`.
* **Events**: none (management-plane only).
* **Persistence**: `database.repositories.kv` (`access:*` rows); all
  mutations write the FULL row (read-modify-write — single-operator
  surface).
* **Failure isolation**: fail-CLOSED — an empty per-account allowlist
  admits nobody but admins; unknown/absent rows behave as empty; the
  adapter and the plugin manager both swallow checker errors as denial.

## core.framework_credentials (REMOVED)

* **Module**: deleted — the class was defined as a contract/seam but had
  ZERO construction sites repo-wide, and the bootstrap hands the env
  `Credentials` object through the services wiring unchanged.
* **Reality**: `AccountPluginServices.credentials`
  (`core/account_manager/services.py`) returns the injected credentials
  view; DB-first admin ids / Mudae id / prefix live in
  `core.access_manager` (`AccessManager`), not in a credentials wrapper.

## core.plugin_scopes (channel scopes — NEW)

* **Module**: `core.plugin_scopes` (PURE: no I/O, no framework imports).
* **Purpose**: the single source of truth for the RESERVED `"channels"`
  settings key — the channel scope a plugin profile may carry
  (`{"mode": "all"|"allow"|"deny", "rules": [{"guild_id",
  "channel_ids": [...]}]}`, ids as STRINGS). Every surface — profile
  validation, the effective-settings merge, the dashboard picker,
  command-side evaluation — validates and evaluates EXACTLY this
  structure.
* **Key symbols**: `core.plugin_scopes.validate` (normalized copy or
  `None`; ValueError naming the field), `channel_allowed` (lenient
  evaluation of stored data: malformed rules are skipped, never crash),
  `CHANNELS_KEY`, `MODES`.
* **Semantics**: `allow` = ONLY listed channels pass (an empty allow-list
  allows NOTHING); `deny` = listed blocked, everything else passes;
  `all`/absent/`None` = no restriction.

## core.profile_manager (profile manager — RETAINED FUTURE INFRASTRUCTURE)

* **Module**: `core.profile_manager.manager`.
* **Purpose**: named profile records — a profile lists plugins, and the
  CRUD + assignment bookkeeping persists. It has **NO capability effect**:
  profiles do NOT decide which plugins an account runs (that is the direct
  `account_plugins` assignment owned by the account manager). Retained as
  the foundation for the FUTURE (not yet implemented) plugin-specific
  profiles / multiple-configuration feature.
* **Owns**: no duplicate cache — read-through over the repositories ("the
  repository is the single source of truth").
* **Key symbols**: `core.profile_manager.manager.ProfileManager`
  (`set_profile`, `delete_profile`, `profiles`, `assign`, `unassign`,
  `assigned`, `allowed_plugins` — retained but not consulted by the attach
  path).
* **Events**: none (management-plane only).
* **Persistence**: `database.repositories.profiles` (profiles + plugin
  lists), `database.repositories.accounts` (assignments; delete cascades).
  Mutations persist FIRST, then report success.
* **Failure isolation**: unknown profile names in an account's assignment
  list are logged and skipped; validation errors are `core.errors.ProfileError` with the
  known names listed.

## core.settings_manager (settings)

* **Module**: `core.settings_manager`.
* **Purpose**: ONE typed settings schema consumed by CLI + GUI alike.
  Layered resolution: schema defaults ← global layer
  (`config/global_settings.json`) ← account layer (database). The same
  `Setting` manifests auto-generate the dashboard editors and CLI
  `set` validation.
* **Owns**: the global-layer copy, the lazy-loaded DB layers
  (`account → plugin → {key: value}` and framework account settings),
  `ACCOUNT_SETTINGS` — the five framework per-account knobs
  (`reconnect_enabled`, `reconnect_initial_delay`, `reconnect_max_delay`,
  `reconnect_multiplier`, `typing_probability`).
* **Key symbols**: `core.settings_manager.SettingsManager`
  (`resolve`, `set_plugin_setting`, `account_settings`, `set_account_setting`,
  `layers_view`), `core.settings_manager.Setting`,
  `SettingsValidationError`.
* **Events emitted**: `SettingChanged` (scope/key/old/new on every
  successful set). **Consumed**: none directly.
* **Persistence**: `database.repositories.plugins` (plugin settings),
  `database.repositories.settings` (framework account settings). Memory only
  updates after a successful DB write.
* **Failure isolation**: unknown keys/bad values raise
  `SettingsValidationError` naming the layer; invalid *stored* account
  settings are warned about and ignored at resolve time (a hand-edited DB
  must not crash boot).

## core.rate_limit_manager (rate limiting, A4)

* **Modules**: `core.rate_limit_manager.policies` (model + validation),
  `.engine` (`RateLimitManager`), `.warnings`.
* **Purpose**: owns execution timing. Four policy levels (`global`,
  `account`, `plugin`, `account_plugin` — `database.models.POLICY_LEVELS`)
  composed by MAX interval; an interval+pacing engine (not token buckets)
  with priority-ordered waiters, server 429 cooldowns, post-grant jitter,
  and LEARNED per-action defaults.
* **Owns**: the live policy table
  `(level, owner, action) → RatePolicy`; the safety floor (raise-only;
  `MIN_ACTION_INTERVAL = 0.05` — an absolute sanity minimum, Discord's
  own bucket limits are the actual constraint); `DEFAULT_ACTION_INTERVALS`
  (message 2.6 s, dm 2.6 s, reaction 2.5 s, component 3.0 s — the
  fallback when NO policy is stored for an action); `_last_fire` pacing
  clocks in TWO scopes — per-task `(account, action)` and per-group
  `(group:<name>, action)`; account→group metadata; per-key waiter heaps +
  the shared wake event; fleet-wide and per-(account, action) cooldown
  clocks; **429 telemetry** (`hits_429` per account + fleet total — user
  accounts receive no `X-RateLimit` headers on success, so hits are the
  observable signal); grant/wait counters.
* **Key symbols**: `core.rate_limit_manager.engine.RateLimitManager`
  (`acquire`, `set_policy`/`set_policy_values`/`clear_policy`, `policies`,
  `effective`, `set_floor`, `set_account_group`, `report_server_limit`,
  `cooldown_remaining`, `stats`, `warnings`, `DEFAULT_ACTION_INTERVALS`), `.Grant`, `.Clock`/`.SystemClock`/
  `.ManualClock`; `core.rate_limit_manager.policies.RatePolicy`,
  `parse_scope`, `policy_from_row`, `MIN_ACTION_INTERVAL`;
  `core.rate_limit_manager.warnings.evaluate_policies`.
* **Events emitted**: `RateLimitTriggered` (action/wait_s/reason — awaited
  inline so sync subscribers see it before `acquire` returns).
  **Consumed**: none (fed by scheduler and account manager instead of the bus).
* **Persistence**: `database.repositories.rate_policies` (best-effort —
  "pacing safety never depends on the database"), `database.repositories.kv`
  (`ratelimit:floor`, `ratelimit:removed` tombstones).
* **Failure isolation**: structurally bad stored rows are logged and skipped
  at load; persistence failures degrade to in-memory-only; malformed
  `acquire` input raises precise `RatePolicyError` instead of crashing;
  logging failures are swallowed. Determinism seams: injected `clock`,
  `_sleep`, `jitter_seed`.
* **REMOVED**: the fleet-wide sequential send lane (`GlobalSendLane`) —
  redundant with sync groups, throttled single-account throughput. The
  `ratelimit lane` surfaces answer a compat stub. Retries are DELAY-FREE:
  the scheduler adds no backoff sleeps; re-entered `acquire` calls let
  the cooldown clocks own every delay.

### The lane — REMOVED (compat stub)

`GlobalSendLane` (FarmLite's sequential-send concept) was the stage-0
fleet-wide admission gate in front of `acquire()`. It is **removed**:
redundant with sync groups and a throttle on single-account throughput.
Per-account pacing + sync groups + the floor are the pacing layers; the
`ratelimit lane` CLI/dashboard surfaces answer
`{"enabled": false, "prefixrate": 0, "removed": true, ...}` and ignore
mutations. (`core/rate_limit_manager/lane.py` remains on disk as the
dormant implementation; the engine no longer calls it.)

## core.task_scheduler (task pipeline, A5)

* **Modules**: `core.task_scheduler.scheduler`, `.tasks`.
* **Purpose**: the ONE place outbound work is dispatched:
  `submit() → per-account worker lane (concurrency = 1) → limiter.acquire →
  sender(task) → audit + history + events` at the terminal transition.
  Cross-account entry into `acquire` is ordered by the shared `_GrantGate`
  (priority, then seq) so a late HIGH task overtakes an already-registered
  LOW one while per-account pacing stays independent. Retries are
  DELAY-FREE: they re-enter `limiter.acquire` immediately and the
  limiter's cooldown + interval own every delay (pacing is exclusively
  the limiter's job — no scheduler-owned backoff sleeps).
* **Owns**: the live task registry, per-account priority heaps + lane tasks
  + wake events, bounded finished-task memory tails (`_FINISHED_KEEP = 500`),
  terminal counters, the grant gate. `core.task_scheduler.tasks.Task` is the
  mutable lifecycle object (`queued → running → completed | failed |
  cancelled | timed_out`; `attempts` counts retries used; `sender_ran`
  guards audit integrity; `grant_wait_ms`/`grant_jitter_ms` carry the grant).
* **Key symbols**: `core.task_scheduler.scheduler.TaskScheduler`
  (`submit`, `cancel`, `cancel_matching`, `get`, `tasks`, `history`,
  `stats`, `start`, `stop`), the private `_GrantGate`;
  `core.task_scheduler.tasks.Task` + status constants.
* **Events emitted**: `TaskQueued`, `TaskStarted`, `TaskCompleted`,
  `TaskFailed` (carries `error_kind` = exception class name so the services
  layer can faithfully re-raise `FlowBlocked` subclasses across the ack
  seam), `TaskCancelled`. **Consumed**: none (drives the sender itself).
* **Persistence**: `database.repositories.tasks` (one row per terminal task,
  best-effort; `history()` prefers the repo, falls back to the memory tail).
* **Failure isolation**: workers swallow every sender exception into a task
  failure; retry classification — `ServerRateLimit` → report + retry (always
  ≥ 1 retry), `ConnectionError`/`TimeoutError`/`asyncio.TimeoutError` →
  retry with `min(retry_backoff·2^(attempts-1), max_backoff)`,
  `FlowBlocked` + everything else → fail immediately with the reason.
  `stop(drain=True)` never hangs (bounded drain then cancel sweep; every
  task left terminal). `start()` re-arms after `stop()` (the resume path).
  Outbound audit rows are written HERE (exactly once per task) — not in the
  send hook — with `wait_ms`/`jitter_ms` and attempts context.

## core.event_bus (event bus)

* **Module**: `core.event_bus`.
* **Purpose**: minimal async pub/sub, subclass-aware dispatch, error
  isolation; also the framework's typed event vocabulary (21 event classes).
* **Owns**: the `Event type → [handlers]` subscription table; strong refs
  for fire-and-forget publish tasks.
* **Key symbols**: `core.event_bus.EventBus` (`subscribe` returns an
  idempotent unsubscribe, `publish`, `publish_nowait`,
  `subscriber_count`); events: `Event` base (`account`, `ts`, `.name`),
  `AccountConnected`/`AccountDisconnected`/`AccountFailed`,
  `PluginLoaded`/`PluginUnloaded`/`PluginEnabledChanged`,
  `TaskQueued`/`TaskStarted`/`TaskCompleted`/`TaskFailed`/`TaskCancelled`,
  `RateLimitTriggered`, `SettingChanged`, `MessageReceived` (flat fields +
  rich `message: Any`), `MessageEdited`, `MessageDeleted`, `ReactionAdded`
  (no message fetch), `ScenarioStarted`/`ScenarioFinished`.
* **Persistence**: none.
* **Failure isolation**: per-handler try/except (exception objects are
  returned as results); in-handler `CancelledError` counts as handler
  failure unless the publish task itself is cancelled;
  `KeyboardInterrupt`/`SystemExit` always propagate; base-class subscribers
  receive subclass events.

## core.logging_service (logging)

* **Module**: `core.logging_service`.
* **Purpose**: every component records STRUCTURED entries
  (`ts, severity, source, account, plugin, task_id, event, message`) through
  one service instead of prints; consumed by the stderr mirror, the
  dashboard Logs page (live WS) and the bounded DB table.
* **Owns**: the ring buffer (capacity 4000), subscriber list, the optional
  DB sink, severity + failure counters, the thread-local ingest flag.
* **Key symbols**: `core.logging_service.LogService` (`record`, `info`/
  `warning`/`error`, `tail`, `subscribe`, `stats`, `attach_db`),
  `.LogEntry`, `.ServiceLogger` (stdlib-shaped adapter over the service —
  the seam that lets managers keep both logger conventions),
  `.StdlibBridge` + `attach_stdlib_bridge`/`detach_stdlib_bridge`,
  `configure_stderr`.
* **Events consumed**: everything in `core.bootstrap._LOGGED_EVENTS`
  (lifecycle events → structured INFO lines; `MessageReceived` is excluded
  as too chatty).
* **Persistence**: `database.repositories.logs` (best-effort, pruned).
* **Failure isolation**: `record()` never raises; DB/subscriber failures are
  *counted*, never logged (that fed the original recursion loop); the
  `_sm_from_service` marker + `ingest_context` thread-local make the
  stdlib mirror/bridge pair loop-proof.

## core.audit (audit trail)

* **Module**: `core.audit`.
* **Purpose**: "every Discord touch the framework makes" — direction `out`
  rows for every rate-limited send (content preview, `wait_ms`, `jitter_ms`),
  direction `in` rows for every dispatched message event. Review behavior
  BEFORE it triggers Discord's anti-abuse, plus a persistent record.
* **Owns**: the ring (capacity 2000), live subscribers, the optional DB sink.
* **Key symbols**: `core.audit.AuditRecorder` (`record`, `tail`,
  `history`, `subscribe`), `.AuditEntry`, `format_audit`.
* **Persistence**: `database.repositories.logs.AuditRepository` (pruned to
  `audit_keep`); `history()` prefers the DB and falls back to the ring.
* **Failure isolation**: `record()` never raises (DB write and every
  subscriber callback individually isolated); `tail(n<=0)` returns `[]`.

## core.credentials (credentials)

* **Module**: `core.credentials`.
* **Purpose**: every token and user/bot id lives in `.env`, never in code;
  secrets never enter the database; human-facing output is redacted.
* **Owns**: the parsed key/value map + source path (immutable-ish).
* **Key symbols**: `core.credentials.Credentials` (`from_file`,
  `from_values`, `parse_env`, `get`, `require`, `token_for`,
  `mudae_bot_id`, `owner_user_ids`, `dashboard_token`,
  `remote_control_enabled`, `redacted`), `.CredentialsError`.
* **Persistence**: none (by design — `token_for` resolves
  `DISCORD_TOKEN_<NAME>` at connect time).
* **Failure isolation**: missing file → empty credential set + warning;
  missing required keys raise `CredentialsError` naming the file; `__repr__`
  is redacted as a safety net.

## core.config (project configuration)

* **Module**: `core.config`.
* **Purpose**: loads the optional `config/` directory:
  `global_settings.json` (settings layer 2), `rate_limits.json` (policy
  format `{"global", "accounts", "lane"}`; `_comment`-style keys dropped),
  and `accounts/<name>.json` bootstrap files (no tokens — `.env` key refs
  only; duplicates and nameless files warned + skipped).
* **Owns**: nothing at runtime — `ProjectConfig` is a value object
  (`global_settings`, `policies`, `accounts`).
* **Key symbols**: `core.config.load_config`, `.ProjectConfig`,
  `DEFAULT_POLICIES` (conservative: message/dm 2.0 s + 0.0–0.5 s jitter; no
  lane key — lane defaults to disabled when the file is absent).
* **Failure isolation**: malformed JSON/shape fails LOUD (`ValueError` —
  typo protection at boot); a flat policy file is tolerated as global-level.

## database (persistence layer)

* **Modules**: `database.db`, `database.models`, `database.schema`,
  `database.repositories.*` (`account_plugins`, `accounts`, `base`, `cache`,
  `cooldowns`, `credentials`, `kv`, `logs` (Audit+Log), `plugin_profiles`,
  `plugins`, `profiles`, `rate_policies`, `settings`, `sync_groups`,
  `tasks`).
  `Database` exposes lazily-created repository properties
  (`accounts`, `account_settings`, `account_plugins`, `profiles`, `plugins`,
  `plugin_profiles`, `policies`, `audit`, `logs`, `tasks`, `cache`, `kv`,
  ...).
  `account_plugins` holds the DIRECT account→plugin assignments — the
  capability source, plus the `profile` binding column (migration v7);
  `plugin_profiles` holds the per-plugin named setting bundles; the
  `profiles`/`profile_plugins`/`account_profiles` trio is retained future
  infrastructure.
* **Purpose**: thread-safe SQLite persistence — one connection + one RLock
  shared by every repository; migrations run in the constructor; the file is
  created 0600 before first write; WAL on; `":memory:"` works fully.
* **Owns**: the sqlite3 connection, migration state, lazily-created repos.
* **Key symbols**: `database.db.Database` (`migrate`, `close`, and repo
  properties: `accounts`, `account_plugins`, `account_settings`, `profiles`,
  `sync_groups`, `plugins`, `policies`, `kv`, `cache`, `credentials`,
  `cooldowns`, plus `audit`/`logs`/`tasks`);
  `database.models.AccountRecord` (with `effective_token_ref`), `.ProfileRecord`,
  `.PluginMetadata`, `.PluginProfileRecord`, `.RatePolicyRecord`, `POLICY_LEVELS`;
  `database.schema.MIGRATIONS` (001 initial schema, 002 `api_cache`,
  003 credentials+cooldowns, 004 trail indexes, 005 sync groups,
  006 `account_plugins`, 007 `plugin_profiles` + the `profile` binding
  column — append-only, never edit an applied migration).
* **Failure isolation**: `core.errors.DatabaseError` with context; repos
  share the connection guard + error shaping of `BaseRepository`; secrets
  policy — token values never stored, only refs.

## adapters — the Discord boundary

### adapters.base (the contract)

* **Purpose**: "ONE interface, two implementations."
  `adapters.base.DiscordClient` is a `runtime_checkable` Protocol:
  lifecycle (`connect`/`disconnect`), messaging (`send_message`,
  `send_dm`, `open_dm`, `fetch_user`), capability actions (`add_reaction`,
  `click_button`), observability (`user_id`, `latency_ms`,
  `cached_snapshot` — in-memory only, NEVER a network fetch), and the
  callback slots (`on_message`, `on_disconnect`, optional `on_reaction`,
  `on_message_edit`, `on_message_delete`) assigned by the account manager
  BEFORE connect. `adapters.base.ClientFactory` builds a client per account.
  Re-exports the rich model so `from adapters.base import DiscordMessage`
  keeps working. Server 429s cross as `core.errors.ServerRateLimit`.

### adapters.model (the rich DTOs)

Covered in [ARCHITECTURE.md](ARCHITECTURE.md#the-rich-event-model-boundary-adaptersmodelpy):
frozen `DiscordMessage` + entity projections, pure converters
(`convert_message`, `convert_embed`, `convert_component`,
`convert_reaction`), the runtime-only `raw` escape hatch.

### adapters.discord (live client)

* **Module**: `adapters.discord.client`.
* **Purpose**: bridges the protocol to a live user-account client
  (`discord.py-self`), imported LAZILY inside `RealDiscordClient.connect` so
  tests/sims/CLI dry-runs never require the library.
* **Key symbols**: `adapters.discord.client.RealDiscordClient`,
  `.RealClientFactory` (falls back to the conventional
  `DISCORD_TOKEN_<NAME>` entry; raises `CredentialsError` when absent).
* **Behavior**: startup errors FAIL the connect (no silent `user_id=None`
  session); self-messages filtered upstream; every gateway event converted
  once via `convert_message`; HTTP errors translated to the domain
  vocabulary (`_map_send_error`: 429 → `ServerRateLimit` with
  Retry-After/X-RateLimit-Global intake, 403 → `AccessDenied`,
  404 → `TargetGone`); `send_typing` best-effort (unknown channel → skip,
  never fetch); `click_button` resolves the live message on demand and
  raises `TargetGone` for a vanished `custom_id`.

### adapters.simulator (offline half)

* **Modules**: `adapters.simulator.world`, `.client`, `.scenarios`,
  `.runner`.
* **Purpose**: a complete in-memory Discord world so the whole stack runs
  offline. `SimWorld` (users, channels, symmetric DM channel ids, per-channel
  history + an append-only journal that survives deletion, a scripted
  responder, one-shot 429 queue via `server_limit_next`, `channel_gone`,
  `wait_for_quiet` with real quiet-window semantics);
  `SimClient` (full protocol, idempotent connect/disconnect, `drop(reason)`
  for unexpected-loss tests, observability lists: `typing_calls`,
  `open_dm_calls`, `button_clicks`); `SCENARIOS` (7 registered scenarios);
  `SimulationRunner` (see below). All ids are synthetic constants — real ids
  live in `.env` only.

## core.remote_control (RETIRED mention-prefix bridge)

* **Module**: `core.remote_control`.
* **Purpose**: RETIRED from production — the bootstrap no longer wires
  it; mention-prefix commands ride the self-command layer
  (`core/plugin_manager/commands.py`, gated by `AccessManager`). The
  class survives only because the simulator stack constructs it for the
  `control_mention` scenario: owners manage the fleet FROM Discord by
  mentioning a running account (`@<account> <command>`); commands run
  through ControlAPI and
  replies are submitted at NORMAL priority through the full paced pipeline
  (`scheduler.submit(account, "control", "message", ...)`).
* **Owns**: the command allowlist (`help/status/accounts/plugins/ratelimit/
  lane/tasks/estop/simulation`), per-account reply cooldown state, the bus
  subscription.
* **Key symbols**: `core.remote_control.RemoteControl` (`start`, `stop`),
  `MAX_REPLY_CHARS = 1900`.
* **Events consumed**: `MessageReceived`. **Emitted**: none (its replies are
  tasks, not events).
* **Persistence**: none.
* **Failure isolation**: non-owner mentions ignored completely (no reply);
  per-account self-throttle (`reply_cooldown_s`); a broken command becomes
  an `error: ...` reply; the bridge never crashes on handler exceptions;
  each account answers only its OWN mention (`<@uid>` / `<@!uid>`).

## interfaces.cli (CLI surface)

* **Modules**: `interfaces.cli.commands` (shared engine, 38 functions),
  `interfaces.cli.repl`.
* **Purpose**: both CLI modes over one engine: `run_command(api, line)`
  shlex-parses and dispatches to ControlAPI; rendering happens in pure
  `format_*` helpers (unit-testable without a stack). `Repl` runs the
  `sm> ` loop and calls `api.shutdown()` on EVERY exit path;
  `run_once(api, argv)` runs one command without owning the lifecycle
  (`main.py` owns it).
* **Key symbols**: `interfaces.cli.repl.Repl`, `.run_once`, `render_error`;
  `interfaces.cli.commands.run_command`, `.help_text`, `.HELP`,
  `QuitRequested`, `UsageError`.
* **Failure isolation**: exactly one `error: ...` line per failure (type
  name kept only for non-domain errors); no tracebacks leak; unclosed
  quotes become a UsageError.

## interfaces.dashboard (browser surface)

* **Modules**: `interfaces.dashboard.server` (+ the no-build-step SPA in
  `static/index.html`), `interfaces.dashboard.ws` (minimal RFC 6455
  subset, text frames). SPA JS modules: `app.js` (nav/render
  dispatch/WS client), `views.js` (page templates incl. the 429 card),
  `accounts.js`, `groups.js`, `plugins-ui.js` (Plugins tab profile
  library + Profiles tab bindings), `admin-ui.js` (the Admin panel),
  `plugin-ui.js` (**plugin-contributed UI**: Home stat cards + sidebar
  panels, values esc()'d at render time), `api.js`, `state.js`, `ui.js`.
* **Purpose**: JSON REST mapping 1:1 to ControlAPI + a WebSocket pushing
  live stats (1 s broadcaster), log entries, audit entries and Task* events.
  Full route/WS reference: [API_ENDPOINTS.md](API_ENDPOINTS.md).
* **Threading model**: `ThreadingHTTPServer` on one daemon thread (capped
  at `MAX_CONCURRENT_CONNECTIONS = 64` handler threads — a socket flood
  is shed, never wedges healthy clients); each
  request (including accepted WebSockets) gets its own handler thread;
  route handlers hop to the MAIN asyncio loop via
  `asyncio.run_coroutine_threadsafe` (`DashboardServer.call`) — a stuck
  route surfaces as 504, never a hung thread.
* **Key symbols**: `interfaces.dashboard.server.DashboardServer`
  (`start`, `stop`, `attach_logs`, `attach_audit`, `bound_port`, `url`,
  `call`), `parse_json_bool` (strict booleans), field-validation helpers;
  `interfaces.dashboard.ws` (frame codec used by `WebSocketClient`).
* **Auth**: `X-Dashboard-Token` header / `?token=` when `DASHBOARD_TOKEN`
  is set; static index served without auth (the SPA prompts); binds
  127.0.0.1 by default; loopback Host + Origin checks defeat DNS
  rebinding and cross-site requests.
* **Notable documented gap**: ControlAPI exposes no event-bus accessor, so
  the server accepts `bus=` explicitly (bootstrap passes it) or probes
  `bus`/`event_bus`/`_bus` on the API object for task events.
* **Known latent bug**: `POST /api/admin/ids/set` and
  `POST /api/admin/perms/set` reference an undefined
  `require_json_list` helper → 500 (the SPA only uses the add/remove
  forms; see [DISCREPANCIES.md](DISCREPANCIES.md)).
* **Failure isolation**: 400/404/413/504/500 semantics pinned in the module
  docstring; body > 1 MB rejected after a bounded drain; error messages name
  the field and the route.

## adapters.simulator.runner (simulation runner)

* **Module**: `adapters.simulator.runner`.
* **Purpose**: a COMPLETE ephemeral framework per scenario run — the same
  pinned constructor wiring as production (`Database(":memory:")`,
  `EventBus`, `LogService` without a DB sink, `SettingsManager`,
  `RateLimitManager`, `AuditRecorder`, `TaskScheduler` with the SAME
  late-bound sender seam, `PluginManager` + discover, `ProfileManager`,
  `AccountManager` over a `SimClient` factory) plus a real `ControlAPI`
  and `RemoteControl` (the retired bridge — exercised by the
  `control_mention` scenario) so scenarios exercise Discord-side
  management. Produces `{"name", "passed", "duration_s", "failures",
  "log_lines", "interactions", "summary"}`.
* **Key symbols**: `adapters.simulator.runner.SimulationRunner`
  (`list`, `scenario`, `run`).
* **Events**: `ScenarioStarted`/`ScenarioFinished` published on the
  ephemeral bus; observability subscriptions attach BEFORE any traffic.
* **Persistence**: in-memory only; the DB closes in `finally`.
* **Failure isolation**: scenario scripts never propagate exceptions — a
  crash becomes a failure string; scenario policies are clamped to the
  floor; a shared stdlib bridge is never swapped mid-run (documented
  cross-leak instead); teardown failures become report failures, not raises.
