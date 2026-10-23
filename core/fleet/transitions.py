"""Fleet state-transition tables + validation (PHASE 9, spec §2/§8).

PURE functions over the models' state enums — the single place a legal
transition is defined. The service (Phase B) commits transitions to the
database (D2: database-authoritative, in-memory is a projection) THROUGH
these validators; nothing else decides what a legal move is.

Rollout A acceptance: every table below is testable with no Discord
connection, no database, no event bus.
"""
from __future__ import annotations

from typing import FrozenSet, Mapping

from .models import (
    AssignmentStatus,
    MemberEligibility,
)

__all__ = [
    "InvalidTransition",
    "MEMBER_TRANSITIONS",
    "ASSIGNMENT_TRANSITIONS",
    "validate_member_transition",
    "member_can_transition",
    "validate_assignment_transition",
    "assignment_can_transition",
]


class InvalidTransition(ValueError):
    """An illegal state transition, with the exact why.

    Message contract (rendered by the hub and CLI verbatim):
    ``<entity> cannot move <current> -> <new>: <why>``.
    """

    def __init__(self, entity: str, current: str, new: str, why: str) -> None:
        super().__init__(
            f"{entity} cannot move {current} -> {new}: {why}")
        self.entity = entity
        self.current = current
        self.new = new
        self.why = why


#: Member eligibility moves (spec §2):
#:   ELIGIBLE -> WAITING -> (PAUSED | COOLING) -> back to ELIGIBLE/WAITING
#: RETIRED happens ONLY on leave (validator enforces the ``leaving`` flag);
#: PAUSED and COOLING are sibling branches — never cross directly; a
#: member that both pauses and cools expresses it through the return
#: states. RETIRED is terminal.
MEMBER_TRANSITIONS: Mapping[str, FrozenSet[str]] = {
    MemberEligibility.ELIGIBLE: frozenset({
        MemberEligibility.WAITING,
        MemberEligibility.PAUSED,
        MemberEligibility.COOLING,
        MemberEligibility.RETIRED,
    }),
    MemberEligibility.WAITING: frozenset({
        MemberEligibility.ELIGIBLE,
        MemberEligibility.PAUSED,
        MemberEligibility.COOLING,
        MemberEligibility.RETIRED,
    }),
    MemberEligibility.PAUSED: frozenset({
        MemberEligibility.ELIGIBLE,
        MemberEligibility.WAITING,
        MemberEligibility.RETIRED,
    }),
    MemberEligibility.COOLING: frozenset({
        MemberEligibility.ELIGIBLE,
        MemberEligibility.WAITING,
        MemberEligibility.RETIRED,
    }),
    MemberEligibility.RETIRED: frozenset(),
}


def validate_member_transition(current: str, new: str,
                               *, leaving: bool = False) -> None:
    """Validate a member-eligibility move; raise InvalidTransition.

    ``leaving`` gates RETIRED: the spec pins retirement to the LEAVE
    flow (§2 "plus RETIRED on leave"; §8), so an ordinary state edit
    can never silently retire a member.
    """
    if current not in MemberEligibility.ALL:
        raise InvalidTransition("member", current, new,
                                f"unknown state {current!r}")
    if new not in MemberEligibility.ALL:
        raise InvalidTransition("member", current, new,
                                f"unknown state {new!r}")
    if new == MemberEligibility.RETIRED and not leaving:
        raise InvalidTransition("member", current, new,
                                "retirement only happens through LEAVE")
    if new not in MEMBER_TRANSITIONS[current]:
        raise InvalidTransition("member", current, new,
                                "not a legal member move (spec §2)")


def member_can_transition(current: str, new: str, *,
                          leaving: bool = False) -> bool:
    try:
        validate_member_transition(current, new, leaving=leaving)
        return True
    except InvalidTransition:
        return False


#: Assignment moves (spec §2):
#:   QUEUED -> ASSIGNED -> RUNNING -> COMPLETED | SUSPENDED | FAILED | CANCELLED
#: * ASSIGNED -> QUEUED is WITHDRAW (D3's operator action)
#: * SUSPENDED -> QUEUED returns when prerequisites clear (relay handoff,
#:   group pause); CANCELLED from any non-terminal state is the operator
#: * FAILED -> QUEUED happens ONLY under an explicit retry allowance
#:   (guard flag; the resume_policy decides it at the call site)
#: * COMPLETED/CANCELLED are terminal
ASSIGNMENT_TRANSITIONS: Mapping[str, FrozenSet[str]] = {
    AssignmentStatus.QUEUED: frozenset({
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.CANCELLED,
    }),
    AssignmentStatus.ASSIGNED: frozenset({
        AssignmentStatus.RUNNING,
        AssignmentStatus.QUEUED,          # withdraw
        AssignmentStatus.SUSPENDED,
        AssignmentStatus.CANCELLED,
    }),
    AssignmentStatus.RUNNING: frozenset({
        AssignmentStatus.COMPLETED,
        AssignmentStatus.FAILED,
        AssignmentStatus.SUSPENDED,
        AssignmentStatus.CANCELLED,
    }),
    AssignmentStatus.SUSPENDED: frozenset({
        AssignmentStatus.QUEUED,          # prerequisites cleared
        AssignmentStatus.CANCELLED,
    }),
    AssignmentStatus.FAILED: frozenset({
        AssignmentStatus.QUEUED,          # ONLY with retry_allowed=True
    }),
    AssignmentStatus.COMPLETED: frozenset(),
    AssignmentStatus.CANCELLED: frozenset(),
}


def validate_assignment_transition(current: str, new: str,
                                   *, retry_allowed: bool = False) -> None:
    """Validate an assignment move; raise InvalidTransition.

    ``retry_allowed`` gates FAILED -> QUEUED: the spec pins resume on an
    EXPLICIT retry-policy allowance, never a default.
    """
    if current not in AssignmentStatus.ALL:
        raise InvalidTransition("assignment", current, new,
                                f"unknown state {current!r}")
    if new not in AssignmentStatus.ALL:
        raise InvalidTransition("assignment", current, new,
                                f"unknown state {new!r}")
    if current == AssignmentStatus.FAILED and not retry_allowed:
        raise InvalidTransition("assignment", current, new,
                                "resuming a FAILED task requires an "
                                "explicit retry allowance")
    if new not in ASSIGNMENT_TRANSITIONS[current]:
        raise InvalidTransition("assignment", current, new,
                                "not a legal assignment move (spec §2)")


def assignment_can_transition(current: str, new: str,
                              *, retry_allowed: bool = False) -> bool:
    try:
        validate_assignment_transition(current, new, retry_allowed=retry_allowed)
        return True
    except InvalidTransition:
        return False
