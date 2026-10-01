# SlaughterMudae — Integration Contract (READ FIRST)

This document is the **binding contract** between all modules of the rewrite.
Every module MUST implement exactly the public API pinned here. When something
is ambiguous, choose the **safest** option, document the decision in your
module docstring, and move on — do not block on questions.

Spec source: `TODO + Ideas.md` (architecture, phases, safety rules).

## Ground rules

- Python **>= 3.10**, **standard library only** (no pip deps anywhere).
- Repo root: `/home/Liboster/Workspace/SlaughterMudae`. All paths below are
  relative to it.
- Old implementation was battle-tested through 3 audit rounds. It is preserved
  in git history — consult it with `git show HEAD:<old-path>` (see each
  section). Port proven logic; do not reinvent.
- Every public function/class gets a docstring (what/why/raises).
- Domain errors carry **human-readable messages**; validate inputs at the
  boundary; never crash the process on bad input.
- **Never** touch files outside your deliverables (except your own tests).
- Secrets/tokens: values only ever live in `.env`; DB stores refs only.
- Safety floor: **no rate policy faster than 1.5s/action can ever be set**
  (`UnsafePolicyError`). Constant `MIN_ACTION_INTERVAL = 1.5` in
  `core/rate_limit_manager`.

## Target structure

```
core/                    framework (managers + shared contracts)
  __init__.py            (spine)
  api.py                 (spine)  CoreAPI — the ONE facade both interfaces call
  bootstrap.py           (lead)   composition root used by main.py
  event_bus.py           (spine)  EventBus + all Event types
  errors.py              (spine)  domain errors (FlowBlocked family etc.)
  priority.py            (spine)  Priority enum (shared by limiter+scheduler)
  logging_service.py     (spine)  structured LogService
  settings_manager.py    (spine)  Setting schema + SettingsManager
  account_manager/       (A6)     Account (first-class), AccountManager, DiscordCache
  profile_manager/       (A6)     ProfileManager (profiles/capabilities)
  plugin_base.py         (A3)     PluginBase, PluginContext, PluginServices protocol
  plugin_manager/        (A3)     discovery, per-account instances, dispatch
  rate_limit_manager/    (A4)     policies (4 levels), pacing engine, server limits, warnings
                          + lane.py GlobalSendLane (server-wide sequential sends)
  task_scheduler/        (A5)     queue/priority/cancel/retry/timeout/history
  audit.py               (A5)     AuditRecorder (interaction trail)
database/                (A2 + spine models/schema)
  models.py, schema.py   (spine)  dataclasses + SQL migrations
  db.py, repositories/   (A2)
adapters/
  base.py                (spine)  DiscordClient protocol + DiscordMessage
  discord/               (A7)     real client (lazy discord.py-self import)
  simulator/             (A7)     SimWorld, SimClient, scenarios, runner
interfaces/
  cli/                   (A1)     REPL + one-shot command groups
  dashboard/             (A8)     HTTP+WS server + SPA
plugins/                 (A3: soulcopy migrated + roll example)
tests/unit|integration|simulation/
config/  main.py  scripts/run_tests.sh
```

## Send pipeline (THE core dataflow)

```
Plugin -> PluginServices.send_message()
        -> TaskScheduler.submit(account, plugin, 'message', ...)      [TaskQueued]
        -> per-account worker lane (concurrency = 1 per account)
        -> RateLimitManager.acquire(action, account, plugin, prio)    [pacing sleep + jitter]
        -> AccountManager.send_hook(task)                             [cache validate]
        -> DiscordClient.send_message / send_dm                       [adapter]
        -> audit record (wait_ms, jitter_ms)                          [TaskCompleted]
```

- Scheduler retries ONLY on: `ConnectionError`, `TimeoutError`,
  `core.errors.ServerRateLimit` (reported to limiter first). `FlowBlocked`
  and everything else fail immediately with the reason.
- Server 429: adapter raises `ServerRateLimit(retry_after, global_)`;
  scheduler calls `limiter.report_server_limit(...)` then retries after
  backoff. The framework ALWAYS respects server-provided limits.

## Events (core/event_bus.py — already written, import from it)

