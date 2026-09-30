"""Centralized rate limiter: token buckets for every outbound action.

Two layers of buckets per action:
  * GLOBAL layer ("__global__", action): one bucket shared by ALL accounts —
    this is the "set for all bots" rule.
  * ACCOUNT layer (account, action): per-bot bucket — an account override when
    set manually, otherwise the same rule as the global default.

Precedence: account-specific rule > global default rule. A reservation must
take a token from BOTH layers (global + account) before the action may run,
so many accounts acting together still respect the bus-wide cap.

Time is injected via a Clock (SystemClock in production, ManualClock in tests)
which makes token math deterministically testable. Async acquire() adds
optional jitter so traffic does not look metronome-perfect.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import random
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Protocol, Tuple

GLOBAL_KEY = "__global__"

_RULE_RE = re.compile(r"^\s*(\d+)\s*/\s*([0-9]*\.?[0-9]+)\s*s?\s*$", re.IGNORECASE)


class RateRuleError(ValueError):
    """Raised when a rule string is malformed (expected 'actions/seconds', e.g. '8/12s')."""


class Clock(Protocol):
    """Minimal clock protocol so tests can drive time manually."""

    def now(self) -> float: ...


class SystemClock:
    """Production clock (monotonic)."""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """Test clock: advance() moves time forward without sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += float(seconds)
        return self._t


class Priority(IntEnum):
    """Queue priority: HIGH actions are granted before NORMAL before LOW."""

    HIGH = 0
    NORMAL = 1
    LOW = 2


@dataclass(frozen=True)
class RateRule:
    """'actions' tokens refilled over 'window' seconds; jitter fraction 0..4."""

    actions: int
    window: float
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if self.actions < 1:
            raise RateRuleError(f"actions must be >= 1, got {self.actions}")
        if self.window <= 0:
            raise RateRuleError(f"window must be > 0, got {self.window}")
        if not 0.0 <= self.jitter <= 4.0:
            raise RateRuleError(f"jitter fraction must be 0..4, got {self.jitter}")

    @property
    def capacity(self) -> float:
        return float(self.actions)

    @property
    def refill_per_second(self) -> float:
        return self.actions / float(self.window)

    @classmethod
    def parse(cls, text: Any) -> "RateRule":
        """Parse 'n/seconds' (e.g. '8/12s', '1/0.5'). Raises RateRuleError."""
        if isinstance(text, RateRule):
            return text
        match = _RULE_RE.match(str(text))
        if not match:
            raise RateRuleError(f"cannot parse rate rule {text!r} (expected 'actions/seconds')")
        return cls(int(match.group(1)), float(match.group(2)))

    def __str__(self) -> str:
        return f"{self.actions}/{self.window}s"


@dataclass
class Grant:
    """Receipt proving tokens were reserved for one action."""

    action: str
    account: Optional[str]
    at: float
    amount: float = 1.0
    #: human-like delay (seconds) the limiter applied AFTER granting, if any
    delay_applied: float = 0.0


@dataclass(frozen=True)
class DelaySpec:
    """Human-like random pause range applied after a grant (x.xx–y.yy seconds).

    maximum == 0 disables the delay entirely (legacy behavior).
    """

    minimum: float = 0.0
    maximum: float = 0.0

    def __post_init__(self) -> None:
        if self.minimum < 0 or self.maximum < 0:
            raise RateRuleError(f"delay bounds must be >= 0, got {self.minimum}..{self.maximum}")
        if self.maximum < self.minimum:
            raise RateRuleError(f"delay maximum {self.maximum} < minimum {self.minimum}")

    @property
    def enabled(self) -> bool:
        return self.maximum > 0

    def sample(self, rng: random.Random) -> float:
        """Uniform draw inside [minimum, maximum]; 0.0 when disabled."""
        if not self.enabled:
            return 0.0
        return rng.uniform(self.minimum, self.maximum)

    def __str__(self) -> str:
        if not self.enabled:
            return "off"
        return f"{self.minimum:.2f}s..{self.maximum:.2f}s"


