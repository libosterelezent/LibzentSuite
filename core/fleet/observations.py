"""Fleet observations — the parser-to-fleet boundary (PHASE 9, D).

Spec §3: observations AUTHORIZE NOTHING. A parser (through its plugin —
core never imports parsers/, §10b) or the limiter reports what it saw;
this intake translates the report into state changes THROUGH the
service: scoped cooldowns (source=observed/server), eligibility moves
(derived connectivity, §2), and the RELAY strategy (D3: limit observed
-> suspend the owner's task -> scoped cooldown -> hand the task to the
next eligible member).

There is deliberately NO code path from an observation to a dispatch
or an ALLOWED verdict — an observation can only ADD restrictions or
move work within the transition tables. Rollout D's acceptance
("parser output alone cannot bypass state/policy") is a property of
this module's shape, and its tests.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import (
    AssignmentStatus,
    CooldownScopeType,
    CooldownSource,
    MemberEligibility,
    observation_dedup_key,
)

__all__ = [
    "Observation",
    "FleetObservations",
    "OBS_LIMIT_MARKER",
    "OBS_SERVER_COOLDOWN",
    "OBS_ACCOUNT_DISCONNECTED",
    "OBS_ACCOUNT_CONNECTED",
]

LOG = logging.getLogger("libzent.fleet.observations")

#: generic observation types (core vocabulary ONLY — game terms live in
#: parser packs; a pack's "you've been rate limited by the game" maps to
#: OBS_LIMIT_MARKER with its own window)
OBS_LIMIT_MARKER = "limit_marker"
OBS_SERVER_COOLDOWN = "server_cooldown"
OBS_ACCOUNT_DISCONNECTED = "account_disconnected"
OBS_ACCOUNT_CONNECTED = "account_connected"


@dataclass(frozen=True)
class Observation:
    """One raw observation crossing the fleet boundary.

    ``source_account`` / ``message_id`` are STRINGS (snowflake rule);
    ``message_id`` is synthetic for non-message sources (connectivity,
    limiter reports). ``cooldown_window_s`` is REQUIRED for the limit/
    server types — the engine never invents a window.
    """

    source_account: str
    observation_type: str
    message_id: str = ""
    observed_at: float = field(default_factory=time.time)
    cooldown_window_s: Optional[float] = None
    reason: str = ""


class FleetObservations:
    """Deduplicating intake; every effect routes through FleetService.

    Dedup key (spec §3): ``(source_account, message_id,
    observation_type)`` — the same marker delivered twice (reconnect
    replays, at-least-once busses) registers once. Bounded LRU; a
    restart starts clean (persisted cooldowns carry the truth forward).
    """

    MAX_SEEN = 4096

    def __init__(self, fleet: Any,
                 *, time_fn: Optional[Callable[[], float]] = None) -> None:
        self._fleet = fleet
        self._now = time_fn or time.time
        self._seen: "OrderedDict[tuple, bool]" = OrderedDict()

    # ------------------------------------------------------------- intake

    async def observe(self, obs: Observation) -> bool:
        """Process one observation. True when it was NEW and handled;
        False when it was a duplicate, an unknown type, or a
        non-member's report (observations about accounts the fleet does
        not manage are not its business — D1)."""
        key = observation_dedup_key(
            obs.source_account, obs.message_id, obs.observation_type)
        if key in self._seen:
            return False
        self._seen[key] = True
        self._seen.move_to_end(key)
        while len(self._seen) > self.MAX_SEEN:
            self._seen.popitem(last=False)

        handler = {
            OBS_LIMIT_MARKER: self._on_limit,
            OBS_SERVER_COOLDOWN: self._on_server_cooldown,
            OBS_ACCOUNT_DISCONNECTED: self._on_disconnected,
            OBS_ACCOUNT_CONNECTED: self._on_connected,
        }.get(obs.observation_type)
        if handler is None:
            # unknown type: remembered (so a replay stays a no-op) but
            # NO state changes — future types land without effect until
            # a handler exists
            return False
        if self._fleet.member(obs.source_account) is None:
            return False        # not fleet-managed: D1 boundary
        try:
            await handler(obs)
        except Exception:
            # an observation handler failure is logged, never raised —
            # a bad report must not take down the reporter's flow; the
            # dedup entry stays so the poisoned message replays harmlessly
            LOG.exception("observation handler failed (%s from %s)",
                          obs.observation_type, obs.source_account)
            return False
        return True

    # ------------------------------------------------------------ handlers

    async def _on_limit(self, obs: Observation) -> None:
        """§2: a detected limit registers a scoped COOLDOWN
        (source=observed, expiry=the game's own window) and the relay
        strategy reassigns the account's live work."""
        window = obs.cooldown_window_s
        if not isinstance(window, (int, float)) or window <= 0:
            LOG.warning("limit marker from %s without a positive window"
                        " — cooldown skipped (never invented)", obs.source_account)
            return
        await self._fleet.apply_cooldown(
            CooldownScopeType.ACCOUNT, obs.source_account,
            obs.reason or "observed limit marker", float(window),
            source=CooldownSource.OBSERVED)
        await self._relay(obs.source_account, obs.reason or "observed limit")

    async def _on_server_cooldown(self, obs: Observation) -> None:
        """The limiter REPORTED a server cooldown (§4): same shape as a
        limit observation, source=server — the game's own truth, never
        operator-clearable."""
        window = obs.cooldown_window_s
        if not isinstance(window, (int, float)) or window <= 0:
            return
        await self._fleet.apply_cooldown(
            CooldownScopeType.ACCOUNT, obs.source_account,
            obs.reason or "server cooldown", float(window),
            source=CooldownSource.SERVER)
        await self._relay(obs.source_account, obs.reason or "server cooldown")

    async def _on_disconnected(self, obs: Observation) -> None:
        """Connectivity is DERIVED into eligibility (§2): disconnected
        members wait. Only an ELIGIBLE member starts waiting — an
        operator PAUSE (or an active cooldown's COOLING) outranks
        connectivity and stands until its own owner clears it."""
        member = self._fleet.member(obs.source_account)
        if member is None or member.eligibility != MemberEligibility.ELIGIBLE:
            return
        await self._fleet.mark_eligibility(
            obs.source_account, MemberEligibility.WAITING)

    async def _on_connected(self, obs: Observation) -> None:
        """Back online: WAITING -> ELIGIBLE. PAUSED members stay paused
        (an operator decision is not connectivity's to undo)."""
        member = self._fleet.member(obs.source_account)
        if member is None or member.eligibility != MemberEligibility.WAITING:
            return
        await self._fleet.mark_eligibility(
            obs.source_account, MemberEligibility.ELIGIBLE)

    # -------------------------------------------------------------- relay

    async def _relay(self, account: str, reason: str) -> None:
        """D3 relay: suspend the account's live assignment(s), return
        them to the group queue, hand each to the next ELIGIBLE member.
        No eligible member -> the task stays QUEUED (never forced onto a
        cooling/paused account — assign_task re-validates everything)."""
        live = self._fleet.assignments(
            account=account, status=AssignmentStatus.RUNNING)
        live += self._fleet.assignments(
            account=account, status=AssignmentStatus.ASSIGNED)
        for rec in live:
            await self._fleet.suspend_task(rec.id, reason)
            await self._fleet.resume_task(rec.id)     # SUSPENDED -> QUEUED
            await self._reassign(rec.id, exclude=account)

    async def _reassign(self, assignment_id: str, *, exclude: str) -> bool:
        rec = self._fleet.assignment(assignment_id)
        if rec is None:
            return False
        candidates = sorted(
            m.account for m in self._fleet.members(rec.group_name)
            if m.account != exclude
            and m.eligibility == MemberEligibility.ELIGIBLE)
        for candidate in candidates:
            try:
                await self._fleet.assign_task(assignment_id, candidate)
                return True
            except ValueError:
                continue        # raced out of eligibility — next member
        return False
