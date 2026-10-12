# DASHBOARD — REST + WebSocket + SPA reference

Source of truth: [interfaces/dashboard/server.py](../interfaces/dashboard/server.py),
[interfaces/dashboard/ws.py](../interfaces/dashboard/ws.py) (RFC 6455 subset),
[interfaces/dashboard/static/index.html](../interfaces/dashboard/static/index.html)
(single-file SPA, no build step). The REST surface maps 1:1 onto
`core.api.ControlAPI`.

## Running it

```bash
python3 main.py --dashboard                # REPL + dashboard on 127.0.0.1:8070
python3 main.py --dashboard --headless     # daemon mode, no terminal
python3 main.py --dashboard --port 8080    # custom port
python3 main.py --start-all --dashboard    # boot every enabled account too
```

The boot line prints `dashboard: http://127.0.0.1:<port>`. Set
`DASHBOARD_TOKEN` in `.env` to require auth.

---

## 1. REST route table

> This page summarizes the routes the SPA actually drives. The
> COMPLETE endpoint reference — every route with request fields,
> response payload and status semantics, plus the WS protocol, the CLI
> grammar and the self-command surface — is
> [API_ENDPOINTS.md](API_ENDPOINTS.md).

Every response body is `{"ok": true, "data": …}`; errors are
`{"ok": false, "error": "<message>"}`. Query params are shown as `?a=`;
body fields as JSON. "Auth" applies to every `/api/*` route (see §3).

### System

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/status` | — | `status()` |
| POST | `/api/estop` | — | `emergency_stop_all()` |
| POST | `/api/shutdown` | — | `shutdown()` |
| POST | `/api/resume` | — | `system_resume()` |

### Accounts

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/accounts` | — | `accounts_list()` |
| POST | `/api/accounts/add` | body: `name` (req, non-empty str), `enabled?` (strict bool), `token_ref?`, `sync_group?`, `plugins?` (list of names — DIRECT assignments; a `profiles` key is REJECTED by the backend) | `accounts_add(body)` |
| POST | `/api/accounts/remove` | body: `name` | `accounts_remove(name)` |
| GET | `/api/accounts/info` | `?account=` (req) | `accounts_info(account)` |
| POST | `/api/accounts/enable` | body: `name`, `enabled` (strict bool) | `accounts_enable(name, enabled)` |
| POST | `/api/accounts/start` | body: `account?` | `accounts_start(account?)` |
| POST | `/api/accounts/stop` | body: `account?` | `accounts_stop(account?)` |
| POST | `/api/accounts/emergency-stop` | body: `name` | `accounts_emergency_stop(name)` |
| POST | `/api/accounts/credential` | body: `name`, `credential` (**credential-gated**) | `accounts_set_credential(...)` |

### Account plugins (direct assignment)

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/accounts/plugins` | `?account=` (req) | `accounts_plugins_list(account)` |
| POST | `/api/accounts/plugins/assign` | body: `account`, `plugin` | `accounts_plugin_assign(...)` |
| POST | `/api/accounts/plugins/unassign` | body: `account`, `plugin` | `accounts_plugin_unassign(...)` |
| POST | `/api/accounts/plugins/set` | body: `account`, `plugins` (list of names) | `accounts_plugins_set(...)` |

### Account plugin profiles

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/accounts/plugin-profiles` | `?account=` (req) | `accounts_plugin_profiles(account)` |
| POST | `/api/accounts/plugin-profile` | body: `account`, `plugin`, `profile` (string or `null` = clear) | `accounts_plugin_profile(...)` |
| GET | `/api/accounts/effective-settings` | `?account=` (req), `?plugin=` (req) | `accounts_effective_settings(...)` |
| GET | `/api/accounts/channels` | `?account=` (req) | `accounts_channels(account)` |

