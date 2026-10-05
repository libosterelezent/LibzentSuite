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
from .policies import (
    MAX_INTERVAL,
    MIN_ACTION_INTERVAL,
    RatePolicy,
    parse_scope,
    policy_from_row,
)
from .warnings import evaluate_policies

#: pacing key owner used when acquire() carries no account
GLOBAL_KEY = "__global__"

#: FINAL-PHASE (F2): level-rank built ONCE (was a per-call comprehension
#: in both effective() and policies())
_LEVEL_RANK: Dict[str, int] = {name: i for i, name in enumerate(POLICY_LEVELS)}

#: FINAL-PHASE (F2): hard bound on the effective-config memo. The natural
#: key space is actions × (accounts+1) × (plugins+1) — a few hundred even
#: at fleet scale; 4096 leaves huge headroom while keeping memory O(1)
#: against caller-supplied action/account/plugin strings.
_EFFECTIVE_CACHE_CAP = 4096

#: kv keys shared with the ControlAPI persistence layer: the engine READS
#: them at boot (load_persisted), the ControlAPI WRITES them on edits.
#: Defined here — the reading layer — and exported so the writer layer
#: imports them (a literal on each side could silently drift apart).
KV_LANE_KEY = "ratelimit:lane"
KV_FLOOR_KEY = "ratelimit:floor"
KV_REMOVED_KEY = "ratelimit:removed"