`Event(ts)` base; concrete: `AccountConnected, AccountDisconnected(reason),
AccountFailed(error), PluginLoaded, PluginUnloaded, PluginEnabledChanged(enabled),
TaskQueued(task_id, plugin, action), TaskStarted(task_id, action),
TaskCompleted(task_id, duration_s, summary), TaskFailed(task_id, error),
TaskCancelled(task_id), RateLimitTriggered(action, wait_s, reason),
SettingChanged(scope, key, old_value, new_value),
MessageReceived(message_id, channel_id, author_id, content, dm),
ScenarioStarted(name), ScenarioFinished(name, passed, duration_s)`.
All carry `account: str` ("" when N/A).

## Pinned constructors & APIs

### core/logging_service (spine — done)
`LogService(capacity=4000)`; `.record(severity, source, message, *, account=None,
plugin=None, task_id=None, event=None) -> dict`; `.info/.warning/.error(source,
msg, **ctx)`; `.tail(n=100, *, account=None, plugin=None, severity=None,
source=None) -> list[dict]`; `.subscribe(cb) -> unsub`; `.attach_db(repo)`.
Severities: `"DEBUG"|"INFO"|"WARNING"|"ERROR"`.

### core/settings_manager (spine — done)
`Setting(name, type, default, description, min_value, max_value)` frozen;
`.validate(value)`; `.as_manifest()`. Types: `str|int|float|bool`.
`SettingsManager(bus, logger, *, plugin_repo=None, account_settings_repo=None,
global_layer=None)`; `.resolve(plugin_name, schema, account=None) -> dict`;
`.set_plugin_setting(account, plugin, key, value, schema) -> value`;
`.account_settings(account) -> dict`; `.set_account_setting(account, key, value)`.
Framework account schema: `ACCOUNT_SETTINGS` (reconnect_enabled,
reconnect_initial_delay, reconnect_max_delay, reconnect_multiplier).

### core/rate_limit_manager (A4 + GlobalSendLane)
```python
RatePolicy(action, interval, jitter_min=0.0, jitter_max=0.0)   # frozen; __str__ "2.00s +0.10..0.50s"
MIN_ACTION_INTERVAL = 1.5
class RateLimitManager:
    def __init__(self, bus, logger, policy_repo=None, *, floor=MIN_ACTION_INTERVAL,
                 clock=None, jitter_seed=None, lane_enabled=False,
                 lane_prefixrate=1.5, kv_repo=None): ...
    def set_policy(self, policy: RatePolicy, level="global", owner=None) -> RatePolicy
    def set_policy_values(self, action, interval, jitter_min=0.0, jitter_max=0.0,
                          level="global", owner=None) -> RatePolicy   # convenience used by ControlAPI
        # level in {"global","account","plugin","account_plugin"};
        # owner: None | account name | plugin name | "account/plugin"
        # validates: finite, jitter 0<=min<=max, interval >= floor (else UnsafePolicyError)
    def clear_policy(self, level, owner, action) -> bool
    def policies(self) -> list[dict]          # all stored, incl. level/owner
    def effective(self, action, account=None, plugin=None) -> dict
        # {"action","interval","jitter":[min,max],"sources":[{"level","owner","interval"}...]}
        # interval = MAX across applicable levels (never faster than ANY level demands)
    async def acquire(self, action, account=None, plugin=None, priority=Priority.NORMAL) -> Grant
        # Grant(action, account, plugin, wait_s, jitter_s, reason) dataclass
        # waits for: server cooldowns (account+global) THEN pacing (min interval since
        # last fire of (account,action)) THEN applies random jitter sleep.
        # Concurrent waiters granted in priority order (heap like old limiter).
        # Publishes RateLimitTriggered when wait_s > 0 (reason "pacing"|"server").
    def report_server_limit(self, account, action, retry_after: float, global_=False) -> None
        # account-scoped cooldown; global_=True ALSO cools down EVERY account (fleet-wide)
    def cooldown_remaining(self, account=None, action=None) -> float
    def stats(self) -> dict     # policies + cooldowns + last-fire ages + counters (UI page)
    def warnings(self) -> list[dict]   # [{"level","code","message"}] aggressive(<3s), no_jitter, narrow_jitter
    def set_floor(self, seconds)       # may only raise, never below 1.5
    # --- sequential send lane (FarmLite concept, robust port — lane.py) ---
    def set_lane_enabled(self, enabled: bool) -> dict      # persisted (kv 'ratelimit:lane')
    def set_lane_prefixrate(self, seconds: float) -> dict  # 0 PAUSES; persisted
    def lane_status(self) -> dict  # {"enabled","prefixrate","paused","waiting",
                                   #  "grants","last_grant_age_s","held_off_seconds"}
```
Port pacing/priority-waiter ideas from `git show HEAD:core/rate_limiter.py`
and server-limit ideas from `git show HEAD:core/rest_buckets.py` +
`git show HEAD:core/warnings.py`. Use injectable clock (`Clock` protocol,
`ManualClock`) like the old code so pacing is deterministically testable;
`jitter_seed` injectable too.

