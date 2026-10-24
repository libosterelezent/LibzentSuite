# Module `core.rate_limit_manager.lane`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `core\rate_limit_manager\lane.py`

```
GlobalSendLane — FarmLite's sequential-send concept, made robust (spec add-on).

WHAT IT IS
    A process-wide, server-wide admission gate in front of every outbound
    API call: when enabled, ``acquire()`` from EVERY account/plugin passes
    through ONE queue, and grants are handed out strictly one after another,
    spaced at least ``prefixrate`` seconds apart. No two Discord requests
    from the whole fleet ever leave back-to-back.

    This complements — not replaces — the 4-level pacing policies: pacing
    still enforces a per-(account, action) minimum interval on top, so an
    individual account can never exceed its own policy; the lane only adds
    fleet-wide serialization on the shared Discord API.

PORTED FROM FARMLITE (core/util/rate_limiter.py)
    * ``prefixrate`` spacing BEFORE every execution — sleep, then run;
    * ``prefixrate == 0`` PAUSES the lane: submissions hold until a
      non-zero rate is set (``set_prefixrate``/dashboard/CLI);
    * a single priority queue ordering all pending sends;
    * the engine never crashes on task exceptions.

ROBUSTNESS OVER THE FARMLITE ORIGINAL
    * NO background engine task at all — FarmLite's ``_task_engine`` dies the
      moment it finds an empty queue (``return`` on ``queue.empty()``) and
      never restarts. Here the lane is WAITER-DRIVEN, exactly like the pacing
      heaps: whoever is waiting drives the granting, so an empty lane costs
      nothing and a busy lane cannot die.
    * cancellation-safe: cancelled waiters are pruned; a cancelled heap head
      is skipped on the next pass and never blocks the queue;
    * disable-mid-wait drains every waiter immediately (no one is stranded
      on a lane that no longer gates anything);
    * configuration changes wake parked waiters through the engine's shared
      wake event so they recompute instead of oversleeping;
    * no coalescing/shadow-queue by design: two different messages are two
      different sends and must BOTH go out (anonymous sends never collapse —
      deliberate difference from FarmLite's named-task invalidation).

SEMANTICS (precise)
    ``prefixrate`` is the minimum spacing between consecutive lane GRANTS,
    ordered by (priority, seq). Each caller passes the jitter it has
    ALREADY drawn (before admission — see engine.acquire); a grant stamps
    the spacing clock at ``admission + jitter`` — the EXPECTED wire
    instant — so the next admission waits a full prefixrate after the
    previous send was expected to leave. Cooldown/pacing run AFTER lane
    admission (they can add delay, never remove it), so an actual wire can
    only leave LATER than its expected instant: same-key wire gaps always
    hold, and a cross-account wire gap can only shrink below prefixrate
    when the EARLIER admission's own pacing held it past its expectation
    (documented residual — the lane cannot know a future pacing hold at
    admission time; the earlier send's own per-key interval still holds).
    Every single account still honors its own effective interval + the
    1.5s floor. The prefixrate itself is NOT floor-bound (it spaces the
    fleet share of the API, not one account's action rate) but it is
    validated: finite, >= 0, <= one day.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `_LaneEntry` | class | no | One pending lane waiter, heap-ordered by (priority, seq). |
| `GlobalSendLane` | class | yes | Server-wide sequential send gate (waiter-driven, cannot die). |

### `_LaneEntry`


### `GlobalSendLane`

- `def set_enabled(self, enabled: bool) -> None`
  - Enable/disable the gate; disabling DRAINS every current waiter.
- `def set_prefixrate(self, seconds: Any) -> float`
  - Set the spacing between consecutive grants (FarmLite semantics).
- `def paused(self) -> bool`
  - True when the lane is enabled with prefixrate 0 (everything holds).
- `def status(self) -> dict`
  - JSON-safe lane state for stats()/CLI/dashboard.
- `async def enter(self, priority: int, jitter: float=0.0) -> float`
  - Wait for this caller's turn through the gate.
