# The Account System

> One `Account` per Discord user account: registry, lifecycle, cache, direct
> plugin assignments, profiles (retained), supervisor,
> emergency stops. Source of truth: `core/account_manager/{manager,cache,services}.py`,
> `database/models.py`, `core/settings_manager.py`,
> `core/api.py`, `core/rate_limit_manager/engine.py`.
> Plugin-side view: [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) · [PLUGIN_API.md](PLUGIN_API.md) ·
> Adapters: [ADAPTERS.md](ADAPTERS.md).

---

## 1. Persistent vs runtime split

```
Account (core.account_manager.manager.Account)
├── record: AccountRecord      # PERSISTENT (database/models.py) — survives restarts
└── runtime: AccountRuntime    # RUNTIME — never persisted
    └── cache: DiscordCache    # one per account, for its whole life
```

### 1.1 `database.models.AccountRecord` (persistent)

| Field | Type | Meaning |
|---|---|---|
| `id` | `int` | database row id |
| `name` | `str` | account slug, must match `^[a-z0-9][a-z0-9_-]{0,31}$` (`AccountManager.NAME_RE`) |
| `token_ref` | `Optional[str]` | LEGACY **.env key** holding the token (`effective_token_ref()` falls back to `DISCORD_TOKEN_<NAME>`). The account's actual credential now lives encrypted in the `credentials` table (§1.3); `token_ref` only matters when no DB credential exists |
| `enabled` | `bool` | `False` = skipped by `start_all` |
| `sync_group` | `Optional[str]` | accounts that act in sync (rate scope, §8) |
| `settings` | `Dict[str, Any]` | framework account settings layer |
| `created_at` | `float` | wall clock |

### 1.3 Credential storage (the `credentials` table)

Per-account tokens are stored **encrypted at rest** (Fernet ciphertext;
key outside the DB — `LZ_MASTER_KEY` env or `state/credentials.key`
chmod 0600) by `core.credential_service.CredentialService`. Resolution
precedence on start (spec §17):

| State (`credential_source`) | Meaning | Where the value lives |
|---|---|---|
| `db` | stored credential exists | `credentials` table (ciphertext; decrypted in memory only while running) |
| `env` | no DB credential, legacy `.env` key present | `.env` (`DISCORD_TOKEN_<NAME>` / `token_ref`) — the fallback NEVER overwrites the DB; converting is the explicit one-shot `credentials import` |
| `missing` | neither | start fails with `MISSING_CREDENTIAL` — the fix is storing a credential, not restarting |

Related account statuses (set by `_start_failed` classification):

| Status | Meaning |
|---|---|
| `missing_credential` | nothing to connect with (`CredentialsError` at resolve) — fix by storing a credential (`accounts set-credential`) |
| `invalid_credential` | the token was **rejected by Discord** at login (auth-shaped failure text) — the token itself must be replaced |

`AccountManager` wires the optional `credential_service`; when present,
`status_dict()` carries `credential` = the state above (display-safe, no
value). Storing a credential performs **no network activity** — saving is
always separate from connecting (spec §16).

### 1.2 `core.account_manager.manager.AccountRuntime` (runtime)

| Field | Type | Meaning |
|---|---|---|
| `status` | `AccountStatus` | `CREATED` → `CONNECTING` → `RUNNING` → `RECONNECTING`/`STOPPED`/`FAILED` |
| `client` | `Any` | the live `DiscordClient` adapter (or `None`) |
| `cache` | `Optional[DiscordCache]` | the account's world view (§3) |
| `started_at` | `Optional[float]` | `time.monotonic()` — uptime is wall-clock-jump safe; reset on reconnect (uptime = *current* connection) |
| `last_error` | `Optional[str]` | human-readable last failure |
| `user_id` | `Optional[int]` | logged-in Discord user id |
| `supervisor` | `Optional[asyncio.Task]` | the reconnect watcher (`supervise-<name>`) |
| `stop_requested` | `bool` | deliberate-stop flag (suppresses reconnects) |
| `lost` | `asyncio.Event` | set by the disconnect callback on **unexpected** drops; the supervisor waits on it |

`Account.status_dict()` is the JSON-safe snapshot for the accounts list (id/name/status/user_id/
enabled/sync_group/profiles (legacy, informational)/assigned_plugins/plugin_profiles
(`{plugin: profile or None}` — §4.3)/latency_ms/uptime_s/
last_error/plugins). `Account.plugins` caches the
last known per-plugin `{name: {enabled, last_error, error_count}}` projection, refreshed from
`PluginManager.states()` whenever a snapshot is built.

