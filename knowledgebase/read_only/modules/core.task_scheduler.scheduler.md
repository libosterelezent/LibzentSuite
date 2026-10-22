# Module `core.task_scheduler.scheduler`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\task_scheduler\scheduler.py`

```
TaskScheduler — centralized queue, per-account lanes, retry/timeout/cancel.

The ONE place outbound work is dispatched (INTEGRATION "Send pipeline"):

    submit() -> per-account worker lane (concurrency = 1 per account)
             -> limiter.acquire(action, account, plugin, priority)
             -> sender(task)                       [AccountManager.send_hook]
             -> audit record + task history        [terminal transition]

Ported discipline (from the audited ``git show HEAD:core/send_engine.py``):
a token bucket paces the RATE of actions but does not prevent two Discord
requests being IN FLIGHT for one account at the same moment — concurrent
API calls are exactly what flags selfbot accounts. Each account therefore
gets ONE serialized worker ("lane"): a task is popped from that account's
priority heap, granted, sent, and only then does the lane pick the next one.

Worker model (decision):
  * Lanes are spawned lazily on the first submit for an account and live
    until stop(); strong references are kept so no worker is ever GC'd
    mid-flight.
  * Within one account the heap orders by (priority, submission seq), so a
    HIGH task submitted after a LOW one still runs first.

Cross-account fairness (decision):
  * A shared ``_GrantGate`` orders ENTRY into ``limiter.acquire`` globally by
    (priority, seq): when several accounts have runnable work at once, the
    higher-priority task's acquire starts first and the limiter — whose own
    waiter heap is priority-aware — grants it first. The gate batches
    same-tick arrivals with one ``sleep(0)`` hop before releasing the best
    candidate, which is what lets a late-submitted HIGH task overtake an
    already-registered LOW one. It only sequences the *entry*; the acquires
    themselves run concurrently (per-account pacing stays independent), so
    two accounts still send in parallel.

Failure containment (from ``git show HEAD:core/scheduler.py``): workers
swallow every sender exception into a task failure — the scheduler NEVER
crashes. History-repo and audit failures are likewise isolated (try/except
+ log).

Retry classification (INTEGRATION contract): ``ServerRateLimit`` is
reported to the limiter then retried; ``ConnectionError`` retries;
``FlowBlocked`` and everything else fail immediately with the reason.
Timeouts — budget expiry (``asyncio.TimeoutError`` from the outer
``wait_for``) AND sender-internal ``TimeoutError`` alike — are TERMINAL
(status "timed_out", never auto-retried): a timed-out sender may already
have the request on the wire, and cancelling the wait cannot un-send it,
so an automatic retry would DUPLICATE the message (the primary
anti-flag hazard for a selfbot framework). The two sources are not
distinguishable at the scheduler seam, so the safe default applies to
both (documented decision).
Retries carry NO scheduler delay — they re-enter limiter.acquire immediately; the limiter's cooldown + interval own every delay (pacing is exclusively the limiter's job)
through the ``_sleep`` seam (deterministic tests) and are capped at
``1 + max_retries`` total attempts.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `_AcquireCancelled` | exception | no | limiter.acquire raised CancelledError without being cancelled by us. |
| `_GrantGate` | class | no | Shared priority turnstile ordering ENTRY into limiter.acquire. |
| `TaskScheduler` | class | yes | Centralized task queue with one serialized worker lane per account. |

### `_GrantGate`

- `async def enter(self, priority: int, seq: int) -> None`
  - Wait until it is this (priority, seq) pair's turn to acquire.

### `TaskScheduler`

- `async def restore_sequence(self) -> int`
  - Continue task ids AFTER the highest one already in the history.
- `def start(self) -> None`
  - Mark the scheduler ready (idempotent; RE-ARMS after stop()).
- `async def stop(self, *, drain: bool=True, timeout: float=10.0) -> None` — side effects: `sleeps, publishes_event`
  - Stop the scheduler (idempotent).
- `async def cleanup_account(self, account: str) -> None`
  - Tear down ONE account's lane bookkeeping (called on removal).
- `def submit(self, account: str, plugin: str, action: str, *, channel_id: Optional[int]=None, user_id: Optional[int]=None, content: Optional[str]=None, message_id: Optional[int]=None, priority: Priority=Priority.NORMAL, name: Optional[str]=None, timeout: Optional[float]=None, retries: int=0) -> Task` *(plugin API)*
  - Validate, queue and publish one task; returns the live Task.
- `def cancel(self, task_id: int) -> bool`
  - Cancel one task by id.
- `async def forget_account(self, account: str) -> int`
  - Compat alias for cleanup_account (phase-3 name). See K1.
- `def cancel_matching(self, *, account: Optional[str]=None, plugin: Optional[str]=None, action: Optional[str]=None) -> int`
  - Cancel every live task matching ALL given filters.
- `def get(self, task_id: int) -> Optional[Task]`
  - The Task object for task_id (live first, then recently finished).
- `def tasks(self, *, status: Optional[str]=None, account: Optional[str]=None) -> List[dict]`
  - Live queued/running tasks plus recently finished ones, newest last.
- `async def history(self, limit: int=50, account: Optional[str]=None) -> List[dict]`
  - Finished tasks, newest last — repo-backed (awaited), else the
- `def stats(self) -> dict`
  - Scheduler counters from in-memory tallies (UI Overview page).
- `def pressure(self) -> dict`
  - Soft pressure signal (FINAL-PHASE G2; pure, no task mutation).
