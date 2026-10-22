# GLOSSARY

Terms in the order a new agent meets them. Each definition is grounded in
source; follow the links for detail.

| Term | Definition |
|---|---|
| **account** | A first-class Discord user identity the framework runs (`core/account_manager`). Has a persistent half (DB row: `name`, `token_ref`, `enabled`, `sync_group`, profiles (retained bookkeeping)) plus its direct plugin assignments (`account_plugins` rows) and a runtime half (status `created/connecting/running/reconnecting/stopped/failed`, cached gateway state, supervisor task). One token per account, resolved from `.env` at start |
| **plugin assignment** | A row in `account_plugins` meaning "this account runs this plugin" — the CURRENT capability model (direct Account→Plugins). Mutations persist then re-attach the account's plugins immediately; assignment ≠ enabled (the per-account enabled flag is separate) |
| **plugin** | A module under `plugins/` exporting one `PluginBase` subclass (`core/plugin_base.py`): metadata (`name` ∈ `[a-z0-9_-]+`, version, description), a settings manifest, lifecycle hooks (`on_load/on_enable/on_disable/on_unload`) and `handle_event`. Instantiated once per (account, plugin) pair |
| **capability** | The permission for one account to run one plugin — held DIRECTLY via `account_plugins` rows (`Account.assigned_plugins`). The plugin manager attaches exactly the assigned set; profiles are not consulted |
| **pacing key** | The `(owner, action)` tuple the pacing engine spaces actions on (`core/rate_limit_manager/engine.py`); owner is the account name or `__global__` when `acquire()` carries no account. Each key has its own last-fire clock + waiter heap |
| **per-task scope** | Pacing scope `("<account>", action)`: consecutive API calls (tasks) of ONE account are spaced by the effective interval — the pre-existing behaviour |
| **group scope** | Pacing key `("group:<sync_group>", action)`: accounts sharing a `sync_group` serialize against ONE shared clock (A → [limit] → B → [limit] → A). Both scopes compose by max |
| **lane / prefixrate** | The GlobalSendLane (`core/rate_limit_manager/lane.py`): a process-wide admission gate in front of every outbound API call. When enabled, grants across EVERY account are strictly sequential, spaced ≥ `prefixrate` seconds; `prefixrate = 0` **pauses** the lane (submissions hold until a non-zero rate). Waiter-driven so it cannot die; config persists in kv `ratelimit:lane` |
| **floor** | The hard safety floor `MIN_ACTION_INTERVAL = 1.5 s/action`: no rate policy can ever be set faster (`UnsafePolicyError`); `ratelimit floor` can only raise it. Effective intervals are max(all applicable policies, floor) |
| **grant** | The receipt returned by `limiter.acquire()`: `Grant(action, account, plugin, wait_s, jitter_s, reason)` where reason is `"server"` (a 429 cooldown contributed) or `"pacing"` ("" when no wait). Wait/jitter land in the audit trail as `wait_ms`/`jitter_ms` |
| **jitter** | A uniform random pause drawn from the policy's `[jitter_min, jitter_max]` window and slept **after** the grant so traffic does not look metronome-perfect. Seeded (`jitter_seed`) in tests for reproducibility |
| **task / action kinds** | A `Task` (`core/task_scheduler/tasks.py`) is ONE outbound action for ONE account: `queued → running → completed | failed | cancelled | timed_out`, with `attempts` = retries used, cooperative `cancelled` flag, per-attempt timeout. The `action` string (usually `message` or `dm`) is passed opaquely to the sender |
| **FlowBlocked family** | Domain errors (`core/errors.py`) meaning "this plugin flow for ONE account hit a dead end": `FlowBlocked` ← `CacheInvalid` ← `TargetGone`; plus `AccessDenied`, `FetchRequired`. Plugins catch, log one clean abort line and return — per-account isolation is the dispatcher's job |
| **ServerRateLimit** | Discord itself said WAIT (429 / Retry-After). Raised by adapters, consumed by the scheduler which reports it to the limiter as account + fleet-global cooldowns waited out before pacing |
| **send pipeline** | THE core dataflow: plugin → `ctx.services.send_message` → task submit (`TaskScheduler`) → lane (stage 0) → server cooldowns → pacing (priority heap) → jitter sleep → adapter send → audit. Plugins never self-throttle |
| **ack registry** | `TaskAckRegistry` (`core/account_manager/services.py`): correlates task completion events back to the awaiting plugin coroutine (task-ack sends/waits) so `wait_for_message`/`collect_messages` resolve against the right send |
| **api_cache** | The per-account persistent cache of resolved Discord entities (`api_cache` table; kinds `dm|user|channel`). Resolution order memory → DB → ONE client call, so restarts never re-fetch; invalidated on `TargetGone` |
| **rich DTO** | The project-owned frozen dataclasses of `adapters/model.py` (`DiscordMessage`, `DiscordUser`, `DiscordChannel`, `DiscordEmbed`, components, typed events …) captured once at the adapter boundary. Plugins consume these — never `discord.py-self` types |
| **raw escape hatch** | The `raw` field on rich DTOs: the original library object, runtime-only (never serialized to DB/tasks/caches), `repr=False` (never dumped in logs), accessed via `message.unwrap()` for advanced plugins |
| **bridge** | `StdlibBridge` in `core/logging_service.py`: forwards stdlib `logging` records (plugins/third-party code) into the structured LogService. Deliberately does NOT mirror back — the historical mirror→bridge feedback loop multiplied every line ~143× and was fixed (see [CHANGELOG.md](CHANGELOG.md)) |
| **canonical log event** | A structured `LogEntry` as recorded by LogService — the ONE canonical record of something happening (ts, severity ∈ DEBUG/INFO/WARNING/ERROR, source, account?, plugin?, task_id?, event?, message). Consumers: stderr mirror, dashboard Logs page, bounded DB sink |
| **scenario** | One registry entry in `adapters/simulator/scenarios.py`: `policies` + `world_setup` + `script` + post-mortem `checks`. Runs a complete ephemeral stack over the simulator; `[]` from checks = pass |
| **drift baseline** | `knowledgebase/generated/drift_baseline.json`: symbol-signature hashes written by the KB generator; `scripts/gen_knowledgebase.py --check` diffs the current source against it (exit 2 on any added/removed/changed symbol) |

Cross-reference: [CONFIGURATION.md](CONFIGURATION.md) for the knobs,
[AGENT_GUIDE.md](AGENT_GUIDE.md) for where things live.
