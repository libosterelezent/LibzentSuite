# LibzentSuite — Complete Architecture & Capability Inventory

**Compiled:** 2026-11-13 · **HEAD:** post-Mudae-removal working tree · **Tests:** 2,177+ (107 files, 2,268 test functions) · **CI:** ruff+format gates, mypy advisory, uv.lock exact-install, 40-min timeout

---

## 1. System identity

LibzentSuite is a **multi-account Discord selfbot orchestration framework** — one process manages any number of Discord user accounts, pacing their outbound actions to look like well-paced humans, coordinating work across them (fleet orchestration), and exposing everything through a local dashboard, CLI, and in-chat self-command layer.

**Core principle:** accounts behave like humans, not bots. The safety floor (1.5s/action), human-like jitter, sync-group serialization, and always-on server-limit handling exist to *mitigate* (not authorize) Discord ToS risk.

---

## 2. Composition root & dependency direction

```
main.py (argparse entry, signal handling, surface wiring)
  └─ core/bootstrap.py::build() ← THE ASYNC COMPOSITION ROOT
       ├─ Credentials ← .env or injected values
       ├─ CredentialService ← Fernet-encrypted DB store + key management
       ├─ config.load_config ← config/ directory (JSON files)
       ├─ Database (aiosqlite WAL, 9 migrations, 16 repositories)
       ├─ LogService + AuditRecorder (queued persistence)
       ├─ EventBus (typed in-process pub/sub, 20 event types)
       ├─ RateLimitManager ← policy engine, shared clocks, safety floor
       ├─ SyncGroupManager ← persistent membership + limiter mapping
       ├─ SettingsManager ← schema-driven multi-layer resolution
       ├─ TaskScheduler ← priority queue, execution lanes, retries
       ├─ PluginManager ← discovery, per-account instances, dispatch
       ├─ AccountManager ← lifecycle governor, client cache, sender hook
       ├─ PluginDataStore ← separate WAL SQLite (state/plugin-data.db)
       ├─ FleetService ← assignment lifecycle, handoff, cooldowns
       ├─ FleetPolicy ← dispatch-time eligibility gate
       ├─ FleetReconciler ← 30s idempotent convergence passes
       ├─ SimulationRunner ← in-memory scenario execution
       └─ ControlAPI (THE facade) → Framework → main wires surfaces
```

**Dependency rules (enforced by AGENTS.md):**
- Interfaces (CLI, dashboard) → ControlAPI → services → repositories/adapters
- Plugins → PluginContext/PluginServices only; never internals
- `core/` never imports from `parsers/` (parser registry is decoupled)
- No service locators, no hidden imports across boundaries

---

## 3. ControlAPI — the shared application facade

**93 public methods** — every interface (dashboard REST, CLI, self-commands) calls the same facade. No parallel business-logic paths.

### 3.1 System lifecycle
| Method | Purpose |
|---|---|
| `status()` | Full system status snapshot |
| `shutdown()` | Graceful teardown (scheduler drain, accounts stop, persistence drain, DB close) |
| `emergency_stop_all(surface)` | Immediate halt of all outbound work |
| `system_resume()` | Re-arm the scheduler after emergency stop |

### 3.2 Account management (11 methods)
| Method | Purpose |
|---|---|
| `accounts_list()` | All accounts with runtime status |
| `accounts_add(payload, surface)` | Add an account (name, plugins, sync_group, enabled) |
| `accounts_remove(name, surface)` | Remove an account + cascade cleanup |
| `accounts_info(name)` | Detailed account info (cache, plugins, settings) |
| `accounts_enable(name, enabled)` | Toggle the persisted enabled flag |
| `accounts_start(name, surface)` | Connect a single account |
| `accounts_stop(name, surface)` | Disconnect a single account |
| `accounts_emergency_stop(name, surface)` | Halt one account's outbound work |
| `accounts_set_credential(name, credential, surface)` | Store/replace encrypted token |
| `accounts_credential_status()` | Per-account credential state (db/env/missing) |
| `credentials_import_env(overwrite, surface)` | One-shot .env → DB migration |