### UI extension

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/ui/stats` | `?account=` (req) | `ui_stats(account)` — Home-tab stat cards |
| GET | `/api/ui/panels` | — | `ui_panels()` — plugin sidebar panels |

### Admin access

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/admin/overview` | — | `admin_overview()` |
| POST | `/api/admin/ids/set` | body: `admins` (list of int) | `admin_set_ids(...)` ⚠ latent 500 — see note |
| POST | `/api/admin/ids/add` / `ids/remove` | body: `user_id` (int) | `admin_add_id` / `admin_remove_id` |
| POST | `/api/admin/account-flag` | body: `account`, `is_admin` (bool) | `admin_account_flag(...)` |
| GET | `/api/admin/perms` | `?account=` (req) | `admin_perms(account)` |
| POST | `/api/admin/perms/set` | body: `account`, `allowed_user_ids` (list of int) | `admin_perms_set(...)` ⚠ latent 500 — see note |
| POST | `/api/admin/perms/add` / `perms/remove` | body: `account`, `user_id` (int) | `admin_perms_add` / `admin_perms_remove` |
| GET | `/api/admin/prefix` | `?account=` (req) | `admin_prefix(account)` |
| POST | `/api/admin/prefix/set` | body: `account`, `mode` (`mention`/`symbol`/`both`), `symbol?` | `admin_prefix_set(...)` |
| GET | `/api/admin/mudae-id` | — | `admin_mudae_id()` |
| POST | `/api/admin/mudae-id/set` | body: `mudae_bot_id` (int or `null`) | `admin_mudae_id_set(...)` |

> ⚠ `POST /api/admin/ids/set` and `POST /api/admin/perms/set` call a
> `require_json_list` helper that is not defined in server.py — both
> currently answer 500. The Admin tab only uses the `add`/`remove`
> forms, which work. (Tracked in [DISCREPANCIES.md](DISCREPANCIES.md).)

### Credentials / cooldowns

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/credentials/status` | — (**credential-gated**) | `accounts_credential_status()` |
| POST | `/api/credentials/import-env` | body: `overwrite?` (strict bool; **credential-gated**) | `credentials_import_env(...)` |
| GET | `/api/cooldowns` | `?account=` (optional) | `cooldowns_list(account?)` |
| POST | `/api/cooldowns/clear` | body: `account`, `plugin`, `cooldown_key?` | `cooldowns_clear(...)` |

### Plugins

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/plugins` | — | `plugins_manifests()` |
| GET | `/api/plugins/state` | `?account=` (req) | `plugins_state(account)` |
| POST | `/api/plugins/set-enabled` | body: `account`, `plugin`, `enabled` (strict bool) | `plugins_set_enabled(...)` |
| GET | `/api/plugins/settings` | `?account=` (req) | `plugins_settings(account)` |
| POST | `/api/plugins/set-setting` | body: `account`, `plugin`, `key` (non-empty strings), `value` (**any** JSON type; `null` = reset to default) | `plugins_set_setting(...)` |

### Plugin profiles

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/plugins/profiles` | `?plugin=` (optional) | `plugin_profiles_list(plugin?)` |
| POST | `/api/plugins/profiles/create` | body: `plugin`, `name`, `description?`, `settings?` (schema-validated) | `plugin_profile_create(...)` |
| POST | `/api/plugins/profiles/update` | body: `plugin`, `name` + ≥1 of `name_new`/`description`/`settings` | `plugin_profile_update(...)` |
| POST | `/api/plugins/profiles/delete` | body: `plugin`, `name` | `plugin_profile_delete(...)` |

### Profiles

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/profiles` | — | `profiles_list()` |
| POST | `/api/profiles/create` | body: `name`, `plugins` (list of str), `description?` | `profiles_create(...)` |
| POST | `/api/profiles/delete` | body: `name` | `profiles_delete(name)` |
| POST | `/api/profiles/assign` | body: `account`, `profile` | `profiles_assign(...)` |
| POST | `/api/profiles/unassign` | body: `account`, `profile` | `profiles_unassign(...)` |

