# CONFIGURATION — complete settings reference

Everything LibzentSuite can be tuned with, verified against source:
`.env` ([core/credentials.py](../core/credentials.py)), `config/*.json`
([core/config.py](../core/config.py)), framework account settings
([core/settings_manager.py](../core/settings_manager.py)) and the SQLite
state ([database/schema.py](../database/schema.py)).

Priority order for any conflict: **source code → tests → config schema →
this file.** See [AGENT_GUIDE.md](AGENT_GUIDE.md).

> Runtime dependencies note: the async persistence layer requires
> **`aiosqlite`** and the credential encryption requires
> **`cryptography`** — both are listed in `requirements.txt` (the
> stdlib-only core era ended with the async conversion; see
> [DATABASE.md](DATABASE.md)).

---

## 1. `.env` — credentials layer (layer 0)

Parsed by `Credentials.parse_env` ([core/credentials.py](../core/credentials.py)):
one `KEY=value` per line; `#` comments and blank lines skipped; optional
`export ` prefix; matching surrounding `"`/`'` quotes stripped; lines without
`=` silently skipped. A missing file yields **empty credentials + a warning**
(not a crash). Path is overridable (`--env` / `build(env_file=...)`).

### 1.1 Key classification

| Key | Classification | Purpose | Validation | Mutability / restart | Security |
|---|---|---|---|---|---|
| `DISCORD_TOKEN_<NAME>` | **SECRET — credential (LEGACY)** | Per-account token convention: account `main` → `DISCORD_TOKEN_MAIN` (name upper-cased, `Credentials.token_for`). LEGACY storage: the **DB credential store takes precedence** — if an encrypted credential exists for the account it is used and the env value is ignored; if not, the env value is the fallback (status `env`, never auto-stored). Converting to encrypted storage is the explicit `credentials import` command (one-shot per account; never overwrites an existing `db` credential unless forced) | Absent token + no DB credential + `accounts start` → `CredentialsError` / status `missing_credential` naming the exact key to set | Values parsed once at boot → **restart the process** to change; an imported DB credential can be replaced at runtime via `accounts set-credential` (no restart) | **Secret.** `.env` should be `chmod 600`; masked by `redacted()` (first 4 chars + length, or `***` when ≤ 4 chars). DB-stored copies are Fernet-encrypted at rest ([SECURITY.md](SECURITY.md)) |
| `LZ_MASTER_KEY` | **SECRET — key material** | Master key for the encrypted credential store (urlsafe-base64 Fernet key). When set it wins over the key file; when absent a key file is used/created at `state/credentials.key` (chmod 0600, outside the DB, `state/` is gitignored) | Invalid non-Fernet value raises `CredentialsError` at first use with generation instructions | Read lazily on first credential use → restart to change. **Losing the key means re-entering every stored credential — by design** (no recoverable plaintext anywhere) | **Highest-value secret in the deployment**: possession of key + DB file = all credentials. Never commit; rotate by re-importing |
| `MUDAE_BOT_ID` | Public identifier (bootstrap) | Mudae bot user id — **DB-first now**: the authoritative source is the admin-panel kv row `access:mudae_bot_id` (`AccessManager.mudae_bot_id`); this env value is the FALLBACK when the DB value is unset (and `POST /api/admin/mudae-id/set` `null` clears back to it). Plugins read it via `services.credentials`, never hardcode it | `require("MUDAE_BOT_ID")` raises `CredentialsError` when missing; non-integer raises `CredentialsError` | Loaded at boot → **restart to change** (the DB value can change at runtime via the admin panel) | Public identifier (not a secret), but keep it in `.env` anyway |
| `OWNER_USER_IDS` | Authorization (**DEPRECATED after migration**) | Comma- or semicolon-separated Discord user ids. Read **ONCE** by `AccessManager.migrate_from_credentials` (bootstrap) to seed the DB admin list (`access:admins`) when it is EMPTY — first boot on the new model only. After that the env value is IGNORED for authorization: command access lives in the per-account permission lists + the admin list (dashboard Admin tab / `adminlist` verbs). Remains the diagnostics view for legacy unwired stacks | Non-integer element raises `CredentialsError` (parsed once, cached; malformed caches as EMPTY — fail closed) | Loaded at boot → **restart to change**; irrelevant for authorization after the one-time migration | The pre-migration authorization allowlist; treat as sensitive |
| `SELF_COMMAND_PREFIX` | Feature default | The DEFAULT self-command prefix for every account (`"@me"` = the account's own mention works as prefix). Each account's admin-panel prefix config (`prefix_mode` mention/symbol/both + `prefix_symbol`) OVERRIDES this default at client build time; with no prefix configured the account runs a plain Client with no command machinery | absent/empty = disabled | Loaded at boot → **restart to change** (per-account overrides are runtime-editable via `/api/admin/prefix/set`) | Not a secret; gates who can type commands at the account (the AccessManager gate is the actual authorization) |
| `DASHBOARD_TOKEN` | **SECRET — deployment** | Local auth token for the dashboard (REST `X-Dashboard-Token` header / `?token=` and the WS handshake) | Absent/empty → `None` → dashboard serves **without auth** (logged as `auth=OFF`) | Read once at boot when the dashboard is attached → **restart to change** | Set it whenever the dashboard is enabled; also gates the credential-management routes ([SECURITY.md](SECURITY.md)); see [DASHBOARD.md](DASHBOARD.md) |
| `REMOTE_CONTROL` | Feature flag (simulator-only) | Gates the simulator's mention bridge (`control_mention` scenario, [adapters/simulator/runner.py](../adapters/simulator/runner.py)); production wiring is unaffected — chat commands there are self-commands gated by the admin panel ([SECURITY.md §4](SECURITY.md)) | Disabled iff value (lower-cased, stripped) ∈ `0, false, no, off` | Read when the simulator stack wires `RemoteControl` → **restart to change** | Not an authorization control (the `AccessManager` gate is); see [SECURITY.md](SECURITY.md) |
| *(account `token_ref`)* | Reference (LEGACY) | Optional per-account override of which env key holds the legacy token (`AccountRecord.effective_token_ref`, [database/models.py](../database/models.py)) | Must exist in `.env` when the account starts and has no DB credential | Stored in the `accounts` table; set at creation (dashboard add payload / `config/accounts/*.json`) | Stores a **reference**, never the value |

Token VALUES never enter the database **in plaintext** — the `credentials`
table stores Fernet ciphertext only, with the key outside the DB (see
[DATABASE.md](DATABASE.md) §secrets-policy and [SECURITY.md](SECURITY.md)).

### 1.2 Credential resolution precedence (spec §17)

```
DB credential exists        → use it            (status "db")
DB absent, .env has token   → legacy fallback   (status "env"; never overwrites DB)
neither                     → MISSING_CREDENTIAL (status "missing")
```

The `.env` → DB conversion is **explicit and one-shot per account**
(`credentials import` / `POST /api/credentials/import-env`): it never
silently overwrites an existing stored credential.

Any *other* key is legal (the parser stores everything); keys containing
`token`/`secret`/`password` are masked in `redacted()` output.

### 1.3 The env → database migration (admin panel model)

Three `.env` values moved into DB-backed, admin-panel-editable state.
The bootstrap runs the ONE-TIME migration on every boot (idempotent —
it only fires when the DB side is empty):

| Env key | DB destination | Migration behavior | After migration |
|---|---|---|---|
| `OWNER_USER_IDS` | `access:admins` (kv) — the framework admin id list | `AccessManager.migrate_from_credentials` seeds the list from the env value **only when the stored list is empty** (first boot); logs a warning either way | The env value is IGNORED for authorization. Command access = admin list + per-account `allowed_user_ids` (both editable in the dashboard Admin tab). No env owners and no stored admins → NOBODY can command anything (fail-closed) until an admin is added |
| `MUDAE_BOT_ID` | `access:mudae_bot_id` (kv) | No auto-migration: the DB value simply WINS when set | `AccessManager.mudae_bot_id()` resolves DB-first, env fallback; `POST /api/admin/mudae-id/set` with `null` clears back to the env value |
| `SELF_COMMAND_PREFIX` | `access:account:<name>.prefix_mode/prefix_symbol` (kv) | No auto-migration: the env value stays the DEFAULT when an account has no per-account prefix row (default row = `mention` mode) | Each account's admin-panel prefix config (mention/symbol/both) overrides the env default at client build time; changes apply on next connect |

---

## 2. `config/global_settings.json` — global plugin overrides (layer 2 of 3)

Shape ([core/config.py](../core/config.py)): `{"<plugin_name>": {"<setting_name>": value}}`.
Keys starting with `_` (e.g. the shipped `_comment`) are dropped at load
(typo protection). Malformed JSON or a non-object file **fails boot loudly**
(`ValueError`).

The shipped file carries only the `_comment`: **the framework bundles no
plugins**, so the layer is empty until you add plugins under `plugins/`
(per-plugin defaults come from each plugin's `Setting` manifest; per-plugin
named bundles are the plugin-profiles system — see §5).

Semantics: values are merged over plugin schema defaults and under the
per-account DB layer (see §5). Unknown keys for a plugin raise
`SettingsValidationError` **at resolve time** (boot fails fast; at runtime
it renders as a clean error). File is read once at boot → **restart to
change**; runtime edits go through `plugins set` (account layer).

---

## 3. `config/rate_limits.json` — rate policies (learned defaults)

Full schema ([core/config.py](../core/config.py), applied by
`core/bootstrap._apply_policies` into
[core/rate_limit_manager](../core/rate_limit_manager)):

```json
{
  "global":   { "<action>": {"interval": <sec>, "jitter": [<min>, <max>]} },
  "accounts": { "<account>": { "<action>": {…} } }
}
```

`_`-prefixed keys are dropped. A **flat file** (no `global`/`accounts` keys)
is tolerated and treated as the `global` level. When the file is absent,
the in-code `DEFAULT_POLICIES` applies: `message` and `dm` at
`interval=2.0`, `jitter=[0.0, 0.5]`, empty `accounts`.

The SHIPPED file seeds the **learned defaults** from the live rate study
(`DEFAULT_ACTION_INTERVALS` in the engine — the measured zero-throttle
rates): `message` 2.6 s + 0.1–0.5 s jitter, `dm` 2.6 s + 0.1–0.5 s,
`reaction` 2.5 s + 0.1–0.4 s (shared account bucket), `component`
3.0 s + 0.1–0.4 s (independent interaction route). These are fallbacks,
not policy: stored policies compose on top with safest-wins (max)
semantics, and the `prefixrate` self-command / `ratelimit set … account
<name>` set a per-account override that wins whenever it is the only
stored layer.

### 3.1 Per-action policies (`global` / `accounts.<name>`)

| Field | Type | Allowed | Purpose |
|---|---|---|---|
| `interval` | float seconds | finite, `> 0`, `≤ 86400` (`MAX_INTERVAL`) | Minimum spacing between consecutive fires of the action on one pacing key |
| `jitter` | `[min, max]` floats | `0 ≤ min ≤ max`, `max ≤ 3600` (`MAX_JITTER`) | Uniform human-like pause sampled **after** each grant |

| Concern | Rule |
|---|---|
| Safety floor | `MIN_ACTION_INTERVAL = 1.5 s` — no configured action may pace faster than 1.5 s per action (the documented safety floor; raised 2026-10-26 from the former 0.05 s anti-loop sanity minimum). The floor is **raise-only** (`ratelimit floor`) and persists in kv (`ratelimit:floor`) so a restart cannot revert a post-incident raise |
| Composition | Effective interval = **max** across every applicable level (global / account / plugin / `account_plugin`); never below the floor; actions with NO stored policy fall back to the learned default, or the **unstudied-action default (1.2 s nominal)** when the action has no learned default either — clamped up to the 1.5 s floor |
| Persistence | Stored in the `rate_limit_policies` table (levels: `global`, `account`, `plugin`, `account_plugin` with owner `<account>/<plugin>`); a CLEARED first-boot default is tombstoned (`ratelimit:removed`) so it cannot be re-seeded on the next boot |
| Boot precedence | DB rows load first; then every action defined **in this file** is re-applied and re-persisted — the **file wins at every boot for the actions it defines**. Runtime changes for actions *not* in the file persist in the DB and survive restarts |
| Mutability | Runtime via `ratelimit set|clear|floor` (CLI) or `POST /api/ratelimit/*` (dashboard) or the `prefixrate` self-command (per-account message interval); takes effect immediately, wakes parked waiters |
| Two scopes | Per-task scope `("<account>", action)` spaces consecutive tasks of one account; group scope `("group:<sync_group>", action)` serializes accounts sharing a `sync_group` (set via DB `accounts.sync_group`) |
| 429 telemetry | Per-account throttle-hit counts + a fleet total (`hits_429` in `ratelimit_stats()` and the dashboard Policies card) — user accounts get no `X-RateLimit` headers on success, so 429 counts are the observable signal |
| Retry delays | Retries carry NO scheduler-owned delay — they re-enter `limiter.acquire` immediately; the limiter's server cooldown + interval own every delay |

### 3.2 Fleet send lane — REMOVED

The server-wide sequential send lane was removed (redundant with sync
groups; it throttled single-account throughput without adding safety).
Per-account pacing + sync groups + the floor are the pacing layers. The
`lane` config key is tolerated and ignored; `ratelimit lane` (CLI) and
`/api/ratelimit/lane` answer a compat stub
(`{"enabled": false, "prefixrate": 0, "removed": true, ...}`).

---

## 4. Framework account settings (`ACCOUNT_SETTINGS`)

Declared in [core/settings_manager.py](../core/settings_manager.py);
persisted per account in the `account_settings` table; **not** plugin
settings.

| Setting | Type | Default | Bounds | Purpose |
|---|---|---|---|---|
| `reconnect_enabled` | bool | `True` | — | Reconnect automatically after unexpected drops |
| `reconnect_initial_delay` | float | `1.0` | `[0.1, 60.0]` | First reconnect wait (seconds) |
| `reconnect_max_delay` | float | `60.0` | `[0.5, 600.0]` | Reconnect wait cap (seconds) |
| `reconnect_multiplier` | float | `2.0` | `[1.0, 10.0]` | Exponential-backoff growth factor |
| `typing_probability` | float | `0.0` | `[0.0, 1.0]` | Chance a guild send first fires a typing indicator (anti-flag: humans type; `0` disables — zero extra API calls; ~0.85 looks human) |

| Concern | Rule |
|---|---|
| Validation | Same `Setting.validate` as plugin settings (types, bounds, NaN/inf rejected); invalid stored values are **ignored with a warning**, schema defaults win |
| Mutability | `SettingsManager.set_account_setting()` validates + persists + emits `SettingChanged` — **but it is currently NOT wired to any CLI command or dashboard route** (see [DISCREPANCIES](#7-known-gaps)). The shipped surfaces can only *read* the effective values indirectly (reconnect/typing behaviour) |
| Restart requirement | None for behaviour: reconnect policy is re-read on every drop (`_reconnect_settings`), `typing_probability` on every send. Editing the DB takes effect without restart |
| Fallbacks | The account manager falls back to safe defaults (`enabled=True, initial=1.0, max=60.0, multiplier=2.0`) whenever the settings manager fails |

---

## 5. Plugin settings layering (defaults ← global ← account)

One schema (`Setting`: name, type ∈ `str|int|float|bool`, default, optional
`min_value`/`max_value`, description) drives validation, the CLI `set`
command and the dashboard editors — parity by construction.

```
1. schema defaults          (lowest priority — plugin class attribute `settings`)
2. global layer             (config/global_settings.json)
3. account layer            (database `plugin_settings` — highest priority)
```

* Resolution: `SettingsManager.resolve(plugin, schema, account)`; unknown
  keys or bad values raise `SettingsValidationError` naming the layer.
* Live view: `PluginContext.settings` is the *resolved* dict and is updated
  **in place** by `PluginManager.set_setting` — long-lived plugin instances
  see changes without rebuild. Emits `SettingChanged`.
* Bool string forms accepted at validation: `1/true/yes/on` and
  `0/false/no/off` (case-insensitive); the CLI `_on_off` helper accepts the
  narrower `on|off|true|false`.

**Shipped plugin schemas: none.** The framework ships with zero bundled
plugins, so there are no shipped schemas — drop a plugin under `plugins/`
and its manifest appears everywhere automatically. The test fixture
`tests/fixtures/plugins/probe` declares a representative two-setting
schema (`trigger` str, `factor` float bounded `[0.1, 10.0]`).

### 5.1 Plugin profiles — the middle layer for bound plugins

A plugin **profile** (`plugin_profiles` table) is a named, reusable
settings bundle for ONE plugin; an account binds it per assigned plugin
through `account_plugins.profile` (`NULL` = plugin defaults). The
effective-settings merge becomes:

```
schema defaults ← bound profile's settings (minus "channels") ← account overrides
                 (+ the profile's channel scope under the reserved "channels" key)
```

Profiles are validated against the plugin's Setting schema at write time
(dashboard/CLI-equivalent `plugin_profiles_*` ControlAPI paths); the
reserved `channels` scope (`{"mode": "all"|"allow"|"deny", "rules":
[{"guild_id", "channel_ids"}]}`) is validated by `core.plugin_scopes` —
an empty allow-list allows NOTHING. Runtime edits: dashboard Plugins/Profiles
tabs; `value: null` on a per-account key still resets that override.
Restart: none.

---

## 6. Database-persisted state (SQLite)

Default path `state/libzent.db` (`--db` to override; `:memory:` for
simulations). File created with mode `0600` before first write, WAL journal
mode, migrations are append-only and versioned in `schema_migrations`.
Schema: [database/schema.py](../database/schema.py).

| Table | Contents | Written by |
|---|---|---|
| `accounts` | Registry rows: `name` (unique), `token_ref`, `enabled`, `sync_group`, `settings_json`, `created_at` | Account manager (add/remove/enable) |
| `account_settings` | Framework account settings (`ACCOUNT_SETTINGS`, §4) | Settings manager |
| `plugins` | Plugin manifest cache (name/version/description/settings schema) | Plugin manager discovery |
| `plugin_settings` | Per-account plugin setting overrides (layer 3) | Settings manager |
| `plugin_states` | Per-account plugin enabled/disabled flags | Plugin manager |
| `account_plugins` | DIRECT account→plugin assignments — the capability source (which plugins an account runs) + the `profile` binding column (NULL = plugin defaults; migration v7) | Account manager / ControlAPI profile binding |
| `plugin_profiles` | Per-plugin named setting bundles (validated against the plugin's schema; may carry the reserved `channels` scope) — migration v7 | ControlAPI `plugin_profiles_*` |
| `profiles`, `profile_plugins`, `account_profiles` | Profile records, their plugin lists, assignment bookkeeping — RETAINED future infrastructure (no capability effect) | Profile manager |
| `rate_limit_policies` | 4-level policy rows (`level, owner, action, interval, jitter_min, jitter_max`) | Rate-limit manager |
| `audit_logs` | Every audited interaction (direction, action, channel, content, `wait_ms`, `jitter_ms`, note); pruned to newest 10 000 | Audit recorder |
| `logs` | Structured log sink; pruned to newest 10 000 | Log service |
| `task_history` | **Finished** tasks only (status/attempts/duration/error/summary) | Task scheduler |
| `kv` | Generic key/value store — the admin-access rows (`access:admins`, `access:mudae_bot_id`, `access:account:<name>`), the rate floor/tombstones (`ratelimit:floor`, `ratelimit:removed`), namespaced plugin state (`pstate:<scope>:...`) | AccessManager, RateLimitManager, PluginStateService |
| `api_cache` | Per-account resolved Discord entities (`kind` ∈ `dm|user|channel`, PK `(account, kind, key)`) — restarts never re-fetch | Account manager |
| `credentials` | **Fernet ciphertext** of account tokens (key outside the DB: `LZ_MASTER_KEY` or `state/credentials.key`) | CredentialService |
| `cooldowns` | Persistent cooldowns keyed `(account, plugin, cooldown_key)` with **absolute epoch expiry** — restarts restore the REMAINING time | CooldownService |
| `schema_migrations` | Applied migration versions | Database |

**Not persisted (in-memory only):** queued/running tasks (the scheduler
"engine" — a restart drops anything not yet finished), the pacing/last-fire
clocks, server cooldown timers, log/audit in-memory rings (beyond the DB
sink), reconnect supervisors. Secrets are persisted **only as ciphertext**
(`credentials` table; §1.2) — plaintext exists solely in process memory
while running.

---

## 7. Known gaps

Verified against source; see also the consolidated `DISCREPANCIES.md`:

* `SettingsManager.set_account_setting` and `ControlAPI` expose **no**
  CLI/dashboard path — `ACCOUNT_SETTINGS` are consumed (reconnect, typing)
  but only editable by writing the DB directly.
* `SettingsManager.layers_view` ("dashboard Advanced page" per its
  docstring) and `ControlAPI.credentials_summary` are defined but called by
  no interface.

---

## 8. Entrypoint flags (where config comes from)

`main.py` argument parsing:

| Flag | Default | Purpose |
|---|---|---|
| `--env` | `.env` | Credentials file path |
| `--config` | `config` | Config directory (`global_settings.json`, `rate_limits.json`, optional `accounts/*.json`) |
| `--db` | `state/libzent.db` | SQLite persistence file |
| `--plugins` | `plugins` | Plugin discovery directory |
| `--dashboard` | off | Serve the browser dashboard (+WS) |
| `--port` | `8070` | Dashboard port |
| `--headless` | off | Run forever with no terminal (pair with `--dashboard`) |
| `--start-all` | off | Boot every enabled account on start |
| `--sim [NAME]` / `--sim-list` | — | Offline simulation; omit NAME to list |

Optional `config/accounts/<name>.json` files (directory absent in a fresh
clone): `{name, token_ref?, enabled?, sync_group?, plugins?}` — one account
bootstrap per file (`plugins` = the DIRECT assignments; a legacy `profiles`
key is deliberately ignored — it no longer decides capabilities);
`_`-prefixed keys dropped; files without `name` and
duplicate names are skipped with a warning.