### 3.3 Plugin assignment & profiles (12 methods)
| Method | Purpose |
|---|---|
| `accounts_plugins_list(account)` | Assigned plugins for one account |
| `accounts_plugin_assign(account, plugin, surface)` | Assign a plugin to an account |
| `accounts_plugin_unassign(account, plugin, surface)` | Remove assignment |
| `accounts_plugins_set(account, plugins, surface)` | Replace full assignment set |
| `plugin_profiles_list(plugin)` | Named settings presets for a plugin |
| `plugin_profile_create(plugin, name, description, settings, surface)` | Create a preset |
| `plugin_profile_update(plugin, name)` | Bump the preset's updated_at |
| `plugin_profile_delete(plugin, name, surface)` | Delete a preset |
| `accounts_plugin_profile(account, plugin, profile, surface)` | Bind account→preset |
| `accounts_plugin_profiles(account)` | Available + bound presets for an account |
| `accounts_effective_settings(account, plugin)` | Fully merged effective settings view |
| `accounts_channels(account)` | Cached guild/channel list for account's UI |

### 3.4 Plugin data (AccountPluginData, Phase 10)
| Method | Purpose |
|---|---|
| `accounts_data_list(account)` | Scope nodes for one account (browser view) |
| `accounts_data_get(account, plugin)` | One document at a scope node |
| `accounts_data_put(account, plugin, guild_id, channel_id, data)` | Write with optimistic versioning |
| `accounts_data_delete(account, plugin)` | Remove a scope node's document |

### 3.5 Plugin runtime (8 methods)
| Method | Purpose |
|---|---|
| `plugins_manifests()` | Discovery manifest for every plugin |
| `plugins_state(account)` | Per-account enabled/disabled state |
| `plugins_reload(plugin, surface)` | Hot-reload plugin code |
| `plugins_unload(plugin, surface)` | Unload plugin from all accounts |
| `plugins_set_enabled(account, plugin, enabled)` | Toggle per-account |
| `plugins_settings(account)` | Settings view for account's plugins |
| `plugins_set_setting(account, plugin, key, value)` | Set one override |
| `plugins_clear_setting(account, plugin, key)` | Remove one override |

### 3.6 Sync groups (8 methods)
| Method | Purpose |
|---|---|
| `groups_list()` | All groups with member counts |
| `groups_get(group)` | One group detail |
| `groups_create(payload, surface)` | Create a sync group |
| `groups_update(group, payload, surface)` | Update name/description/enabled |
| `groups_delete(group, detach_members, surface)` | Delete (requires empty or explicit detach) |
| `groups_add_account(group, account, surface)` | Add member |
| `groups_remove_account(group, account, surface)` | Remove member |
| `groups_set_account(account, group, surface)` | Move account to group |
| `groups_clear_account(account, surface)` | Remove from any group |

### 3.7 Fleet orchestration (3 methods)
| Method | Purpose |
|---|---|
| `fleet_groups()` | Fleet state for all groups |
| `fleet_group(group)` | Fleet state for one group |
| `fleet_op(op)` | Explicit operation dispatch (join/leave/pause/resume/suspend/requeue/handoff/withdraw/cancel/estop/cooldown apply/cooldown clear/eligibility) |

### 3.8 Admin & access control (11 methods)
| Method | Purpose |
|---|---|
| `admin_overview()` | Admin IDs, per-account perms, prefixes, primary bot id |
| `admin_set_ids(ids, surface)` | Replace framework admin list |
| `admin_add_id(user_id, surface)` / `admin_remove_id(user_id, surface)` | Modify admin list |
| `admin_account_flag(account, is_admin, surface)` | Flag an account as admin |
| `admin_perms(account, surface)` | Per-account allowed user IDs |
| `admin_perms_set/add/remove` | Manage per-account allowlists |
| `admin_prefix(account, surface)` / `admin_prefix_set(mode, symbol)` | Command prefix config |
| `admin_primary_bot_id(surface)` / `admin_primary_bot_id_set(user_id)` | Target bot id |
| `admin_forget_account(account, surface)` | Remove account + all related data |

### 3.9 Rate limiting (8 methods)
| Method | Purpose |
|---|---|
| `ratelimit_policies()` | All stored policies across levels |
| `ratelimit_set(action, interval, jitter_min, jitter_max, level, owner)` | Set/update a policy |
| `ratelimit_clear(level, owner, action)` | Remove a policy |
| `ratelimit_effective(action, account, plugin)` | Composed effective interval |
| `ratelimit_stats()` | Grant/wait/cooldown/429 counters |
| `ratelimit_warnings()` | Configuration health warnings |
| `ratelimit_set_floor(seconds)` | Raise the safety floor (raise-only) |
| `ratelimit_lane()` / `ratelimit_set_lane(enabled, prefixrate)` | Server-wide send lane |