The account manager also records the account's connected Discord user
id into its access row (`AccessManager.set_account_user_id`) so the
admin panel's admin-flag bypass can gate on the REAL user id of an
admin-flagged account (see §10).

---

## 2. Account lifecycle

### 2.1 Registration

* `AccountManager.add_account(name, *, token_ref=None, enabled=True, sync_group=None,
  plugins=None)` — validates the name (`AccountError` on mismatch/duplicate), **persists first**
  (a failed write leaves memory untouched), builds the `Account`, stores the DIRECT plugin
  assignments (`plugins` → `account_plugins` rows — the capability source), attaches exactly
  those plugins, registers the rate group. Passing the legacy `profiles=` kwarg raises an
  explanatory `ValueError` (profiles no longer control capabilities).
  Credentials resolve per §1.3 (DB ciphertext → legacy `.env` fallback → `CredentialsError`
  naming the key when missing); an `accounts_set_credential`/add-payload `credential` string is
  stored ENCRYPTED via the CredentialService (no network activity — saving is separate from
  connecting).
* `remove_account(name)` — refuses (`AccountError`) while running/connecting/reconnecting; then
  detaches plugins, drops services, cancels the supervisor, deregisters the group, deletes the row
  (and its credential + cooldowns via the service-layer cascades).
* `set_enabled(name, enabled)` — flips the persisted flag (persist first); disabled accounts are
  skipped by `start_all`.
* Boot path `load()` (async) rebuilds `Account` objects (and attaches their plugins) from the
  repository, best-effort per account.

### 2.2 `start(name)`

1. Refuse if already running/connecting/reconnecting (`AccountError`); clear `lost`/`last_error`;
   status `CONNECTING`.
2. `_connect_client`: resolve the token → `client_factory(name, token)` → **wire callbacks before
   connect** (`on_message`, `on_disconnect`, `on_reaction`, `on_message_edit`, `on_message_delete`)
   → `await client.connect()` → read `user_id` → **ingest the client's cached snapshot into the
   DiscordCache**.
3. Failures are CLASSIFIED (spec): `CredentialsError` at token resolution → status
   `MISSING_CREDENTIAL` (fix = store a credential, do not retry-loop); an auth-shaped login
   failure → `INVALID_CREDENTIAL` (the token was rejected — replace it); anything else →
   `FAILED`. In every case `last_error` is set and `AccountFailed` published;
   `AccountError`/`CredentialsError` re-raise. `KeyboardInterrupt`/`SystemExit` never
   swallowed. A `stop()` that raced mid-connect is honoured (status `STOPPED`, no supervisor).
4. Status `RUNNING`, `started_at = time.monotonic()`.
5. **Re-attach plugins with the current direct assignments** (`account_plugins`
   rows — profiles are not consulted), then
   `activate_account(name, services_factory)` — a plugin-activation failure is logged, the account
   keeps running.
6. Publish `AccountConnected`.
7. Spawn the reconnect supervisor when the account's `reconnect_enabled` setting is true.

### 2.3 `stop(name)` (idempotent; pinned order)

1. Already `STOPPED` → return the snapshot.
2. `stop_requested = True`; `lost.set()` (wake the supervisor now).
3. **Deactivate plugins first** — hooks still see the cache and the client.
4. `await client.disconnect()` (None-safe).
5. Reap the supervisor: bounded 1s wait, then cancel + bounded reap.
6. Status `STOPPED`; drop the account's per-plugin services; publish
   `AccountDisconnected(reason="stopped")` when the account was live.

`start_all()` / `stop_all()` iterate sorted names, log-and-continue per account (returning
started/failed/skipped and stopped/failed lists).

### 2.4 Callback wiring (the event source)

`_wire_client` installs five callbacks **before** connect:

| Callback | Bus event published |
|---|---|
| `on_message(DiscordMessage)` | `MessageReceived(account, message_id, channel_id, author_id, content, dm, message=<rich DTO>)` |
| `on_reaction(kind, message_id, channel_id, guild_id, user_id, emoji, raw)` | `ReactionAdded(...)` — only `kind == "add"`; removal reserved for future plugins; no message fetch |
| `on_message_edit(DiscordMessage)` | `MessageEdited(account, message_id, channel_id, guild_id, message=<rich post-edit DTO>)` |
| `on_message_delete(message_id, channel_id, guild_id, raw)` | `MessageDeleted(account, message_id, channel_id, guild_id)` |
| `on_disconnect(reason)` | none directly — sets `RECONNECTING` + `lost` **unless** `stop_requested` (deliberate stops ignore it) |

