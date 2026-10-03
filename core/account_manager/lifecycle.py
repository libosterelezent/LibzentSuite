"""Account lifecycle rate limiting — start/stop/restart abuse prevention.

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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "OUTCOME_STARTED", "OUTCOME_STOPPED", "OUTCOME_RATE_LIMITED",
    "LifecycleDecision", "LifecyclePolicy", "LifecycleGovernor",
    "LifecycleRateLimited",
]

OUTCOME_STARTED = "STARTED"
OUTCOME_STOPPED = "STOPPED"
OUTCOME_RATE_LIMITED = "RATE_LIMITED"


class LifecycleRateLimited(Exception):
    """A lifecycle operation was refused by the lifecycle governor.

    ``retry_after_s`` is the earliest legal re-try. Subclass of Exception
    (not AccountError) so surfaces can render it distinctly; the account
    manager wraps it into an AccountError with the same message.
    """

    def __init__(self, account: str, op: str, retry_after_s: float,
                 reason: str) -> None:
        super().__init__(
            f"account {account!r} {op} rate-limited ({reason}): retry in "
            f"{retry_after_s:.1f}s")
        self.account = account
        self.op = op
        self.retry_after_s = float(retry_after_s)
        self.reason = reason


@dataclass(frozen=True)
class LifecycleDecision:
    """One governor verdict for a lifecycle operation request."""
    allowed: bool
    outcome: str            # OUTCOME_* vocabulary
    retry_after_s: float = 0.0
    reason: str = ""        # "account_cooldown" | "global_bucket" | ""


@dataclass(frozen=True)
class LifecyclePolicy:
    """Validated lifecycle limiting knobs (all seconds / counts).

    Defaults rationale (measured, see module docstring):
      start_burst 5 / refill 5s — Discord's ~1-IDENTIFY/5s per-IP budget;
        the bucket adds refusal semantics on top of the IdentifyGate's
        serialization without extra slowing.
      stop_burst 16 / refill 1s — stops are local + bounded; stay generous.
      restart_cooldown 10s — stop→start floor; two identify slots; caps
        machine-speed start/stop churn (~2-3s/cycle) at a 4x reduction
        without blocking legit stop→inspect→restart flows.
      start_retry_cooldown 15s — FAILED→start floor; sits between the
        supervisor's own backoff attempts 4-5, so manual retries are never
        faster than the machine's escalation.
      invalid_credential_retry 300s — retrying a rejected token cannot
        succeed and trips the login throttle (observed as a false
        "bad token"); escalates from start_retry only on streak >= 2.
      stop_cooldown 0 — stopping is the safety direction.
    """
    start_burst: int = 5
    start_refill_s: float = 5.0
    stop_burst: int = 16
    stop_refill_s: float = 1.0
    restart_cooldown_s: float = 10.0
    start_retry_cooldown_s: float = 15.0
    invalid_credential_retry_s: float = 300.0
    stop_cooldown_s: float = 0.0
    invalid_streak_threshold: int = 2

    def __post_init__(self) -> None:
        for name in ("start_burst", "stop_burst"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise ValueError(f"{name} must be an int >= 1, got {v!r}")
        for name in ("start_refill_s", "stop_refill_s",
                     "restart_cooldown_s", "start_retry_cooldown_s",
                     "invalid_credential_retry_s", "stop_cooldown_s"):
            v = getattr(self, name)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a number, got {v!r}") from None
            if fv < 0 or fv > 86400:
                raise ValueError(f"{name} must be within [0, 86400], got {v!r}")


class _TokenBucket:
    """Monotonic token bucket (clock injectable for deterministic tests)."""

    __slots__ = ("capacity", "refill_s", "_tokens", "_last", "_clock")

    def __init__(self, capacity: int, refill_s: float,
                 clock: Optional[Any] = None) -> None:
        self.capacity = int(capacity)
        self.refill_s = max(float(refill_s), 1e-9)
        self._tokens = float(capacity)
        self._clock = clock or time.monotonic
        self._last: float = self._clock()

    def try_acquire(self) -> Tuple[bool, float]:
        """(ok, retry_after_s) for one token."""
        now = self._clock()
        self._tokens = min(
            float(self.capacity),
            self._tokens + (now - self._last) / self.refill_s)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True, 0.0
        return False, (1.0 - self._tokens) * self.refill_s


@dataclass
class _Pacer:
    """Per-account lifecycle op timestamps (monotonic)."""
    last_start: float = 0.0
    last_stop: float = 0.0
    last_failed_start: float = 0.0
    last_invalid: float = 0.0
    invalid_streak: int = 0


class LifecycleGovernor:
    """Precheck/commit lifecycle limiter; O(1) decisions, no I/O.

    The account manager consults ``precheck`` only AFTER its own state
    gates (state-illegal ops never reach here and never consume tokens),
    and reports outcomes via ``commit`` so cooldowns track reality.
    """

    def __init__(self, policy: Optional[LifecyclePolicy] = None, *,
                 clock: Optional[Any] = None,
                 enabled: bool = True) -> None:
        self.policy = policy or LifecyclePolicy()
        self.enabled = enabled
        self._clock = clock or time.monotonic
        self._pacers: Dict[str, _Pacer] = {}
        self._start_bucket = _TokenBucket(
            self.policy.start_burst, self.policy.start_refill_s, self._clock)
        self._stop_bucket = _TokenBucket(
            self.policy.stop_burst, self.policy.stop_refill_s, self._clock)
        self.counters: Dict[str, int] = {
            "decisions": 0, "allowed": 0, "refused": 0, "bypassed": 0}

    # ------------------------------------------------------------ decisions
    def precheck(self, account: str, op: str, *,
                 status: Any = None,
                 bypass: bool = False) -> LifecycleDecision:
        """Decide one lifecycle request. ``op`` is 'start' or 'stop'."""
        self.counters["decisions"] += 1
        if not self.enabled or bypass:
            self.counters["bypassed"] += 1
            return LifecycleDecision(
                True, OUTCOME_STARTED if op == "start" else OUTCOME_STOPPED)
        now = self._clock()
        pacer = self._pacers.get(account)
        if op == "start":
            cooldown, since = self._start_cooldown(pacer, status)
            ready = (since + cooldown) if since else 0.0
            if ready > now:
                self.counters["refused"] += 1
                return LifecycleDecision(
                    False, OUTCOME_RATE_LIMITED, ready - now,
                    "account_cooldown")
            ok, retry = self._start_bucket.try_acquire()
            if not ok:
                self.counters["refused"] += 1
                return LifecycleDecision(
                    False, OUTCOME_RATE_LIMITED, retry, "global_bucket")
        elif op == "stop":
            if self.policy.stop_cooldown_s > 0 and pacer:
                ready = pacer.last_stop + self.policy.stop_cooldown_s
                if ready > now:
                    self.counters["refused"] += 1
                    return LifecycleDecision(
                        False, OUTCOME_RATE_LIMITED, ready - now,
                        "account_cooldown")
            ok, retry = self._stop_bucket.try_acquire()
            if not ok:
                self.counters["refused"] += 1
                return LifecycleDecision(
                    False, OUTCOME_RATE_LIMITED, retry, "global_bucket")
        else:  # pragma: no cover - internal misuse
            raise ValueError(f"unknown lifecycle op {op!r}")
        self.counters["allowed"] += 1
        return LifecycleDecision(
            True, OUTCOME_STARTED if op == "start" else OUTCOME_STOPPED)

    def commit(self, account: str, op: str, *, ok: bool,
               invalid_credential: bool = False) -> None:
        """Record the outcome so cooldowns track reality."""
        pacer = self._pacers.setdefault(account, _Pacer())
        now = self._clock()
        if op == "start":
            pacer.last_start = now
            if not ok:
                pacer.last_failed_start = now
                if invalid_credential:
                    pacer.last_invalid = now
                    pacer.invalid_streak += 1
                else:
                    pacer.invalid_streak = 0
            else:
                pacer.invalid_streak = 0
                pacer.last_invalid = 0.0
        elif op == "stop":
            pacer.last_stop = now

    def forget(self, account: str) -> None:
        """Removal cascade: drop the pacer (name-reuse starts clean)."""
        self._pacers.pop(account, None)

    def retry_after(self, account: str, op: str) -> float:
        """Seconds until the next legal ``op`` (0 when legal now)."""
        decision = self.precheck(account, op)
        return decision.retry_after_s if not decision.allowed else 0.0

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "policy": {
                "start_burst": self.policy.start_burst,
                "start_refill_s": self.policy.start_refill_s,
                "stop_burst": self.policy.stop_burst,
                "stop_refill_s": self.policy.stop_refill_s,
                "restart_cooldown_s": self.policy.restart_cooldown_s,
                "start_retry_cooldown_s": self.policy.start_retry_cooldown_s,
                "invalid_credential_retry_s":
                    self.policy.invalid_credential_retry_s,
                "stop_cooldown_s": self.policy.stop_cooldown_s,
            },
            "counters": dict(self.counters),
            "cooldowns": {
                name: self.retry_after(name, "start")
                for name in sorted(self._pacers)
                if self.retry_after(name, "start") > 0
            },
        }

    # ------------------------------------------------------------- helpers
    def _start_cooldown(self, pacer: Optional[_Pacer], status: Any,
                        ) -> Tuple[float, float]:
        """(cooldown_seconds, reference_monotonic) for a start request.

        The reference is the timestamp the cooldown is measured FROM:
        the last STOP for ordinary restarts, the last FAILED start for
        retry throttling, the last INVALID start for lockouts.
        """
        policy = self.policy
        status_value = getattr(status, "value", status)
        if status_value == "invalid_credential" and pacer is not None:
            if pacer.invalid_streak >= policy.invalid_streak_threshold:
                return (policy.invalid_credential_retry_s,
                        pacer.last_invalid or pacer.last_failed_start)
            if pacer.last_failed_start:
                return (policy.start_retry_cooldown_s, pacer.last_failed_start)
        if status_value in ("failed", "missing_credential") \
                and pacer is not None and pacer.last_failed_start:
            return (policy.start_retry_cooldown_s, pacer.last_failed_start)
        return (policy.restart_cooldown_s,
                pacer.last_stop if pacer else 0.0)