### 3.10 Tasks & observability (8 methods)
| Method | Purpose |
|---|---|
| `tasks_list(status, account)` | Active tasks by status/account |
| `tasks_cancel(task_id)` | Cancel one task |
| `tasks_cancel_matching(account, plugin)` | Cancel all matching |
| `tasks_history(limit, account)` | Recent task history |
| `logs_tail(n, account, severity)` | Structured log tail |
| `audit_tail(n, account)` | Interaction audit trail |
| `simulation_list()` | Available scenario names |
| `simulation_run(name)` | Execute a scenario (offline) |

### 3.11 Dashboard UI (5 methods)
| Method | Purpose |
|---|---|
| `ui_stats(account)` / `ui_panels()` | Plugin stat cards + sidebar panels |
| `dashboard_ui_navigation()` | Builder-registered page tree |
| `dashboard_ui_page(plugin, page_id, account)` | Rendered page components |
| `dashboard_ui_action(plugin, component_id, payload, surface, account)` | Dispatch UI action |
| `dashboard_ui_data(plugin, component_id, account)` | Fetch component data |

### 3.12 Theme (2 methods via GET/POST /api/theme)
Theme persistence: presets, accent, radius, density, fonts, layout, page overrides, custom CSS.

---

## 4. Outbound action pipeline

```
PluginServices request
    → TaskScheduler.submit (priority queue, retry policy)
    → dispatch guard (FleetPolicy: eligibility, cooldowns, generation fence)
    → one in-flight action per account (execution lane)
    → RateLimitManager.acquire (pacing + jitter + server cooldowns)
    → final validation (account alive, plugin enabled, scope check)
    → AccountManager sender hook
    → DiscordClient or SimClient
    → audit + history + event publication
```

**Invariants:**
- Safety floor: `MIN_ACTION_INTERVAL = 1.5s` — no policy may pace faster
- `MAX_INTERVAL = 86400.0s`, `MAX_JITTER = 3600.0s` — sanity caps
- Server 429s are always respected (cooldown before retry)
- Retries only for scheduler-classified transient errors
- Timeouts are terminal (never auto-retried — anti-duplicate-send)
- Cancellation is cooperative with a bounded re-reap

---

## 5. Fleet orchestration (Phase 9)

### 5.1 State machine
```
QUEUED → ASSIGNED → RUNNING → COMPLETED | SUSPENDED | FAILED | CANCELLED
SUSPENDED → QUEUED (requeue when prerequisites clear)
FAILED → QUEUED (only when retry policy allows)
COMPLETED/CANCELLED are terminal
```

### 5.2 Components
| Component | File | Responsibility |
|---|---|---|
| `FleetService` | `core/fleet/service.py` | Authoritative writer: join/leave, assignment lifecycle, handoff, cooldowns |
| `FleetPolicy` | `core/fleet/policy.py` | Dispatch-time eligibility (§4 check order, cooldown retry_at, generation fence) |
| `transitions.py` | `core/fleet/transitions.py` | Single source of transition validation tables |
| `observations.py` | `core/fleet/observations.py` | Dedup'd observation intake; restrictions only through validated ops |
| `reconciler.py` | `core/fleet/reconciler.py` | 30s idempotent passes; surfaces ambiguity; zombie fencing via generation bumps |
| `capability.py` | `core/fleet/capability.py` | FleetTaskHandler plugin capability (opt-in, default OFF) |

### 5.3 Fleet REST surface
- `GET /api/fleet/groups` — all fleet state
- `GET /api/fleet/group?name=X` — one group detail
- `POST /api/fleet/op` — explicit operation dispatch with op table

### 5.4 Key invariants
- Sync groups (membership + pacing) ≠ Fleet (task ownership + eligibility)
- `transitions.py` is the only validator — never reimplemented in the dashboard
- Observations authorize nothing; restrictions go through validated service ops
- Generation fences reject stale assignments
- Handoff validates destination before revoking source
- `resume_task` requeues to QUEUED (UI says "Requeue")

---

## 6. Plugin system

