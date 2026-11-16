"""GlobalSendLane — FarmLite's sequential-send concept, made robust (spec add-on).

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
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
from typing import TYPE_CHECKING, Any

from ..errors import RatePolicyError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: same sanity cap as policies.MAX_INTERVAL — one day of spacing is plenty
MAX_PREFIXRATE = 86400.0

#: backstop poll for non-head lane waiters (correctness never relies on wakes)
#: non-head backstop poll. PHASE-4 (S7-C2): mirrors the engine's measured
#: phase-3 fix (0.25 -> 2.0s, a 4-37x waiter-CPU reduction): wakes already
#: cover grants/config changes; the poll exists only as a correctness
#: backstop, so 2s bounds a lost-wake delay without burning CPU per
#: non-head waiter at 4 Hz.
LANE_POLL_INTERVAL = 2.0


class _LaneEntry:
    """One pending lane waiter, heap-ordered by (priority, seq).

    ``jitter`` is the caller's ALREADY-DRAWN post-admission pause: granting
    stamps the spacing clock at admission + jitter (the expected wire
    instant), so a small follow-up draw can no longer compress or invert
    the cross-account wire gap (probe P2/P7).
    """

    __slots__ = ("cancelled", "future", "jitter", "priority", "seq")

    def __init__(self, priority: int, seq: int, jitter: float = 0.0) -> None:
        self.priority = priority
        self.seq = seq
        self.jitter = max(0.0, float(jitter))
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.cancelled = False

    def __lt__(self, other: _LaneEntry) -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


class GlobalSendLane:
    """Server-wide sequential send gate (waiter-driven, cannot die).

    The engine calls :meth:`enter` as stage 0 of every ``acquire()`` when the
    lane is enabled; everything else (configuration, status, persistence)
    goes through the manager's ``set_lane_*`` / ``lane_status`` wrappers.
    """

    def __init__(
        self,
        *,
        clock: Any,
        wait_either: Callable[[float], Awaitable[None]],
        on_change: Callable[[], None] | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        """Wire the lane onto its host engine.

        Args:
            clock: the engine's injectable clock (``now() -> float``) — keeps
                lane spacing deterministic under ManualClock in tests.
            wait_either: the engine's wake-aware sleep (``_wait_either``) —
                lane waits race the SAME shared wake event, so policy or lane
                changes re-kick parked waiters instead of letting them
                oversleep.
            on_change: called after every configuration change so the engine
                can wake its own waiters too.
            log: ``log(level, message)`` sink (optional).
        """
        self._clock = clock
        self._wait_either = wait_either
        self._on_change = on_change
        self._log = log or (lambda level, message: None)
        self.enabled = False
        self.prefixrate = 1.5
        self._heap: list[_LaneEntry] = []
        self._seq = itertools.count()
        self._last_grant: float | None = None  # last admission + jitter
        # stats
        self.grants = 0
        self.held_off_seconds = 0.0  # total time waiters spent holding (paused)

    # ---------------------------------------------------------- configuration
    def set_enabled(self, enabled: bool) -> None:
        """Enable/disable the gate; disabling DRAINS every current waiter."""
        enabled = bool(enabled)
        changed = enabled != self.enabled
        self.enabled = enabled
        if not enabled:
            self._drain()
        if changed:
            self._notify(f"sequential send lane {'enabled' if enabled else 'disabled'}")

    def set_prefixrate(self, seconds: Any) -> float:
        """Set the spacing between consecutive grants (FarmLite semantics).

        ``0`` PAUSES the lane — submissions hold until a non-zero rate is
        set. Raises RatePolicyError for anything not finite / negative /
        beyond the one-day cap.
        """
        try:
            value = float(seconds)
        except (TypeError, ValueError) as exc:
            raise RatePolicyError(f"prefixrate must be a number, got {seconds!r}") from exc
        if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
            raise RatePolicyError(f"prefixrate must be finite, got {seconds!r}")
        if value < 0:
            raise RatePolicyError(f"prefixrate must be >= 0, got {value:g}s")
        if value > MAX_PREFIXRATE:
            raise RatePolicyError(f"prefixrate {value:g}s exceeds the {MAX_PREFIXRATE:g}s cap")
        self.prefixrate = value
        self._notify(
            f"lane prefixrate set to {value:g}s"
            + (" (lane PAUSED — submissions hold)" if value == 0 else "")
        )
        return self.prefixrate

    def paused(self) -> bool:
        """True when the lane is enabled with prefixrate 0 (everything holds)."""
        return self.enabled and self.prefixrate <= 0

    def status(self) -> dict:
        """JSON-safe lane state for stats()/CLI/dashboard."""
        now = self._clock.now()
        waiting = sum(1 for e in self._heap if not e.cancelled and not e.future.done())
        return {
            "enabled": self.enabled,
            "prefixrate": self.prefixrate,
            "paused": self.paused(),
            "waiting": waiting,
            "grants": self.grants,
            "last_grant_age_s": (
                round(max(0.0, now - self._last_grant), 3) if self._last_grant is not None else None
            ),
            "held_off_seconds": round(self.held_off_seconds, 3),
        }

    # ---------------------------------------------------------------- enter
    async def enter(self, priority: int, jitter: float = 0.0) -> float:
        """Wait for this caller's turn through the gate.

        ``jitter`` is the caller's already-drawn post-admission pause; the
        grant stamps the spacing clock at admission + jitter (the expected
        wire instant), so consecutive admissions are spaced a full
        prefixrate after the previous expected wire departure.

        Returns the seconds spent waiting INSIDE the lane (0 when admitted
        immediately). No-op pass-through (0.0, no queueing) when disabled —
        but a waiter that arrives while disabled and gets disabled-mid-wait
        is drained out instantly.
        """
        if not self.enabled:
            return 0.0
        entry = _LaneEntry(int(priority), next(self._seq), jitter)
        heapq.heappush(self._heap, entry)
        started_at = self._clock.now()
        was_held = False
        try:
            while not entry.future.done():
                self._grant_ready()
                if entry.future.done():
                    break
                was_held = was_held or self.paused()  # held at ANY poll pass
                await self._wait_either(self._entry_wait(entry))
            waited = max(0.0, self._clock.now() - started_at)
            if was_held:
                # telemetry: time waiters spent holding while the lane was
                # PAUSED (the field existed but was never accumulated)
                self.held_off_seconds += waited
            return waited
        finally:
            entry.cancelled = True
            self._prune()
            # PHASE-4 (S7-P2): a cancelled head must wake its successors —
            # before the poll interval was raised, the 0.25s backstop masked
            # this; with a 2s backstop the next waiter would otherwise sit
            # parked for the full interval despite being grantable now.
            self._wake()

    # ------------------------------------------------------------ internals
    def _entry_wait(self, entry: _LaneEntry) -> float:
        """How long this waiter should sleep before re-checking.

        The heap head computes its exact remaining block: infinite-hold when
        the lane is paused (the wake event alone resumes it — a modest poll
        backstop keeps correctness independent of wakes); otherwise the
        prefixrate remainder. Non-heads poll briefly: they can only proceed
        after the entries ahead of them are granted.
        """
        self._prune()
        heap = self._heap
        if not heap or heap[0] is not entry:
            return LANE_POLL_INTERVAL
        if self.paused():
            return LANE_POLL_INTERVAL  # wake-driven; poll is a backstop only
        if self.prefixrate <= 0:  # defensive: paused() covers this
            return LANE_POLL_INTERVAL
        if self._last_grant is None:
            return 0.0
        remaining = self._last_grant + self.prefixrate - self._clock.now()
        return max(0.0, remaining)

    def _grant_ready(self) -> None:
        """Grant the heap head when its spacing has elapsed.

        Only the HEAD is grantable — priority order is structural, exactly
        like the pacing heaps. Granting stamps ``_last_grant`` (the spacing
        clock) at admission + the grantee's jitter draw — the expected wire
        instant — and wakes the next waiter through ``on_change``.
        """
        self._prune()
        heap = self._heap
        if not heap:
            return
        if not self.enabled:
            self._drain()
            return
        if self.paused():
            return
        granted_any = False
        while heap:
            head = heap[0]
            if head.cancelled or head.future.done():
                heapq.heappop(heap)
                continue
            if self._last_grant is not None:
                remaining = self._last_grant + self.prefixrate - self._clock.now()
                if remaining > 0:
                    break  # spacing not elapsed: everyone behind waits too
            self._last_grant = self._clock.now() + head.jitter
            heapq.heappop(heap)
            if not head.future.done():
                head.future.set_result(None)
            self.grants += 1
            granted_any = True
            # exactly ONE grant per spacing window — stop after the head
            break
        if granted_any:
            self._wake()

    def _drain(self) -> None:
        """Disable path: release every waiter immediately, in order."""
        released = 0
        while self._heap:
            head = heapq.heappop(self._heap)
            if head.cancelled or head.future.done():
                continue
            head.future.set_result(None)
            released += 1
        if released:
            self._wake()

    def _prune(self) -> None:
        """Drop dead waiters so a cancelled head can never block the queue."""
        if not self._heap:
            return
        self._heap[:] = [e for e in self._heap if not e.cancelled and not e.future.done()]
        heapq.heapify(self._heap)

    def _notify(self, message: str) -> None:
        """Log + propagate the configuration change (wake + callback)."""
        self._log("info", message)
        self._wake()
        if self._on_change is not None:
            # a notification callback must never break configuration
            with contextlib.suppress(Exception):
                self._on_change()

    def _wake(self) -> None:
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change()


__all__ = ["LANE_POLL_INTERVAL", "MAX_PREFIXRATE", "GlobalSendLane"]
