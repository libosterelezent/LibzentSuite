# SECURITY — credentials, exposure and safety posture

> ⚠️ Selfbot usage violates Discord's Terms of Service and can get accounts
> suspended. The mitigations below **reduce** risk; they do not authorize
> anything. Keep policies conservative and never add accounts you cannot
> afford to lose. (Stance mirrored from the root `README.md`.)

---

## 1. Credential handling

Rule ([core/credentials.py](../core/credentials.py),
[core/credential_service.py](../core/credential_service.py), spec §7/§12):
**every token and user/bot id lives in `.env` or the encrypted credential
store — never in code, never in the database as plaintext.**

| Control | Detail |
|---|---|
| Two sanctioned stores | (1) legacy `.env` file; (2) the encrypted-at-rest `credentials` table (Fernet ciphertext). Resolution precedence: DB → `.env` fallback (never overwrites the DB) → `MISSING_CREDENTIAL` |
| DB stores **ciphertext** | The `credentials` table keeps Fernet ciphertext only (AES-128-CBC + HMAC-SHA256 via `cryptography.fernet` — established library, nothing homemade). The legacy `accounts.token_ref` keeps only the `.env` KEY name |
| Key outside the DB | Master key = `LZ_MASTER_KEY` env (urlsafe-base64 Fernet key) when set, else generated at `state/credentials.key` (chmod 0600, git-ignored via `state/`). Losing the key means re-entering credentials — by design (no recoverable plaintext anywhere) |
| No network on save | Storing/rotating a credential performs zero network activity — saving is always separate from connecting (spec §16) |
| Explicit import only | `.env` → DB conversion is the one-shot `credentials import`; it never silently overwrites an existing stored credential |
| `redacted()` masking | Human-safe copy: any key containing `token`/`secret`/`password` renders as `<first 4 chars>…(<length> chars)`; values of **≤ 4 chars are fully hidden** (`***`) |
| `__repr__` guards | `Credentials.__repr__` returns only the path + sorted key names; `CredentialService.__repr__` returns only repo presence + cached account NAMES + key path — an accidental log of either leaks nothing |
| Display-safe status | Credential state surfaces as words only: `db` / `env` / `missing` — never the value, never the ciphertext |
| Synthetic ids in tests/sim | Simulations and fixtures use module constants (`SYNTHETIC_*`); `Credentials.from_values` builds credentials from injected dicts. Committed tests carry obviously-fake tokens only |

### 1.1 Threat model for encryption-at-rest (honest scope)

| Scenario | What the attacker gets | Why |
|---|---|---|
| **DB file stolen/copied** (`state/libzent.db`, backups, WAL sidecars) | Fernet **ciphertext only** — no usable tokens | The key never lives in the database; without `LZ_MASTER_KEY`/`credentials.key` the rows are undecryptable blobs |
| **Running process compromised** (memory read, crash dumps, ptrace, malicious plugin) | **Plaintext credentials may exist in memory** | Decryption happens at resolve time and is cached for runtime use (spec §14). Encryption-at-rest does NOT protect a running process — a malicious plugin with process memory access is out of scope by design |
| **Dashboard compromised** | Severe by design — it manages accounts | Credential routes are management APIs: they REQUIRE `DASHBOARD_TOKEN` auth (like every other mutating route) and the bind stays `127.0.0.1` by default. Status/list routes return words (`db`/`env`/`missing`), never values. Never expose the dashboard beyond loopback without the token |
| **Key + DB both stolen** | All stored credentials | The key is the crown jewel: `0600` key file or env var, never backed up next to the DB, never committed |
| **`.env` stolen** | The legacy tokens it contains | Same operational hygiene as ever: `chmod 600`, git-ignored |

## 2. Token gate on account start

`AccountManager.start()` resolves the credential through the pinned
precedence (DB ciphertext → legacy `.env` via `token_ref` /
`DISCORD_TOKEN_<NAME>`) → `CredentialsError` with an actionable message
naming the exact key to set **before** any client is built. A missing
credential can never produce a half-connected account; the failure is
CLASSIFIED (`missing_credential` status — fix by storing a credential, do
not retry-loop it) and publishes `AccountFailed`. A token Discord
REJECTS at login is classified separately (`invalid_credential` — the
value itself must be replaced).