Every inbound `MessageReceived` additionally produces an **inbound audit row** (`_audit_inbound`:
direction `"in"`, action `"message"`, 80-char content preview, note `author=<id>[ dm][ embeds=N
components=M attachments=K]` — metadata, never payloads).

---

## 3. `DiscordCache` — the per-account world view (`core.account_manager.cache`)

Principle: *interact with Discord in real time, cache channels and guilds where needed, RARELY
use the fetch API.*

### 3.1 Ingestion (READY)

`DiscordCache.ingest_snapshot(snapshot)` replaces/merges contents from the client's
`cached_snapshot()` — **zero fetches**, only what the client already holds post-READY: our user,
guilds + their channels, open DM channels. DM merge rule: a guild channel id can never be a DM
channel, so a snapshot DM colliding with a known guild channel keeps the guild view intact and
only records the recipient. `ready` flips to `True`.

### 3.2 The `TargetGone` contract

`validate_channel(channel_id) -> ChannelInfo` (alias of `channel()`): raises
`core.errors.TargetGone(account, reason)` when the channel is unknown or deleted — this is the
central hook of the flow-abort contract. Callers (`AccountManager.send_hook`, plugin flows via
`services.validate_channel`) let the `FlowBlocked` subclass propagate with the reason;
`describe(channel_id)` is the never-raising label twin (`#name in guild` / `dm with <user>` /
`channel-<id>` fallback).

`invalidate_channel(channel_id, reason)` marks a channel gone, notifies listeners
(`on_invalidate(callback) -> unsubscribe`, used by manager/dashboard) and logs. `snapshot()` is
the JSON-safe summary (ready/user/guilds/channels_total/dms/invalidated).

### 3.3 DM resolution — three layers, one client call ever

`resolve_dm_channel(cache, client, user_id, account, cache_repo)` (shared by the send hook and
`AccountPluginServices.open_dm`):

1. **live memory cache** — `DiscordCache.dm_channel_for(user_id)` (from READY or a prior open),
2. **persistent api_cache** — the account-scoped `dm` row (survives restarts; a hit is promoted
   into memory via `record_dm`),
3. **`await client.open_dm(user_id)`** — the one REST call; the result is recorded in BOTH caches
   so it is never fetched again for the account's lifetime.

Full miss with no live client → `AccountError`. `invalidate_dm_channel(cache, repo, user_id,
account)` drops both layers when Discord reports the target gone (called by the send hook when a
DM send raises `TargetGone`) — the next resolution re-opens instead of hammering a dead id.
`record_dm` re-opening a previously invalidated DM clears the deleted flag (the channel was just
proven usable). `dms()` counts live DM channels (details-page metric).

### 3.4 Anti-flag discipline in the send path

The account manager **never** initiates a Discord API call for its own purposes — the complete
client-touch inventory (all demand-driven): `connect/disconnect` (lifecycle),
`cached_snapshot()` (memory-only read), `send_hook` → `client.send_message` (executes a
plugin-submitted task), `resolve_dm_channel` → `client.open_dm` (DM-send cache miss only),
`services.fetch_user` → `client.fetch_user` (plugin-demanded, DB-cached), and
`_maybe_typing` → `client.send_typing` (the one opt-in decoration, `typing_probability`
default `0.0` = never). No prefetching, no "just in case" fetches, no API-derived state beyond
what a live flow consumed; `details()` reads exclusively from the ingested snapshot + repos.

### 3.5 The send hook (`AccountManager.send_hook(task)`)

One task → one validated, audited send, dispatched by the scheduler's per-account lane:

| Action | Path |
|---|---|
| `"message"` | `cache.validate_channel` → optional typing → `client.send_message(channel_id, content)` |
| `"dm"` | `resolve_dm_channel` (3-layer) → `client.send_message(resolved_channel_id, content)` — deliberately **not** `client.send_dm(user_id)`, which would re-fetch the user + re-open the DM (2 redundant REST calls per send, a classic selfbot flag vector); on `TargetGone` the DM mapping is invalidated and the error re-raised |
| `"reaction"` | validate channel → `client.add_reaction(channel_id, message_id, emoji)` |
| `"button"` | validate channel → `client.click_button(channel_id, message_id, custom_id)` (adapter resolves the live message on demand) |

