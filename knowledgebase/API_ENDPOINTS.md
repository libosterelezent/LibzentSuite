# API ENDPOINTS — the complete surface reference

> THE single exhaustive reference for every dashboard REST route, the
> WebSocket protocol, the CLI command grammar and the plugin
> self-command surface. Source of truth, verified line by line:
> [interfaces/dashboard/server.py](../interfaces/dashboard/server.py)
> (`DashboardServer._route` — every `(method, path)` pair below is a
> literal branch), [interfaces/dashboard/ws.py](../interfaces/dashboard/ws.py),
> [interfaces/cli/commands.py](../interfaces/cli/commands.py) (the `HELP`
> registry) and [core/plugin_manager/commands.py](../core/plugin_manager/commands.py)
> (`FRAMEWORK_COMMANDS` + `SelfCommandHub`).
>
> Companion pages: [DASHBOARD.md](DASHBOARD.md) (SPA behaviour + security),
> [CLI.md](CLI.md) (rendering details), [PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md)
> (the plugin-side view of self-commands).

## Conventions (apply to every route)

* **Envelope** — every JSON route answers `{"ok": true, "data": <payload>}`.
  The `data` column below IS the payload. Errors are
  `{"ok": false, "error": "<message>"}`.
* **Auth** — when `DASHBOARD_TOKEN` is set, every `/api/*` route requires
  `X-Dashboard-Token` (header) or `?token=` (query); comparison is
  constant-time (`hmac.compare_digest`). Without a token configured auth
  is OFF (logged `auth=OFF`). The static SPA and assets are served
  WITHOUT auth; `/ws` requires the token like the REST surface.
* **Credential-gated routes** — `POST /api/accounts/credential`,
  `GET /api/credentials/status`, `POST /api/credentials/import-env` and
  `POST /api/accounts/add` **when its body carries a `credential`**
  additionally REQUIRE a configured token: with no token set they answer
  `403 "set DASHBOARD_TOKEN in .env to manage credentials"` instead of
  becoming an open credential-writing surface.
* **Booleans are strict** (`parse_json_bool`): `{"enabled": "yes"}` is a
  400, never a silent truthy cast. Numbers must be finite
  (`require_finite`); ints reject bools.
* **Status codes** — 400 missing/invalid field (message names the field
  AND the route) or domain error (`ValueError`/`RuntimeError` family);
  401 bad token; 403 credential-gate / cross-site request refused;
  404 no route; 408 stalled body; 409 duplicate account name
  (`/api/accounts/add`); 411 Transfer-Encoding unsupported; 413 body
  > 1 MB; 500 unexpected (traceback logged); 503 main loop unavailable;
  504 route exceeded `ROUTE_TIMEOUT_S = 10 s` on the main loop
  (`POST /api/simulation/run` gets a 200 s budget).
* **Bodies** — JSON objects only; `Content-Length` required (chunked
  bodies are refused with 411); duplicate conflicting `Content-Length`
  headers are refused (smuggling guard).

---

## 1. System — 4 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/status` | — | `{accounts: [...], ratelimit: {...}, tasks: {...}, warnings: [...], uptime_s: float}` | `status()` | The overview snapshot (also the WS `stats` push). Account rows are enriched with `credential` state + active-cooldown counts; a 30 s reconciliation bounds DB divergence |
| POST | `/api/estop` | — | `{cancelled_tasks: int, accounts: [...], halted: true}` | `emergency_stop_all()` | GLOBAL emergency stop: cancel all tasks, stop scheduler + every account |
| POST | `/api/shutdown` | — | `{shutdown: true}` | `shutdown()` | Graceful shutdown (scheduler drain + stop all accounts). With `exit_signal` (main.py only) the process exits after the response |
| POST | `/api/resume` | — | `{resumed: true, status: {...}}` | `system_resume()` | Re-arm the scheduler after a global estop (accounts stay stopped — restart them individually) |

