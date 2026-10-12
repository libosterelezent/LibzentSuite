# Module `core.rate_limit_manager.engine`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/rate_limit_manager/engine.py`

```
Pacing engine: priority waiters, server cooldowns, jitter (A4).

This is the rewrite of the audited ``core/rate_limiter.py`` (token buckets)
as an interval+pacing engine, plus the server-limit cooldown ideas from the
audited ``core/rest_buckets.py`` (fleet-wide + account-scoped 429 cooldowns).

Waiting model (three stages inside ``acquire``, in order):

1. SERVER cooldowns — the framework ALWAYS respects Discord-provided limits:
   the fleet-wide global cooldown and the (account, action) cooldown are
   waited out first (Grant.reason == "server").
2. PACING — actions on a pacing key ``(account or "__global__", action)``
   must be spaced by at least the effective interval (max across applicable
   policies, never below the safety floor). Concurrent waiters for the SAME
   key are granted in priority order, then FIFO within a priority — the
   heap + sequence-number waiter pattern ported from the old limiter
   (Grant.reason == "pacing").
3. JITTER — one uniform draw from the effective jitter window, made BEFORE
   admission (right after ``effective()``) and slept AFTER the grant. The
   draw travels through the waiter entry (and the lane), and every spacing
   clock is stamped at ``grant + jitter`` — the EXPECTED wire instant — so
   consecutive WIRE sends stay >= the effective interval apart no matter
   how the draws differ (a post-grant-only draw let a smaller second jitter
   compress the wire gap to ``interval - jitter_max`` and even invert it).

Determinism seams (all time flows through them — see INTEGRATION.md):

* ``clock``      — injected Clock; default is monotonic SystemClock; tests
                   pass a ManualClock and advance() it.
* ``_sleep``     — EVERY timed wait goes through ``await self._sleep(s)``;
                   default ``asyncio.sleep``. Tests subclass/patch this seam
                   and advance the manual clock instead of blocking.
* ``jitter_seed``— seeds the jitter RNG for reproducible draws.

Wake mechanics: waiters park on ``_wait_either`` which sleeps through the
seam but returns EARLY when the shared wake event fires. The event is set
whenever policies change (set/clear/floor) or a server cooldown lands, so
sleeping waiters recompute their remaining wait instead of oversleeping.
_grant_ready also wakes successors after any grant so the next waiter in
priority order re-evaluates immediately. A bounded poll (POLL_INTERVAL)
backstops non-head waiters against lost wakes — correctness never depends
on the wake.

Other documented decisions (INTEGRATION.md: pick the safest option):

- effective() interval is max(all applicable policies, floor) — a stored
  policy can never pace faster than the floor even after set_floor()
  raised it past existing policy intervals.
- waiters of one pacing key do NOT block other keys (per-account pacing),
  exactly as the pinned contract states.
- persistence is NOT the engine's job anymore: ``set_policy``/
  ``clear_policy``/``set_lane_*`` mutate MEMORY ONLY (sync, wake waiters);
  the ControlAPI layer persists policy edits, and :meth:`load_persisted`
  (awaited once at boot, after ``database.connect()``) loads the stored
  rows into memory. Pacing safety never depends on the database.
- single-event-loop assumption, like the audited limiter (no locks).
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Clock` | protocol | yes | Minimal clock protocol so tests can drive time manually. |
| `SystemClock` | class | yes | Production clock (monotonic — immune to wall-clock jumps). |
| `ManualClock` | class | yes | Test clock: advance() moves time forward without sleeping. |
| `Grant` | dataclass | yes | Receipt for one acquired action: what waited, why, and the jitter. |
| `_Entry` | dataclass | no | Pending waiter, heap-ordered by (priority, sequence) — ported as-is. |
| `RateLimitManager` | class | yes | 4-level rate policies + pacing engine + server limits + warnings. |

### `Clock`

- `def now(self) -> float`

### `SystemClock`

- `def now(self) -> float`

### `ManualClock`

- `def now(self) -> float`
- `def advance(self, seconds: float) -> float`

### `_Entry`


### `RateLimitManager`

- `async def load_persisted(self) -> None` — side effects: `db_write, network`
  - Boot path: read the stored policies + lane config into memory.
- `def set_policy(self, policy: RatePolicy, level: str='global', owner: Optional[str]=None) -> RatePolicy`
  - Store a RatePolicy at (level, owner) — memory only (the ControlAPI
- `def set_policy_values(self, action: str, interval: float, jitter_min: float=0.0, jitter_max: float=0.0, level: str='global', owner: Optional[str]=None) -> RatePolicy` — side effects: `db_write`
  - Convenience used by ControlAPI: build + store a policy in one call.
- `def clear_policy(self, level: str, owner: Optional[str], action: str) -> bool`
  - Remove one stored policy from MEMORY. True when it existed.
- `def policies(self) -> List[dict]`
  - Every stored policy as ``{level, owner, action, interval, jitter}``,
- `def effective(self, action: str, account: Optional[str]=None, plugin: Optional[str]=None) -> dict`
  - Compose all applicable policy levels for one action.
- `def effective_interval(self, action: str, account: Optional[str]=None) -> float`
  - The effective interval (seconds) for one action+account — the
- `def floor_value(self) -> float`
  - Current safety floor (seconds).
- `def set_removed_defaults(self, entries: list) -> None`
  - Sync the cleared-policy tombstones (ControlAPI persist path).
- `def removed_default(self, level: str, owner, action: str) -> bool`
  - True when the user explicitly CLEARED this key (seed must skip).
- `def set_floor(self, seconds: float) -> float`
  - Raise the safety floor (never lower, never below 1.5s).
- `async def prune_account(self, account: str) -> None` — side effects: `db_write`
  - Drop a removed account's engine residue + policy rows (cascade).
- `def set_account_group(self, account: str, group: Optional[str]) -> None`
  - Register which GROUP an account operates in (per-account/group
- `def account_groups(self) -> Dict[str, str]`
  - account -> group mapping (stats/UI).
- `async def acquire(self, action: str, account: Optional[str]=None, plugin: Optional[str]=None, priority: Priority=Priority.NORMAL) -> Grant` — side effects: `publishes_event`
  - Wait for permission to fire one action; returns a Grant.
- `def report_server_limit(self, account: str, action: str, retry_after: float, global_: bool=False) -> None`
  - Register a Discord 429: cooldown the (account, action) pair, and
- `def cooldown_remaining(self, account: Optional[str]=None, action: Optional[str]=None) -> float`
  - Seconds left on the most relevant server cooldown (0 when none).
- `def stats(self) -> dict`
  - JSON-safe snapshot for the UI: floor, policies, active cooldowns,
- `def warnings(self) -> List[dict]`
  - Advisory safety review of every stored policy.
