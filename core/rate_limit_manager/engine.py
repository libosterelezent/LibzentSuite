"""Pacing engine: priority waiters, server cooldowns, jitter (A4).

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
3. JITTER — one uniform draw from the effective jitter window, slept AFTER
   the grant (human-like pause; does not move the pacing clock).

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
priority order re-evaluates immediately. A bounded poll (0.25s) backstops
non-head waiters against lost wakes — correctness never depends on the wake.

Other documented decisions (INTEGRATION.md: pick the safest option):
* effective() interval is max(all applicable policies, floor) — a stored
  policy can never pace faster than the floor even after set_floor() raised
  it past existing policy intervals.
* waiters of one pacing key do NOT block other keys (per-account pacing),
  exactly as the pinned contract states.
* persistence is best-effort: a failing repo.set/delete is logged but the
  in-memory (runtime-enforced) policy still changes; pacing safety never
  depends on the database.
* single-event-loop assumption, like the audited limiter (no locks).
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple

from ..errors import RatePolicyError, UnsafePolicyError
from ..event_bus import EventBus, RateLimitTriggered
from ..priority import Priority
from database.models import POLICY_LEVELS
from .policies import MIN_ACTION_INTERVAL, RatePolicy, parse_scope, policy_from_row
from .warnings import evaluate_policies

#: pacing key owner used when acquire() carries no account
GLOBAL_KEY = "__global__"

#: backstop poll for waiters that are not the head of their key's heap
POLL_INTERVAL = 0.25

#: single-task concurrency is guaranteed by the heap; grant order is
#: (priority, seq) — seq comes from this counter (FIFO within a priority).


# --------------------------------------------------------------------- clocks
class Clock(Protocol):
    """Minimal clock protocol so tests can drive time manually."""

    def now(self) -> float: ...


class SystemClock:
    """Production clock (monotonic — immune to wall-clock jumps)."""

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


# --------------------------------------------------------------------- values
@dataclass
class Grant:
    """Receipt for one acquired action: what waited, why, and the jitter.

    Attributes:
        action:    the action name paced.
        account:   account the action fires for (None -> shared global key).
        plugin:    plugin the action belongs to, when known.
        wait_s:    TOTAL pre-jitter wait (server cooldown + pacing + queue).
        jitter_s:  the human-like jitter actually slept after the grant.
        reason:    "server" when a server cooldown contributed to the wait,
                   else "pacing" ("" when there was no wait at all).
    """

    action: str
    account: Optional[str] = None
    plugin: Optional[str] = None
    wait_s: float = 0.0
    jitter_s: float = 0.0
    reason: str = ""


@dataclass
class _Entry:
    """Pending waiter, heap-ordered by (priority, sequence) — ported as-is."""

    priority: int
    seq: int
    action: str
    account: Optional[str]
    plugin: Optional[str]
    future: "asyncio.Future[None]"
    cancelled: bool = False

    def __lt__(self, other: "_Entry") -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


# -------------------------------------------------------------------- manager
class RateLimitManager:
    """4-level rate policies + pacing engine + server limits + warnings."""

    def __init__(
        self,
        bus: Optional[EventBus],
        logger: Any,
        policy_repo: Any = None,
        *,
        floor: float = MIN_ACTION_INTERVAL,
        clock: Optional[Clock] = None,
        jitter_seed: Optional[int] = None,
    ) -> None:
        self._bus = bus
        self._logger = logger
        self._repo = policy_repo
        self._floor = self._validated_floor(floor)
        self._clock = clock if clock is not None else SystemClock()
        self._jitter_rng = random.Random(jitter_seed)
        #: (level, owner, action) -> RatePolicy — the live policy table
        self._policies: Dict[Tuple[str, Optional[str], str], RatePolicy] = {}
        #: fleet-wide 429 cooldown (monotonic "until" timestamp)
        self._global_until: float = 0.0
        #: (account, action) -> monotonic "until" timestamp
        self._account_cooldowns: Dict[Tuple[str, str], float] = {}
        #: pacing key (owner, action) -> monotonic time of the last grant
        self._last_fire: Dict[Tuple[str, str], float] = {}
        #: pacing key -> heap of pending _Entry waiters
        self._pending: Dict[Tuple[str, str], List[_Entry]] = {}
        self._seq = itertools.count()
        self._wake: Optional[asyncio.Event] = None
        # counters for stats()
        self._grants = 0
        self._waits = 0
        self._total_wait = 0.0
        self._load_stored_policies()

    # ------------------------------------------------------------ validation
    @staticmethod
    def _validated_floor(seconds: Any) -> float:
        """Validate a floor candidate: finite and >= MIN_ACTION_INTERVAL."""
        try:
            value = float(seconds)
        except (TypeError, ValueError) as exc:
            raise RatePolicyError(
                f"safety floor must be a number, got {seconds!r}"
            ) from exc
        if not math.isfinite(value):
            raise RatePolicyError(f"safety floor must be finite, got {seconds!r}")
        if value < MIN_ACTION_INTERVAL:
            raise RatePolicyError(
                f"safety floor {value:g}s is below the hard minimum "
                f"{MIN_ACTION_INTERVAL:g}s per action"
            )
        return value

    def _load_stored_policies(self) -> None:
        """Load persisted policies from the repo at init (best-effort, both
        RatePolicyRecord-shaped rows and plain dicts accepted).

        A structurally bad row is logged and SKIPPED — a hand-edited database
        must never crash the process (ground rule). Rows below the current
        floor are kept (effective() clamps them via max(interval, floor)).
        """
        if self._repo is None:
            return
        try:
            rows = self._repo.list()
        except Exception as exc:  # pragma: no cover - defensive
            self._log("error", f"could not load rate policies: {exc}")
            return
        for row in rows or []:
            try:
                level, owner, policy = policy_from_row(row)
            except Exception as exc:
                self._log(
                    "warning",
                    f"skipping invalid stored rate policy {row!r}: {exc}",
                )
                continue
            self._policies[(level, owner, policy.action)] = policy
            if policy.interval < self._floor:
                scope = f"{level}/{owner}" if owner else level
                self._log(
                    "warning",
                    f"stored policy {policy.action!r} at {scope} is faster "
                    f"than the floor; effective pacing is clamped to "
                    f"{self._floor:g}s",
                )

    # ------------------------------------------------------------- policies
    def set_policy(
        self, policy: RatePolicy, level: str = "global", owner: Optional[str] = None
    ) -> RatePolicy:
        """Store a RatePolicy at (level, owner); persists via repo when present.

        Raises:
            RatePolicyError: invalid level/owner scope, or policy is not a
                RatePolicy.
            UnsafePolicyError: interval below the current safety floor.
        """
        if not isinstance(policy, RatePolicy):
            raise RatePolicyError(
                f"set_policy expects a RatePolicy, got {type(policy).__name__} "
                f"(use set_policy_values for raw numbers)"
            )
        return self._store(policy, level, owner)

    def set_policy_values(
        self,
        action: str,
        interval: float,
        jitter_min: float = 0.0,
        jitter_max: float = 0.0,
        level: str = "global",
        owner: Optional[str] = None,
    ) -> RatePolicy:
        """Convenience used by ControlAPI: build + store a policy in one call.

        Raises the same errors as set_policy (plus RatePolicyError from
        RatePolicy construction for bad interval/jitter values).
        """
        policy = RatePolicy(
            action=action,
            interval=interval,
            jitter_min=jitter_min,
            jitter_max=jitter_max,
        )
        return self._store(policy, level, owner)

    def _store(
        self, policy: RatePolicy, level: str, owner: Optional[str]
    ) -> RatePolicy:
        """Shared set path: validate scope, enforce the floor, persist, wake."""
        level, owner = parse_scope(level, owner)
        if policy.interval < self._floor:
            raise UnsafePolicyError(
                f"unsafe rate policy for {policy.action!r}: interval "
                f"{policy.interval:g}s is faster than the safety floor "
                f"{self._floor:g}s (hard minimum {MIN_ACTION_INTERVAL:g}s per "
                f"action). Slowest allowed near the floor: interval "
                f"{self._floor:g}s + jitter 0.50..2.00s."
            )
        self._policies[(level, owner, policy.action)] = policy
        scope = f"{level}/{owner}" if owner else level
        if self._repo is not None:
            try:
                self._repo.set(
                    level, owner, policy.action,
                    policy.interval, policy.jitter_min, policy.jitter_max,
                )
            except Exception as exc:  # best-effort persistence
                self._log(
                    "error",
                    f"could not persist rate policy {policy.action!r} at "
                    f"{scope}: {exc} (policy is still active in memory)",
                )
        self._log(
            "info", f"rate policy set: {policy} at {scope}"
        )
        self._wake_waiters()
        return policy

    def clear_policy(self, level: str, owner: Optional[str], action: str) -> bool:
        """Remove one stored policy (repo + memory). True when it existed.

        Raises RatePolicyError for an invalid level/owner scope.
        """
        level, owner = parse_scope(level, owner)
        if not isinstance(action, str) or not action.strip():
            raise RatePolicyError(f"policy action must not be empty, got {action!r}")
        action = action.strip()
        existed = self._policies.pop((level, owner, action), None) is not None
        if self._repo is not None:
            try:
                existed = self._repo.delete(level, owner, action) or existed
            except Exception as exc:  # best-effort persistence
                self._log("error", f"could not delete rate policy from db: {exc}")
        if existed:
            scope = f"{level}/{owner}" if owner else level
            self._log(
                "info", f"rate policy cleared: {action!r} at {scope}"
            )
            self._wake_waiters()
        return existed

    def policies(self) -> List[dict]:
        """Every stored policy as ``{level, owner, action, interval, jitter}``,
        sorted by (action, level, owner) — deterministic for CLI/UI."""
        level_rank = {name: i for i, name in enumerate(POLICY_LEVELS)}
        rows = [
            {
                "level": level,
                "owner": owner,
                "action": policy.action,
                "interval": policy.interval,
                "jitter": [policy.jitter_min, policy.jitter_max],
            }
            for (level, owner, _action), policy in self._policies.items()
        ]
        rows.sort(
            key=lambda r: (
                r["action"],
                level_rank.get(r["level"], 99),
                r["owner"] or "",
            )
        )
        return rows

    def effective(
        self, action: str, account: Optional[str] = None, plugin: Optional[str] = None
    ) -> dict:
        """Compose all applicable policy levels for one action.

        Returns ``{"action", "interval", "jitter": [min, max], "sources": [...]}``
        where interval is the MAX across applicable policies AND the safety
        floor (safest option: a stored policy can never beat the floor, e.g.
        after set_floor() raised it), jitter is (max of mins, max of maxes),
        and sources lists every applicable policy. With no policy at all the
        result is the floor with empty sources — still safe.
        """

        def _applies(level: str, owner: Optional[str]) -> bool:
            if level == "global":
                return True
            if level == "account":
                return account is not None and owner == account
            if level == "plugin":
                return plugin is not None and owner == plugin
            return (
                account is not None
                and plugin is not None
                and owner == f"{account}/{plugin}"
            )

        level_rank = {name: i for i, name in enumerate(POLICY_LEVELS)}
        applicable = [
            (level, owner, policy)
            for (level, owner, act), policy in self._policies.items()
            if act == action and _applies(level, owner)
        ]
        applicable.sort(
            key=lambda item: (level_rank.get(item[0], 99), item[1] or "")
        )
        interval = max(
            [self._floor] + [p.interval for _, _, p in applicable]
        )
        if applicable:
            jitter = [
                max(p.jitter_min for _, _, p in applicable),
                max(p.jitter_max for _, _, p in applicable),
            ]
        else:
            jitter = [0.0, 0.0]
        return {
            "action": action,
            "interval": interval,
            "jitter": jitter,
            "sources": [
                {"level": level, "owner": owner, "interval": policy.interval}
                for level, owner, policy in applicable
            ],
        }

    def set_floor(self, seconds: float) -> float:
        """Raise the safety floor (never lower, never below 1.5s).

        Raises:
            RatePolicyError: when seconds would LOWER the floor, is below the
                hard minimum, or is not a finite number.
        """
        candidate = self._validated_floor(seconds)
        if candidate < self._floor:
            raise RatePolicyError(
                f"the safety floor may only be raised (current {self._floor:g}s, "
                f"requested {candidate:g}s)"
            )
        self._floor = candidate
        self._wake_waiters()  # waiters recompute against the new floor
        self._log("info", f"rate-limit safety floor raised to {candidate:g}s")
        return self._floor

    # ------------------------------------------------------------- acquiring
    async def acquire(
        self,
        action: str,
        account: Optional[str] = None,
        plugin: Optional[str] = None,
        priority: Priority = Priority.NORMAL,
    ) -> Grant:
        """Wait for permission to fire one action; returns a Grant.

        Stages: server cooldowns (account+action and fleet-wide) -> pacing on
        key (account or "__global__", action) -> post-grant jitter sleep.
        Concurrent waiters on the SAME pacing key are granted in priority
        order, then FIFO. last_fire is stamped at GRANT time; Grant.wait_s is
        the total pre-jitter wait and Grant.reason is "server" when a server
        cooldown contributed, else "pacing".

        Raises:
            RatePolicyError: for a malformed action/account/plugin/priority.
        """
        action, account, plugin = self._validate_acquire_args(action, account, plugin)
        try:
            priority = Priority.coerce(priority)
        except ValueError as exc:
            raise RatePolicyError(str(exc)) from exc

        loop = asyncio.get_running_loop()
        key = (account or GLOBAL_KEY, action)
        entry = _Entry(
            priority=int(priority),
            seq=next(self._seq),
            action=action,
            account=account,
            plugin=plugin,
            future=loop.create_future(),
        )
        heapq.heappush(self._pending.setdefault(key, []), entry)
        started_at = self._clock.now()
        reason = "pacing"
        try:
            while not entry.future.done():
                self._grant_ready()
                if entry.future.done():
                    break
                wait, server_blocks = self._entry_wait(entry)
                if server_blocks:
                    reason = "server"
                await self._wait_either(wait)
            granted_at = self._clock.now()
            wait_s = max(0.0, granted_at - started_at)
            if wait_s <= 0:
                reason = ""
        finally:
            entry.cancelled = True
            self._prune(key)

        # counters + event (before jitter: the action is already authorized)
        self._grants += 1
        if wait_s > 0:
            self._waits += 1
            self._total_wait += wait_s
            await self._publish(
                RateLimitTriggered(
                    account=account or "", action=action, wait_s=wait_s, reason=reason
                )
            )
        # stage 3: human-like jitter pause AFTER the grant (never moves pacing)
        jitter_s = 0.0
        jitter_min, jitter_max = self.effective(action, account, plugin)["jitter"]
        if jitter_max > 0:
            jitter_s = self._jitter_rng.uniform(jitter_min, jitter_max)
            if jitter_s > 0:
                await self._sleep(jitter_s)
        return Grant(
            action=action,
            account=account,
            plugin=plugin,
            wait_s=wait_s,
            jitter_s=jitter_s,
            reason=reason if wait_s > 0 else "",
        )

    async def _sleep(self, seconds: float) -> None:
        """Sleep seam — every timed wait in the engine goes through here.

        Tests subclass/patch this method (typically advancing the injected
        ManualClock by ``seconds``) so pacing is deterministic.
        """
        if seconds > 0:
            await asyncio.sleep(seconds)

    async def _wait_either(self, seconds: float) -> None:
        """Sleep ``seconds`` through the seam, but wake early on policy/cooldown
        changes (shared wake event) so waiters recompute instead of oversleeping.

        The event is cleared BEFORE re-registering: asyncio.Event.set()
        resolves every waiter already parked on it synchronously, and any
        coroutine not yet parked ran its own state re-check (grant_ready) in
        the same sync section — so a clear can never drop a real wake.
        """
        wake = self._wake_event()
        wake.clear()
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waker = asyncio.ensure_future(wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waker):
                task.cancel()
            await asyncio.gather(sleeper, waker, return_exceptions=True)

    def _entry_wait(self, entry: _Entry) -> Tuple[float, bool]:
        """How long THIS entry should wait before re-checking, and whether a
        server cooldown is currently part of what blocks it.

        The heap head waits its exact remaining block (server + pacing);
        other entries poll briefly — they can only proceed after the entries
        ahead of them are granted.
        """
        key = (entry.account or GLOBAL_KEY, entry.action)
        heap = self._pending.get(key, [])
        self._prune(key)
        if heap and heap[0] is entry:
            wait, server_wait = self._entry_block(entry)
            return wait, server_wait > 0
        return POLL_INTERVAL, False

    def _entry_block(self, entry: _Entry) -> Tuple[float, float]:
        """(total block, server block) in seconds until `entry` may be granted.

        Total = max(fleet-wide cooldown, account+action cooldown, pacing
        remaining). Zero when grantable right now.
        """
        now = self._clock.now()
        server_waits = [0.0]
        if now < self._global_until:
            server_waits.append(self._global_until - now)
        if entry.account is not None:
            until = self._account_cooldowns.get((entry.account, entry.action))
            if until is not None and now < until:
                server_waits.append(until - now)
        server_wait = max(server_waits)
        interval = self.effective(entry.action, entry.account, entry.plugin)["interval"]
        last = self._last_fire.get((entry.account or GLOBAL_KEY, entry.action))
        pacing_wait = (
            max(0.0, last + interval - now) if last is not None else 0.0
        )
        return max(server_wait, pacing_wait), server_wait

    def _grant_ready(self) -> None:
        """Grant every pacing-key head whose block has elapsed.

        Priority order is preserved structurally: only the heap HEAD can be
        granted, so a HIGH waiter submitted after a LOW one still goes first.
        last_fire is stamped at grant time, and successors are woken.
        """
        granted_any = False
        for key in list(self._pending):
            heap = self._pending.get(key)
            if not heap:
                continue
            self._prune(key)
            while heap:
                head = heap[0]
                if head.cancelled or head.future.done():
                    heapq.heappop(heap)
                    continue
                if self._entry_block(head)[0] > 0:
                    break  # head not ready: everyone behind it waits too
                self._last_fire[key] = self._clock.now()
                heapq.heappop(heap)
                head.future.set_result(None)
                granted_any = True
        if granted_any:
            self._wake_waiters()

    def _prune(self, key: Tuple[str, str]) -> None:
        """Drop dead waiters from one key's heap (ported from the old limiter)."""
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
        """Wake every parked waiter so it recomputes its remaining wait."""
        if self._wake is not None:
            self._wake.set()

    @staticmethod
    def _validate_acquire_args(
        action: Any, account: Any, plugin: Any
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Boundary validation for acquire() — malformed input never crashes
        the process, it raises a precise RatePolicyError instead."""

        def _name(value: Any, what: str, allow_none: bool) -> Optional[str]:
            if value is None:
                if allow_none:
                    return None
                raise RatePolicyError(f"{what} must not be None")
            if not isinstance(value, str) or not value.strip():
                raise RatePolicyError(f"{what} must be a non-empty string, got {value!r}")
            return value.strip()

        return (
            _name(action, "action", allow_none=False),
            _name(account, "account", allow_none=True),
            _name(plugin, "plugin", allow_none=True),
        )

    # -------------------------------------------------------- server limits
    def report_server_limit(
        self,
        account: str,
        action: str,
        retry_after: float,
        global_: bool = False,
    ) -> None:
        """Register a Discord 429: cooldown the (account, action) pair, and
        with global_=True ALSO the whole fleet (max with existing cooldowns).

        The framework ALWAYS respects server-provided limits; waiters parked
        on pacing are woken so they extend their wait for the new cooldown.
        """
        try:
            wait = float(retry_after)
        except (TypeError, ValueError) as exc:
            raise RatePolicyError(
                f"retry_after must be a number, got {retry_after!r}"
            ) from exc
        if not math.isfinite(wait):
            raise RatePolicyError(f"retry_after must be finite, got {retry_after!r}")
        wait = max(0.0, wait)
        account, action = self._validate_acquire_args(account, action, None)[:2]
        now = self._clock.now()
        self._account_cooldowns[(account, action)] = max(
            self._account_cooldowns.get((account, action), 0.0), now + wait
        )
        scope = ""
        if global_:
            self._global_until = max(self._global_until, now + wait)
            scope = " (fleet-wide)"
        self._log(
            "warning",
            f"server rate limit on {action!r}: cooling account {account!r} "
            f"{wait:.2f}s{scope}",
            account=account,
        )
        self._wake_waiters()

    def cooldown_remaining(
        self, account: Optional[str] = None, action: Optional[str] = None
    ) -> float:
        """Seconds left on the most relevant server cooldown (0 when none).

        Filters: account=None means every account; action=None means every
        action; the fleet-wide global cooldown is ALWAYS considered.
        """
        now = self._clock.now()
        remaining = [max(0.0, self._global_until - now)]
        for (acct, act), until in self._account_cooldowns.items():
            if account is not None and acct != account:
                continue
            if action is not None and act != action:
                continue
            remaining.append(max(0.0, until - now))
        return round(max(remaining), 3)

    # -------------------------------------------------------------- reporting
    def stats(self) -> dict:
        """JSON-safe snapshot for the UI: floor, policies, active cooldowns,
        last-fire ages per pacing key, and grant/wait counters."""
        now = self._clock.now()
        accounts: Dict[str, Dict[str, float]] = {}
        for (acct, act) in sorted(self._account_cooldowns):
            until = self._account_cooldowns[(acct, act)]
            left = round(max(0.0, until - now), 3)
            if left > 0:
                accounts.setdefault(acct, {})[act] = left
        last_fire_ages = {
            f"{owner}:{action}": round(max(0.0, now - ts), 3)
            for (owner, action), ts in sorted(self._last_fire.items())
        }
        avg = round(self._total_wait / self._waits, 3) if self._waits else 0.0
        return {
            "floor": self._floor,
            "policies": self.policies(),
            "cooldowns": {
                "global": round(max(0.0, self._global_until - now), 3),
                "accounts": accounts,
            },
            "last_fire_ages": last_fire_ages,
            "grants": self._grants,
            "waits": self._waits,
            "avg_wait_s": avg,
        }

    def warnings(self) -> List[dict]:
        """Advisory safety review of every stored policy + the floor entry.

        Entries are ``{"level", "code", "message"}`` sorted danger > warn >
        info; see core/rate_limit_manager/warnings.py for the taxonomy.
        """
        stored = (
            (policy, level, owner)
            for (level, owner, _action), policy in self._policies.items()
        )
        return evaluate_policies(stored, self._floor)

    # --------------------------------------------------------------- helpers
    async def _publish(self, event: Any) -> None:
        """Publish through the bus (awaited inline: sync subscribers see the
        event before acquire() returns — deterministic for tests)."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(event)
        except Exception:  # pragma: no cover - publish isolates handlers
            self._log("error", f"publishing {event.name} failed")

    def _log(self, severity: str, message: str, **ctx: Any) -> None:
        """Log through the injected logger (LogService-shaped); logging must
        never break pacing, so failures are swallowed."""
        if self._logger is None:
            return
        try:
            method = getattr(self._logger, severity)
            method("ratelimit", message, **ctx)
        except Exception:
            pass


__all__ = [
    "RateLimitManager",
    "Grant",
    "Clock",
    "SystemClock",
    "ManualClock",
    "GLOBAL_KEY",
]