### core/task_scheduler (A5)
```python
TaskStatus = "queued|running|completed|failed|cancelled|timed_out"
@dataclass Task: id, name, account, plugin, action, channel_id, user_id, content,
    priority, submitted_at, started_at, finished_at, attempts, max_retries,
    timeout_s, status, error, result_summary, grant_wait_ms, grant_jitter_ms
    def as_dict(self) -> dict
class TaskScheduler:
    def __init__(self, bus, logger, limiter, sender, audit=None, *,
                 default_timeout=30.0, retry_backoff=0.5, max_backoff=30.0,
                 history_repo=None): ...
        # sender: async Callable[[Task], Any] — supplied by AccountManager
    def start(self) -> None                 # spawn dispatch loop (idempotent)
    async def stop(self, *, drain=True, timeout=10.0) -> None
    def submit(self, account, plugin, action, *, channel_id=None, user_id=None,
               content=None, priority=Priority.NORMAL, name=None,
               timeout=None, retries=0) -> Task      # raises SchedulerError when stopped
    def cancel(self, task_id) -> bool
    def cancel_matching(self, *, account=None, plugin=None, action=None) -> int
    def get(self, task_id) -> Task | None
    def tasks(self, *, status=None, account=None) -> list[dict]
    def history(self, limit=50, account=None) -> list[dict]   # DB when repo present
    def stats(self) -> dict   # {"queued":n,"running":n,"completed":n,"failed":n,
                              #  "cancelled":n,"avg_duration_s":x,"workers":n}
```
Semantics: **one worker per account** (lazy spawn; concurrency=1 per account =
never two in-flight requests for one account — port lane ideas from
`git show HEAD:core/send_engine.py`), cross-account fairness by priority.
Timeout via `asyncio.wait_for`. Retry backoff `retry_backoff * 2**attempt`
capped at `max_backoff`. Persist finished tasks to `history_repo`
(`tasks.add` / see database section). Before calling sender, run
`limiter.acquire(...)` and set `task.grant_wait_ms/grant_jitter_ms`.
On `ServerRateLimit`: `limiter.report_server_limit(task.account, task.action,
exc.retry_after, exc.global_)`, retry (counts as attempt). Emit
Task* events throughout. Old refs: `git show HEAD:core/scheduler.py`.

### core/audit.py (A5)
`AuditRecorder(db_repo=None, capacity=2000)`; `.record(account, direction,
action, channel_id=None, content="", wait_ms=0.0, jitter_ms=0.0, note="") ->
dict`; `.tail(n=50, account=None)`; `.history(limit=200, account=None)`;
`.subscribe(cb)`. Port from `git show HEAD:core/audit.py` (keep the
best-effort DB write + subscriber isolation).