## 3. Dashboard exposure model + auth

See [DASHBOARD.md](DASHBOARD.md) for the full route/WS reference.

* Binds **`127.0.0.1`** by default — a loopback service, not a public one.
* When `DASHBOARD_TOKEN` is set, REST requires `X-Dashboard-Token` or
  `?token=`; the WS handshake requires the same (401 otherwise). Without a
  token the server runs with auth **off** (and says so in its log line) —
  set the token whenever you enable the dashboard.
* The static `index.html` is served **without auth** by design: the SPA must
  load to prompt for the token; a `?token=` share link seeds the store and
  is scrubbed from the address bar.
* Hardened input handling: strict JSON booleans (400, never truthy casts),
  NaN/inf rejection, 1 MB body/frame caps (413 / connection close),
  actionable 400s naming the field and route, 500s logged with tracebacks,
  504 on main-loop hop timeouts.

## 4. Chat commands (self-command layer + authorization)

Framework management from Discord rides the **self-command layer**
([core/plugin_manager/commands.py](../core/plugin_manager/commands.py) —
the `SelfCommandHub` registry): each live account runs the library's
`ext.commands` machinery with its own prefix — `mention`, `symbol` or
`both`, configured per account from the dashboard's Admin tab. The old
`RemoteControl` mention bridge is **retired from production**:
[core/remote_control.py](../core/remote_control.py) survives only
because the simulator stack wires it for its `control_mention` scenario,
and the `REMOTE_CONTROL` env now gates that simulator behavior only
([adapters/simulator/runner.py](../adapters/simulator/runner.py)).

| Control | Detail |
|---|---|
| Authorization gate | `AccessManager.can_invoke` ([core/access_manager.py](../core/access_manager.py)) — THE command gate: the invoking Discord user id must be in the account's per-account allowlist (`allowed_user_ids`) OR be a framework admin (DB-backed admin list / admin-flagged accounts) |
| Fail-closed | Empty per-account allowlist and no admins → NOBODY can invoke that account's commands; an unknown/absent account row is the same as empty |
| `OWNER_USER_IDS` | A ONE-TIME migration seed only: on first boot it seeds the DB admin list, after which the env value is IGNORED for authorization (pre-migration it let every env-listed id command every account) |
| Managing access | Admin list, per-account allowlists and prefix configs are DB-backed kv rows, edited in the dashboard's Admin tab or via the `adminlist` / `adminperms` / `adminprefix` self-command verbs |
| Command surface | Framework verbs (`status`/`accounts`/`plugins`/`ratelimit`/`lane`/`tasks`/`estop`/`simulation`, the `admin*` settings verbs, `pg`, `say`, dynamic `help`) + every plugin-declared `COMMANDS` verb — one registry, one dispatch path, one gate |
| Paced replies | Handlers run with plugin-grade pacing: replies go through the services facade into the FULL send pipeline (scheduler → lane → pacing → jitter → audit), never a raw client send; truncated to 1900 chars |

## 5. ToS / flag-risk posture (mitigations, not authorization)

| Mitigation | Where |
|---|---|
| Safety floor | `MIN_ACTION_INTERVAL = 1.5 s` hard safety floor (raise-only via `set_floor`, `UnsafePolicyError` below it) — aligned with the documented contract in INTEGRATION.md / ARCHITECTURE.md §14 as of 2026-10-26; the value before that date was a 0.05 s anti-loop sanity minimum. The EFFECTIVE pacing net remains the learned per-action defaults that apply when no policy is stored (message/dm 2.6 s, reaction 2.5 s, component 3.0 s — [core/rate_limit_manager/engine.py](../core/rate_limit_manager/engine.py)) |
| Human-like jitter | Uniform jitter window sampled after every grant; warning taxonomy flags `no_jitter` / `narrow_jitter` / `aggressive` configurations |
| Server limits respected | `ServerRateLimit` (429 / Retry-After, route + fleet-global) → cooldowns waited out before anything else; the simulator scenario `server_429_respected` pins the behaviour |
| Server-wide send lane | Optional GlobalSendLane serializes every outbound action of the whole fleet, spaced by `prefixrate` (0 = pause) |
| Typing indicator opt-in | `typing_probability` account setting, **default 0.0** (zero extra API calls); ~0.85 "looks human" |
| Minimal API surface | Risky API calls were moved out of the framework into plugins; the persistent `api_cache` means one `open_dm`/`fetch` per entity **ever**, not per boot |
| Discord message limits respected | Chunker hard-caps at 2000 chars (prefix included); remote-control replies cap at 1900 |

