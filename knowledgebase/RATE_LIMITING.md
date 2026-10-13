# RATE_LIMITING — pacing, policies, server limits, learned defaults

> Source of truth: `core/rate_limit_manager/{policies,engine,warnings}.py`.
> Per-module reference: `read_only/modules/core.rate_limit_manager.*.md`.

The rate-limit subsystem answers one question for every outbound action: *when
may this fire, so the fleet looks human and Discord never has to say no?* It is
a **pacing engine** (fixed minimum interval between consecutive fires), not a
token bucket — the audited pre-rewrite limiter used `n/window` buckets; the
rewrite keeps the same safety goal with a simpler mental model. It combines:

1. **4-level policy model** (`policies.py`) — global / account / plugin /
   account_plugin rules, composed by MAX interval, clamped by the safety floor;
2. **learned default intervals** (`engine.DEFAULT_ACTION_INTERVALS`) — when
   NO policy is stored for an action, the measured zero-throttle rates apply
   (message 2.6 s, dm 2.6 s, reaction 2.5 s, component 3.0 s); they are
   FALLBACKS, not policy — stored policies compose on top with max-wins
   semantics;
3. **pacing engine** (`engine.py`) — priority waiter heaps per pacing key,
   server-provided cooldowns, post-grant jitter, and two pacing scopes
   (per-TASK and per-ACCOUNT/GROUP); **429 telemetry** (`hits_429` per
   account + fleet total — user accounts get no `X-RateLimit` headers on
   success, so throttle hits are the observable signal);
4. **advisory warnings** (`warnings.py`) — never blocks, reviews configuration.

**REMOVED — the fleet send lane.** The former stage-0 `GlobalSendLane`
(server-wide sequential admission; `lane.py` remains on disk dormant) was
removed: redundant with sync groups and a throttle on single-account
throughput. The `ratelimit lane` CLI/dashboard surfaces answer a compat
stub (`{"removed": true, ...}`).

```
                      acquire(action, account, plugin, priority)
                                      |
                  stage 1: SERVER cooldowns (always respected)
                           fleet-wide global 429 + (account,action) reason="server"
                                      |
                  stage 2: PACING on every applicable key
                           ("__global__"|account, action)          reason="pacing"
                           ("group:<name>", action) when grouped
                           interval = MAX(all applicable policies, floor,
                                          learned default when unpolicied)
                           waiters granted in (priority, seq) order
                                      |
                  grant -> stamp last_fire on EVERY pacing key
                                      |
                  stage 3: JITTER sleep (post-grant, uniform draw,
                           never moves the pacing clock)            Grant.jitter_s
                                      |
                                  -> Grant
```

---

## 1. Policy model (`core.rate_limit_manager.policies`)

A **`RatePolicy`** is a frozen dataclass: `action`, `interval` (minimum seconds
between two consecutive fires of the same action on the same pacing key),
`jitter_min`/`jitter_max` (extra uniformly sampled human-like delay applied
AFTER each grant). `__str__` renders `"2.50s +0.10..0.50s"` (jitter omitted
when 0).

### The four levels

| Level | owner key | Applies to |
|---|---|---|
| `global` | `None` (required) | every account, every plugin |
| `account` | `"<account name>"` | one account, any plugin |
| `plugin` | `"<plugin name>"` | one plugin, any account |
| `account_plugin` | `"<account>/<plugin>"` | one account+plugin pair (exactly one `/`, both parts non-empty) |

Composition is **max-interval** (see `RateLimitManager.effective`): an account
can never make itself FASTER than a global rule — only slower. Levels mirror
`database.models.POLICY_LEVELS` = `("global", "account", "plugin", "account_plugin")`.

### Construction-time validation (module constants)

