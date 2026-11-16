# Module `core.account_manager.lifecycle`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\account_manager\lifecycle.py`

```
Account lifecycle rate limiting — start/stop/restart abuse prevention.

PHASE-4 hardening (S4 design, scoped core): a dedicated limiter for
LIFECYCLE operations, deliberately independent of the Discord request/
content rate engine (``core/rate_limit_manager`` never sees these).

Two levels:
  * GLOBAL token buckets per op class — bounds fleet-wide lifecycle bursts
    (the genuinely shared cost is the per-IP IDENTIFY budget: one token
    per ``start_refill_s`` mirrors the adapter's IdentifyGate interval).
  * PER-ACCOUNT pacers — cooldown floors between lifecycle ops on the
    same account, so start→stop→start thrashing cannot churn clients,
    plugin hooks, supervisors and gateway sessions at operator speed.

State-awareness: the governor never replaces the lifecycle state machine.
State-refusable operations (start on RUNNING, stop on a clean STOPPED)
are answered by the manager's existing gates BEFORE the governor is
consulted, and consume no capacity. The governor only answers "how soon
may this state-legal operation run".

Safety direction: STOP is never cooldown-gated (stopping is the safety
action, estop-adjacent); emergency stop, framework teardown and account
removal bypass the governor entirely.

Costs (defaults derived from measured behavior, not magic):
  * START ≈ token resolve (ms) + client build + gateway IDENTIFY through
    the shared 5s gate + READY wait + cache ingest + plugin activation —
    typically 2-5s plus 5s of shared identify budget.
  * STOP ≈ plugin hooks + disconnect + bounded supervisor reap — <1s,
    worst ~2-3s.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `LifecycleRateLimited` | exception | yes | A lifecycle operation was refused by the lifecycle governor. |
| `LifecycleDecision` | dataclass | yes | One governor verdict for a lifecycle operation request. |
| `LifecyclePolicy` | dataclass | yes | Validated lifecycle limiting knobs (all seconds / counts). |
| `_TokenBucket` | class | no | Monotonic token bucket (clock injectable for deterministic tests). |
| `_Pacer` | dataclass | no | Per-account lifecycle op timestamps (monotonic). |
| `LifecycleGovernor` | class | yes | Precheck/commit lifecycle limiter; O(1) decisions, no I/O. |

### `LifecycleRateLimited`


### `LifecyclePolicy`


### `_TokenBucket`

- `def try_acquire(self) -> Tuple[bool, float]`
  - (ok, retry_after_s) for one token.
- `def peek(self) -> Tuple[bool, float]`
  - (ok, retry_after_s) WITHOUT consuming a token (no state write).

### `LifecycleGovernor`

- `def precheck(self, account: str, op: str, *, status: Any=None, bypass: bool=False) -> LifecycleDecision`
  - Decide one lifecycle request. ``op`` is 'start' or 'stop'.
- `def commit(self, account: str, op: str, *, ok: bool, invalid_credential: bool=False) -> None`
  - Record the outcome so cooldowns track reality.
- `def forget(self, account: str) -> None`
  - Removal cascade: drop the pacer (name-reuse starts clean).
- `def retry_after(self, account: str, op: str) -> float`
  - Seconds until the next legal ``op`` (0 when legal now).
- `def stats(self) -> Dict[str, Any]`