## 6. File permissions

| Artefact | Protection |
|---|---|
| Database file | Created with mode `0600` (best-effort) after connect, parent dirs auto-created; WAL journal mode |
| `state/credentials.key` | Generated with mode `0600`, outside the DB, git-ignored via `state/`; holds the Fernet master key when `LZ_MASTER_KEY` is unset |
| `.env` | Not enforced by code — git-ignored (`.gitignore`: `.env`, `.env.*`, `!​.env.example`, `accounts/`, `*.token`, `*_token.txt`) and the README instructs `chmod 600 .env` |
| Logs/audit | Message *content* is stored in `audit_logs.content` (needed for the trail) — treat the DB as sensitive; it lives under git-ignored `state/` |

## 7. What is **not** protected (honesty section)

* `knowledgebase/read_only/` is "READ ONLY" **by convention only** — nothing
  enforces immutability at the filesystem level; regenerating
  (`scripts/gen_knowledgebase.py`) legitimately overwrites it. Never treat
  its contents as untampered input for security decisions.
* No filesystem access controls exist for `config/` — anyone who can write
  `config/rate_limits.json` can change pacing (still floor-bound) or add
  account bootstrap files.
* The dashboard has no TLS, no rate limiting of its own and no account
  concept: the token is a single shared secret. Loopback + token is the
  threat model; do not port-forward it bare.
* The DB stores message content and channel ids in the audit trail;
  credentials are encrypted at rest, but the **content tables are not
  encrypted** — physical access plus the key file still yields the
  interaction history. Treat `state/` as sensitive wholesale.

## 8. `api_cache` per-account isolation

The persistent API cache (`api_cache` table, migration 2) is keyed
`(account, kind, key)` with `kind ∈ dm|user|channel` — rows are scoped to
exactly one account and are never shared across accounts (anti-flag: each
account re-resolves only its own entities, and restarts never re-fetch).
Resolution order is memory → database → **one** client call, with
invalidation on `TargetGone`. See [CONFIGURATION.md §6](CONFIGURATION.md#6-database-persisted-state-sqlite).

Related: [CONFIGURATION.md](CONFIGURATION.md) (every knob),
[DASHBOARD.md](DASHBOARD.md), [TESTING.md](TESTING.md).

---

## Diagnostic-artifact + setup hardening (final phases)

> Verified at commit `487dc53`.

* **Boot permission audit** (POSIX, warn-only): db / WAL sidecars /
  credential key / .env checked for group/world bits at boot; WAL/-shm
  tightened 0600 at connect; a loose key file is tightened on read.
* **Key/DB mismatch canary** at boot: one decrypt probe over the stored
  accounts — a wrong/restored master key warns once at boot instead of
  per-account at first use.
* **Redacting `__repr__`** on `RealDiscordClient` (token can never leak
  into tracebacks/benchmark output).
* **Dashboard without `DASHBOARD_TOKEN`** prints a prominent warning
  (loopback + credential routes stay locked; start/stop/estop remain
  local-process drivable).
* **Diagnostic artifacts are 0600** (bench JSON reports + .pstats dumps)
  and value-free by construction (pstats records filenames/counts;
  tracemalloc records file:lineno + bytes — never values); a sentinel
  test proves a full-stack report contains neither the fake bench token
  nor live key material.
* **No-auth dashboard** refuses nothing but warns; credential routes
  hard-gate (CREDENTIAL_GATED_ROUTES).
* **`--debug` mode** (`main.py --debug`): libzent tree at DEBUG,
  discord library at INFO (gateway reconnect/close-code lines), DB
  worker telemetry live (`LZ_DB_TELEMETRY=1`), scheduler pressure on
  the Overview.