| Constant | Value | Meaning |
|---|---|---|
| `MIN_ACTION_INTERVAL` | `0.05` | hard sanity floor (an absolute minimum against zero-delay tight loops — Discord's own per-route bucket limits are the ACTUAL pacing constraint since the learned-rate study); enforced at SET time by the manager, not by `RatePolicy` |
| `MAX_INTERVAL` | `86400.0` | one-day-per-action sanity cap |
| `MAX_JITTER` | `3600.0` | one-hour jitter sanity cap |

`RatePolicy.__post_init__` rejects: non-string/empty action, non-numeric fields
(numeric strings included — validation happens at the boundary), booleans,
NaN/inf, `interval <= 0` or `> 86400`, negative jitter, `jitter_max <
jitter_min`, `jitter_max > 3600`. Every failure raises
`core.errors.RatePolicyError` with a precise message (NaN previously poisoned
float comparisons in the audited limiter — ported fix).

Module helpers:

- **`parse_scope(level, owner)`** — validates + normalizes a (level, owner)
  pair; `global` takes NO owner (`None`/`""` → `None`), every other level
  requires a non-empty owner string; raises `RatePolicyError` on any mismatch.
- **`policy_from_row(row)`** — converts one repository row
  (`database.models.RatePolicyRecord`-shaped object OR plain dict with the same
  keys) into a validated `(level, owner, policy)` tuple; raises
  `RatePolicyError` when the row is structurally invalid (callers log + skip).

### effective() = MAX composition + floor clamp

`RateLimitManager.effective(action, account=None, plugin=None)` returns:

```python
{"action": str, "interval": float, "jitter": [min, max], "sources": [...]}
```

- `interval = max([floor] + [p.interval for every applicable policy])` — a
  stored policy can never pace faster than the floor, even after `set_floor()`
  raised it past existing policy intervals (safest option per INTEGRATION.md);
- `jitter = (max of mins, max of maxes)` across applicable policies — `[0.0,
  0.0]` when none apply;
- `sources` lists every applicable `{level, owner, interval}` (deterministic
  order: level rank, then owner);
- with NO policy at all the result is the floor with empty sources — still safe.

Applicability per level: `global` always; `account` when `owner == account`;
`plugin` when `owner == plugin`; `account_plugin` when
`owner == f"{account}/{plugin}"` (both caller values required).

### The safety floor (raise-only)

`RateLimitManager._validated_floor` accepts only finite values
`>= MIN_ACTION_INTERVAL` (raises `RatePolicyError` otherwise).
**`RateLimitManager.set_floor(seconds)`** may only RAISE the floor — never
lower it, never below `MIN_ACTION_INTERVAL` (0.05 s); a lowering request
raises `RatePolicyError`. The floor starts at `MIN_ACTION_INTERVAL`, is
persisted in kv (`ratelimit:floor`) so a restart cannot revert a
post-incident raise, and is woken into waiters via `_wake_waiters()` after
every change.

**`UnsafePolicyError`** (subclass of `RatePolicyError`, from `core.errors`) is
raised by `RateLimitManager._store` when a policy's `interval` is below the
current floor: no policy faster than the floor can ever be SET through the
manager. Rows below the floor that were hand-edited into the database are
loaded but clamped by `effective()` at runtime (warning logged at load).

---

## 2. Pacing keys — two scopes sharing one clock table

`RateLimitManager._last_fire` maps `(owner, action) -> last grant time
(monotonic)` and hosts TWO scopes (spec §5's two-rate-limit design):

| Scope | Key shape | Serializes |
|---|---|---|
| **per-TASK** | `(account, action)` — `("__global__", action)` when `acquire()` carries no account | one account's consecutive API calls (tasks) for that action |
| **per-ACCOUNT/GROUP** | `("group:<name>", action)` | every member account of the group against ONE shared clock |

**`RateLimitManager.set_account_group(account, group)`** registers/removes an
account's group membership (`group=None`/empty removes it). It is
framework-fed metadata only — no Discord API involved. Wired from
`core.account_manager.AccountManager._sync_group_scope` (fed from the
account's `sync_group` field, probed via `hasattr`, exception-tolerant).

`RateLimitManager._pacing_keys(account, action)` returns every clock an
acquisition must respect: always `[(account or "__global__", action)]`, plus
`[("group:<name>", action)]` when the account is grouped. BOTH scopes compose
by max in `_entry_block`, and a grant stamps EVERY key via `_grant_ready` — so
group member A's grant makes member B wait the full interval: the exact
`A -> [limit] -> B -> [limit] -> A` serialization.

Waiters of one pacing key do NOT block other keys (per-account pacing stays
independent), exactly as the pinned contract states.

---

## 3. The pacing engine (`core.rate_limit_manager.engine`)

### Determinism seams

All time flows through injectable seams (see INTEGRATION.md):

- `clock` — `Clock` protocol (`now() -> float`); production `SystemClock`
  (monotonic, immune to wall-clock jumps); tests use `ManualClock.advance()`;
- `_sleep(seconds)` — EVERY timed wait goes through `await self._sleep(s)`
  (default `asyncio.sleep`); tests patch this seam;
- `jitter_seed` — seeds the jitter RNG (`random.Random`) for reproducible draws.

Other constants: `GLOBAL_KEY = "__global__"` (pacing-key owner when acquire()
carries no account), `POLL_INTERVAL = 0.25` (backstop poll for non-head
waiters — correctness never depends on the wake event).

### Waiter mechanics: heap + wake event

Pending waiters are `_Entry(priority, seq, action, account, plugin, future,
cancelled)` objects ordered by `(priority, seq)`; `seq` comes from an
`itertools.count()` so grant order is priority-first, FIFO within a priority
(priority order is *structural*: only the heap HEAD is grantable in
`_grant_ready`, so a HIGH waiter submitted after a LOW one still goes first).

Waiters park on **`_wait_either(seconds)`**: it races the sleep seam against
the shared wake event (`asyncio.Event`, created lazily by `_wake_event`). The
event is cleared BEFORE re-registering — `Event.set()` resolves every already
parked waiter synchronously and any coroutine not yet parked ran its own
`_grant_ready()` re-check in the same sync section, so a clear can never drop a
real wake. `_wake_waiters()` is called whenever: a policy is set/cleared, the
floor changes, a server cooldown lands (`report_server_limit`), a grant
happens (successors re-evaluate immediately), or lane config changes.

`_entry_wait(entry)` computes the current sleep: the heap HEAD waits its exact
remaining block (`_entry_block` = max(fleet-wide cooldown, account+action
cooldown, pacing remaining across both scopes)); other entries poll briefly
(`POLL_INTERVAL`) — they can only proceed after the entries ahead of them are
granted. `_prune(key)` drops cancelled/done entries so a dead head can never
block the queue.

Single-event-loop assumption (like the audited limiter): no locks.

### Grant — the receipt

```python
@dataclass
class Grant:
    action: str
    account: Optional[str] = None
    plugin: Optional[str] = None
    wait_s: float = 0.0    # TOTAL pre-jitter wait (server cooldown + pacing + queue + lane)
    jitter_s: float = 0.0  # the human-like jitter actually slept AFTER the grant
    reason: str = ""       # "server" | "pacing" | "" (no wait; "lane" is
                           # historical — the fleet send lane was removed)
```

`reason` is `"server"` when a server cooldown contributed to the wait, else
`"pacing"` (historically `"lane"` when the removed send lane contributed).
`""` when there was no wait.

---

## 4. DEEP DIVE — `RateLimitManager.acquire`

### Purpose
Wait for permission to fire one action, respecting (in order) the optional
server-provided cooldowns and per-key pacing — then sleep
the human-like jitter. Returns a `Grant` receipt.

### Why
This is the single funnel through which every outbound Discord action passes
(the scheduler calls it once per task attempt). Concentrating
server-limit obedience, policy pacing, and jitter here means no plugin or
adapter can bypass pacing — the anti-flag guarantee is structural, not
conventional.

### Inputs

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `action` | `str` | required | action name paced (e.g. `"message"`, `"dm"`); non-empty string, stripped |
| `account` | `Optional[str]` | `None` | account the action fires for; `None` → shared `"__global__"` pacing key |
| `plugin` | `Optional[str]` | `None` | plugin the action belongs to, when known (policy applicability) |
| `priority` | `Priority` | `Priority.NORMAL` | waiter order; `Priority.coerce`d (int, name string, or enum; `None` → NORMAL) |

### Expected Result
A `Grant` with the total pre-jitter wait (`wait_s`), the jitter slept
(`jitter_s`), and the dominant `reason` (`"server"` if a server cooldown
contributed at any wake cycle, else `"pacing"`; the historical `"lane"`
reason can no longer fire — the send lane was removed. Forced to `""`
when `wait_s <= 0`). `wait_s` is measured from
entry to grant; the jitter sleep happens AFTER the grant (and never moves any
pacing clock).

### Side Effects
1. *(Historical)* lane admission was stage 0; the lane is REMOVED —
   `acquire()` starts at the server-cooldown stage.
2. Registers a `_Entry` on the pacing key's heap (`_pending`).
3. On grant, `_grant_ready` stamps `self._last_fire` for EVERY pacing key this
   acquisition satisfies (account key + group key) at grant time.
4. Counters: `self._grants += 1`; when `wait_s > 0`, `self._waits += 1` and
   `self._total_wait += wait_s`, and a
   `core.event_bus.RateLimitTriggered(account, action, wait_s, reason)` event
   is published (awaited inline through `_publish` so sync subscribers see it
   before `acquire()` returns — deterministic for tests; bus failures are
   logged, never raised).
5. Jitter: one `self._jitter_rng.uniform(jitter_min, jitter_max)` draw from the
   effective jitter window, slept through `self._sleep` when > 0.
6. In `finally`: `entry.cancelled = True` + `self._prune(key)` so an aborted
   waiter (cancellation, exception) never blocks its key's heap.

### Errors
- `RatePolicyError` for a malformed action/account/plugin (via
  `_validate_acquire_args`: `None` action, empty/whitespace strings,
  non-strings) or an un-coercible priority (the `Priority.coerce`
  `ValueError` is re-raised as `RatePolicyError`).
- Never raises for time passing: cancellation of the caller propagates
  naturally through the awaited sleeps (the `finally` cleans the heap entry).

### Concurrency
Single event loop, no locks. Multiple waiters on the SAME pacing key are
granted strictly in `(priority, seq)` order; different keys are fully
independent. The wake event + `POLL_INTERVAL` backstop guarantee liveness even
if a wake were lost — the head always recomputes its exact block, non-heads
re-poll. Server cooldowns landing mid-wait wake the head, which then extends
its wait (max semantics in `_entry_block`).

### Callers
- `core.task_scheduler.scheduler.TaskScheduler._run_task` (through the
  `_GrantGate`) — once per task attempt, passing
  `(task.action, task.account, task.plugin, task.priority)`.
- Tests (`tests/unit/test_rate_engine.py`, `test_rate_lane.py`,
  `test_rate_groups.py`) via `ManualClock` + patched `_sleep`.

### Callees
`_validate_acquire_args`, `Priority.coerce`, `GlobalSendLane.enter`,
`_grant_ready`, `_entry_wait`, `_entry_block`, `_wait_either`, `_prune`,
`effective` (for the jitter window), `_jitter_rng.uniform`, `_sleep`,
`_publish` (`EventBus.publish`), `_log`.

### Config
The effective interval/jitter come from stored policies (see §7) and the
floor (persisted in kv `ratelimit:floor`). The `lane` config key is
tolerated and ignored — the fleet send lane was REMOVED.

### Related
[SCHEDULER.md](SCHEDULER.md) (the acquire caller and its checkpoints),
[EVENT_BUS.md](EVENT_BUS.md) (`RateLimitTriggered`),
[ERROR_HANDLING.md](ERROR_HANDLING.md) (`RatePolicyError` family).

---

## 5. Server limits — the framework always respects Discord

**`RateLimitManager.report_server_limit(account, action, retry_after,
global_=False)`** registers a Discord 429: sets/raises the `(account, action)`
cooldown to `now + retry_after` (monotonic, max with any existing), and with
`global_=True` ALSO the fleet-wide `_global_until` cooldown. Validates
`retry_after` (finite number, >= 0 after clamping) and the account/action
names (raises `RatePolicyError` otherwise), logs a warning
(`"server rate limit on {action!r}: cooling account {account!r} {wait:.2f}s
[(fleet-wide)]"`), and wakes parked waiters so they extend their wait for the
new cooldown.

- **`cooldown_remaining(account=None, action=None)`** — seconds left on the
  most relevant server cooldown (0 when none); `None` filters mean "every";
  the fleet-wide cooldown is ALWAYS considered. Rounded to 3 decimals.
- Inflow path: `adapters.discord.client.RealClient._map_send_error` converts
  library `HTTPException` 429 into `core.errors.ServerRateLimit(retry_after,
  global_)` (Retry-After float-parsed, fallback 1.0; global flag from
  `X-RateLimit-Global`); the scheduler catches it, calls
  `report_server_limit`, and retries (always at least once — see
  SCHEDULER.md §retry).

### stats() — JSON-safe snapshot

`RateLimitManager.stats()` returns (for CLI `ratelimit show` / dashboard
Overview):

```python
{
  "floor": float,
  "lane": {"enabled": False, "prefixrate": 0, "removed": True},  # §6
  "groups": {account: group},
  "policies": [ {level, owner, action, interval, jitter:[min,max]} ... ],  # sorted (action, level-rank, owner)
  "cooldowns": {"global": seconds_left, "accounts": {acct: {action: left}}},
  "last_fire_ages": {"owner:action": age_s, ...},
  "grants": int, "waits": int, "avg_wait_s": float,
}
```

### warnings() — advisory taxonomy (`core.rate_limit_manager.warnings`)

Never blocks anything (the floor does the blocking). Output entries are
`{"level", "code", "message"}` with level `"danger" | "warn" | "info"`, sorted
danger > warn > info (stable, by code then message). `RateLimitManager.warnings()`
reviews every STORED policy individually (a global default and an account
override each get their own verdicts — ported audit fix) plus the floor entry,
plus one synthesized lane entry when the lane is paused.

| Code | Level | Trigger | Message gist |
|---|---|---|---|
| `aggressive` | warn | `interval < 3.0`s (`AGGRESSIVE_INTERVAL`) — legal but fast | "paces every X.XXs — aggressive; prefer >= 3s/action with jitter" |
| `no_jitter` | info | `jitter_max <= 0` | "add a range (e.g. 0.20..0.80s) to avoid metronome-perfect timing" |
| `narrow_jitter` | info | `jitter_max - jitter_min < 0.3` (`NARROW_JITTER_SPREAD`) | "widen it for less robotic spacing" |
| `floor` | info | always present | "safety floor is X.XXs per action — faster policies refused (UnsafePolicyError)" |
| `lane_paused` | warn | lane enabled with prefixrate 0 (added by the manager, not `evaluate_policies`) | "every outbound action is holding until a non-zero spacing is set" |

Helpers: `evaluate_policy` (one policy → 0+ entries), `evaluate_policies`
(every policy + floor, sorted), `floor_warning`, `sort_warnings`,
`describe_scope(level, owner)` (human scope name).

---

## 6. GlobalSendLane — REMOVED (historical; `lane.py` stays dormant on disk)

**Status: the fleet send lane is no longer part of `acquire()`.** It was
removed as redundant with sync groups (and a throttle on single-account
throughput): per-account pacing + sync groups + the floor are the pacing
layers. The `ratelimit lane` CLI verb and `/api/ratelimit/lane` routes
answer a compat stub (`{"enabled": false, "prefixrate": 0, "removed":
true, ...}`); `stats()["lane"]` reports the same stub.

*Historically* it was a process-wide, server-wide admission gate in front
of every outbound API call: when enabled, `acquire()` from EVERY
account/plugin passed through ONE priority queue, and grants were handed
out strictly one after another, spaced at least `prefixrate` seconds
apart — no two Discord requests from the whole fleet ever left
back-to-back. It complemented (not replaced) the 4-level policies.

**Ported from FarmLite** (`core/util/rate_limiter.py`): `prefixrate` spacing
before every execution; `prefixrate == 0` PAUSES the lane (submissions hold
until a non-zero rate is set); a single priority queue; the engine never
crashes on task exceptions.

**Robustness over the FarmLite original:**

| FarmLite flaw | This port |
|---|---|
| `_task_engine` background task dies on empty queue and never restarts | NO background engine task at all — WAITER-DRIVEN exactly like the pacing heaps: whoever is waiting drives granting; an empty lane costs nothing, a busy lane cannot die |
| cancelled tasks could wedge the queue | cancellation-safe: `_prune()` drops dead waiters; a cancelled head is skipped on the next pass |
| — | disable-mid-wait drains every waiter immediately (`_drain`), releasing in (priority, seq) order |
| — | configuration changes wake parked waiters through the engine's shared wake event (`on_change` → `_wake_waiters`) so they recompute instead of oversleeping |
| named-task invalidation/shadow-queue coalescing | NO coalescing by design: two different messages are two different sends and must BOTH go out |

**Semantics.** `prefixrate` is the minimum spacing between consecutive lane
GRANTS, ordered by `(priority, seq)` (`_LaneEntry`, its own
`itertools.count()` seq). Cooldown/pacing/jitter run AFTER lane admission
(they can add delay, never remove it), so the wire guarantee is: consecutive
globally-admitted actions are >= prefixrate apart AND every single account
still honors its own effective interval + floor. `prefixrate` is NOT
floor-bound (it spaces the fleet's share of the API, not one account's rate)
but IS validated: finite, >= 0, <= `MAX_PREFIXRATE` (86400s).
`LANE_POLL_INTERVAL = 0.25` is the backstop poll; while paused, the head
wake-driven with poll backstop only.

**API**: `enter(priority) -> float` (seconds spent inside the lane; 0.0
pass-through when disabled), `set_enabled(bool)` (disabling drains),
`set_prefixrate(seconds)` (0 pauses), `paused()` (enabled and prefixrate <= 0),
`status()` (dict for stats/CLI/dashboard):

```python
{"enabled": bool, "prefixrate": float, "paused": bool, "waiting": int,
 "grants": int, "last_grant_age_s": float|None, "held_off_seconds": float}
```

The lane is constructed by `RateLimitManager.__init__` wired onto the engine's
clock, `_wait_either`, `_wake_waiters` (as `on_change`), and `_log`.

Manager wrappers: `set_lane_enabled(enabled)` / `set_lane_prefixrate(seconds)`
(both persist via `_persist_lane` then return `lane_status()`), `lane_status()`.

---

## 7. DEEP DIVE — `RateLimitManager.set_policy_values`

### Purpose
Build a `RatePolicy` from raw numbers and store it at a (level, owner) scope
in one call — the convenience path used by `ControlAPI`, bootstrap, and the
simulator.

### Why
`set_policy` requires an already-constructed `RatePolicy`; every JSON-driving
caller (config file, REST body, CLI flags, scenario scripts) has raw numbers.
One entry point keeps floor enforcement, persistence, logging, and waiter
waking identical for every source.

### Inputs

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `action` | `str` | required | action name; validated by `RatePolicy` (non-empty string) |
| `interval` | `float` | required | minimum seconds between fires; finite, `0 < interval <= 86400` |
| `jitter_min` | `float` | `0.0` | lower jitter bound; finite, >= 0 |
| `jitter_max` | `float` | `0.0` | upper jitter bound; finite, `>= jitter_min`, `<= 3600` |
| `level` | `str` | `"global"` | one of `POLICY_LEVELS` |
| `owner` | `Optional[str]` | `None` | `None` for global; required for the other levels; `"account/plugin"` shape for `account_plugin` |

### Expected Result
The stored `RatePolicy` (returned), active immediately in the in-memory table
`self._policies[(level, owner, action)]` and persisted through the policy repo
when one is attached. `effective()` for every matching acquisition reflects it
on the next evaluation.

### Side Effects
- `RatePolicy(action, interval, jitter_min, jitter_max)` construction (all
  structural validation happens here);
- the shared set path `_store`: `parse_scope` normalization; floor check;
  in-memory table update; `self._repo.set(level, owner, action, interval,
  jitter_min, jitter_max)` (best-effort — a repo failure is LOGGED with
  "policy is still active in memory" and the runtime policy still changes;
  pacing safety never depends on the database);
  `self._log("info", f"rate policy set: {action}={policy} at {scope}")`;
  `self._wake_waiters()` so parked waiters recompute against the new interval.

### Errors
- `RatePolicyError` — from `RatePolicy.__post_init__` for bad numbers, or from
  `parse_scope` for a bad level/owner scope;
- `UnsafePolicyError` (subclass of `RatePolicyError`) — when `interval <
  self._floor`: "unsafe rate policy ... faster than the safety floor ...
  (hard minimum 1.5s per action)". The policy is NOT stored, in memory or in
  the repo.

### Concurrency
Single-loop; `_wake_waiters` makes the change visible to parked waiters on
their next wake (or 0.25s poll backstop). Safe to call while acquisitions are
in flight — waiters recompute their remaining block, extending (never
shortening below the new floor) as needed.

### Callers
- `core.bootstrap._apply_policies` — pushes `config/rate_limits.json`'s
  `global` and `accounts.<name>` blocks at boot;
- `core.api.ControlAPI.ratelimit_set` (CLI `ratelimit set` + dashboard
  `POST /api/ratelimit/policy`);
- `adapters.simulator.runner.SimulationRunner.run` — scenario policies,
  clamped to the scenario floor before the call.

### Callees
`RatePolicy`, `_store` → `parse_scope`, `RatePolicyRepository.set`,
`_log`, `_wake_waiters`.

### Config
`config/rate_limits.json` (see §9) is the first-boot source; every
`set_policy_values` after boot wins and persists to the
`rate_limit_policies` table, which is loaded back by
`_load_stored_policies` at init (bad rows logged + skipped — a hand-edited
database must never crash the process; below-floor rows kept with a warning,
clamped by `effective()`).

### Related
`clear_policy(level, owner, action)` (removes memory + repo, wakes waiters,
returns existence), `policies()` (deterministic listing),
[DATABASE.md](DATABASE.md) (`RatePolicyRepository`).

---

## 8. DEEP DIVE — `RateLimitManager.set_lane_prefixrate`

### Purpose
Set the fleet-wide spacing between consecutive send-lane grants; `0` PAUSES
the lane (every submission holds until a non-zero rate is set).

### Why
The lane is the kill-switch-shaped throttle for the whole fleet: raising the
prefixrate slows every account at once; dropping it to 0 halts all outbound
traffic without stopping the scheduler. It must be runtime-changeable from the
dashboard/CLI, persisted across boots, and instantly visible to waiters
already parked inside the lane (both to extend their wait and to resume them
out of a pause).

### Inputs

| Parameter | Type | Meaning |
|---|---|---|
| `seconds` | `float`-coercible | new prefixrate in seconds; finite, `>= 0`, `<= 86400` (`MAX_PREFIXRATE`). `0` = pause |

### Expected Result
The manager's `lane_status()` dict (see §6 shape) with the new prefixrate
applied and persisted.

### Side Effects
1. `self._lane.set_prefixrate(seconds)` — validates and stores the value, then
   `_notify`s: logs `"lane prefixrate set to Xs"` (+ " (lane PAUSED —
   submissions hold)" when 0) through the engine's `_log`, wakes lane waiters
   via `_wake` → the engine's `on_change` (`_wake_waiters`), so parked waiters
   recompute (a pause holds them on wake-driven polling; a resume releases
   the head on its next wake/poll).
2. `self._persist_lane()` — best-effort kv write of the whole lane config as
   JSON under the key `"ratelimit:lane"` (`{"enabled", "prefixrate"}`); a kv
   failure is logged as a warning, never raised.

### Errors
`RatePolicyError` from `GlobalSendLane.set_prefixrate` when the value is not a
number, is NaN/inf, is negative, or exceeds the one-day cap. The old value
stays in effect.

### Concurrency
Safe while waiters are parked inside `enter()`: the notify path wakes them,
and the 0.25s poll backstop bounds any missed wake. The paused state is
`enabled and prefixrate <= 0`; `set_enabled(False)` at any time drains every
waiter immediately.

### Callers
- `core.api.ControlAPI.ratelimit_set_lane` (dashboard
  `POST /api/ratelimit/lane`, which pre-validates the number and turns the
  error into an honest 400; CLI `ratelimit lane rate <s>`);
- tests (`tests/unit/test_rate_lane.py`: pause/resume, wake + recompute,
  validation). The Discord-side self-command `lane` verb is READ-ONLY
  (`api.ratelimit_lane()` status display) — it does not change the prefixrate.

### Callees
`GlobalSendLane.set_prefixrate` → `_notify` → `_log` + `_wake`/`on_change`
(engine `_wake_waiters`); `_persist_lane` → `KvRepository.set`.

### Config
First-boot baseline: the `lane` key of `config/rate_limits.json`
(`{"enabled": bool, "prefixrate": float}`), parsed by `core.config.load_config`
and passed as constructor defaults; `_load_lane_config` then prefers the
persisted kv value when present, repairs a corrupt row back to defaults
(enabled=False, prefixrate=1.5), and on first boot persists the effective
config so CLI/dashboard edits have a base state. Runtime precedence: kv >
config file.

### Related
[SCHEDULER.md](SCHEDULER.md) (tasks hold inside the lane),
[LOGGING.md](LOGGING.md) (the engine's `_log` swallows logger failures —
logging must never break pacing).

---

## 9. Configuration file & persistence summary

`config/rate_limits.json` (all keys optional; `_comment`-style keys starting
with `_` are dropped as typo protection):

```json
{
  "_comment": "documentation string (ignored)",
  "global":   {"message": {"interval": 2.0, "jitter": [0.1, 0.5]}, "dm": {...}},
  "accounts": {"<name>": {"message": {"interval": 3.0, "jitter": [0.2, 0.8]}}},
  "lane":     {"enabled": true, "prefixrate": 1.5}
}
```

- Missing file → `core.config.DEFAULT_POLICIES` (global `message`/`dm` at
  2.0s + 0.0–0.5s jitter; floor 1.5s everywhere).
- A flat `{action: {...}}` file (no `global`/`accounts` keys) is tolerated and
  applied at global level.
- The `lane` key is popped before policy parsing and becomes the
  first-boot lane baseline (kv runtime state wins on later boots).
- Malformed policy specs fail LOUD at boot (`core.bootstrap._policy_triple`
  raises `ValueError` with the action + expected shape).

Persistence chain: policies → `rate_limit_policies` table (via
`database.repositories.rate_policies.RatePolicyRepository`, best-effort);
lane → `kv` table key `"ratelimit:lane"` (JSON). The in-memory engine is
always authoritative for pacing safety — the database only survives reboots.

---

## Effective-config cache, group clocks, and scheduler pressure

> Source of truth: `core/rate_limit_manager/engine.py`,
> `core/task_scheduler/scheduler.py`. Verified at commit `487dc53`.

**Effective() memoization** — the 4-level composition is a pure sync
function of `_policies` + `_floor`, and ran ≥ 2× per uncontended acquire
(≈ N+1 per grant with N parked co-waiters). It is now memoized per exact
`(action, account, plugin)` tuple under a config generation counter:
4.1–5.6 µs/call independent of policy count, hot paths use the ~0.5 µs
payload form. Group changes deliberately do NOT invalidate (membership
affects pacing timing only, never the composed interval). Cache capped
at 4096 with full-flush fallback; every mutation site bumps the version.

**Sync groups (turn-taking)** — a grouped account paces on BOTH its own
`(account, action)` clock AND `(group:<name>, action)`: any member's
grant stamps the shared clock, so the next member waits the full
interval — N accounts in one group emit ONE account's cadence. Combined
with per-account persistent cooldowns (keyed `(account, plugin, key)`),
the take-turns rolling pattern emerges: whoever isn't cooldown-blocked
rolls, and the shared clock keeps the combined rate constant regardless
of how many members are active.

**Scheduler pressure** (observability, NOT a cap) — `stats()` exposes
depth/HWM, per-priority/per-account QUEUED breakdowns, oldest age,
queue-wait p50/p95/p99, and a soft `pressure` signal
(NORMAL/ELEVATED/PRESSURED/CRITICAL; thresholds grounded in the pacing
floor and the 30 s ack budget). Rate-limited warning at ≥ PRESSURED;
tasks are never expired or rejected by telemetry. Admission control was
evidence-rejected: every producer is an ack-awaiting sequential loop,
structurally bounded by the ack-timeout cancel.