## 2. Accounts — 10 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/accounts` | — | `[{name, status, user_id, enabled, sync_group, profiles, assigned_plugins, plugin_profiles, credential, cooldowns, latency_ms, uptime_s, last_error, plugins}]` | `accounts_list()` | Every account with its DIRECT plugin assignments (`assigned_plugins`), profile bindings (`plugin_profiles`: `{plugin: profile\|null}`) and credential state (`db`/`env`/`missing`) |
| POST | `/api/accounts/add` | body: `name` (req), `enabled?` (strict bool), `token_ref?`, `sync_group?`, `plugins?` (list — direct assignments), `credential?` (token value; one-way into encrypted storage) | the new account's status dict | `accounts_add(body)` | Register + persist. Unknown keys are rejected (a `profiles` key 400s — profiles no longer carry capabilities). Duplicate name → **409** |
| POST | `/api/accounts/remove` | body: `name` | manager's removal payload | `accounts_remove(name)` | Remove a STOPPED account (cascades credential, cooldowns, assignments) |
| GET | `/api/accounts/info` | `?account=` (req) | `{name, status, identity, runtime, plugins, profiles, rate_limit, credential}` | `accounts_info(account)` | The account details page (spec §8) |
| POST | `/api/accounts/enable` | body: `name`, `enabled` (strict bool) | `{account, enabled, ...}` | `accounts_enable(name, enabled)` | Flip the persisted enabled flag (disabled accounts are skipped by start-all) |
| POST | `/api/accounts/start` | body: `account?` (null/absent = every enabled account) | start report | `accounts_start(account?)` | Connect one account or all enabled |
| POST | `/api/accounts/stop` | body: `account?` | stop report | `accounts_stop(account?)` | Graceful stop of one account or all |
| POST | `/api/accounts/emergency-stop` | body: `name` | `{account, cancelled_tasks, stop_initiated: true}` | `accounts_emergency_stop(name)` | Account-scoped estop: cancel its tasks NOW + disconnect |
| POST | `/api/accounts/credential` | body: `name`, `credential` (req, non-empty) | `{account, credential: "db"}` | `accounts_set_credential(name, credential)` | Store (replace) the encrypted credential. **Credential-gated**; the value is NEVER echoed |