Raises: `AccountError` (unknown account, no retry), `ConnectionError` (account not running —
scheduler-retryable, resolves when the supervisor reconnects), `TargetGone` (no retry — the flow
is dead), `SchedulerError` (unsupported action). Outbound **audit** is written by the scheduler at
task-terminal (exactly-once, with attempts context) — recording here too would duplicate every
send.

---

## 4. Capability model & profiles

### 4.1 Current (implemented): direct `Account → Plugins` assignment

* The capability source of truth is the **`account_plugins` table**
  (`database.repositories.account_plugins.AccountPluginsRepository`): one
  row means "this account runs this plugin".
* `Account.assigned_plugins` is the in-memory mirror — bulk-loaded at boot,
  rewritten on every assignment mutation, read by the attach path
  (`AccountManager._allowed_plugins` NEVER returns `None`; an empty
  assignment set attaches ZERO plugins — fail-closed, the old
  "no profiles = unrestricted" default is gone).
* `AccountManager.assign_plugin` / `unassign_plugin` /
  `set_account_plugins` persist FIRST, then **re-attach immediately** — a
  running account's plugin set follows the mutation right away (no
  next-start semantics). Surfaced by `ControlAPI.accounts_plugins_list` /
  `accounts_plugin_assign` / `accounts_plugin_unassign` /
  `accounts_plugins_set`; CLI: `accounts plugins <a>` / `plugin-add` /
  `plugin-remove`.
* **Assignment ≠ enabled**: the assignment decides which plugins ATTACH to
  the account; the per-account enabled flag (`plugin_states`, CLI
  `plugins enable|disable`) decides whether an attached plugin runs.

### 4.3 Plugin setting profiles (implemented — the OTHER profile system)

Not to be confused with §4.2: **plugin profiles** (`plugin_profiles`
table, migration v7) are per-plugin named setting *bundles* that an
account binds per assigned plugin. This IS implemented:

* A profile row is `{plugin, name, description, settings}`; `settings`
  is validated against the plugin's `Setting` schema by the ControlAPI
  layer and may carry the RESERVED top-level `"channels"` key — the
  channel scope (`{"mode": "all"|"allow"|"deny", "rules": [...]}`,
  validated by `core.plugin_scopes`; an empty allow-list allows
  NOTHING, never everything).
* The binding lives ON the assignment row (`account_plugins.profile`,
  `NULL` = plugin defaults). Binding REQUIRES assignment (a
  binding-less assignment is fine; an assignment-less binding can never
  take effect and is rejected).
* `Account.plugin_profiles: Dict[str, Optional[str]]` is the in-memory
  mirror of `{plugin: profile or None}` — bulk-loaded at boot from
  `AccountPluginsRepository.all_bindings()`, served in
  `status_dict()`, updated by `ControlAPI.accounts_plugin_profile`
  (which delegates to the repo's `set_profile` and patches the memory
  record — the FINAL-PHASE F3 pattern).
* **Effective settings merge** (highest wins): plugin `Setting`
  defaults ← bound profile's settings (minus `"channels"`) ←
  per-account overrides (`plugin_settings` rows). The profile's
  channel scope rides along under `"channels"` (`None` = no
  restriction). Surfaced by `ControlAPI.accounts_effective_settings`
  and the same merge backs the `CommandSettingsApi.effective()` view
  self-command handlers see.
* Renaming/deleting a profile cascades in the SAME repository
  transaction (bindings rewritten / NULLed), then the account
  manager's memory records are bulk-refreshed
  (`ControlAPI._refresh_all_plugin_profiles`).

Surfaces: dashboard Plugins tab (per-plugin profile library +
per-account quick-bind), Profiles tab (account-centric bindings +
effective-settings view), `/api/accounts/plugin-profile(s)`,
`/api/accounts/effective-settings`, `/api/plugins/profiles/*`. See
[API_ENDPOINTS.md](API_ENDPOINTS.md) §4/§10.

### 4.4 Future (NOT implemented)

Multiple saved *account-level* configuration sets beyond the per-plugin
profiles of §4.3.

---

## 5. Reconnect supervisor

Spawned at the end of `start()` when the account's `reconnect_enabled` framework setting is true
(default `True`). Two pieces:

* `_supervise(name, rt)` — polls `lost` / status every `supervisor_tick` seconds (default `0.2`).
  Stands down on `stop_requested` or when the account disappears (removed mid-flight);
  `CancelledError` propagates (the reaper in `stop()` handles it).