### 6.1 Contract
Plugins subclass `PluginBase` (abstract), export one instance per module:
```python
class MyPlugin(PluginBase):
    name: ClassVar[str] = "myplugin"
    version: ClassVar[str] = "1.0.0"
    settings: ClassVar[Sequence[Setting]] = [Setting("trigger", "str", "$cmd", "...")]
    COMMANDS: ClassVar[tuple[CommandSpec, ...]] = ()
    fleet_handler: ClassVar[Any | None] = None  # Phase 9 opt-in
```

### 6.2 Lifecycle hooks (all optional)
| Hook | When |
|---|---|
| `on_load(ctx)` | First enable on an account |
| `on_enable(ctx)` | Every activation |
| `on_disable(ctx)` | Deactivation or account stop |
| `on_unload(ctx)` | Removal from account (last call) |
| `handle_event(ctx, event)` | Every MessageReceived routed to the account |
| `initialize_dashboard_ui(ui)` | Register dashboard UI (sync, once) |
| `ui_stats(ctx)` / `ui_panels(ctx)` | Dashboard stat cards + sidebar panels |

### 6.3 PluginServices (the `ctx.services` facade)
| Capability | Methods |
|---|---|
| Messaging | `send_message`, `send_dm`, `open_dm`, `add_reaction`, `click_button` |
| User resolution | `fetch_user` (cached per-account, one fetch ever) |
| Cooldowns | `cooldown_set/remaining/active/clear/list` |
| State KV | `state_get/set/delete` (per account+plugin) |
| Message waiting | `wait_for_message`, `collect_messages` |
| Plugin data | `data_get/put/list/delete` (Phase 10 scope lattice) |
| Events | `publish(event)` |
| Credentials | `credentials` property (primary_bot_id, owner_ids) |
| Settings | `settings` property (live resolved dict) |
| Channel utils | `validate_channel`, `channel_label` |

### 6.4 Discovery
`PluginLoader` scans `plugins/` directory for Python modules exporting a `PluginBase` subclass. Each plugin is instantiated once per (account, plugin) pair. The loader validates slug names (`[a-z0-9_-]+`), collects settings schemas, and builds synthetic parent packages for module isolation.

### 6.5 Self-command layer
Framework verbs + plugin-declared commands ride one dispatch path:
- Framework: `status`, `accounts`, `plugins`, `ratelimit`, `lane`, `tasks`, `estop`, `simulation`, `adminlist`, `adminperms`, `adminprefix`, `pg`, `say`, `help`
- Plugin: any `CommandSpec` with typed `ArgSpec` positional args
- Authorization: `AccessManager.can_invoke` — per-account allowlist OR framework admin (fail-closed)
- Prefix: per-account `mention`/`symbol`/`both` (DB-backed)

---

## 7. Parser registry

The parser system is a **generic, empty registry** — game packs register themselves:

```python
from parsers import PARSER_MANAGER

PARSER_MANAGER.register_parser(
    name="my_game",
    factory=lambda: MyGameParser(),
    description="My game's bot messages",
    families=("character", "cooldown", ...),
)
```

**Backend contract:** one callable `parse(message, *, account_username=None) -> typed result` with a `family` discriminator. Pure, sync, no I/O.

---

## 8. Rate limiting engine

### 8.1 Policy composition
- Scopes: global → account → plugin → account+plugin
- Effective interval = **max** (most restrictive wins) across all applicable levels
- Jitter: uniform `[min, max]` sampled after every grant
- When no policy stored: learned defaults apply (message/dm 2.6s, reaction 2.5s, button 3.0s)

### 8.2 Safety mechanisms
| Mechanism | Detail |
|---|---|
| Hard floor | 1.5s minimum per action (raise-only at runtime) |
| Server cooldowns | 429/Retry-After → per-account cooldown, waited before retry |
| Global send lane | Optional serialization of ALL outbound (prefixrate-spaced) |
| Warning taxonomy | `below_floor`, `no_jitter`, `narrow_jitter`, `aggressive`, `faster_than_learned`, `default_no_jitter` |

### 8.3 Persistence
Policies stored in `rate_limit_policies` table; floor in kv (`ratelimit:floor`); lane in kv. Boot-time config seeding clamps below-floor values with a warning.

---

## 9. Persistence