### Rate limits

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/ratelimit/policies` | — | `ratelimit_policies()` |
| POST | `/api/ratelimit/set` | body: `action`, `interval` (finite number), `jitter_min?`, `jitter_max?`, `level?` (default `"global"`), `owner?` (null/"" = global) | `ratelimit_set(...)` |
| POST | `/api/ratelimit/clear` | body: `level`, `owner?`, `action` | `ratelimit_clear(...)` |
| GET | `/api/ratelimit/effective` | `?action=` (req), `?account=`, `?plugin=` | `ratelimit_effective(...)` |
| GET | `/api/ratelimit/stats` | — | `ratelimit_stats()` — incl. the `hits_429` telemetry (per-account throttle hits + fleet total) |
| GET | `/api/ratelimit/warnings` | — | `ratelimit_warnings()` |
| POST | `/api/ratelimit/floor` | body: `seconds` (finite number) | `ratelimit_set_floor(seconds)` |
| GET | `/api/ratelimit/lane` | — | `ratelimit_lane()` — **removed-feature stub** (`{"removed": true, ...}`) |
| POST | `/api/ratelimit/lane` | body: `enabled?` (bool), `prefixrate?` (number ≥ 0); at least one required | `ratelimit_set_lane(...)` — stub, setting ignored |

### Tasks

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/tasks` | `?status=`, `?account=` | `tasks_list(status?, account?)` |
| POST | `/api/tasks/cancel` | body: `task_id` (int; bools rejected, integral floats tolerated) | `tasks_cancel(task_id)` |
| POST | `/api/tasks/cancel-matching` | body: `account?`, `plugin?` | `tasks_cancel_matching(...)` |
| GET | `/api/tasks/history` | `?limit=50`, `?account=` | `tasks_history(limit, account?)` |

### Simulation / logs / audit

| Method | Path | Payload | → ControlAPI |
|---|---|---|---|
| GET | `/api/simulation/list` | — | `simulation_list()` |
| POST | `/api/simulation/run` | body: `name` | `simulation_run(name)` |
| GET | `/api/logs` | `?n=100`, `?account=`, `?severity=` | `logs_tail(...)` |
| GET | `/api/audit` | `?n=100`, `?account=` | `audit_tail(...)` |