## 3. Account plugins (direct assignment) — 4 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/accounts/plugins` | `?account=` (req) | `[plugin name, ...]` (sorted) | `accounts_plugins_list(account)` | The account's DIRECT plugin assignments |
| POST | `/api/accounts/plugins/assign` | body: `account`, `plugin` | `{account, plugin}` | `accounts_plugin_assign(...)` | Assign ONE plugin (idempotent; a RUNNING account's plugin set follows immediately — no restart) |
| POST | `/api/accounts/plugins/unassign` | body: `account`, `plugin` | `{account, plugin, existed: bool}` | `accounts_plugin_unassign(...)` | Remove ONE assignment (idempotent; `existed=false` when it was already gone) |
| POST | `/api/accounts/plugins/set` | body: `account`, `plugins` (list of names — ALL validated first) | `{account, plugins: [...]}` | `accounts_plugins_set(...)` | REPLACE the whole assignment set (all-or-nothing: an unknown name rejects the call) |

Unknown account → `AccountError` (400); unknown plugin → `ValueError`
listing the discovered set (400).

## 4. Account plugin profiles — 4 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/accounts/plugin-profiles` | `?account=` (req) | `{plugin: profile name \| null, ...}` (every ASSIGNED plugin) | `accounts_plugin_profiles(account)` | Current profile bindings of the account |
| POST | `/api/accounts/plugin-profile` | body: `account`, `plugin`, `profile` (non-empty string OR `null` = clear) | `{account, plugin, profile}` | `accounts_plugin_profile(...)` | Bind the assignment to a settings profile (requires the plugin to be ASSIGNED; `null` returns it to plugin defaults — the assignment itself is untouched) |
| GET | `/api/accounts/effective-settings` | `?account=` (req), `?plugin=` (req) | merged settings dict + `"channels"` scope (see below) | `accounts_effective_settings(...)` | The fully merged settings the account actually runs with: plugin Setting defaults ← bound profile (minus the reserved `channels` key) ← per-account overrides; the profile's channel scope rides under `"channels"` |
| GET | `/api/accounts/channels` | `?account=` (req) | `[{guild_id, guild_name, channels: [{id, name, type}]}]` | `accounts_channels(account)` | The account's visible TEXT channels from the live client's READY cache (memory-only, zero API calls; `[]` when offline — the UI falls back to manual channel-id entry) |

## 5. UI extension — 2 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/ui/stats` | `?account=` (req) | `[{plugin, label, value, sub, level}]` | `ui_stats(account)` | Plugin-contributed Home-tab metric cards. `level`: `""`/`ok`/`warn`/`err`. A crashing provider contributes nothing (isolation) |
| GET | `/api/ui/panels` | — | `[{id, plugin, title, icon, sections: [{kind, title, data}]}]` | `ui_panels()` | Plugin-contributed sidebar panels (framework-level, deduped by `id`, sorted). `kind`: `metrics`/`table`/`list`/`text`. Rendered by the dashboard's own templates — structured data only, XSS-safe by construction |

## 5a. Dashboard UI (builder-based plugin pages) — 4 routes

Plugin pages built with the [Dashboard UI API](DASHBOARD_UI_API.md)
(`initialize_dashboard_ui` → `DashboardUIToolset` builders →
`DashboardUIRegistry`). Unknown plugin/page/provider is a MISSING READ
(**404**, not a client payload fault); action-dispatch failures stay
`UIError`/`ValueError` → 400 (they are domain outcomes the frontend
renders inline). Reference example: `tests/fixtures/plugins/config_demo/`.

The dashboard's own MAIN TABS ride these same four routes: the
reserved plugin name `"$framework"` (`core/framework_ui.py`) serves the
home/plugins/activity/policies/admin/tools pages through the identical
pipeline — the SPA's tab renderer and the sidebar filter treat the name
specially, the wire contract does not.

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/dashboard-ui/navigation` | — | `[{plugin, page_id, title, icon, order}]` (sorted by `(order, plugin)`) | `dashboard_ui_navigation()` | Sidebar entries for every attached plugin page. Reads the frozen snapshots only — no plugin code runs, so this is safe for every boot/refresh |
| GET | `/api/dashboard-ui/page` | `?plugin=` (req), `?page_id=` (req) | the page's serialized component tree (`{type, id, …props, children: […]}`) | `dashboard_ui_page(plugin, page_id)` | One page's full builder tree (404 `unknown plugin page` when the plugin has no UI or the id is unknown). Visibility-denied subtrees are PRUNED before the tree crosses the wire (rendered and server-side verdicts match). Actions/data sources serialize as `true` flags — callables never cross the wire |
| POST | `/api/dashboard-ui/action` | body: `plugin` (req), `component_id` (req), `payload?` (object, default `{}`) | `{success, message, data, field_errors, refresh}` — the normalized `ActionResult` | `dashboard_ui_action(...)` | Visibility-checked dispatch of a component's action callback (buttons `payload {}`, standalone inputs `{"value": …}`, forms `{field_id: value}`). `field_errors` key by field id; `refresh` names component ids whose data the frontend re-fetches. Unknown/hidden component or callback failure → `UIError` → 400 |
| GET | `/api/dashboard-ui/data` | `?plugin=` (req), `?component_id=` (req) | the provider's JSON-safe result (tables: row-dict lists or `{columns, rows}`; lists: `{label, value}` rows) | `dashboard_ui_data(...)` | Visibility-checked data-provider fetch (lazy — only when the page is shown). Results are JSON-safe and bounded (lists capped at 500, nesting ≤ 8). Unknown plugin/component, hidden, or no provider → `UIError` → **404** |

## 6. Admin access — 13 routes

The admin panel's store is `core.access_manager.AccessManager` (kv rows
under the `access:` prefix). Prefix modes: `mention` | `symbol` | `both`
(`symbol`/`both` require a 1–4 char whitespace-free symbol).

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/admin/overview` | — | `{admins: [int], mudae_bot_id: int\|null, accounts: {name: {is_admin, prefix_mode, prefix_symbol, allowed_user_ids, user_id}}}` | `admin_overview()` | The admin panel's full view |
| POST | `/api/admin/ids/set` | body: `admins` (list of ints) | `{admins: [...]}` | `admin_set_ids(ids)` | REPLACE the framework admin id list. ⚠ **latent bug**: the route calls `require_json_list(...)`, which is not defined in server.py — the request 500s (see [DISCREPANCIES.md](DISCREPANCIES.md); use `ids/add`/`ids/remove` instead) |
| POST | `/api/admin/ids/add` | body: `user_id` (int) | `{admins: [...]}` | `admin_add_id(user_id)` | Add one admin id |
| POST | `/api/admin/ids/remove` | body: `user_id` (int) | `{admins: [...]}` | `admin_remove_id(user_id)` | Remove one admin id |
| POST | `/api/admin/account-flag` | body: `account`, `is_admin` (bool; absent = false) | the account's access row | `admin_account_flag(...)` | Mark/unmark the ACCOUNT as admin (its Discord user id gains the admin bypass) |
| GET | `/api/admin/perms` | `?account=` (req) | `{account, allowed_user_ids: [int]}` | `admin_perms(account)` | Who may invoke THIS account's commands |
| POST | `/api/admin/perms/set` | body: `account`, `allowed_user_ids` (list of ints) | `{account, allowed_user_ids}` | `admin_perms_set(...)` | REPLACE the per-account allowlist. ⚠ same `require_json_list` latent 500 as `ids/set` — use `perms/add`/`perms/remove` |
| POST | `/api/admin/perms/add` | body: `account`, `user_id` (int) | `{account, allowed_user_ids}` | `admin_perms_add(...)` | Allow one user id on this account |
| POST | `/api/admin/perms/remove` | body: `account`, `user_id` (int) | `{account, allowed_user_ids}` | `admin_perms_remove(...)` | Revoke one user id |
| GET | `/api/admin/prefix` | `?account=` (req) | `{account, prefix_mode, prefix_symbol}` | `admin_prefix(account)` | The account's command-prefix configuration |
| POST | `/api/admin/prefix/set` | body: `account`, `mode` (mention/symbol/both), `symbol?` (string, default `""`) | `{account, prefix_mode, prefix_symbol}` | `admin_prefix_set(...)` | Set the prefix config (applies on next connect/rebuild; `CredentialsError` → 400 on a bad mode/symbol) |
| GET | `/api/admin/mudae-id` | — | `{mudae_bot_id: int\|null}` | `admin_mudae_id()` | The Mudae bot id (DB value first, env `MUDAE_BOT_ID` fallback) |
| POST | `/api/admin/mudae-id/set` | body: `mudae_bot_id` (int or `null`) | `{mudae_bot_id}` | `admin_mudae_id_set(...)` | Store/clear the DB value (`null` clears — env fallback applies again) |

Semantics: **fail-closed**. An empty per-account `allowed_user_ids`
means NOBODY may invoke that account's commands except admin ids and
admin-flagged accounts' own user ids.

## 7. Credentials — 2 routes (both credential-gated)

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/credentials/status` | — | `[{name, credential: "db"\|"env"\|null}]` | `accounts_credential_status()` | Per-account credential state — display-safe, NEVER a value |
| POST | `/api/credentials/import-env` | body: `overwrite?` (strict bool, absent = false) | `{imported: [names], skipped: [names], failed: {name: reason}}` | `credentials_import_env(overwrite)` | MIGRATION-ONLY one-shot import of legacy `.env` tokens into encrypted storage |

## 8. Cooldowns — 2 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/cooldowns` | `?account=` (optional) | `[{account, plugin, cooldown_key, remaining_s, source, reason}]` (soonest first) | `cooldowns_list(account?)` | Active persistent cooldowns (one account or every) |
| POST | `/api/cooldowns/clear` | body: `account`, `plugin`, `cooldown_key?` (null = ALL of that plugin) | `{account, plugin, cooldown_key, cleared: int}` | `cooldowns_clear(...)` | Clear one cooldown or every cooldown of a plugin on an account |

## 9. Plugins — 5 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/plugins` | — | manifests: `[{name, version, description, settings: [...]}]` | `plugins_manifests()` | Discovered plugins incl. settings schema (auto-generates the editors) |
| GET | `/api/plugins/state` | `?account=` (req) | `[{name, enabled, loaded, settings, last_error, error_count}]` | `plugins_state(account)` | Per-account runtime state |
| POST | `/api/plugins/set-enabled` | body: `account`, `plugin`, `enabled` (strict bool) | `{account, plugin, enabled}` | `plugins_set_enabled(...)` | Enable/disable for this account (lifecycle hooks fire) |
| GET | `/api/plugins/settings` | `?account=` (req) | `[{name, settings: {...}}]` | `plugins_settings(account)` | Resolved settings per plugin |
| POST | `/api/plugins/set-setting` | body: `account`, `plugin`, `key`, `value` (ANY JSON type; `null` = RESET — deletes the account-layer override) | `{account, plugin, key, value}` (or the `cleared` payload) | `plugins_set_setting(...)` | Validate + persist one account-layer override (live view updates in place) |

## 10. Plugin profiles — 4 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/plugins/profiles` | `?plugin=` (optional) | `[{id, plugin, name, description, settings, created_at, updated_at}]` | `plugin_profiles_list(plugin?)` | The profile library (one plugin's or all), plugin-then-name order |
| POST | `/api/plugins/profiles/create` | body: `plugin`, `name` (req), `description?` (default `""`), `settings?` (object; validated against the plugin's Setting schema + the reserved `channels` scope) | the created profile record | `plugin_profile_create(...)` | Create a settings bundle. Unknown plugin/key, bad value, malformed channels scope → 400 naming the field; duplicate (plugin, name) → 400 |
| POST | `/api/plugins/profiles/update` | body: `plugin`, `name` (req) + at least ONE of `name_new`, `description`, `settings` (null = keep) | the updated record | `plugin_profile_update(...)` | Update fields; a rename rewrites bound accounts' pointers in the same transaction |
| POST | `/api/plugins/profiles/delete` | body: `plugin`, `name` | `{plugin, profile, existed: bool}` | `plugin_profile_delete(...)` | Delete; bound accounts' bindings cascade to NULL (plugin defaults) |

## 11. Profiles (retained legacy infra) — 5 routes

> Profile records are RETAINED FUTURE INFRASTRUCTURE — they have NO
> effect on which plugins an account runs (capabilities are the direct
> `/api/accounts/plugins*` assignments above).

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|

## 12. Sync groups — 9 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/groups` | — | `[{id, name, description, enabled, member_count}]` | `groups_list()` | Every sync group |
| GET | `/api/groups/info` | `?group=` (req) | `{id, name, description, enabled, pacing_key, member_count, members: [{name, status, enabled}]}` | `groups_snapshot(group)` | Group snapshot enriched with live member status |
| POST | `/api/groups/create` | body: `name` (req), `description?`, `enabled?` (strict bool) | the group row | `groups_create(payload)` | Create (may have zero members) |
| POST | `/api/groups/update` | body: `group` + ≥1 of `name`, `description`, `enabled` | the updated row | `groups_update(...)` | Update; a rename moves every member's pacing scope with it |
| POST | `/api/groups/delete` | body: `group`, `detach_members?` (strict bool, default false) | `{deleted, detached: [...]}` | `groups_delete(...)` | Refuses while members remain unless `detach_members=true` |
| POST | `/api/groups/add-member` | body: `group`, `account` | membership payload | `groups_add_account(...)` | Add an account to a group |
| POST | `/api/groups/remove-member` | body: `group`, `account` | `{group, account, existed, ...}` | `groups_remove_account(...)` | Remove (back to per-account pacing) |
| POST | `/api/groups/set-account` | body: `account`, `group` | membership payload | `groups_set_account(...)` | Set/replace the account's group |
| POST | `/api/groups/clear-account` | body: `account` | membership payload | `groups_clear_account(...)` | Remove from any group |

## 13. Rate limits — 8 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/ratelimit/policies` | — | `[{level, owner, action, interval, jitter}]` | `ratelimit_policies()` | Every stored policy |
| POST | `/api/ratelimit/set` | body: `action`, `interval` (finite), `jitter_min?` (default 0), `jitter_max?` (default 0), `level?` (default `"global"`), `owner?` (null/"" = global) | `{action, level, owner, policy: "2.50s +0.10..0.50s"}` | `ratelimit_set(...)` | Set a policy (engine first, then best-effort persist) |
| POST | `/api/ratelimit/clear` | body: `level`, `owner?`, `action` | `{cleared: bool, level, owner, action}` | `ratelimit_clear(...)` | Remove one policy (tombstoned so first-boot defaults don't resurrect it) |
| GET | `/api/ratelimit/effective` | `?action=` (req), `?account=`, `?plugin=` | `{action, interval, jitter, sources: [...]}` | `ratelimit_effective(...)` | What actually paces this (account, plugin, action) — MAX across levels |
| GET | `/api/ratelimit/stats` | — | `{floor, lane, groups, policies, cooldowns: {global, accounts}, hits_429: {accounts, total}, last_fire_ages, grants, waits, avg_wait_s}` | `ratelimit_stats()` | Visualization data incl. the 429 telemetry card |
| GET | `/api/ratelimit/warnings` | — | `[{level, code, message}]` | `ratelimit_warnings()` | Safety review of the configuration |
| POST | `/api/ratelimit/floor` | body: `seconds` (finite) | `{floor: float}` | `ratelimit_set_floor(seconds)` | Raise the pacing floor (persisted; can never drop below the hard minimum) |
| GET / POST | `/api/ratelimit/lane` | GET —; POST body: `enabled?` (bool), `prefixrate?` (number ≥ 0; at least one required) | `{enabled: false, prefixrate: 0, removed: true, ...}` | `ratelimit_lane()` / `ratelimit_set_lane(...)` | **REMOVED feature stub**: the fleet send lane was removed (sync groups + per-account pacing replace it); the routes answer the compat stub and mutations are ignored |

Learned pacing: when NO policy is stored for an action, the engine
falls back to `DEFAULT_ACTION_INTERVALS` (message 2.6 s, dm 2.6 s,
reaction 2.5 s, component 3.0 s — measured zero-throttle rates).
Stored policies compose on top with max-wins semantics.

## 14. Tasks — 4 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/tasks` | `?status=`, `?account=` | `[{id, status, account, plugin, action, channel_id}]` | `tasks_list(...)` | Queued/running tasks with optional filters |
| POST | `/api/tasks/cancel` | body: `task_id` (int) | `{cancelled: bool, task_id}` | `tasks_cancel(task_id)` | Cancel one task |
| POST | `/api/tasks/cancel-matching` | body: `account?`, `plugin?` | `{cancelled: int}` | `tasks_cancel_matching(...)` | Bulk cancel |
| GET | `/api/tasks/history` | `?limit=50`, `?account=` | history rows | `tasks_history(...)` | Finished tasks (outcome + duration) |

## 15. Simulation — 2 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/simulation/list` | — | `[{name, description}]` | `simulation_list()` | Available offline scenarios |
| POST | `/api/simulation/run` | body: `name` | `{name, passed, duration_s, failures, log_lines, interactions, summary}` | `simulation_run(name)` | Run one scenario in-process (200 s route budget — the runner bounds runs at ~185 s) |

## 16. Logs & audit — 2 routes

| Method | Path | Request | `data` payload | ControlAPI | Description |
|---|---|---|---|---|---|
| GET | `/api/logs` | `?n=100`, `?account=`, `?severity=` | `[{ts, severity, source, account, plugin, task_id, event, message}]` | `logs_tail(...)` | Structured log tail |
| GET | `/api/audit` | `?n=100`, `?account=` | `[{ts, account, direction, action, channel_id, content, wait_ms, jitter_ms, note}]` | `audit_tail(...)` | Interaction audit trail |

## 17. Static & WebSocket — 5 routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` and `/index.html` | none | the SPA (prompts for the token itself) |
| GET | `/styles/*` | none | CSS assets (path-traversal guarded, correct MIME) |
| GET | `/js/*` | none | JS assets (same guards) |
| GET | `/ws` (Upgrade) | token (when set) | WebSocket upgrade (see below) |

**Route count: 84 JSON routes + 5 static/WS.**

---

## WebSocket protocol (`/ws`)

Handshake: `GET` + `Upgrade: websocket` + `Connection: upgrade` +
`Sec-WebSocket-Version: 13` + `Sec-WebSocket-Key`, else 400 with an
actionable message. Auth via `?token=` or `X-Dashboard-Token` (401 when
wrong). Frames: the RFC 6455 subset in `ws.py` — text only, no
fragmentation reassembly, inbound frames masked, 1 MB frame cap,
protocol violations close the connection (1002; invalid UTF-8 → 1007).

**Server → client** (JSON text frames, the `type` field discriminates):

| `type` | Fields | When |
|---|---|---|
| `stats` | `**api.status()` — `accounts`, `ratelimit` (incl. `hits_429`), `tasks`, `warnings`, `uptime_s` | every second (broadcaster survives API errors), immediately on connect and on `refresh`; per-tick snapshot shared across clients |
| `log` | `**entry` — `ts, severity, source, account?, plugin?, task_id?, event?, message` | every log-service entry |
| `audit` | `**entry` — `ts, account, direction, action, channel_id, content, wait_ms, jitter_ms, note?` | every audited interaction |
| `task` | `event` (`TaskStarted`/`TaskCompleted`/`TaskFailed`/`TaskCancelled`) + the event's fields | task bus events |
| `error` | `error` | a per-client stats fetch failed |

**Client → server:** the single app message `refresh` forces an
immediate stats push (debounced: ≤1 in flight, ≥0.1 s apart). Pings are
answered with pongs; CLOSE is echoed per RFC 6455.

---

## CLI command grammar

`python3 main.py` (REPL, prompt `sm> `) or `python3 main.py <command …>`
(one-shot). shlex parsing; exact argument counts; one `error: …` line
per failure (never a traceback, never exits the REPL). Full rendering
reference: [CLI.md](CLI.md).

| Group | Grammar |
|---|---|
| `help` | `help [command]` |
| `status` | `status` |
| `accounts` | `list` · `add <name> [plugin ...]` · `plugins <account>` · `plugin-add <account> <plugin>` · `plugin-remove <account> <plugin>` · `remove <name>` · `info <name>` · `credential <name> <token>` · `enable <name> <on\|off>` · `start [name\|all]` · `stop [name\|all]` · `estop <name>` |
| `credentials` | `status` · `import-env [--overwrite]` |
| `cooldowns` | `[account]` · `clear <account> <plugin> [key]` |
| `plugins` | `list` · `state <account>` · `enable <account> <plugin>` · `disable <account> <plugin>` · `settings <account>` · `set <account> <plugin> <key> <value>` |
| `groups` | `list` · `show <group>` · `create <name> [description]` · `update <group> <field> <value>` · `delete <group> [--detach-members]` · `add-member <group> <account>` · `remove-member <group> <account>` · `set-account <account> <group>` · `clear-account <account>` |
| `ratelimit` | `show` · `stats` · `set <action> <interval> [jitter_min jitter_max] [level owner]` · `clear <level> <owner> <action>` · `effective <action> [account] [plugin]` · `warnings` · `floor <seconds>` · `lane [on [rate]\|off\|rate <s>]` (lane = removed-feature stub) |
| `tasks` | `list [status]` · `cancel <id>` · `history [n]` · `cancel-matching [account] [plugin]` |
| `simulation` | `list` · `run [name]` (no name = UsageError listing scenarios) |
| `logs` | `[n] [account] [DEBUG\|INFO\|WARNING\|ERROR]` |
| `audit` | `[n] [account]` |
| `estop` | `[resume]` (alias `unhalt`) |
| `quit` | (alias: `exit`) |

Policy levels: `global|account|plugin|account_plugin`; `global` takes
no owner (CLI placeholder `-`/`none`), `account_plugin` owner is
`<account>/<plugin>`.

> NOTE: `prefixrate`, `adminlist`, `adminperms` and `adminprefix` are
> **Discord self-command verbs** (see below), NOT CLI commands. The CLI
> has no admin group — the admin panel is dashboard-driven
> (`/api/admin/*`), and per-account pacing is adjustable from Discord
> via `prefixrate` or the `ratelimit set … account <name>` CLI form.

---

## Plugin self-command surface (Discord)

Invoked as `<prefix><command> [args...]` where the prefix is the
account's admin-panel prefix config (`mention`/`symbol`/`both`; env
`SELF_COMMAND_PREFIX` is the default when a per-account config is
absent). The invoking Discord user must pass
`AccessManager.can_invoke(account, user_id)` (per-account allowlist +
admin bypass, fail-closed) — the adapter gates first and the executor
re-checks (defense in depth). Replies go back through the paced
pipeline (`MAX_REPLY_CHARS = 1900`, NORMAL priority).

**Framework verbs** (declared in `core/plugin_manager/commands.py`,
category in parentheses; reserved names — a plugin re-declaring one is
dropped with a warning):

| Command | Usage | Category | Description |
|---|---|---|---|
| `adminlist` | — | Settings | list admins and per-account access (admin-only) |
| `adminperms` | `<account> <add\|remove\|list> [user_id]` | Settings | manage per-account command perms (admin-only) |
| `adminprefix` | `<account> <mention\|symbol\|both> [symbol]` | Settings | set an account's command prefix (admin-only; applies on next connect) |
| `prefixrate` | `[seconds]` | Settings | show/set THIS account's send pacing (message-action interval; a per-account policy override that persists; warns below the measured safe ~2.6 s) |
| `pg` | — | Utilities | gateway latency + uptime of this account |
| `say` | `<text>` | Utilities | repeats the invoker's message verbatim |
| `help` | `[command]` / `[page]` | No Category | plugin-grouped listing (PLUGIN-NAME headers, 25-char truncated descriptions, paginated ~1750 chars/page); `help <command>` shows usage + description |

**Plugin-declared commands** — a plugin declares `COMMANDS: Tuple[CommandSpec, ...]`
on its class; each `CommandSpec(name, help, usage, args=(ArgSpec(...),),
category, handler)` becomes available on every account the plugin is
ASSIGNED to and ENABLED on. The declarative `ArgSpec` grammar
(`name`, `kind` = str/int/float/callable, `required`, `default`,
`greedy`, `choices`) is parsed/validated BEFORE the handler runs —
failures render the auto-generated usage line. Reference:
[PLUGIN_SYSTEM.md](PLUGIN_SYSTEM.md) §self-commands.