#: backstop poll for waiters that are not the head of their key's heap
#: non-head backstop poll (lost-wake insurance ONLY — heads compute their
#: exact remaining block and every grant/policy/cooldown change wakes all
#: parked waiters, so a short poll just burned a full policy recomputation
#: per waiter 4x/second)
POLL_INTERVAL = 2.0

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
                   "lane" when the sequential send lane contributed, else
                   "pacing" ("" when there was no wait at all).
    """

    action: str
    account: Optional[str] = None
    plugin: Optional[str] = None
    wait_s: float = 0.0
    jitter_s: float = 0.0
    reason: str = ""


@dataclass
class _Entry:
    """Pending waiter, heap-ordered by (priority, sequence) — ported as-is.

    ``jitter`` is this waiter's ALREADY-DRAWN post-grant pause: drawn once
    per acquire() before admission so _grant_ready can stamp every pacing
    clock at grant + jitter (the expected wire instant).
    """

    priority: int
    seq: int
    action: str
    account: Optional[str]
    plugin: Optional[str]
    future: "asyncio.Future[None]"
    jitter: float = 0.0
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
        lane_enabled: bool = False,
        lane_prefixrate: float = 1.5,
        kv_repo: Any = None,
    ) -> None:
        """``policy_repo``/``kv_repo`` are kept for signature back-compat.

        The engine performs NO repository I/O at construction anymore (and
        no writes at all): await :meth:`load_persisted` once at boot to pull
        stored policies + the lane config into memory. Persistence of policy
        edits is owned by the ControlAPI layer.
        """
        self._bus = bus
        self._logger = logger
        self._repo = policy_repo
        self._kv = kv_repo
        self._floor = self._validated_floor(floor)
        self._removed_defaults: set = set()  # 'level|owner|action' cleared by the user
        # FINAL-PHASE (F2): effective-config memoization state
        self._config_version: int = 0
        self._effective_cache: Dict[
            Tuple[str, Optional[str], Optional[str]],
            Tuple[int, Tuple[float, float, float,
                             Tuple[Tuple[str, Optional[str], float], ...]]],
        ] = {}
        self._clock = clock if clock is not None else SystemClock()
        self._jitter_rng = random.Random(jitter_seed)
        #: (level, owner, action) -> RatePolicy — the live policy table
        self._policies: Dict[Tuple[str, Optional[str], str], RatePolicy] = {}
        #: fleet-wide 429 cooldown (monotonic "until" timestamp)
        self._global_until: float = 0.0
        #: (account, action) -> monotonic "until" timestamp
        self._account_cooldowns: Dict[Tuple[str, str], float] = {}
        #: pacing key (owner, action) -> monotonic time of the last expected
        #: WIRE send (grant + jitter). TWO SCOPES share this table (spec:
        #: per-account/group + per-task):
        #:   ("alpha", "message")        per-TASK scope — consecutive API
        #:                               calls (tasks) of ONE account
        #:   ("group:grinders", "message") per-ACCOUNT/GROUP scope — the
        #:                               shared clock serializing every member
        #:                               account of the group
        self._last_fire: Dict[Tuple[str, str], float] = {}
        #: account -> group name (framework-fed metadata, no API involved)
        self._groups: Dict[str, str] = {}
        #: pacing key -> heap of pending _Entry waiters
        self._pending: Dict[Tuple[str, str], List[_Entry]] = {}
        self._seq = itertools.count()
        self._wake: Optional[asyncio.Event] = None
        # counters for stats()
        self._grants = 0
        self._waits = 0
        self._total_wait = 0.0
        # server-wide sequential send lane (FarmLite concept, robust port):
        # every acquire() from every account passes one gate, spaced by
        # prefixrate; waiter-driven so it cannot die; see lane.py
        # FLEET SEND LANE REMOVED: redundant with sync groups;
        # throttled single-account throughput. Per-account pacing
        # + sync groups + the 1.5s floor are the pacing layers.
        self._lane_defaults = (False, 1.5)

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
        if value > MAX_INTERVAL:
            raise RatePolicyError(
                f"safety floor {value:g}s exceeds the {MAX_INTERVAL:g}s cap — "
                f"it would wedge every action forever"
            )
        return value

    async def load_persisted(self) -> None:
        """Boot path: read the stored policies + lane config into memory.

        Awaited once by the bootstrap (and the simulation runner) after
        ``database.connect()`` — the constructor does no repository I/O.
        Accepts both RatePolicyRecord-shaped rows and plain dicts; a
        structurally bad row is logged and SKIPPED — a hand-edited database
        must never crash the process (ground rule). Rows below the current
        floor are kept (effective() clamps them via max(interval, floor)).

        The lane config comes from the kv row ``KV_LANE_KEY``
        (``ratelimit:lane``) when present (precedence: persisted value >
        constructor defaults).
        """
        if self._repo is not None:
            try:
                rows = await self._repo.list()
            except Exception as exc:  # pragma: no cover - defensive
                self._log("error", f"could not load rate policies: {exc}")
                rows = None
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
                self._invalidate_effective_cache()  # F2: boot load mutates
                if policy.interval < self._floor:
                    scope = f"{level}/{owner}" if owner else level
                    self._log(
                        "warning",
                        f"stored policy {policy.action!r} at {scope} is faster "
                        f"than the floor; effective pacing is clamped to "
                        f"{self._floor:g}s",
                    )

        # persisted floor: a restart must not revert a raised floor
        # (was inside _load_lane_config before the lane removal)
        if self._kv is not None:
            try:
                stored_floor = await self._kv.get(KV_FLOOR_KEY)
                if stored_floor is not None:
                    self._floor = max(
                        self._floor,
                        self._validated_floor(float(stored_floor)))
                    self._invalidate_effective_cache()
            except Exception as exc:
                self._log("warning",
                          f"could not load persisted floor: {exc}")
            try:  # cleared-policy tombstones: seeding must skip these keys
                stored_removed = await self._kv.get(KV_REMOVED_KEY)
                if isinstance(stored_removed, list):
                    self._removed_defaults = set(stored_removed)
            except Exception as exc:
                self._log("warning",
                          f"could not load cleared-policy list: {exc}")

    # ------------------------------------------------------------- policies
    def set_policy(
        self, policy: RatePolicy, level: str = "global", owner: Optional[str] = None
    ) -> RatePolicy:
        """Store a RatePolicy at (level, owner) — memory only (the ControlAPI
        layer persists the edit).

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
        """Shared set path: validate scope, enforce the floor, wake waiters.

        MEMORY ONLY — persisting the edit is the ControlAPI layer's job
        (the engine no longer writes to the policy repository).
        """
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
        self._invalidate_effective_cache()  # F2: policy mutation
        scope = f"{level}/{owner}" if owner else level
        self._log(
            "info", f"rate policy set: {policy.action}={policy} at {scope}"
        )
        self._wake_waiters()
        return policy

    def clear_policy(self, level: str, owner: Optional[str], action: str) -> bool:
        """Remove one stored policy from MEMORY. True when it existed.

        Persisting the removal is the ControlAPI layer's job (the engine no
        longer writes to the policy repository).

        Raises RatePolicyError for an invalid level/owner scope.
        """
        level, owner = parse_scope(level, owner)
        if not isinstance(action, str) or not action.strip():
            raise RatePolicyError(f"policy action must not be empty, got {action!r}")
        action = action.strip()
        existed = self._policies.pop((level, owner, action), None) is not None
        if existed:
            self._invalidate_effective_cache()  # F2: policy removed
            scope = f"{level}/{owner}" if owner else level
            self._log(
                "info", f"rate policy cleared: {action!r} at {scope}"
            )
            self._wake_waiters()
        return existed

    def policies(self) -> List[dict]:
        """Every stored policy as ``{level, owner, action, interval, jitter}``,
        sorted by (action, level, owner) — deterministic for CLI/UI."""
        level_rank = _LEVEL_RANK
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

        FINAL-PHASE (F2): results are memoized per (action, account,
        plugin) under a config generation counter — the composition is a
        pure sync function of ``self._policies`` + ``self._floor``
        (verified: nothing else is read), it ran ≥2x per uncontended
        acquire and ≈N+1 per grant with N parked co-waiters, and every
        mutation bumps the generation so a stale hit is structurally
        impossible. The public dict shape (fresh objects per call) is
        unchanged — callers may mutate their dict freely.
        """
        interval, jmin, jmax, sources = self._effective_payload(
            action, account, plugin)
        return {
            "action": action,
            "interval": interval,
            "jitter": [jmin, jmax],
            "sources": [
                {"level": level, "owner": owner, "interval": pol_interval}
                for level, owner, pol_interval in sources
            ],
        }

    def _effective_payload(
        self, action: str, account: Optional[str], plugin: Optional[str]
    ) -> Tuple[float, float, float, Tuple[Tuple[str, Optional[str], float], ...]]:
        """Cached composition as an immutable tuple; never raises.

        Failure-harmless by construction: any cache error bypasses to the
        direct computation, and any fill error is swallowed — the cache
        can never become a correctness dependency.
        """
        key = (action, account, plugin)
        try:
            hit = self._effective_cache.get(key)
            if hit is not None and hit[0] == self._config_version:
                return hit[1]
        except Exception:
            return self._compute_effective(action, account, plugin)
        payload = self._compute_effective(action, account, plugin)
        try:
            if len(self._effective_cache) >= _EFFECTIVE_CACHE_CAP:
                # bounded: full flush (version makes post-flush reads
                # correct); natural key space is actions × accounts ×
                # plugins — far below the cap for any sane fleet
                self._effective_cache.clear()
            self._effective_cache[key] = (self._config_version, payload)
        except Exception:
            pass
        return payload

    def _compute_effective(
        self, action: str, account: Optional[str], plugin: Optional[str]
    ) -> Tuple[float, float, float, Tuple[Tuple[str, Optional[str], float], ...]]:
        """The (uncached) composition — the pre-F2 effective() body."""

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

        applicable = [
            (level, owner, policy)
            for (level, owner, act), policy in self._policies.items()
            if act == action and _applies(level, owner)
        ]
        applicable.sort(
            key=lambda item: (_LEVEL_RANK.get(item[0], 99), item[1] or "")
        )
        interval = max(
            [self._floor] + [p.interval for _, _, p in applicable]
        )
        if applicable:
            jmin = max(p.jitter_min for _, _, p in applicable)
            jmax = max(p.jitter_max for _, _, p in applicable)
        else:
            jmin, jmax = 0.0, 0.0
        return (
            interval, jmin, jmax,
            tuple((level, owner, policy.interval)
                  for level, owner, policy in applicable),
        )

    def _invalidate_effective_cache(self) -> None:
        """Bump the config generation AND drop the entries.

        Belt and braces: the version check alone is correct (any entry
        from an older generation misses); the clear alone is correct; a
        future code path that mutates ``_policies`` without this helper
        would need BOTH missed to serve stale config — the safety
        direction (a stricter policy under-applied) stays impossible.
        """
        self._config_version += 1
        self._effective_cache.clear()

    @property
    def floor_value(self) -> float:
        """Current safety floor (seconds)."""
        return float(self._floor)

    def set_removed_defaults(self, entries: list) -> None:
        """Sync the cleared-policy tombstones (ControlAPI persist path)."""
        self._removed_defaults = {str(e) for e in entries or []}

    def removed_default(self, level: str, owner, action: str) -> bool:
        """True when the user explicitly CLEARED this key (seed must skip)."""
        return f"{level}|{owner or ''}|{action}" in self._removed_defaults

    def set_floor(self, seconds: float) -> float:
        """Raise the safety floor (never lower, never below 1.5s).

        Raises:
            RatePolicyError: when seconds would LOWER the floor, is below the
                hard minimum, is not a finite number, or exceeds the
                one-day cap (it would wedge every action forever).
        """
        candidate = self._validated_floor(seconds)
        if candidate < self._floor:
            raise RatePolicyError(
                f"the safety floor may only be raised (current {self._floor:g}s, "
                f"requested {candidate:g}s)"
            )
        self._floor = candidate
        self._invalidate_effective_cache()  # F2: floor feeds the composition
        self._wake_waiters()  # waiters recompute against the new floor
        self._log("info", f"rate-limit safety floor raised to {candidate:g}s")
        return self._floor

    async def prune_account(self, account: str) -> None:
        """Drop a removed account's engine residue + policy rows (cascade).

        K15 (S6-C3/S8-C5/S10-C7): ``_last_fire`` pacing stamps,
        ``_account_cooldowns`` 429 entries and account-scoped policy rows
        would otherwise survive account removal and be silently inherited
        by a re-created same-named account (a stale pacing delay, a
        leftover 429 cooldown, predecessor pacing overrides). The group
        membership is dropped here too: when this was the group's LAST
        member, the shared ``group:<x>`` pacing stamps go with it (they
        would otherwise outlive the group and delay the next member of a
        re-created same-named group).
        """
        group = self._groups.pop(account, None)
        for key in [k for k in self._last_fire if k[0] == account]:
            del self._last_fire[key]
        for key in [k for k in self._account_cooldowns if k[0] == account]:
            del self._account_cooldowns[key]
        for key in [k for k in self._pending if k[0] == account]:
            del self._pending[key]  # PHASE-4 (S3-G1): waiter-heap husks
        prefix = f"{account}/"
        for key in [k for k in self._policies
                    if k[0] in ("account", "account_plugin")
                    and (k[1] == account
                         or (k[0] == "account_plugin"
                             and str(k[1]).startswith(prefix)))]:
            del self._policies[key]
        self._drop_group_stamps(group)
        # FINAL-PHASE (F2): always invalidate — the cache may hold entries
        # for this account even when no policy rows were (the table can be
        # empty while cached compositions from earlier still exist).
        self._invalidate_effective_cache()
        if self._repo is not None:
            try:
                await self._repo.delete_account(account)
            except Exception as exc:  # guarded: removal must never fail on it
                self._log("warning",
                          f"policy row cascade failed for {account!r}: {exc}")

    # ------------------------------------------------------------ group scope
    def set_account_group(self, account: str, group: Optional[str]) -> None:
        """Register which GROUP an account operates in (per-account/group
        rate scope, spec: accounts in the same group serialize their API
        activity against one shared clock).

        Framework-fed metadata only — no Discord API is involved. ``None``
        removes the account from any group (back to per-task pacing alone);
        when the LAST member leaves (here or via prune_account), the
        group's shared pacing stamps are dropped so a re-created
        same-named group starts fresh.
        """
        account = (account or "").strip()
        if not account:
            raise RatePolicyError("group registration needs an account name")
        previous = self._groups.get(account)
        if group is None or not str(group).strip():
            self._groups.pop(account, None)
        else:
            self._groups[account] = str(group).strip()
        if previous is not None and previous != self._groups.get(account):
            self._drop_group_stamps(previous)
        self._wake_waiters()

    def _drop_group_stamps(self, group: Optional[str]) -> None:
        """Drop a group's shared pacing stamps when its LAST member left.

        ``('group:<x>', action)`` stamps would otherwise outlive the group
        itself and be silently inherited by the next member of a re-created
        same-named group (a stale pacing delay). No-op while any member
        remains, and for stamps of groups that never existed.
        """
        if not group:
            return
        if any(membership == group for membership in self._groups.values()):
            return  # still populated: the shared clock stays meaningful
        prefix = f"group:{group}"
        for key in [k for k in self._last_fire if k[0] == prefix]:
            del self._last_fire[key]

    def account_groups(self) -> Dict[str, str]:
        """account -> group mapping (stats/UI)."""
        return dict(self._groups)

    def _pacing_keys(self, account: Optional[str], action: str) -> List[Tuple[str, str]]:
        """Every pacing clock this acquisition must respect.

        Per-TASK scope is always present: ``(account, action)`` — the
        account's own consecutive API calls. A grouped account ALSO paces on
        ``(group:<name>, action)``: any member's grant stamps the shared
        group clock, so the NEXT member's call waits the full interval —
        exactly the A -> [limit] -> B -> [limit] -> A serialization.
        """
        keys = [(account or GLOBAL_KEY, action)]
        if account is not None:
            group = self._groups.get(account)
            if group:
                keys.append((f"group:{group}", action))
        return keys

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
        order, then FIFO. The jitter is drawn BEFORE admission (once, after
        effective()) and slept after the grant; pacing clocks are stamped
        at grant + jitter — the expected wire instant — so consecutive
        WIRE sends stay >= the effective interval apart. Grant.wait_s is
        the total pre-jitter wait and Grant.reason is "lane" when the
        sequential lane admission contributed, "server" when a server
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
        # draw the jitter BEFORE admission so every spacing clock can be
        # stamped at grant + jitter (the expected WIRE instant): with a
        # post-grant-only draw, wire_i = grant_i + jitter_i while grant
        # spacing ignored the jitter, letting a small second draw compress
        # the wire gap to interval - jitter_max or even invert it (probe
        # P1/P2/P7). The draw rides the entry (and the lane admission) so
        # _grant_ready stamps the clocks correctly.
        jitter_min, jitter_max = self._effective_payload(
            action, account, plugin)[1:3]
        jitter_s = 0.0
        if jitter_max > 0:
            jitter_s = max(0.0, self._jitter_rng.uniform(jitter_min, jitter_max))
        entry = _Entry(
            priority=int(priority),
            seq=next(self._seq),
            action=action,
            account=account,
            plugin=plugin,
            future=loop.create_future(),
            jitter=jitter_s,
        )
        started_at = self._clock.now()
        reason = "pacing"
        # stage 0: server-wide sequential send lane (no-op when disabled) —
        # one grant at a time across EVERY account, spaced by prefixrate
        # measured between expected wire instants (admission + jitter);
        # cooldown/pacing below only ever ADD delay after admission
        lane_wait_s = 0.0
        heapq.heappush(self._pending.setdefault(key, []), entry)
        try:
            while not entry.future.done():
                # service ONLY this waiter's key: cross-key effects (floor,
                # server cooldowns, policy changes) arrive via the shared
                # wake event, and every grant on a key wakes that key's
                # successors — sweeping every key per waiter was O(waiters
                # x keys x policies) per poll tick for zero semantic gain
                self._grant_ready(key)
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
            # a cancelled head must not leave its successors parked until
            # the (long) backstop poll — wake them to recompute now
            self._wake_waiters()

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
        # stage 3: human-like jitter pause AFTER the grant — the draw was
        # made before admission; the pacing/lane stamps already account for
        # it, so the pause cannot compress the next send's wire gap
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
        interval = self._effective_payload(
            entry.action, entry.account, entry.plugin)[0]
        # BOTH scopes compose (max): per-task (the account's own clock) and,
        # when grouped, the shared group clock stamped by every member
        pacing_wait = 0.0
        for key in self._pacing_keys(entry.account, entry.action):
            last = self._last_fire.get(key)
            if last is not None:
                pacing_wait = max(pacing_wait, last + interval - now)
        pacing_wait = max(0.0, pacing_wait)
        return max(server_wait, pacing_wait), server_wait

    def _grant_ready(self, only: Optional[Tuple[str, str]] = None) -> None:
        """Grant every pacing-key head whose block has elapsed.

        ``only`` restricts the sweep to a single pacing key (the hot acquire
        path); None sweeps all keys. Priority order is preserved
        structurally: only the heap HEAD can be granted, so a HIGH waiter
        submitted after a LOW one still goes first. last_fire is stamped at
        grant + jitter (the expected wire instant), and successors are
        woken.
        """
        granted_any = False
        for key in [only] if only is not None else list(self._pending):
            heap = self._pending.get(key)
            if not heap:
                if only is not None:  # drop the empty-key husk (memory)
                    self._pending.pop(key, None)
                continue
            self._prune(key)
            while heap:
                head = heap[0]
                if head.cancelled or head.future.done():
                    heapq.heappop(heap)
                    continue
                if self._entry_block(head)[0] > 0:
                    break  # head not ready: everyone behind it waits too
                # stamp EVERY pacing clock this grant satisfies — the account
                # key (per-task scope) and, for grouped accounts, the shared
                # group key (per-account/group scope) — at grant + jitter,
                # the EXPECTED wire instant, so the next member's WIRE send
                # stays a full interval after this one's
                for stamp_key in self._pacing_keys(head.account, head.action):
                    self._last_fire[stamp_key] = self._clock.now() + head.jitter
                heapq.heappop(heap)
                head.future.set_result(None)
                granted_any = True
        if granted_any:
            self._wake_waiters()

    def _prune(self, key: Tuple[str, str]) -> None:
        """Drop dead waiters from one key's heap (ported from the old limiter).

        PHASE-4 (S3-G1): an emptied heap's dict key is popped too — a
        removed account would otherwise leave a permanent empty-list husk
        per (account, action) pair.
        """
        heap = self._pending.get(key)
        if not heap:
            self._pending.pop(key, None)  # husk left by an earlier prune
            return
        heap[:] = [e for e in heap if not e.cancelled and not e.future.done()]
        if not heap:
            self._pending.pop(key, None)
            return
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
        if wait > MAX_INTERVAL:
            # intake clamp: a finite-huge header (1e9 -> a 31.7-YEAR fleet
            # cooldown, memory-only) would block every waiter until restart.
            # Policies are capped at MAX_INTERVAL for exactly this reason;
            # the clamp keeps server-reported cooldowns in the same league.
            self._log(
                "warning",
                f"retry_after {wait:g}s exceeds the {MAX_INTERVAL:g}s cap; "
                f"clamping the cooldown to {MAX_INTERVAL:g}s",
            )
            wait = MAX_INTERVAL
        account, action = self._validate_acquire_args(account, action, None)[:2]
        now = self._clock.now()
        # FINAL-PHASE (F8 gap 2): sweep expired tombstones for this account
        # while we write — expired (account, action) rows were retained
        # forever (only account removal pruned them), and plugin-supplied
        # action strings made that an unbounded distinct-key family.
        for key in [k for k, until in self._account_cooldowns.items()
                    if k[0] == account and until <= now]:
            del self._account_cooldowns[key]
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
        # opportunistic expiry sweep (see _sweep_expired_cooldowns)
        self._sweep_expired_cooldowns()
        now = self._clock.now()
        remaining = [max(0.0, self._global_until - now)]
        for (acct, act), until in self._account_cooldowns.items():
            if account is not None and acct != account:
                continue
            if action is not None and act != action:
                continue
            remaining.append(max(0.0, until - now))
        return round(max(remaining), 3)

    def _sweep_expired_cooldowns(self) -> None:
        """Drop expired 429 rows from the table.

        Called from cooldown_remaining AND stats() — production polls
        stats() once per second (dashboard) but never calls
        cooldown_remaining, so without the stats() sweep dead rows lingered
        for the process lifetime (only prune_account removed them before).
        """
        now = self._clock.now()
        for key in [k for k, until in self._account_cooldowns.items()
                    if until <= now]:
            del self._account_cooldowns[key]

    # -------------------------------------------------------------- reporting
    def stats(self) -> dict:
        """JSON-safe snapshot for the UI: floor, policies, active cooldowns,
        last-fire ages per pacing key, and grant/wait counters."""
        self._sweep_expired_cooldowns()  # the 1s dashboard poll owns expiry
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
            "lane": {"enabled": False, "prefixrate": 0, "removed": True},
            "groups": self.account_groups(),
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
        """Advisory safety review: every stored policy, the floor entry, and
        the sequential lane state (a paused lane holds ALL outbound traffic).

        Entries are ``{"level", "code", "message"}`` sorted danger > warn >
        info; see core/rate_limit_manager/warnings.py for the taxonomy.
        """
        stored = (
            (policy, level, owner)
            for (level, owner, _action), policy in self._policies.items()
        )
        items = evaluate_policies(stored, self._floor)
        if False:
            items.append({
                "level": "warn",
                "code": "lane_paused",
                "message": (
                    "the sequential send lane is PAUSED (prefixrate 0) — every "
                    "outbound action is holding until a non-zero spacing is set"
                ),
            })
        return items

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
    "KV_LANE_KEY",
    "KV_FLOOR_KEY",
    "KV_REMOVED_KEY",
]