### Static / WS

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/`, `/index.html` | **none** (the SPA prompts for the token itself) | serves the SPA |
| GET | `/ws` (Upgrade) | token required when set | WebSocket upgrade |

Unknown route → `404 {"ok": false, "error": "no route <METHOD> <path>"}`.

---

## 2. WebSocket protocol

**Handshake** (`_handle_websocket`): must be `GET` with
`Upgrade: websocket`, `Connection: upgrade`, `Sec-WebSocket-Version: 13` and
a `Sec-WebSocket-Key`; otherwise 400 with an actionable message. Auth: the
token via `?token=` **or** the `X-Dashboard-Token` header (401 when wrong).
Frames are the stdlib-free RFC 6455 subset in `ws.py` (text only, no
fragmentation reassembly, inbound frames must be masked, frame cap 1 MB;
protocol violations close the connection).

**Server → client** (JSON text frames):

| `type` | Fields | When |
|---|---|---|
| `stats` | `**api.status()` (accounts, ratelimit, tasks, warnings, uptime_s) | every second (`BROADCAST_INTERVAL = 1.0 s`; the broadcaster loop survives API errors), immediately on connect, and on `refresh` |
| `log` | `**entry` (ts, severity, source, account?, plugin?, task_id?, event?, message) | every log-service entry (`attach_logs`) |
| `audit` | `**entry` (ts, account, direction in/out, action, channel_id, content, wait_ms, jitter_ms, note?) | every audited interaction (auto-subscribed via `api.audit_recorder()`) |
| `task` | `event` (`TaskStarted`/`TaskCompleted`/`TaskFailed`/`TaskCancelled`) + event fields | bus events (requires the `bus=` wiring from bootstrap) |
| `error` | `error` | a per-client stats fetch failed |

**Client → server:** the single app message `refresh` forces an immediate
stats push. Pings are answered with pongs; `OP_CLOSE` closes cleanly.

**SPA side** (`connectWS`): reconnect with exponential backoff starting at
1000 ms, capped at 10 s, **retry cap 10 attempts** then the honest message
`live updates stopped — check the token or server, then reload`; after 3
failed attempts it hints `reconnecting… (bad token?)`. Render throws are
caught and shown instead of freezing the UI on stale data.

---

## 3. Security model

| Concern | Rule |
|---|---|
| Bind address | `127.0.0.1` by default (`DashboardServer(host=...)`) — do not expose publicly without a token |
| Auth token | When `DASHBOARD_TOKEN` is set: REST requires `X-Dashboard-Token` **or** `?token=`; the WS handshake requires the same. Wrong/missing → `401` with a hint. No token configured → auth **OFF** (logged `auth=OFF`) |
| Static index | Served **without auth** (the SPA needs to load before it can prompt; `?token=` seeding + `history.replaceState` cleans the URL) |
| Body cap | `Content-Length > 1 000 000` bytes → bounded drain then `413` |
| WS frame cap | inbound frames `> 1 000 000` bytes → `ProtocolViolation`, connection closed |
| Booleans | strict (`parse_json_bool`) — `{"enabled": "yes"}` is a 400, never a silent truthy cast |
| NaN/inf | rejected (`require_finite`) |
| XSS | the SPA escapes every interpolated server value (`esc()`) before `innerHTML` |

**Status-code semantics** (from `_route` / `call`):

| Code | Meaning |
|---|---|
| 400 | missing/invalid field (message names the field **and** the route), strict-bool violation, or a domain error (`ValueError`/`RuntimeError` family from `core/errors.py`) |
| 401 | bad or missing token |
| 404 | no such route |
| 413 | request body > 1 MB |
| 500 | unexpected failure (traceback logged server-side) |
| 503 | main asyncio loop unavailable |
| 504 | route coroutine exceeded `ROUTE_TIMEOUT_S = 10.0 s` on the main loop (may still complete) |

---

## 4. SPA pages (sidebar sections)

Nav (`app.js` `NAV`): **Home · Accounts & Groups · Plugins · Profiles ·
Activity · Policies · Admin · Tools** — plus any plugin-contributed
panels appended after Tools (`plugin-ui.js`). Mobile keeps the five
primary items; Profiles/Admin stay reachable through navigation.

| Section | Content |
|---|---|
| Home | live stat cards from WS `stats` (accounts, tasks, warnings, uptime) **+ plugin-contributed metric cards** (`GET /api/ui/stats?account=…`, rendered by `pluginStatsHTML`) |
| Accounts & Groups | account cards + details dialog (Status/Identity/Runtime cache/Plugins/Rate Limit); add/enable/start/stop/estop; credential dialog; Assign-Plugins flow (`/api/accounts/plugins*`); sync-group cards |
| Plugins | per-plugin cards rebuilt around the **profile library**: manifests + per-account enable/disable + settings editors (auto-generated from the schema), the plugin's profiles (create/edit/delete; the editor's channel picker draws on `/api/accounts/channels` from any assigned account's live cache) and the **per-account profile quick-bind** select (`/api/accounts/plugin-profile`) |
| Profiles | account-centric bindings: pick an account → one card per ASSIGNED plugin with its bound profile (bind/unbind/edit, `effective…` merged-settings view via `/api/accounts/effective-settings`) |
| Activity | live log + audit trails (WS `log`/`audit` with REST backfill) |
| Policies | policies table, set/clear, effective, warnings, floor, **the 429 telemetry card** (`hits_429`: fleet total + per-account hits, warn-colored when non-zero) |
| Admin | the access panel (`admin-ui.js`): framework admin id list, Mudae bot id, and per-account rows — command-permission id chips, prefix mode/symbol controls, admin flag toggle. Every mutation hits a real `/api/admin/*` endpoint |
| Tools | tasks queue + history; simulation list + run output; global emergency stop, graceful shutdown, token save/forget |
| *(plugin panels)* | whatever enabled plugins contribute via `ui_panels()` — a nav entry + content area per `UIPanel`, rendered from structured sections (metrics/table/list/text), values escaped at render time |

Header: WS status dot + connection message, `push refresh` button (sends the
`refresh` message), token box (Enter-to-save). `#errbox` accrues timestamped
errors and is **never** cleared by successes.

Related: [CLI.md](CLI.md) (same facade), [SECURITY.md](SECURITY.md),
[CONFIGURATION.md](CONFIGURATION.md).

---

## Debug mode + post-merge additions

> Verified at commit `487dc53`.

* `--debug` flag: DEBUG-level structured logs on the Logs page, discord
  gateway INFO in stderr, DB telemetry, scheduler pressure in stats.
* `attach_audit` is IDEMPOTENT (duplicate-audit-lines fix: start()
  auto-attach + main.py manual attach double-subscribed, broadcasting
  every entry twice).
* Shared per-tick stats snapshot: one API build per broadcast second,
  reused by per-client refreshes within the tick.
* WS slow-dribble hardening (round-2): a partial frame must complete
  within `WS_FRAME_DEADLINE_S` or the client is closed.