### 9.1 Main database (`state/libzent.db`, SQLite WAL)
9 append-only migrations, 14 repositories, 24 tables total:

| Migration | Name | Tables added |
|---|---|---|
| v1 | initial schema | accounts, account_settings, plugins, plugin_settings, sync_groups, audit_logs, api_cache, kv |
| v2 | api cache | (migration for api_cache table) |
| v3 | credential store | credentials |
| v4 | sync group backfill | sync_groups data migration |
| v5 | cooldowns | cooldowns |
| v6 | account plugin assignments | account_plugins |
| v7 | plugin profiles | plugin_profiles + account_plugins.profile column |
| v8 | drop legacy tables | DROP profiles, profile_plugins, account_profiles |
| v9 | fleet orchestration | fleet_groups, fleet_members, fleet_assignments, fleet_cooldowns, fleet_transitions |

### 9.2 Plugin data store (`state/plugin-data.db`, separate WAL)
- Scope lattice: account → plugin → guild → channel ('0' sentinels for global scope)
- Standalone copies (no inheritance between channel documents)
- Optimistic version writes (conflict-refusing)
- Digit-string snowflakes enforced at boundary

### 9.3 Credential store
- Fernet ciphertext in `credentials` table (AES-128-CBC + HMAC-SHA256)
- Master key: `LZ_MASTER_KEY` env or `state/credentials.key` (0600)
- Resolution: DB ciphertext → .env fallback → `MISSING_CREDENTIAL`
- Import: one-shot `.env` → DB (never overwrites existing)

---

## 10. Dashboard (current: custom HTTP + WS + SPA)