### core/plugin_base.py + core/plugin_manager (A3)
```python
class PluginBase(abc.ABC):
    name: ClassVar[str]; version: ClassVar[str]; description: ClassVar[str]
    settings: ClassVar[Sequence[Setting]] = ()
    async def on_load/on_enable/on_disable/on_unload(self, ctx): ...
    async def handle_event(self, ctx, event): ...
@dataclass PluginContext: account: str, plugin: str, settings: dict,
    services: "PluginServices", logger: logging.Logger
class PluginServices(Protocol):        # implemented by account_manager (A6)
    async def send_message(self, channel_id, content, *, priority=Priority.LOW) -> DiscordMessage
    async def send_dm(self, user_id, content, *, priority=Priority.LOW) -> DiscordMessage
    async def wait_for_message(self, check, timeout) -> MessageReceived | None
    async def collect_messages(self, check, idle_timeout) -> list[MessageReceived]
    def publish(self, event: Event) -> None
    def validate_channel(self, channel_id) -> None      # raises TargetGone
    def describe_channel(self, channel_id) -> str
    @property credentials, account, plugin, settings
class PluginManager:
    def __init__(self, bus, logger, settings: SettingsManager, plugin_repo=None,
                 plugin_dir="plugins"): ...
    def discover(self) -> dict                          # name -> record (port old loader)
    def manifests(self) -> list[dict]                   # metadata + settings schema (UI)
    def attach_account(self, account, allowed: set | None) -> None
        # instantiate per-account plugins (allowed=None => all discovered)
    def detach_account(self, account) -> None
    def activate_account(self, account, services: PluginServices) -> None
        # resolve settings, fire on_load+on_enable for enabled plugins (repo state)
    def deactivate_account(self, account) -> None       # on_disable + on_unload
    async def set_enabled(self, account, plugin, enabled: bool) -> None
    def set_setting(self, account, plugin, key, value) -> dict   # via SettingsManager
    def states(self, account) -> list[dict]             # [{name,enabled,settings,last_error,error_count}]
```
Internally subscribes to `MessageReceived` once and routes to the owning
account's enabled plugin instances (error-isolated, per-plugin error counters
visible in `states()`). Port loader from `git show HEAD:core/plugin_loader.py`
(strict name validation, duplicate detection, synthetic module names) and
plugin_base from `git show HEAD:core/plugin_base.py`.

Plugins to deliver (A3):
- `plugins/soulcopy/` — migrate from `git show HEAD:plugins/soulcopy/plugin.py`
  and `git show HEAD:plugins/soulcopy/textutils.py` (+ `__init__.py`). Same
  behavior/flow-safety, but sends go through `ctx.services.send_message /
  send_dm` (task pipeline) and collection through
  `ctx.services.collect_messages`. Keep `FlowBlocked` catch-all contract.
- `plugins/roll/` — small example: owner trigger word (setting, default
  `"roll"`) sends a configurable command (setting `command`, default `"$m"`)
  to the triggering channel via `services.send_message`. Demonstrates
  plugin-level rate policies.

### core/profile_manager (A6)
```python
class ProfileManager:
    def __init__(self, profiles_repo, logger, accounts_repo=None): ...
    def set_profile(self, name, plugins, description="") -> dict   # persists
    def delete_profile(self, name) -> dict
    def profiles(self) -> list[dict]        # [{"name","description","plugins":[...]}]
    def assign(self, account, profile) -> dict    # validates profile AND account exist
    def unassign(self, account, profile) -> dict
    def assigned(self, account) -> list[str]
    def allowed_plugins(self, account) -> set | None   # union; None = unrestricted
```
Port semantics from `git show HEAD:core/roles.py` (name regex, non-empty
plugin list, unknown-role warning behavior).

### core/account_manager (A6)
```python
class AccountStatus(str, Enum): created connecting running reconnecting stopped failed
@dataclass Account:  # first-class object: persistent vs runtime split (spec §1)
    record: AccountRecord          # persistent (database/models.py)
    runtime: AccountRuntime        # client, status, cache, started_at, stats, last_error
    plugins: dict                  # plugin name -> runtime info
class AccountManager:
    def __init__(self, bus, logger, accounts_repo, plugin_manager, profile_manager,
                 scheduler, settings: SettingsManager, audit, client_factory,
                 credentials, *, supervisor_tick=0.2): ...
    def add_account(self, name, *, token_ref=None, enabled=True, sync_group=None,
                    profiles=None) -> dict          # validates + persists
    def remove_account(self, name) -> dict          # must be stopped; cascades
    def get(self, name) -> Account                  # AccountError when unknown
    def accounts(self) -> list[dict]                # status list (names + ids + roles)
    def set_enabled(self, name, enabled) -> dict
    async def start(self, name) -> dict ; async def stop(self, name) -> dict
    async def start_all(self) -> dict ; async def stop_all(self) -> dict
    def emergency_stop(self, name) -> dict          # cancel tasks + stop account NOW
    def details(self, name) -> dict                 # spec §8 page (see below)
    async def send_hook(self, task: Task) -> DiscordMessage   # scheduler sender
    def names(self) -> list[str]
```
- `details(name)` returns EXACTLY these sections (spec §8):
  `{"name", "status": {"connected": bool, "state": str, "latency_ms",
  "uptime_s"}, "identity": {"name", "discord_id"}, "runtime": {"cached_guilds",
  "cached_channels", "cached_dms", "cache_source": "client cache (READY)"},
  "plugins": [{"name","enabled"}...], "profiles": [...],
  "rate_limit": {action: {"interval","jitter","source":"inherited from global"|...}}}`
  Everything runtime-y comes ONLY from the live client's cached state
  (`client.cached_snapshot()`); mark it clearly.
