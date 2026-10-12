# CHANGELOG

Themed entries derived from the real commit history (`git log --oneline`,
newest first). No version numbers are invented — each entry names its commit
subject; hashes link entries to `git show` for the full story.

---

## Multi-account isolation + round-2 adversarial audit (merge)
*Commits `077b9bd` (phase 4), `f2cc850` (final), `2f75a01` (residual),
`14e8531` (duplicate-audit fix + --debug), `cb082b0`+`c32f9c2` (remote
round-2 audits), merged as `487dc53` — 1191 tests*

Five hardening generations landed and were integrated:

**Isolation audit (phase 4)** — lifecycle GENERATION tokens on every
AccountRuntime (a stale start can never resurrect a stopped account, an
old stop can never clobber a newer start, a stale supervisor stands down
by identity); the lifecycle governor (per-account + global start/stop
throttling, state-aware, estop-exempt); the benchmark subsystem
(`python main.py --bench`, 7 scenarios, loop-lag histograms, leak
verdicts, baselines/compare); smooth flap-escalation reconnect backoff;
depth-projected IdentifyGate fast-fail; stop-phase deadlines.

**Final phase** — per-account waiter registry (M×E → M/A dispatch fan-out,
structural isolation); version-bumped effective() cache (tuple-keyed,
capped, invalidated at every policy mutation); memory-first status
polling (30s authoritative reconciliation, shared per-tick WS snapshot);
bounded start_all lifecycle scheduler (4 lanes, admission pacing,
overtake-by-generation, single-flight join); coalesced EventBus dispatch
(one bounded FIFO + one dispatcher task in slices replaces one-task-per-
event: 16.4k → 34.4k msg/s, burst lag p50 30.6 → 7.6 ms); --profile and
--tracemalloc bench modes (value-free, compare-excluded); boot permission
audit, WAL tightening, key/DB mismatch canary, redacting reprs.

**Residual-risk mitigation** — DB worker telemetry at the connection
boundary (TOTAL_DB_LATENCY/COMMIT/TRANSACTION, honest about what a
boundary cannot see); scheduler pressure signal (NORMAL/ELEVATED/
PRESSURED/CRITICAL, grounded thresholds — observability, never a cap;
admission control evidence-rejected); loggerDict pruning in the removal
cascade (bounded by LIVE accounts; migration rejected — attribution is
name-derived); subscriber-set dispatch cache (per-type dispatch tuple,
ordering-preserving; awaited publish 7.8 → 4.6 µs and shape-independent);
--repeat medians, run-mode labels, --capture-spikes; 0600 diagnostic
artifacts + sentinel secret scan; tombstone weakref churn tests.

**Duplicate audit lines fix** — DashboardServer.start() auto-attached
the audit recorder AND main.py attached it again: two subscriptions
broadcast every entry twice. attach_audit is idempotent now. The merge
also fixed: remove_account bumped the lifecycle generation BEFORE the
live-status refusal (a refused removal invalidated the in-flight start,
leaving a live client stuck CONNECTING).

**Round-2 adversarial audit (remote)** — explicit BEGIN IMMEDIATE
transactions + autocommit mode, WS slow-dribble frame deadlines,
crashed-supervisor containment, boot/bind error UX, 354 new regression
tests.

---

## Rich Discord message/event boundary — future-proof plugin abstraction
*Commit `35176a8` — `feat(arch): rich Discord message/event boundary`*

The adapter boundary now captures Discord data once into project-owned,
frozen DTOs (`adapters/model.py`): embeds, components/buttons, attachments,
reactions, references, author/channel/guild projections, DM vs guild.
Plugins consume these — never `discord.py-self` types — with a runtime-only
`raw` escape hatch (`unwrap()`). New typed events (`ReactionAdded`,
`MessageEdited`, `MessageDeleted`) arrive through the simulator without
message fetches; reaction/button capability actions work end-to-end through
the paced pipeline. Future plugins (snipe / kakerareact / kakeraloot /
soulmate farm) become additive instead of requiring core rewrites.

## Logging feedback-loop fix + hardened logging architecture
*Commit `def72a4` — `fix(logging): eliminate the mirror->bridge feedback loop`
(10-agent investigation)*

Historical bug: `LogService` mirrored records to the stdlib logger while the
stdlib bridge re-ingested those mirrors — every log line echoed ~143×,
recursion-bounded, slowing boot to ~23 s via ~60k sqlite commits. The
invariant "N source events == N canonical LogService events" is now pinned
by a regression suite, with stderr-exactly-once for bridge-ingested records,
severity mapping (CRITICAL/FATAL → ERROR), failure observability, bridge
detach and shutdown sink ordering.

## In-depth debugging round
*Commit `1e2a050` — `fix: in-depth debugging round — audit integrity,
post-estop resume path, live settings view`*

Audit-trail integrity fixes, a working resume path after a global emergency
stop, and a live settings view.

## Rate-limit enhancements; risky API methods moved into plugins
*Commit `1635446` — `ratelimit enhancements and avoiding risky api methods
from being executed by the framework, now api calls are made in plugins.`*

The framework itself stops making risky Discord API calls; API interactions
now happen in plugins. Rate limiting enhanced accordingly.