### 10.1 REST surface (91 unique routes)
| Domain | Endpoints |
|---|---|
| System | GET /api/status, POST /api/estop, POST /api/shutdown, POST /api/resume |
| Accounts | GET /api/accounts, POST add/remove/enable/start/stop/emergency-stop |
| Credentials | GET /api/credentials/status, POST /api/accounts/credential, POST /api/credentials/import-env |
| Plugin assignment | GET /api/accounts/plugins, POST assign/unassign/set |
| Plugin profiles | GET /api/accounts/plugin-profiles, POST /api/accounts/plugin-profile, GET/POST/DELETE /api/plugins/profiles/* |
| Plugin runtime | GET /api/plugins, GET/POST /api/plugins/state, POST reload/unload/set-enabled/set-setting |
| Plugin data | GET /api/accounts/data, GET/POST/DELETE per scope node |
| Effective settings | GET /api/accounts/effective-settings |
| Channels | GET /api/accounts/channels |
| Sync groups | GET/POST /api/groups/* (list, info, create, update, delete, add-member, remove-member, set-account, clear-account) |
| Fleet | GET /api/fleet/groups, GET /api/fleet/group, POST /api/fleet/op |
| Admin | GET /api/admin/overview, POST ids/ids-add/ids-remove, POST account-flag, GET/POST perms/*, GET/POST prefix/*, GET/POST primary-bot-id |
| Rate limits | GET policies/effective/stats/warnings, POST set/clear/floor, GET/POST lane |
| Tasks | GET /api/tasks, POST cancel/cancel-matching, GET history |
| Logs/audit | GET /api/logs, GET /api/audit |
| Cooldowns | GET /api/cooldowns, POST clear |
| Theme | GET/POST /api/theme |
| UI | GET /api/ui/stats, GET /api/ui/panels |
| Builder UI | GET navigation, GET page, POST action, GET data |
| Simulation | GET /api/simulation/list, POST run |

### 10.2 WebSocket live push
- Hand-rolled RFC 6455 subset (text frames, ping/pong, close)
- Auth: same `DASHBOARD_TOKEN` as REST
- Push: task lifecycle events + fleet state changes + theme changes + logs
- Frame: `{"type": "fleet"|"tasks"|"logs"|..., "data": {...}}`

### 10.3 Security posture
- Binds `127.0.0.1` by default
- `DASHBOARD_TOKEN` auth on REST + WS (header or query param)
- Query-string `?token=` redacted from logs
- Strict JSON booleans (400 on truthy casts)
- NaN/inf rejection, 1MB body/frame caps (413)
- Origin/Host validation (DNS-rebinding + cross-site defense)
- Credential routes hard-gated (`CREDENTIAL_GATED_ROUTES`)
- No-auth mode warns loudly

---

## 11. CLI & REPL

`main.py` entry modes:
- **REPL** (default): interactive command loop with graceful SIGTERM/SIGINT handling
- **One-shot**: `python main.py <command ...>` (e.g., `accounts list`, `status`)
- **Dashboard**: `--dashboard [--port N]` (serves REST + WS + SPA)
- **Headless**: `--headless` (daemon mode, SIGTERM for graceful stop)
- **Start all**: `--start-all` (boot every enabled account)
- **Simulation**: `--sim [name]` / `--sim-list` (offline scenario runner)
- **Benchmark**: `--bench [scenario]` (isolated perf measurement)

All CLI commands call `ControlAPI` — same facade as the dashboard.

---

## 12. Event system

20 typed event classes (plus the base Event and EventBus itself) on `core/event_bus.py`:

| Category | Events |
|---|---|
| Account lifecycle | AccountConnected, AccountDisconnected, AccountFailed |
| Plugin lifecycle | PluginLoaded, PluginUnloaded, PluginEnabledChanged |
| Task lifecycle | TaskQueued, TaskStarted, TaskCompleted, TaskFailed, TaskCancelled |
| Rate limiting | RateLimitTriggered |
| Settings | SettingChanged |
| Messages | MessageReceived, MessageEdited, MessageDeleted, ReactionAdded |
| Simulation | ScenarioStarted, ScenarioFinished |
| Fleet | FleetStateChanged |

Events are typed in-process pub/sub. Handler isolation (one handler's failure doesn't kill others). Bounded drain on shutdown. Events are observations, never authorization.

---

## 13. Settings resolution

Four layers (highest wins):
1. Account per-plugin overrides
2. Bound profile settings (minus reserved `channels` key)
3. Global file layer (`config/global_settings.json`)
4. Plugin schema defaults

The reserved `"channels"` key in profile settings carries the channel scope (positive set, no modes) validated by `core/plugin_scopes.py`.

---

## 14. Credential & security model

| Control | Detail |
|---|---|
| Encryption at rest | Fernet (AES-128-CBC + HMAC-SHA256), key outside DB |
| Key management | `LZ_MASTER_KEY` env or 0600 key file; boot key-mismatch canary |
| Write-only in UI | Status surfaces words only (`db`/`env`/`missing`), never values |
| Token gate | Missing credential → classified `missing_credential`, no half-connect |
| Import | `.env` → DB is one-shot, never overwrites existing |
| Redaction | `redacted()` masks token-like values in logs; `__repr__` guards |
| Permission audit | POSIX boot check: DB/key/.env for group/world bits |
| File permissions | DB 0600, key 0600, WAL/sidecars 0600 |

---

## 15. Channel scope system

`core/plugin_scopes.py` — the single validation source:
- Reserved top-level settings key `"channels"` holds `{"channels": ["id", ...]}`
- Positive set only — no modes, no rules
- Empty/absent = unrestricted
- Guild/channel IDs are strings (snowflake safety)
- Validated at write time; evaluated leniently at read time (fail-open for shape problems only)

---

## 16. Simulator

`adapters/simulator/` — complete offline Discord replacement:

- **SimWorld**: in-memory users, channels, DM channels, message journal
- **SimClient**: Discord client interface over SimWorld (sends, edits, reactions, DMs)
- **SimulationRunner**: scenario execution with failure collection
- **Scenarios**: pacing_respected, soulcopy flow, server_429_respected, cooldown enforcement, fleet scenarios, control_mention
- Configurable response scripting (responder_id, reply patterns, delays)
- Used by: integration tests, full-stack tests, `main.py --sim`, `POST /api/simulation/run`

---

## 17. Testing infrastructure

| Category | Files | Coverage |
|---|---|---|
| Unit (domain) | ~70 files | Access manager, API, bootstrap fixes, cooldowns, credentials, database, fleet (service/policy/models/recovery/ownership/observations), framework UI, hardening, parser manager, plugin (base/commands/data/manager/profiles/reload), plugin scopes, rate (engine/policies/warnings/fixes), residual risk, rich model, scheduler, settings, sim (client/fidelity/world), sync groups, task scheduler, theme engine |
| Integration | ~8 files | Account persistence, plugin assignment e2e, full-stack flow, plugin commands e2e, sync groups |
| Simulation | ~2 files | Scenario runner + scenario behavior |
| Stress | ~2 files | Chaos matrix, fleet soak |
| Acceptance | ~1 file | Dashboard UI acceptance (structural pins) |
| HTTP | ~1 file | Dashboard HTTP (error mapping, peer close, route compat, WS) |
| Static pins | ~1 file | SPA endpoint existence, source-text contracts |

**Infrastructure:** 107 test files, 2,268 test functions. Deterministic (ManualClock, in-memory DBs, simulator adapters, injected dependencies). No real credentials or network in routine tests.

---

## 18. Build & dependency management

| Tool | Role |
|---|---|
| `uv` | Package manager + virtual environment (exact lock installs) |
| `uv.lock` | Universal resolution with hashes (33 packages) |
| `requirements.txt` | Generated pip-compat export (regenerate with `uv export`) |
| `pyproject.toml` | `[project]` deps + `[dependency-groups]` dev + `[tool.ruff]` + `[tool.mypy]` |
| Ruff | Linting + import sorting + formatting (production tree gated in CI) |
| mypy | Strict type checking (advisory, 1,086-error baseline ratcheting) |

### Runtime dependencies
| Package | Version | Purpose |
|---|---|---|
| discord-py-self | 2.1.0 | Discord user-account client (lazily imported) |
| aiosqlite | 0.22.1 | Async SQLite (WAL mode) |
| cryptography | 50.0.2 | Fernet credential encryption |
| aiohttp + deps | 3.14.5 | HTTP (via discord-py-self) |

### Dev dependencies
| Package | Version |
|---|---|
| ruff | 0.17.2 |
| mypy | 2.4.0 |

---

## 19. Configuration files

| File | Purpose | Format |
|---|---|---|
| `.env` | Credentials + IDs (PRIMARY_BOT_ID, OWNER_USER_IDS, DISCORD_TOKEN_*, DASHBOARD_TOKEN) | KEY=value |
| `config/global_settings.json` | Global plugin setting overrides | JSON |
| `config/rate_limits.json` | Rate policies + lane config | JSON |
| `config/accounts/<name>.json` | Per-account bootstrap (never tokens) | JSON |
| `state/theme.json` | Dashboard theme persistence | JSON |

---

## 20. Knowledgebase (local-only, 29 files)

Historical/reference documentation. Currently ~70% stale relative to the post-Mudae pivot. Key still-accurate files: SECURITY.md (updated), RATE_LIMITING.md (updated), TESTING.md (updated). Superseded: ARCHITECTURE.md (replaced by root v2 + §14), DASHBOARD.md (pre-pivot).

---

## 21. Skills (`.agents/skills/`)

| Skill | Purpose |
|---|---|
| `systematic-debugging` | Reproduce → evidence → tested hypothesis → fix |
| `test-driven-development` | Failing regression test first |
| `verification-before-completion` | Fresh evidence gate per claim |
| `ci-failure-investigation` | Repo-specific CI workflow |
| `fastapi` | FastAPI best practices (for the upcoming migration) |
| `library-skills` | Agent skill discovery/management |
| `test-flet-apps-dev` | Flet testing (for the upcoming migration) |

---

## 22. Key architectural invariants (from ARCHITECTURE.md §14)

1. ControlAPI is the single application boundary
2. `build()` is the one composition root
3. Plugins are UI-independent, not sandboxed
4. Safety floor 1.5s (raise-only)
5. Snowflakes are digit strings end-to-end
6. Credentials write-only in UI, encrypted at rest
7. Direct account→plugin assignment (fail-closed)
8. `plugin_profiles` ≠ AccountPluginData (separate stores)
9. Sync groups ≠ Fleet (distinct persistence + semantics)
10. `transitions.py` is the sole fleet validator
11. Observations authorize nothing
12. Generation fences reject stale assignments
13. Handoff validates destination first
14. Events are typed, isolated, never authorization
15. Repositories own persistence; migrations append-only
16. `Database(":memory:")` stays usable by tests
17. SPA + builder split (plugins never get arbitrary DOM/state)
18. Server-confirmed mutations (re-fetch after write)
19. Theme engine is a product feature (tokens not hardcoded)
20. Dashboard page lifecycle never affects backend automation
21. Ordered fault-tolerant shutdown (every step runs)
22. Deterministic testing (no real credentials/network)