- `start()`: client = `client_factory(name, token)`; connect; ingest
  `cached_snapshot()` into `runtime.cache`; publish `AccountConnected`;
  `plugin_manager.activate_account(name, services)` with allowed plugins from
  profiles; start reconnect supervisor (policy from
  `settings.account_settings(name)`; exponential backoff; port from
  `git show HEAD:core/container.py` supervise logic).
- `send_hook`: resolve runtime; not running -> `ConnectionError("account ...
  is not running")` (retryable-classified but will fail out); `action=="dm"` ->
  cache-first `open_dm` then send; guild message -> `cache.validate_channel`
  (TargetGone propagates, no retry); audit `direction="out"` with
  `task.grant_wait_ms/grant_jitter_ms`.
- Inbound audit: subscribe `MessageReceived` -> `audit.record(..., "in", ...)`.
- `DiscordCache` in `core/account_manager/cache.py`: port from
  `git show HEAD:core/discord_cache.py` (ingest_snapshot, validate_channel,
  describe, invalidate_channel, snapshot).
- `PluginServices` implementation (`AccountPluginServices`) lives in
  `core/account_manager/services.py` and implements the pinned protocol:
  sends via `scheduler.submit(...)` awaited to completion; wait/collect via
  bus subscriptions (port `collect_messages` idle-timeout semantics from
  `git show HEAD:core/container.py`); exposes credentials + cache helpers.
  Priority default for plugin sends: `Priority.LOW`.

### adapters (A7)
`adapters/base.py` (spine — done): `DiscordMessage`, `DiscordClient` protocol
(connect/disconnect/send_message/send_dm/open_dm/fetch_user/cached_snapshot/
latency_ms/user_id/on_message/on_disconnect), `ClientFactory` protocol
`__call__(name: str, token: str) -> DiscordClient`.

- `adapters/discord/client.py` — `RealDiscordClient(token)`: lazy
  `import discord` (discord.py-self; 2.x has no Intents — plain
  `discord.Client()`, fall back to `Client(intents=None)` on TypeError).
  Port from `git show HEAD:core/discord_adapter.py` (startup-error surfacing,
  user_id check, cached_snapshot from client cache, DMChannel detection).
  Convert library `HTTPException` with `status == 429` into
  `core.errors.ServerRateLimit(retry_after, global_)` (retry_after from
  `exc.response.headers.get("Retry-After")` when available, else 1.0).
  `latency_ms` from `client.latency * 1000` (None-safe).
  `adapters/discord/__init__.py` exports `RealDiscordClient` +
  `RealClientFactory` (resolves token via `credentials.get(record token ref)`
  — factory signature `(name, token)` per base.py).