@dataclass
class _Bucket:
    """Token bucket for one (owner, action) key."""

    rule: RateRule
    tokens: float
    last_refill: float

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            self.tokens = min(self.rule.capacity, self.tokens + elapsed * self.rule.refill_per_second)
            self.last_refill = now

    def wait_for(self, now: float, amount: float = 1.0) -> float:
        """Seconds until `amount` tokens are available (0 if now)."""
        self.refill(now)
        if self.tokens >= amount:
            return 0.0
        return (amount - self.tokens) / self.rule.refill_per_second

    def take(self, now: float, amount: float = 1.0) -> None:
        self.refill(now)
        self.tokens -= amount

    def snapshot(self) -> Dict[str, Any]:
        return {
            "rule": str(self.rule),
            "tokens": round(self.tokens, 3),
            "capacity": self.rule.capacity,
        }


@dataclass
class _Entry:
    """Pending async waiter, heap-ordered by (priority, sequence)."""

    priority: int
    seq: int
    action: str
    account: Optional[str]
    future: "asyncio.Future[Grant]"
    cancelled: bool = False

    def __lt__(self, other: "_Entry") -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


class RateLimiterService:
    """Central limiter shared by every account session."""

    def __init__(
        self,
        clock: Optional[Clock] = None,
        default_rules: Optional[Dict[str, Any]] = None,
        account_rules: Optional[Dict[Tuple[str, str], Any]] = None,
        jitter_seed: Optional[int] = None,
        delay_seed: Optional[int] = None,
    ) -> None:
        self._clock = clock if clock is not None else SystemClock()
        #: action -> RateRule (the bus-wide default == global bucket rule)
        self._rules: Dict[str, RateRule] = {
            action: RateRule.parse(rule) for action, rule in (default_rules or {}).items()
        }
        #: (account, action) -> RateRule (manual per-bot overrides)
        self._account_rules: Dict[Tuple[str, str], RateRule] = {
            key: RateRule.parse(rule) for key, rule in (account_rules or {}).items()
        }
        #: human-like post-grant pauses: global + per-account
        self._delays: Dict[str, DelaySpec] = {}
        self._account_delays: Dict[Tuple[str, str], DelaySpec] = {}
        self._buckets: Dict[Tuple[str, str], _Bucket] = {}
        self._pending: Dict[Tuple[str, str], List[_Entry]] = {}
        self._seq = itertools.count()
        self._jitter_rng = random.Random(jitter_seed)
        self._delay_rng = random.Random(delay_seed)
        self._wake: Optional[asyncio.Event] = None

    # ------------------------------------------------------------ inspection
    def rule_for(self, action: str, account: Optional[str] = None) -> RateRule:
        """Effective rule: account override if set, else global default."""
        if account is not None:
            override = self._account_rules.get((account, action))
            if override is not None:
                return override
        try:
            return self._rules[action]
        except KeyError as exc:
            raise RateRuleError(
                f"no rule configured for action {action!r} "
                f"(known: {sorted(self._rules)})"
            ) from exc

    def override_for(self, account: str, action: str) -> Optional[RateRule]:
        """Explicit per-account rule or None (inheriting the global default)."""
        return self._account_rules.get((account, action))

    def next_wait(self, action: str, account: Optional[str] = None, amount: float = 1.0) -> float:
        """Seconds until a reservation could succeed (ignores pending queue).

        Raises RateRuleError (not KeyError) when the action is unconfigured.
        """
        now = self._clock.now()
        wait = self._bucket_for((GLOBAL_KEY, action), self.rule_for(action)).wait_for(now, amount)
        if account is not None:
            wait = max(wait, self._bucket_for((account, action), self.rule_for(action, account)).wait_for(now, amount))
        return wait

    def stats(self) -> Dict[str, Any]:
        """JSON-safe snapshot: rules, per-bucket tokens, pending queue depths."""
        pending: Dict[str, int] = {}
        for (owner, action), heap in self._pending.items():
            live = [e for e in heap if not e.cancelled and not e.future.done()]
            if live:
                pending[f"{owner}:{action}"] = len(live)
        return {
            "now": round(self._clock.now(), 3),
            "actions": {
                action: {
                    "default_rule": str(rule),
                    "overrides": {
                        account: str(self._account_rules[(account, action)])
                        for account in sorted({a for (a, act) in self._account_rules if act == action})
                    },
                    "delay": {
                        "global": str(self._delays.get(action, DelaySpec())),
                        "accounts": {
                            owner: str(self._account_delays[(owner, action)])
                            for (owner, act) in sorted(self._account_delays)
                            if act == action and owner != GLOBAL_KEY
                        },
                    },
                    "global_bucket": self._bucket_for((GLOBAL_KEY, action), rule).snapshot(),
                    "account_buckets": {
                        owner: bucket.snapshot()
                        for (owner, act), bucket in self._buckets.items()
                        if act == action and owner != GLOBAL_KEY
                    },
                }
                for action, rule in sorted(self._rules.items())
            },
            "pending": pending,
        }

    # ------------------------------------------------------------- mutation
    def set_default_rule(self, action: str, rule: Any) -> RateRule:
        """Change the rule for ALL accounts (rebuilds the shared global bucket)."""
        parsed = RateRule.parse(rule)
        self._rules[action] = parsed
        self._buckets.pop((GLOBAL_KEY, action), None)
        self._wake_waiters()
        return parsed

    def set_account_rule(self, account: str, action: str, rule: Any) -> RateRule:
        """Set a manual per-bot rule (overrides the global default for that bot)."""
        parsed = RateRule.parse(rule)
        self._account_rules[(account, action)] = parsed
        self._buckets.pop((account, action), None)
        self._wake_waiters()
        return parsed

    def clear_account_rule(self, account: str, action: str) -> bool:
        """Remove a per-bot override; the account falls back to the global rule."""
        existed = self._account_rules.pop((account, action), None) is not None
        self._buckets.pop((account, action), None)
        self._wake_waiters()
        return existed

    # ------------------------------------------------------------- delays
    def set_delay(
        self, action: str, minimum: float, maximum: float, account: Optional[str] = None
    ) -> DelaySpec:
        """Configure the human-like pause range applied AFTER each grant.

        account=None sets the global default; a per-account delay overrides it.
        """
        spec = DelaySpec(float(minimum), float(maximum))
        if account is None:
            self._delays[action] = spec
        else:
            self._account_delays[(account, action)] = spec
        return spec

    def clear_delay(self, action: str, account: Optional[str] = None) -> bool:
        """Remove a delay range (per-account when account given, else global)."""
        if account is None:
            return self._delays.pop(action, None) is not None
        return self._account_delays.pop((account, action), None) is not None

    def delay_for(self, action: str, account: Optional[str] = None) -> DelaySpec:
        """Effective delay spec: account override when set, else global default."""
        if account is not None:
            override = self._account_delays.get((account, action))
            if override is not None:
                return override
        return self._delays.get(action, DelaySpec())

    # ------------------------------------------------------- sync fast-path
    def try_acquire(
        self, action: str, account: Optional[str] = None, amount: float = 1.0
    ) -> Optional[Grant]:
        """Non-blocking reservation (test/simulation friendly).

        Fails (None) if: pending async waiters exist for this group (fairness),
        or either bucket lacks tokens right now.
        """
        key = (account or GLOBAL_KEY, action)
        heap = self._pending.get(key)
        if heap and any(not e.cancelled and not e.future.done() for e in heap):
            return None
        if self.next_wait(action, account, amount) > 0:
            return None
        self._reserve(action, account, amount)
        return Grant(action=action, account=account, at=self._clock.now(), amount=amount)

    # ------------------------------------------------------------ async path
    async def acquire(
        self,
        action: str,
        account: Optional[str] = None,
        priority: Priority | int = Priority.NORMAL,
        amount: float = 1.0,
    ) -> Grant:
        """Blocking (awaitable) reservation honoring priority + jitter.

        Expects: action configured in default_rules; account optional.
        Returns: Grant once tokens exist in BOTH the global and account buckets.
        """
        loop = asyncio.get_running_loop()
        entry = _Entry(
            priority=int(priority),
            seq=next(self._seq),
            action=action,
            account=account,
            future=loop.create_future(),
        )
        key = (account or GLOBAL_KEY, action)
        heapq.heappush(self._pending.setdefault(key, []), entry)
        try:
            while not entry.future.done():
                self._grant_ready()
                if entry.future.done():
                    break
                wait = self._entry_wait(entry)
                wait = self.jittered_wait(action, account, wait)
                wake = self._wake_event()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=max(wait, 0.001))
                except asyncio.TimeoutError:
                    pass
                wake.clear()
            grant = entry.future.result()
            # human-like pacing: an extra random pause AFTER the grant
            delay = self.delay_for(action, account)
            if delay.enabled:
                grant.delay_applied = delay.sample(self._delay_rng)
                if grant.delay_applied > 0:
                    await asyncio.sleep(grant.delay_applied)
            return grant
        finally:
            entry.cancelled = True
            self._prune(key)

    def jittered_wait(self, action: str, account: Optional[str], wait: float) -> float:
        """Multiply a wait by (1 + U(0, jitter)) using the effective rule.

        Kept public for deterministic unit tests of the jitter bounds.
        Raises RateRuleError for unknown actions (consistent with rule_for).
        """
        if wait <= 0:
            self.rule_for(action, account)  # validation only
            return wait
        jitter = self.rule_for(action, account).jitter
        if jitter <= 0:
            return wait
        return wait * (1.0 + self._jitter_rng.uniform(0.0, jitter))

    # --------------------------------------------------------------- internals
    def _bucket_for(self, key: Tuple[str, str], rule: RateRule) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None or bucket.rule != rule:
            bucket = _Bucket(rule=rule, tokens=rule.capacity, last_refill=self._clock.now())
            self._buckets[key] = bucket
        return bucket

    def _reserve(self, action: str, account: Optional[str], amount: float) -> None:
        now = self._clock.now()
        self._bucket_for((GLOBAL_KEY, action), self.rule_for(action)).take(now, amount)
        if account is not None:
            self._bucket_for((account, action), self.rule_for(action, account)).take(now, amount)

    def _combined_wait(self, action: str, account: Optional[str], amount: float = 1.0) -> float:
        return self.next_wait(action, account, amount)

    def _entry_wait(self, entry: _Entry) -> float:
        """Wait until THIS entry should re-check: own turn soon, else short poll."""
        key = (entry.account or GLOBAL_KEY, entry.action)
        heap = self._pending.get(key, [])
        self._prune(key)
        if heap and heap[0] is entry:
            return self._combined_wait(entry.action, entry.account)
        return 0.02  # not the head yet: brief poll until ahead of us resolves

    def _grant_ready(self) -> None:
        """Grant every group-head entry whose buckets currently have tokens."""
        for key in list(self._pending):
            heap = self._pending.get(key, [])
            self._prune(key)
            while heap:
                head = heap[0]
                if self._combined_wait(head.action, head.account) > 0:
                    break
                self._reserve(head.action, head.account, 1.0)
                heapq.heappop(heap)
                if not head.cancelled and not head.future.done():
                    head.future.set_result(
                        Grant(head.action, head.account, self._clock.now(), 1.0)
                    )

    def _prune(self, key: Tuple[str, str]) -> None:
        heap = self._pending.get(key)
        if not heap:
            return
        heap[:] = [e for e in heap if not e.cancelled and not e.future.done()]
        heapq.heapify(heap)

    def _wake_event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    def _wake_waiters(self) -> None:
        if self._wake is not None:
            self._wake.set()
