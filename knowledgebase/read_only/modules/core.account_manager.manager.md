# Module `core.account_manager.manager`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/account_manager/manager.py`

```
Account manager (A6): Account, AccountRuntime, AccountManager, DiscordCache wiring.

════════════════════════════════════════════════════════════════════════
API BOUNDARY CONTRACT (architecture spec) — audited & enforced
════════════════════════════════════════════════════════════════════════
This module is the framework: event routing, account control, task
orchestration, lifecycle. It NEVER initiates a Discord API call for its own
purposes. The COMPLETE inventory of client touches (each demand-driven):

  1. ``connect()/disconnect()``          — gateway lifecycle (account control)
  2. ``client.cached_snapshot()``        — memory-only read of what the client
                                           already holds; ZERO API calls
  3. ``send_hook`` → ``client.send_message`` — EXECUTES a task a PLUGIN
                                           submitted (orchestration; paced by
                                           the RateLimiter before it runs)
  4. ``resolve_dm_channel`` → ``client.open_dm`` — ONLY on a DM-send cache
                                           miss, for a plugin-demanded send
  5. ``services.fetch_user`` → ``client.fetch_user`` — PLUGIN-facing,
                                           on-demand, DB-cached per account
  6. ``_maybe_typing`` → ``client.send_typing`` — the ONE deliberate,
                                           opt-in exception: a send-decoration
                                           policy (typing_probability, default
                                           0.0 = never) — no plugin demands it,
                                           so it ships disabled

There is NO prefetching, NO "just in case" fetching, and NO API-derived
state beyond what a live send/flow actually consumed. ``details()`` reads
exclusively from the ingested READY snapshot + repos. Rate-scope metadata
(``_sync_group_apply`` / ``_sync_group_scope``) is pure plumbing — no API
involved.
════════════════════════════════════════════════════════════════════════

One :class:`Account` per Discord user account, split into PERSISTENT state
(``record`` — database/models.AccountRecord) and RUNTIME state
(``runtime`` — client, status, cache, supervisor; never persisted).

Lifecycle (ported from ``git show HEAD:core/container.py`` + ``core/account.py``,
audited through 3 review rounds):

  * ``start`` — resolve the token (.env by convention key), build a client via
    the factory, wire callbacks BEFORE connect, connect, ingest the client's
    cached snapshot into the per-account DiscordCache, attach+activate the
    account's plugins, publish ``AccountConnected``, spawn the reconnect
    supervisor (when enabled by the account's framework settings).
  * supervisor — watches a per-account ``lost`` asyncio.Event; on an
    UNEXPECTED drop (never on deliberate stops) reconnects with exponential
    backoff, a FRESH client per attempt, cache re-ingest and another
    ``AccountConnected``. KeyboardInterrupt/SystemExit never kill the process.
  * ``stop`` — idempotent: mark stop_requested, deactivate plugins FIRST
    (hooks still see the cache), disconnect the client (None-safe), reap the
    supervisor with a bounded 1s wait then cancel, status stopped, publish
    ``AccountDisconnected(reason="stopped")``.

Duck typing: ``plugin_manager`` / ``scheduler`` / ``limiter`` are parallel
deliverables (A3/A5/A4) — they are called ONLY via the surfaces pinned in
INTEGRATION.md, with tolerant fallbacks where a collaborator may legitimately
lack a feature. Nothing imports those packages at runtime.

Service wiring note for the lead: ``plugin_manager.activate_account`` is
called with a SERVICES FACTORY (``factory(plugin_name) -> AccountPluginServices``),
not a bare services object — per-plugin task submission needs the plugin
name. This matches the A6 hand-off contract and should be reconciled with A3.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `AccountStatus` | enum | yes | Lifecycle states of one account's connection. |
| `_StaleGeneration` | exception | no | PHASE-4: a lifecycle operation resumed after ownership changed. |
| `AccountRuntime` | dataclass | yes | RUNTIME state of one account (never persisted). |
| `Account` | dataclass | yes | First-class account object: persistent record + runtime + plugin view. |
| `AccountManager` | class | yes | Owns every account: registry, lifecycle, send hook, details page. |

### `_StaleGeneration`


### `Account`

- `def status_dict(self) -> Dict[str, Any]`
  - JSON-safe snapshot (accounts list; see details() for the full page).
- `def channels_snapshot(self) -> List[Dict[str, Any]]`
  - Text-channel snapshot from the LIVE client ([] when offline).

### `AccountManager`

- `async def load(self) -> None`
  - Rebuild Account objects (and attach their plugins) from the repo.
- `async def add_account(self, name: str, *, token_ref: Optional[str]=None, enabled: bool=True, sync_group: Optional[str]=None, plugins: Optional[List[str]]=None, profiles: Any=None) -> Dict[str, Any]` — side effects: `db_write`
  - Register + persist an account; returns its status dict.
- `async def remove_account(self, name: str) -> Dict[str, Any]`
  - Remove a STOPPED account: detach plugins, cancel supervisor, delete.
- `def get(self, name: str) -> Account`
  - The Account object (AccountError when unknown / unhashable name).
- `def names(self) -> List[str]`
  - Sorted account names.
- `async def accounts(self, *, refresh_records: bool=False) -> List[Dict[str, Any]]`
  - Status dicts of every account, sorted by name.
- `async def reconcile_records(self) -> None`
  - Low-frequency authoritative re-read of every account row.
- `async def set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]` — side effects: `db_write`
  - Flip the persisted enabled flag (False = skipped by start_all).
- `async def assigned_plugins(self, name: str) -> List[str]`
  - Sorted plugin names assigned to the account (capability read).
- `async def assign_plugin(self, name: str, plugin: str) -> None`
  - Assign ONE plugin to an account (idempotent; re-attaches NOW).
- `async def unassign_plugin(self, name: str, plugin: str) -> bool`
  - Remove ONE plugin assignment (idempotent; re-attaches NOW).
- `async def set_account_plugins(self, name: str, plugins: List[str]) -> List[str]`
  - REPLACE the account's whole assignment set (re-attaches NOW).
- `async def plugin_profiles(self, name: str) -> Dict[str, Optional[str]]`
  - ``{plugin: profile name or None}`` for every assigned plugin.
- `def channels_snapshot(self, name: str) -> List[Dict[str, Any]]`
  - Text-channel snapshot of the account's LIVE client.
- `async def start(self, name: str) -> Dict[str, Any]` — side effects: `creates_tasks, publishes_event`
  - Connect one account, activate its plugins, spawn the supervisor.
- `def lifecycle_stats(self) -> Dict[str, Any]`
  - Governor stats for status surfaces ({} when unlimited).
- `async def stop(self, name: str, *, _bypass_lifecycle: bool=False) -> Dict[str, Any]` — side effects: `publishes_event`
  - Stop one account (idempotent; plugins deactivated first).
- `async def start_all(self, *, stagger: float=5.0) -> Dict[str, Any]` — side effects: `creates_tasks, sleeps, publishes_event`
  - Start every ENABLED account through the bounded lifecycle scheduler.
- `async def stop_all(self) -> Dict[str, Any]`
  - Graceful shutdown of every account; logs and continues per account.
- `def emergency_stop(self, name: str) -> Dict[str, Any]` — side effects: `creates_tasks`
  - SYNCHRONOUS account-scoped emergency stop: returns immediately.
- `async def send_hook(self, task: Any) -> DiscordMessage` — side effects: `network`
  - Scheduler sender: one task -> one validated, audited Discord send.
- `async def details(self, name: str) -> Dict[str, Any]`
  - Account details page (spec §8) — EXACT shape:
- `def services_for(self, account_name: str, plugin_name: str) -> AccountPluginServices`
  - One AccountPluginServices per (account, plugin); cached per start.

## Functions