- `adapters/simulator/` — port `git show HEAD:core/simulator.py`:
  `world.py` (`SimWorld`: users/channels/DM pairs/history/deliver/subscribers,
  deterministic DM channel ids, configurable scripted responder with pages +
  reply delay; PLUS `server_limit_next(channel_id, retry_after, global_=False)`
  queueing a one-shot 429 on the next send, and `channel_gone(channel_id)`
  simulating deletion), `client.py` (`SimClient`: protocol impl, synthetic
  `latency_ms` 40-90, `drop(reason)` disconnects, `delete_channel`,
  raises `ServerRateLimit`/`KeyError` per world state, per-account DM
  filtering in cached_snapshot).
  `scenarios.py`: registry `SCENARIOS: dict[str, Scenario]`;
  `Scenario` dataclass(name, description, policies: dict[action, RatePolicy],
  world_setup(world), script(api) -> awaitable, checks(audit, world) -> list[str])
  with at least: `soulcopy_basic`, `soulcopy_multichunk`, `pacing_respected`
  (tight-ish policy -> every outbound gap >= interval), `server_429_respected`
  (simulated 429 -> retry happens AFTER retry_after), `channel_gone_aborts`
  (target channel deleted -> flow aborts cleanly, no partial chunks).
  `runner.py`: `SimulationRunner(plugin_dir, db=None)` builds a COMPLETE
  in-memory stack (Database(":memory:"), bus, LogService, RateLimitManager,
  TaskScheduler, SettingsManager, ProfileManager, PluginManager,
  AccountManager with SimClient factory, AuditRecorder) per run — wiring per
  the pinned constructors; `run(name) -> report dict {name, passed,
  duration_s, failures: [str], log_lines: ["00:02.017 INFO scheduler Task 3
  completed"...], interactions: [audit dicts], summary: str}`; `list() ->
  [{name, description}]`. Log lines are relative-timestamped structured
  entries from LogService (spec §9 example format).

### interfaces/cli (A1)
- `interfaces/cli/commands.py`: `run_command(api, line) -> str | None`
  (raises `QuitRequested`, `UsageError`), `HELP` registry, pure `format_*`
  helpers. Command groups (spec §13 — subcommand style):
  `accounts list|add <name> [profiles...]|remove <name>|info <name>|enable|
  disable <name>|start [name|all]|stop [name|all]|estop <name>`,
  `plugins list|state <account>|enable <acct> <plugin>|disable <acct> <plugin>|
  settings <account>|set <account> <plugin> <key> <value>`,
  `profiles list|create <name> <plugin...>|delete <name>|assign <acct> <profile>|
  unassign <acct> <profile>`,
  `ratelimit show|set <action> <interval> [jitter_min jitter_max] [level owner]|
  clear <level> <owner> <action>|effective <action> [account] [plugin]|warnings`,
  `tasks list [status]|cancel <id>|history [n]`,
  `simulation list|run [name]`, `logs [n] [account]`, `audit [n] [account]`,
  `estop` (global emergency stop), `status`, `help`, `quit`.
  Strict arg validation with usage messages; strict bool parsing
  (`parse_bool` from core.api). Port command-engine patterns from
  `git show HEAD:cli/commands.py`.
- `interfaces/cli/repl.py`: `Repl(api)` with injectable stdin/stdout
  (`git show HEAD:cli/terminal.py` for the every-exit-path stop + error
  rendering); `run_once(api, argv) -> int` for one-shot mode.
- Both call ONLY `core.api.ControlAPI` methods.

### interfaces/dashboard (A8)
- `interfaces/dashboard/server.py`: stdlib `ThreadingHTTPServer` +
  `DashboardServer(api, token=None, host="127.0.0.1", port=8070)` with
  `.start()/.stop()/.attach_logs(log_service)`, JSON REST mapping 1:1 to
  CoreAPI (`/api/status`, `/api/accounts`, `/api/accounts/add|remove|info|
  enable|start|stop|emergency-stop`, `/api/plugins`, `/api/plugins/state|
  set-enabled|settings|set-setting`, `/api/profiles`, `/api/profiles/create|
  delete|assign|unassign`, `/api/ratelimit/policies|set|clear|effective|
  stats|warnings`, `/api/tasks`, `/api/tasks/cancel|cancel-matching|history`,
  `/api/simulation/list|run`, `/api/logs`, `/api/audit`, `/api/estop`,
  `/api/shutdown`). Strict bool parsing (`parse_json_bool`), field guards,
  body-size caps, 400/401/404/413/500/504 semantics — port wholesale from
  `git show HEAD:dashboard/server.py` (keep auth via `X-Dashboard-Token` or
  `?token=`).
- `interfaces/dashboard/ws.py`: port `git show HEAD:dashboard/ws.py` verbatim
  (RFC 6455 subset with the protocol-violation hardening).