* `_reconnect_loop(name, rt)` — after an **unexpected** drop (never a deliberate stop):
  * backoff `delay = min(initial * multiplier ** attempt, max)`, slept **before** each attempt;
  * a **fresh client per attempt** (factory + callback rewiring) — never a reused half-dead
    session;
  * settings **re-read per attempt** (`_reconnect_settings`), so live changes apply immediately;
    reconnect can even be disabled mid-loop (stand-down, status preserved);
  * on success: status `RUNNING`, `started_at` reset, `lost.clear()`, `AccountConnected`
    republished;
  * `KeyboardInterrupt`/`SystemExit` mark the account `FAILED` and stop reconnecting — they never
    kill the process.

Backoff knobs are per-account framework settings resolved from the database
(`SettingsManager.account_settings`) with fallbacks
`{"initial": 1.0, "max": 60.0, "multiplier": 2.0}` — any settings failure defaults to enabled
reconnect so a settings problem never blocks recovery.

---

## 6. `details()` — the §8 account page (exact shape)

`AccountManager.details(name) -> Dict` (surfaced by `ControlAPI.accounts_info`):

```python
{
  "name": name,
  "status": {"connected": bool, "state": str, "latency_ms": float|None, "uptime_s": float|None},
  "identity": {"name": cache.user_name or record.name,
               "discord_id": cache.user_id or runtime.user_id},
  "runtime": {"cached_guilds": int, "cached_channels": int, "cached_dms": int,
              "cache_source": "client cache (READY)"},
  "plugins":  [{"name": str, "enabled": bool}, ...],
  "profiles": [...],          # legacy, informational (retained infra)
  "rate_limit": {action: {"interval": ..., "jitter": ..., "source": ...}, ...},
}
```

The `plugins` rows ARE the capability view (the attached set = the direct
`account_plugins` assignments); `profiles` renders as informational only.

Everything runtime-y draws ONLY from the live client's ingested cache (zero API calls).
`rate_limit` is built only when a limiter was injected: actions from `limiter.policies()`, effect
from `limiter.effective(action, account)`; `source` is `"inherited from global"` when at most one
level applies, else the comma-joined `level:owner` list (owner omitted for the global level,
which has none).

---

## 7. Emergency stops

