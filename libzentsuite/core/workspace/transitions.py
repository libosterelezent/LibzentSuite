"""Workspace state-transition tables + validation (W1; supersedes
core/fleet/transitions.py).

PURE functions over the models' state enums — the single place a
legal transition is defined. The service (W3) commits transitions to
the database (database-authoritative, in-memory is a projection)
THROUGH these validators; nothing else decides what a legal move is.

Carried from the fleet module renamed: the member-eligibility table
and the task table are byte-for-byte the fleet tables under the new
names. NEW: chain-step transitions and chain-run transitions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .models import (
    ChainRunStatus,
    MemberEligibility,
    StepStatus,
    TaskStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CHAIN_RUN_TRANSITIONS",
    "MEMBER_TRANSITIONS",
    "STEP_TRANSITIONS",
    "TASK_TRANSITIONS",
    "InvalidTransition",
    "chain_run_can_transition",
    "member_can_transition",
    "step_can_transition",
    "task_can_transition",
    "validate_chain_run_transition",
    "validate_member_transition",
    "validate_step_transition",
    "validate_task_transition",
]


class InvalidTransition(ValueError):
    """An illegal state transition, with the exact why.

    Message contract (rendered by surfaces verbatim):
    ``<entity> cannot move <current> -> <new>: <why>``. (Carried.)
    """

    def __init__(self, entity: str, current: str, new: str, why: str) -> None:
        super().__init__(f"{entity} cannot move {current} -> {new}: {why}")
        self.entity = entity
        self.current = current
        self.new = new
        self.why = why


#: Member eligibility moves (carried):
#:   ELIGIBLE -> WAITING -> (PAUSED | COOLING) -> back to ELIGIBLE/WAITING
#: RETIRED happens ONLY on leave (validator enforces the ``leaving``
#: flag); PAUSED and COOLING are sibling branches — never cross
#: directly; RETIRED is terminal.
MEMBER_TRANSITIONS: Mapping[str, frozenset[str]] = {
    MemberEligibility.ELIGIBLE: frozenset(
        {
            MemberEligibility.WAITING,
            MemberEligibility.PAUSED,
            MemberEligibility.COOLING,
            MemberEligibility.RETIRED,
        }
    ),
    MemberEligibility.WAITING: frozenset(
        {
            MemberEligibility.ELIGIBLE,
            MemberEligibility.PAUSED,
            MemberEligibility.COOLING,
            MemberEligibility.RETIRED,
        }
    ),
    MemberEligibility.PAUSED: frozenset(
        {
            MemberEligibility.ELIGIBLE,
            MemberEligibility.WAITING,
            MemberEligibility.RETIRED,
        }
    ),
    MemberEligibility.COOLING: frozenset(
        {
            MemberEligibility.ELIGIBLE,
            MemberEligibility.WAITING,
            MemberEligibility.RETIRED,
        }
    ),
    MemberEligibility.RETIRED: frozenset(),
}


def validate_member_transition(current: str, new: str, *, leaving: bool = False) -> None:
    """Validate a member-eligibility move; raise InvalidTransition.

    ``leaving`` gates RETIRED: retirement is pinned to the LEAVE flow,
    so an ordinary state edit can never silently retire a member.
    (Carried.)
    """
    if current not in MemberEligibility.ALL:
        raise InvalidTransition("member", current, new, f"unknown state {current!r}")
    if new not in MemberEligibility.ALL:
        raise InvalidTransition("member", current, new, f"unknown state {new!r}")
    if new == MemberEligibility.RETIRED and not leaving:
        raise InvalidTransition("member", current, new, "retirement only happens through LEAVE")
    if new not in MEMBER_TRANSITIONS[current]:
        raise InvalidTransition("member", current, new, "not a legal member move")


def member_can_transition(current: str, new: str, *, leaving: bool = False) -> bool:
    try:
        validate_member_transition(current, new, leaving=leaving)
        return True
    except InvalidTransition:
        return False


#: Task moves (carried):
#:   QUEUED -> ASSIGNED -> RUNNING -> COMPLETED | SUSPENDED | FAILED | CANCELLED
#: * ASSIGNED -> QUEUED is WITHDRAW (the operator action)
#: * SUSPENDED -> QUEUED returns when prerequisites clear
#: * FAILED -> QUEUED happens ONLY under an explicit retry allowance
#: * COMPLETED/CANCELLED are terminal
TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    TaskStatus.QUEUED: frozenset(
        {
            TaskStatus.ASSIGNED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.ASSIGNED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.QUEUED,  # withdraw
            TaskStatus.SUSPENDED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.SUSPENDED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.SUSPENDED: frozenset(
        {
            TaskStatus.QUEUED,  # prerequisites cleared
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.FAILED: frozenset(
        {
            TaskStatus.QUEUED,  # ONLY with retry_allowed=True
        }
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def validate_task_transition(current: str, new: str, *, retry_allowed: bool = False) -> None:
    """Validate a task move; raise InvalidTransition.

    ``retry_allowed`` gates FAILED -> QUEUED: resume requires an
    EXPLICIT retry-policy allowance, never a default. (Carried.)
    """
    if current not in TaskStatus.ALL:
        raise InvalidTransition("task", current, new, f"unknown state {current!r}")
    if new not in TaskStatus.ALL:
        raise InvalidTransition("task", current, new, f"unknown state {new!r}")
    if current == TaskStatus.FAILED and not retry_allowed:
        raise InvalidTransition(
            "task",
            current,
            new,
            "resuming a FAILED task requires an explicit retry allowance",
        )
    if new not in TASK_TRANSITIONS[current]:
        raise InvalidTransition("task", current, new, "not a legal task move")


def task_can_transition(current: str, new: str, *, retry_allowed: bool = False) -> bool:
    try:
        validate_task_transition(current, new, retry_allowed=retry_allowed)
        return True
    except InvalidTransition:
        return False


#: Chain-step moves (NEW — WORKSPACE_DESIGN §10.3):
#:   PENDING -> READY -> RUNNING -> DONE
#:               ↘ SKIPPED (plugin absent | gate denies | no eligible
#:                           member | channel unusable)
#:               ↘ FAILED (attempts exhausted)
#: * PENDING/READY -> SKIPPED is the visible pre-execution skip;
#:   a RUNNING step that becomes un-runnable FAILS (it may have side
#:   effects), never silently skips
#: * FAILED -> READY happens ONLY under an explicit retry allowance
#: * DONE/SKIPPED are terminal; CANCELLED from any live state is the
#:   operator/run-level abort
STEP_TRANSITIONS: Mapping[str, frozenset[str]] = {
    StepStatus.PENDING: frozenset(
        {
            StepStatus.READY,
            StepStatus.SKIPPED,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.READY: frozenset(
        {
            StepStatus.RUNNING,
            StepStatus.SKIPPED,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.DONE,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.FAILED: frozenset(
        {
            StepStatus.READY,  # ONLY with retry_allowed=True
        }
    ),
    StepStatus.DONE: frozenset(),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}


def validate_step_transition(
    current: str,
    new: str,
    *,
    retry_allowed: bool = False,
    reason: str = "",
) -> None:
    """Validate a chain-step move; raise InvalidTransition.

    ``retry_allowed`` gates FAILED -> READY (the per-chain retry
    policy decides it at the call site). SKIPPED must carry a
    ``reason`` — a silent skip is exactly what the spec forbids
    (§10.2: visible skips with stable reason codes).
    """
    if current not in StepStatus.ALL:
        raise InvalidTransition("step", current, new, f"unknown state {current!r}")
    if new not in StepStatus.ALL:
        raise InvalidTransition("step", current, new, f"unknown state {new!r}")
    if new == StepStatus.SKIPPED and not reason.strip():
        raise InvalidTransition(
            "step", current, new, "a skip must carry a visible reason (spec §10.2)"
        )
    if current == StepStatus.FAILED and not retry_allowed:
        raise InvalidTransition(
            "step",
            current,
            new,
            "retrying a FAILED step requires an explicit retry allowance",
        )
    if new not in STEP_TRANSITIONS[current]:
        raise InvalidTransition("step", current, new, "not a legal step move")


def step_can_transition(
    current: str, new: str, *, retry_allowed: bool = False, reason: str = ""
) -> bool:
    try:
        validate_step_transition(current, new, retry_allowed=retry_allowed, reason=reason)
        return True
    except InvalidTransition:
        return False


#: Chain-run moves (NEW): a running run ends COMPLETED (every step
#: DONE), PARTIAL (some steps DONE, others SKIPPED under
#: on_skip=continue), FAILED (a step failed terminally under
#: on_skip=abort / attempts exhausted), or CANCELLED (operator).
CHAIN_RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    ChainRunStatus.RUNNING: frozenset(
        {
            ChainRunStatus.COMPLETED,
            ChainRunStatus.PARTIAL,
            ChainRunStatus.FAILED,
            ChainRunStatus.CANCELLED,
        }
    ),
    ChainRunStatus.COMPLETED: frozenset(),
    ChainRunStatus.PARTIAL: frozenset(),
    ChainRunStatus.FAILED: frozenset(),
    ChainRunStatus.CANCELLED: frozenset(),
}


def validate_chain_run_transition(current: str, new: str) -> None:
    """Validate a chain-run move; raise InvalidTransition."""
    if current not in ChainRunStatus.ALL:
        raise InvalidTransition("chain run", current, new, f"unknown state {current!r}")
    if new not in ChainRunStatus.ALL:
        raise InvalidTransition("chain run", current, new, f"unknown state {new!r}")
    if new not in CHAIN_RUN_TRANSITIONS[current]:
        raise InvalidTransition("chain run", current, new, "not a legal chain-run move")


def chain_run_can_transition(current: str, new: str) -> bool:
    try:
        validate_chain_run_transition(current, new)
        return True
    except InvalidTransition:
        return False