- `interfaces/dashboard/static/index.html`: SPA with a **sidebar navigation**
  exactly per spec §12: Overview, Accounts (list + details modal per §8),
  Plugins (installed + auto-generated settings editors from the schema),
  Profiles (list + assignments), Rate Limits (global + per-account editors +
  visualization of stats/warnings), Tasks (running/queued/history + cancel),
  Logs (live structured tail), Simulation (scenario list, run button,
  results incl. timed log lines), Advanced (DB info, floor, emergency stop).
  Live WS push: `{"type":"stats",...}` every second, `{"type":"log", entry}`,
  `{"type":"audit", entry}`, `{"type":"task", entry}` on task events
  (subscribe the bus). Port the proven SPA skeleton (esc helpers, error
  accrual, token box, ws retry cap) from
  `git show HEAD:dashboard/static/index.html`.

## Database (A2 implements; models+schema in spine)

`database/db.py`: `Database(path="state/slaughter.db", *, audit_keep=10000,
log_keep=10000)` — thread-safe (RLock), 0600 file perms, WAL, `.migrate()`
runs pending migrations from `database/schema.py:MIGRATIONS` using a
`schema_migrations` table, `.close()`, and repository properties:
`.accounts .account_settings .profiles .plugins .policies .audit .logs .tasks .kv`.

Repository methods (exact — other modules call these):
```python
AccountsRepository:
    add(name, *, token_ref=None, enabled=True, sync_group=None) -> AccountRecord
    get(name) -> AccountRecord | None ; list() -> list[AccountRecord]
    set_enabled(name, enabled) ; set_settings(name, settings: dict)
    delete(name) -> bool        # cascades settings/plugin_settings/states/profiles
    assign_profile(name, profile) ; unassign_profile(name, profile)
    profiles_of(name) -> list[str]
AccountSettingsRepository: get(name) -> dict ; set(name, key, value) ; delete(name, key)
ProfilesRepository: upsert(name, plugins, description="") ; delete(name) -> bool
    get(name) -> ProfileRecord | None ; list() -> list[ProfileRecord]
PluginRepository:
    upsert_metadata(name, version, description, settings_manifest) 
    metadata() -> list[dict] ; metadata_for(name) -> dict | None
    state(account, plugin) -> bool | None ; set_state(account, plugin, enabled)
    setting(account, plugin, key) -> value | None
    settings(account, plugin) -> dict ; set_setting(account, plugin, key, value)
    all_account_settings() -> dict[account][plugin][key] = value
RatePolicyRepository: get(level, owner, action) -> RatePolicyRecord | None
    set(level, owner, action, interval, jitter_min, jitter_max)
    delete(level, owner, action) -> bool ; list() -> list[RatePolicyRecord]
AuditRepository: add(entry: dict) ; recent(limit=100, account=None) -> list[dict]
LogRepository: add(entry: dict) ; recent(limit=100, *, account=None, severity=None,
    source=None) -> list[dict]
TaskHistoryRepository: add(entry: dict) -> int ; update(task_id, **fields)
    recent(limit=50, account=None) -> list[dict]
KvRepository: get(key, default=None) ; set(key, value) ; delete(key) ; prefix(p) -> dict
```
`Database(":memory:")` must work (simulations). Port robustness from
`git show HEAD:core/database.py` (pruning, chmod, thread-safety).

## CoreAPI (spine — done, in core/api.py)

Pure facade, duck-typed over the managers (no runtime imports of manager
modules). CLI + dashboard call ONLY this. If you need a method it doesn't
have, note it in your module docstring — do NOT edit core/api.py.

## Tests

- Each agent writes `tests/unit/test_<module>.py` (or several) for its own
  modules; they must pass via
  `python3 -m unittest tests.unit.test_<module> -v` from repo root.
  `tests/__init__.py`, `tests/unit/__init__.py` etc. already exist.
- Use `tests/unit/helpers.py` for shared fixtures (any agent may CREATE it if
  missing; if it exists, extend — coordinate via the file itself).
- Determinism: inject ManualClock/seeds where the module supports it.
- Do NOT run the whole suite (other modules may not exist yet) — run only
  your own tests.