| Level | Entry point | Behavior |
|---|---|---|
| **Account-scoped** | `AccountManager.emergency_stop(name)` (synchronous, returns immediately) | `stop_requested = True` + `lost.set()` (no reconnect can race it) → `scheduler.cancel_matching(account=name)` (cancels this account's queued/running tasks) → fires the full `stop()` as a background task (`estop-<name>`). Requires a running event loop (`AccountError` otherwise). Returns `{"account", "cancelled_tasks", "stop_initiated": True}`; completion is async — poll `get(name).runtime.status` or watch `AccountDisconnected`. |
| **Global** | `ControlAPI.emergency_stop_all()` | `scheduler.cancel_matching()` (all) → `scheduler.stop(drain=False)` (every live task terminal `cancelled`, lanes reaped — new submits rejected) → `stop_all()` accounts. Returns `{"cancelled_tasks", "accounts", "halted": True}`. |
| **Resume** | `ControlAPI.system_resume()` | Re-arms the scheduler only (`scheduler.start()` — clean re-arm: `stop()` already drove every task terminal; finished-task history preserved). Accounts stay stopped **on purpose**: restart the ones you want with `accounts_start`. Exists because the global halt was otherwise irreversible until process restart. |

---

## 8. Group scope registration (`sync_group`)

`AccountManager._sync_group_scope(name, group)` feeds the account's group membership to the
limiter: `RateLimitManager.set_account_group(account, group)` — pure metadata plumbing, **no
Discord API involved**, tolerant of limiters without group support. Registered at boot
(`_load_from_repo`), at `add_account`, and deregistered (`group=None`) at `remove_account`.

Semantics in the limiter (`core/rate_limit_manager/engine.py`): every acquisition still paces on
the per-task key `(account, action)`; a grouped account **also** paces on `(group:<name>, action)`
— any member's grant stamps the shared group clock, so the next member's call waits the full
interval (the `A → [limit] → B → [limit] → A` serialization). `group=None` removes the account
from any group.

---

## 9. Per-(account, plugin) services

`AccountManager.services_for(account_name, plugin_name)` builds/caches one
`core.account_manager.services.AccountPluginServices` per pair (for the lifetime of a start;
dropped on stop/removal). Wiring details that matter to plugin authors:

* its `settings` dict is seeded from `PluginManager.live_settings` — the **same dict** the plugin
  manager mutates on `set_setting`, so `ctx.settings` and `ctx.services.settings` can never
  diverge;
* the **shared `TaskAckRegistry`** (one per process, one bus subscription set) backs the
  fire-and-await-ack semantics of every pipeline call (`task_ack_timeout`, default 30s);
* a `client_getter` closure returns the account's **live** client (or `None`) for DM opens and
  user fetches — so a facade from a stopped account degrades to `AccountError`/`None` instead of
  holding a dead client.

Full services reference: [PLUGIN_API.md](PLUGIN_API.md).

---

## Lifecycle generations, the governor, and the bounded start_all scheduler

> Source of truth: `core/account_manager/{manager,lifecycle}.py`.
> Verified at commit `487dc53`.

**Generation tokens** — `AccountRuntime.generation` is bumped at every
ownership change (start entry, stop inside the lock, remove, estop).
Every async lifecycle path captures it before an await and validates
after: a stale start aborts before even building its client; stop landing
during plugin activation tears down what the start created (no orphaned
roll loops on a stopped account); an old stop's terminal writes never
clobber a newer start; a stale supervisor exits on identity mismatch.
The bump happens only on EFFECTIVE removal — a refused remove (live
account) leaves the in-flight start valid.

**Lifecycle governor** (`core/account_manager/lifecycle.py`) — two-level
abuse prevention, independent of the Discord rate engine: per-account
cooldowns (stop→start 10 s, failed-retry 15 s, invalid-credential lockout
300 s on streak ≥ 2; stops never gated) + global token buckets (start
5/5 s — the per-IP IDENTIFY budget; stop 16/1 s). State-aware (state-
illegal ops consume nothing), estop/teardown bypass entirely, and estop
restarts don't stamp the cooldown clock.

**start_all** — bounded lifecycle scheduler: 4-lane concurrency (one hung
connect delays only its own lane), admission pacing (one start per
`stagger` seconds), overtake-by-generation (an estopped account's queued
start is cancelled), single-flight join for concurrent sweeps. stop()
phases are deadline-bounded (plugin hooks 5 s, disconnect 10 s +
force-reap).

**IdentifyGate** (`adapters/discord/client.py`) — the process-wide
IDENTIFY pacer the library lacks (discord.py-self 2.1.0 ships no identify
serializer). FIFO with 5 s spacing and a depth-projected fast-fail at
45 s: a saturated queue fails cheaply into reconnect backoff instead of
parking a built client for 90 s.

---

## 10. Command access: the AccessManager gate (`core/access_manager.py`)

Who may invoke an account's **self-commands** (and the mention-prefix
management commands) is DB-backed, fail-closed, and per-account:

* **kv rows** under the `access:` prefix —
  `access:admins` → `[discord user id, ...]`,
  `access:mudae_bot_id` → `int`,
  `access:account:<name>` → `{is_admin, prefix_mode (mention|symbol|both),
  prefix_symbol, allowed_user_ids: [int], user_id}` (the account's own
  Discord id, recorded at connect).
* **`can_invoke(account, user_id)`** is THE gate: admin ids (list
  membership OR the user id of an admin-flagged account) bypass;
  otherwise the invoker must be in that account's `allowed_user_ids`;
  an EMPTY list admits nobody (fail-closed).
* **One-time env migration** (`migrate_from_credentials`, run by the
  bootstrap): when the stored admin list is EMPTY, it is seeded from
  `OWNER_USER_IDS` — after which the env value is IGNORED for
  authorization (the "every env-listed id controls every account" hole
  is closed). No env owners and no stored admins → NOBODY can command
  anything until an admin is added via the dashboard.
* **Per-account prefix config** (mention/symbol/both + 1–4 char
  whitespace-free symbol) overrides the env `SELF_COMMAND_PREFIX`
  default; `prefix_config_sync` is the warmed cache the client factory
  reads at build time, `prefix_config` the authoritative async read.
* `mudae_bot_id` resolves DB-first with the env `MUDAE_BOT_ID` as
  fallback (`set_mudae_bot_id(None)` clears back to the fallback).

Surfaces: dashboard Admin tab / `/api/admin/*`
([API_ENDPOINTS.md](API_ENDPOINTS.md) §6); Discord verbs
`adminlist`/`adminperms`/`adminprefix` (admin-only self-commands).
Plugins see credentials through `AccountPluginServices.credentials`,
which returns the injected credentials view (the env `Credentials`
object in the live stack); the former
`core.framework_credentials.FrameworkCredentials` wrapper was never
constructed anywhere and has been removed.