## API boundary enforcement + two-scope rate limiting
*Commit `556806e` — `refactor(arch): enforce the API boundary + two-scope
rate limiting (per-account/group + per-task)`*

The adapter/API boundary becomes binding architecture: per-**task** scope
spaces consecutive API calls of one account, per-**account/group** scope
serializes accounts sharing a `sync_group` against one shared clock; both
compose by max.

## Anti-flag audit round
*Commit `31411d4` — `feat(safety): anti-flag audit round — persistent API
cache, DM double-call fix, typing opt-in, Discord mention-prefix management`*

Persistent per-account API cache (restarts never re-fetch DM channels/users),
a DM double-call fix, opt-in typing indicators (`typing_probability`,
default 0 = zero extra API calls) and mention-prefix management from Discord
(`@account status …`, owner-only — RemoteControl).

## GlobalSendLane — server-wide sequential sends
*Commit `b6c4ea` — `feat(rate): GlobalSendLane — FarmLite-style server-wide
sequential sends, robust port`*

Every outbound action of every account passes one admission gate when
enabled, grants strictly sequential and spaced by `prefixrate` seconds
(0 pauses the lane). Robustness over the FarmLite original: waiter-driven
(no engine task to die), cancellation-safe, disable-mid-wait drain,
wake-on-config-change.

## Full rearchitecture
*Commit `b5c2769` — `rewrite: full rearchitecture per TODO + Ideas.md —
8-agent build + integration`*

The codebase was rebuilt per the spec: first-class accounts
(persistent/runtime split), plugin manager, capability profiles, 4-level
rate policies, centralized task scheduler, SQLite persistence with
repositories + migrations, simulator adapter, and the two parity'd surfaces
(CLI + dashboard) over one Core API. The historical `tests_live/` suite and
`scripts/preflight_live.py` were removed here (offline-only stance since).

## Headless mode + live fixes
*Commit `aa76da4` — `feat: --headless mode for background/dashboard
operation; live fixes — discord.py-self 2.x has no Intents (plain Client),
disabled accounts skipped by start_all, _comment keys tolerated in
global_settings, kv_prefix for rule restore`*

`--headless` daemon mode; discord.py-self 2.x compatibility (plain
`Client`); `_comment`-style keys tolerated in settings files; kv-prefixed
rule restore.

## Go-live readiness
*Commit `47be063` — `feat: go-live readiness — preflight script, fresh venv
w/ discord.py-self, live-boot crash fixes …`*

Pre-live-boot hardening: comment keys in `rate_limits`/accounts, duplicate
account names, an example→disabled doc account.

## v3 real-time layer + component architecture
*Commit `d42a418` — `feat: v3 real-time layer + component architecture —
Discord header buckets, cache-first flows with per-account aborts, Task
Scheduler, Profile Manager, manager facades, live test suite; 310 tests;
ARCHITECTURE.md + KB_23/24`*

## Zero hard dependencies
*Commit `b7b03e0` — `docs: requirements.txt — zero hard deps (stdlib-only),
optional live-mode fork documented`*

## SendEngine (per-account serialized lane)
*Commit `45207f0` — `feat: SendEngine — FarmLite serialized per-account send
lane (prefixrate spacing, pause-at-0, shadow-queue coalescing, priority,
graceful stop) composed with token buckets + human delays; CLI/dashboard
parity, DB persistence, engine_wait in audit trail; 15 tests (271 total)`*

## Error-handling audit round
*Commit `548a227` — `audit: error-handling round (8 agents) — strict bools
(no more opposite actions), transactional account add/remove, field-level
400s, 504 timeouts, WS protocol enforcement + handshake validation, CLI
contextual usage errors, error visibility in status/dashboard, UI error
accrual + NaN guards; 25 regression tests (256 total)`*

## 15-agent deep audit
*Commit `d4da858` — `audit: 15-agent deep audit — 16 bugs fixed (settings
coercion, event isolation, limiter amount/NaN/delay caps, remove-order, sim
transcript, adapter startup, DM leak, UI trio), ~40 risks hardened, 62 new
tests incl. permanent rate-limit stress suite; KB_20 + doc corrections`*

## v2 — settings parity, SQLite persistence, audit trail
*Commit `b3ffd45` — `feat: v2 — settings parity (CLI+dashboard), accounts
mgmt w/ cached details, SQLite persistence, roles, safety floor + human
delays, audit trail + sim inspector …`*

## Dashboard token auth hardening
*Commit `f8b5114` — `fix(dashboard): token auth hardening — localStorage-safe
fallback, Enter-to-save, ?token= accepted on REST too`*

## Dashboard demo + share links
*Commit `71b9de4` — `feat: live dashboard demo script (sim_server) +
share-link token param + expose simulator loader`*

## Rename: MudaeScripts → SlaughterMudae
*Commit `d383b07` — `rename: MudaeScripts -> SlaughterMudae (identity,
logger namespace, CLI prog/prompt, dashboard)`*

## Auto-reconnect, dashboard SPA + WebSocket
*Commit `cc1f17e` — `feat: complete remaining TODO — auto-reconnect
backoff, TUI + live logs, dashboard SPA + WebSocket, state persistence`*

## Initial framework
*Commit `08cb5f4` — `feat: core framework — plugins, multi-account
container, rate limiter, CLI/dashboard API, sims`*
