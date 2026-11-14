"""Workspace transition-table tests (W1) — pure.

The member and task tables are carried from fleet: every fleet
transition test's expectation must hold here unchanged. The step and
chain-run tables are NEW (WORKSPACE_DESIGN §10.3).
"""

from __future__ import annotations

import unittest

from core.workspace import (
    CHAIN_RUN_TRANSITIONS,
    MEMBER_TRANSITIONS,
    STEP_TRANSITIONS,
    TASK_TRANSITIONS,
    ChainRunStatus,
    InvalidTransition,
    MemberEligibility,
    StepStatus,
    TaskStatus,
    chain_run_can_transition,
    member_can_transition,
    step_can_transition,
    task_can_transition,
    validate_chain_run_transition,
    validate_member_transition,
    validate_step_transition,
    validate_task_transition,
)


class CarriedMemberTransitionTests(unittest.TestCase):
    def test_waiting_round_trip(self) -> None:
        validate_member_transition(MemberEligibility.ELIGIBLE, MemberEligibility.WAITING)
        validate_member_transition(MemberEligibility.WAITING, MemberEligibility.ELIGIBLE)

    def test_retired_only_through_leave(self) -> None:
        with self.assertRaises(InvalidTransition) as ctx:
            validate_member_transition(MemberEligibility.ELIGIBLE, MemberEligibility.RETIRED)
        self.assertIn("through LEAVE", str(ctx.exception))
        validate_member_transition(
            MemberEligibility.ELIGIBLE, MemberEligibility.RETIRED, leaving=True
        )

    def test_paused_and_cooling_never_cross(self) -> None:
        with self.assertRaises(InvalidTransition):
            validate_member_transition(MemberEligibility.PAUSED, MemberEligibility.COOLING)
        with self.assertRaises(InvalidTransition):
            validate_member_transition(MemberEligibility.COOLING, MemberEligibility.PAUSED)

    def test_retired_is_terminal(self) -> None:
        self.assertEqual(MEMBER_TRANSITIONS[MemberEligibility.RETIRED], frozenset())
        self.assertFalse(
            member_can_transition(MemberEligibility.RETIRED, MemberEligibility.ELIGIBLE)
        )

    def test_fleet_tables_are_equivalent(self) -> None:
        from core.fleet.transitions import (
            ASSIGNMENT_TRANSITIONS as FLEET_ASSIGNMENT,
        )
        from core.fleet.transitions import (
            MEMBER_TRANSITIONS as FLEET_MEMBER,
        )

        self.assertEqual(
            {k: set(v) for k, v in MEMBER_TRANSITIONS.items()},
            {k: set(v) for k, v in FLEET_MEMBER.items()},
        )
        self.assertEqual(
            {k: set(v) for k, v in TASK_TRANSITIONS.items()},
            {k: set(v) for k, v in FLEET_ASSIGNMENT.items()},
        )


class CarriedTaskTransitionTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        validate_task_transition(TaskStatus.QUEUED, TaskStatus.ASSIGNED)
        validate_task_transition(TaskStatus.ASSIGNED, TaskStatus.RUNNING)
        validate_task_transition(TaskStatus.RUNNING, TaskStatus.COMPLETED)

    def test_withdraw_is_assigned_to_queued(self) -> None:
        validate_task_transition(TaskStatus.ASSIGNED, TaskStatus.QUEUED)

    def test_failed_resume_needs_explicit_allowance(self) -> None:
        with self.assertRaises(InvalidTransition) as ctx:
            validate_task_transition(TaskStatus.FAILED, TaskStatus.QUEUED)
        self.assertIn("explicit retry allowance", str(ctx.exception))
        validate_task_transition(TaskStatus.FAILED, TaskStatus.QUEUED, retry_allowed=True)

    def test_terminals_frozen(self) -> None:
        for terminal in TaskStatus.ALWAYS_TERMINAL:
            self.assertFalse(task_can_transition(terminal, TaskStatus.QUEUED))


class StepTransitionTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        validate_step_transition(StepStatus.PENDING, StepStatus.READY)
        validate_step_transition(StepStatus.READY, StepStatus.RUNNING)
        validate_step_transition(StepStatus.RUNNING, StepStatus.DONE)

    def test_visible_skip_needs_a_reason(self) -> None:
        with self.assertRaises(InvalidTransition) as ctx:
            validate_step_transition(StepStatus.PENDING, StepStatus.SKIPPED, reason="")
        self.assertIn("visible reason", str(ctx.exception))
        validate_step_transition(
            StepStatus.PENDING, StepStatus.SKIPPED, reason="plugin not assigned"
        )

    def test_running_cannot_silently_skip(self) -> None:
        # a RUNNING step may have side effects — it fails, never skips
        self.assertFalse(
            step_can_transition(StepStatus.RUNNING, StepStatus.SKIPPED, reason="late skip")
        )

    def test_retry_gated(self) -> None:
        with self.assertRaises(InvalidTransition):
            validate_step_transition(StepStatus.FAILED, StepStatus.READY)
        validate_step_transition(StepStatus.FAILED, StepStatus.READY, retry_allowed=True)

    def test_cancel_from_live_states(self) -> None:
        for live in (StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING):
            validate_step_transition(live, StepStatus.CANCELLED, reason="run aborted")

    def test_terminals_frozen(self) -> None:
        for terminal in StepStatus.ALWAYS_TERMINAL:
            self.assertEqual(STEP_TRANSITIONS[terminal], frozenset())


class ChainRunTransitionTests(unittest.TestCase):
    def test_running_reaches_every_terminal(self) -> None:
        for terminal in ChainRunStatus.ALWAYS_TERMINAL:
            validate_chain_run_transition(ChainRunStatus.RUNNING, terminal)

    def test_terminals_frozen(self) -> None:
        for terminal in ChainRunStatus.ALWAYS_TERMINAL:
            self.assertFalse(chain_run_can_transition(terminal, ChainRunStatus.RUNNING))

    def test_partial_is_terminal_and_distinct(self) -> None:
        # partial completion is first-class (on_skip=continue carried
        # the run to its end with skips aboard)
        self.assertIn(ChainRunStatus.PARTIAL, CHAIN_RUN_TRANSITIONS[ChainRunStatus.RUNNING])
        self.assertNotIn(ChainRunStatus.RUNNING, CHAIN_RUN_TRANSITIONS[ChainRunStatus.PARTIAL])


if __name__ == "__main__":
    unittest.main()
