"""Fleet domain-model + transition tests (PHASE 9 rollout A).

Pure unit coverage: every transition pair in both matrices, the guard
flags (leaving / retry_allowed), model validation, the generation
fence, most-restrictive-wins cooldowns, and the snowflake-string rule.
No I/O anywhere — rollout A's acceptance criterion.
"""
from __future__ import annotations

import unittest

from core.fleet import (
    ASSIGNMENT_TRANSITIONS,
    MEMBER_TRANSITIONS,
    AssignmentStatus,
    AssignmentStrategy,
    CooldownRecord,
    CooldownScopeType,
    CooldownSource,
    ExecutionMode,
    FieldClass,
    FleetEvent,
    InvalidDefinition,
    InvalidTransition,
    MemberEligibility,
    MetricHint,
    POLICY_CHECK_ORDER,
    PolicyDecision,
    TaskAssignment,
    TaskDefinition,
    TransitionMode,
    assignment_can_transition,
    effective_cooldown,
    member_can_transition,
    observation_dedup_key,
    validate_assignment_transition,
    validate_channel_scope,
    validate_member_transition,
)


class MemberTransitionTests(unittest.TestCase):
    """The full eligibility matrix, per spec §2."""

    VALID = {
        ("eligible", "waiting"), ("eligible", "paused"), ("eligible", "cooling"),
        ("waiting", "eligible"), ("waiting", "paused"), ("waiting", "cooling"),
        ("paused", "eligible"), ("paused", "waiting"),
        ("cooling", "eligible"), ("cooling", "waiting"),
        # RETIRED from every live state — but only through LEAVE
        ("eligible", "retired"), ("waiting", "retired"),
        ("paused", "retired"), ("cooling", "retired"),
    }

    def test_matrix_exhaustively(self):
        states = sorted(MemberEligibility.ALL)
        for cur in states:
            for new in states:
                pair_legal = (cur, new) in self.VALID
                with self.subTest(cur=cur, new=new):
                    self.assertEqual(
                        member_can_transition(cur, new, leaving=True),
                        pair_legal,
                        f"leaving=True must allow exactly the legal moves")

    def test_every_state_can_retire_on_leave(self):
        for state in MemberEligibility.LIVE:
            with self.subTest(state=state):
                validate_member_transition(state, MemberEligibility.RETIRED,
                                           leaving=True)

    def test_retired_requires_leaving_flag(self):
        for state in MemberEligibility.LIVE:
            with self.subTest(state=state):
                with self.assertRaises(InvalidTransition) as ctx:
                    validate_member_transition(state, MemberEligibility.RETIRED)
                self.assertIn("LEAVE", str(ctx.exception))

    def test_retired_is_terminal(self):
        self.assertEqual(MEMBER_TRANSITIONS["retired"], frozenset())
        for new in MemberEligibility.ALL:
            self.assertFalse(member_can_transition("retired", new, leaving=True))

    def test_paused_and_cooling_never_cross_directly(self):
        self.assertFalse(member_can_transition("paused", "cooling", leaving=True))
        self.assertFalse(member_can_transition("cooling", "paused", leaving=True))

    def test_no_self_transitions(self):
        for state in MemberEligibility.ALL:
            self.assertFalse(member_can_transition(state, state, leaving=True))

    def test_unknown_states_rejected_with_clear_message(self):
        with self.assertRaises(InvalidTransition) as ctx:
            validate_member_transition("eligible", "vibing")
        self.assertIn("unknown state", str(ctx.exception))
        with self.assertRaises(InvalidTransition) as ctx:
            validate_member_transition("vibing", "eligible")
        self.assertIn("unknown state", str(ctx.exception))

    def test_message_names_entity_and_both_states(self):
        with self.assertRaises(InvalidTransition) as ctx:
            validate_member_transition("paused", "cooling", leaving=True)
        msg = str(ctx.exception)
        self.assertIn("member", msg)
        self.assertIn("paused", msg)
        self.assertIn("cooling", msg)


class AssignmentTransitionTests(unittest.TestCase):
    """The full assignment matrix, per spec §2."""

    VALID = {
        ("queued", "assigned"), ("queued", "cancelled"),
        ("assigned", "running"), ("assigned", "queued"),      # withdraw
        ("assigned", "suspended"), ("assigned", "cancelled"),
        ("running", "completed"), ("running", "failed"),
        ("running", "suspended"), ("running", "cancelled"),
        ("suspended", "queued"), ("suspended", "cancelled"),
        ("failed", "queued"),        # ONLY under retry_allowed
    }

    def test_matrix_exhaustively_without_retry(self):
        states = sorted(AssignmentStatus.ALL)
        for cur in states:
            for new in states:
                expected = (cur, new) in self.VALID and cur != "failed"
                with self.subTest(cur=cur, new=new):
                    self.assertEqual(
                        assignment_can_transition(cur, new), expected)

    def test_matrix_exhaustively_with_retry(self):
        states = sorted(AssignmentStatus.ALL)
        for cur in states:
            for new in states:
                expected = (cur, new) in self.VALID
                with self.subTest(cur=cur, new=new, retry=True):
                    self.assertEqual(
                        assignment_can_transition(cur, new, retry_allowed=True),
                        expected)

    def test_failed_resume_requires_explicit_retry(self):
        with self.assertRaises(InvalidTransition) as ctx:
            validate_assignment_transition("failed", "queued")
        self.assertIn("explicit retry", str(ctx.exception))
        validate_assignment_transition("failed", "queued", retry_allowed=True)

    def test_terminals_are_terminal(self):
        self.assertEqual(ASSIGNMENT_TRANSITIONS["completed"], frozenset())
        self.assertEqual(ASSIGNMENT_TRANSITIONS["cancelled"], frozenset())

    def test_suspended_returns_to_queued_not_running(self):
        validate_assignment_transition("suspended", "queued")
        with self.assertRaises(InvalidTransition):
            validate_assignment_transition("suspended", "running")

    def test_no_skip_from_queued_to_running(self):
        with self.assertRaises(InvalidTransition):
            validate_assignment_transition("queued", "running")


class CooldownRecordTests(unittest.TestCase):
    def test_valid_record(self):
        cd = CooldownRecord(scope_type=CooldownScopeType.TASK, scope_id="roll",
                            reason="observed limit", started_at=100.0,
                            expires_at=200.0, source=CooldownSource.OBSERVED)
        self.assertTrue(cd.is_active(now=150.0))
        self.assertFalse(cd.is_active(now=200.0))       # boundary = expired
        self.assertFalse(cd.operator_clearable)

    def test_manual_is_operator_clearable(self):
        cd = CooldownRecord(scope_type=CooldownScopeType.GROUP, scope_id="g1",
                            reason="operator pause", started_at=0.0,
                            expires_at=10.0, source=CooldownSource.MANUAL)
        self.assertTrue(cd.operator_clearable)

    def test_validation_rejects_bad_shapes(self):
        base = dict(scope_type=CooldownScopeType.ACCOUNT, scope_id="bravo",
                    reason="r", started_at=0.0, expires_at=1.0)
        with self.assertRaises(InvalidDefinition):
            CooldownRecord(**{**base, "scope_type": "guild"})   # not a scope
        with self.assertRaises(InvalidDefinition):
            CooldownRecord(**{**base, "source": "divine"})      # not a source
        with self.assertRaises(InvalidDefinition):
            CooldownRecord(**{**base, "scope_id": ""})          # empty id
        with self.assertRaises(InvalidDefinition):
            CooldownRecord(**{**base, "expires_at": -1.0})      # before start

    def test_effective_is_most_restrictive(self):
        now = 50.0
        short = CooldownRecord(CooldownScopeType.TASK, "roll", "a",
                               0.0, 60.0, CooldownSource.OBSERVED)
        long_ = CooldownRecord(CooldownScopeType.GROUP, "g1", "b",
                               0.0, 300.0, CooldownSource.SERVER)
        manual = CooldownRecord(CooldownScopeType.ACCOUNT, "bravo", "c",
                                0.0, 70.0, CooldownSource.MANUAL)
        self.assertIs(effective_cooldown([short, long_, manual], now=now), long_)
        # expired records never win
        expired = CooldownRecord(CooldownScopeType.GROUP, "g1", "old",
                                 0.0, 10.0, CooldownSource.SERVER)
        self.assertIs(effective_cooldown([expired], now=now), None)
        self.assertIs(effective_cooldown([], now=now), None)
        self.assertIs(effective_cooldown(None, now=now), None)


class ChannelScopeTests(unittest.TestCase):
    """D5 + the snowflake transport rule: ids are digit STRINGS."""

    def test_valid_scope_normalized_to_tuple(self):
        self.assertEqual(validate_channel_scope(["123", "456"]), ("123", "456"))

    def test_rejects_non_list(self):
        for bad in ("123", 123, {"a": 1}, None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidDefinition):
                    validate_channel_scope(bad)

    def test_rejects_empty(self):
        with self.assertRaises(InvalidDefinition):
            validate_channel_scope([])

    def test_rejects_non_string_ids(self):
        with self.assertRaises(InvalidDefinition) as ctx:
            validate_channel_scope([1553823440471986326])   # the JS-rounded kind
        self.assertIn("STRINGS", str(ctx.exception))

    def test_rejects_non_digit_strings(self):
        with self.assertRaises(InvalidDefinition):
            validate_channel_scope(["channel-general"])

    def test_rejects_duplicates(self):
        with self.assertRaises(InvalidDefinition):
            validate_channel_scope(["123", "123"])


class TaskDefinitionTests(unittest.TestCase):
    def test_minimal_definition(self):
        d = TaskDefinition(id="roll", plugin="game_roll",
                           channel_scope=["123"])
        self.assertEqual(d.channel_scope, ("123",))
        self.assertEqual(d.max_attempts, 1)
        self.assertEqual(d.field_class_of("anything"), FieldClass.HOT)

    def test_field_classes_declared_and_defaulted(self):
        d = TaskDefinition(id="roll", plugin="p", channel_scope=["1"],
                           field_classes={"channel map": FieldClass.BOUNDARY,
                                          "flow shape": FieldClass.STRUCTURAL})
        self.assertEqual(d.field_class_of("channel map"), FieldClass.BOUNDARY)
        self.assertEqual(d.field_class_of("undeclared"), FieldClass.HOT)

    def test_metric_hint_validation(self):
        with self.assertRaises(InvalidDefinition):
            MetricHint(key="total", label="Total", aggregate="avg")
        hint = MetricHint(key="total", label="Total", aggregate="sum")
        d = TaskDefinition(id="roll", plugin="p", channel_scope=["1"],
                           metric_hints=(hint,))
        self.assertEqual(d.metric_hints[0].aggregate, "sum")

    def test_bad_shapes_rejected(self):
        base = dict(id="roll", plugin="p")
        with self.assertRaises(InvalidDefinition):
            TaskDefinition(**base, channel_scope=[])
        with self.assertRaises(InvalidDefinition):
            TaskDefinition(**base, channel_scope=["1"], max_attempts=0)
        with self.assertRaises(InvalidDefinition):
            TaskDefinition(**{**base, "id": ""}, channel_scope=["1"])
        with self.assertRaises(InvalidDefinition):
            TaskDefinition(**base, channel_scope=["1"],
                           field_classes={"x": "spicy"})

    def test_optional_keys_must_be_nonempty_or_absent(self):
        with self.assertRaises(InvalidDefinition):
            TaskDefinition(id="r", plugin="p", channel_scope=["1"],
                           data_key="   ")


class TaskAssignmentTests(unittest.TestCase):
    def test_generation_fence(self):
        a = TaskAssignment(id="a1", group_id="g", task_id="roll",
                           account_id="bravo", generation=3)
        self.assertTrue(a.matches_generation(3))
        self.assertFalse(a.matches_generation(4))   # stale dispatch rejected

    def test_validation(self):
        ok = dict(id="a1", group_id="g", task_id="roll", account_id="bravo")
        TaskAssignment(**ok)
        with self.assertRaises(InvalidDefinition):
            TaskAssignment(**{**ok, "status": "vibing"})
        with self.assertRaises(InvalidDefinition):
            TaskAssignment(**{**ok, "account_id": ""})
        with self.assertRaises(InvalidDefinition):
            TaskAssignment(**{**ok, "generation": 0})


class PolicyDecisionTests(unittest.TestCase):
    def test_rejection_shape(self):
        d = PolicyDecision(allowed=False, reason="group paused",
                           retry_at=123.0)
        self.assertEqual(d.reason, "group paused")

    def test_allowed_carries_no_retry_at(self):
        with self.assertRaises(InvalidDefinition):
            PolicyDecision(allowed=True, reason="ok", retry_at=1.0)

    def test_reason_required(self):
        with self.assertRaises(InvalidDefinition):
            PolicyDecision(allowed=True, reason=" ")

    def test_check_order_starts_with_safety_ends_with_prerequisites(self):
        self.assertEqual(POLICY_CHECK_ORDER[0], "framework_safety")
        self.assertEqual(POLICY_CHECK_ORDER[-1], "prerequisites")


class FleetEventTests(unittest.TestCase):
    def test_defaults_populate(self):
        e = FleetEvent(kind="TaskAssigned", group_id="g1", generation=2,
                       reason="operator assign")
        self.assertEqual(e.task_id, None)
        self.assertTrue(e.correlation_id)          # uuid default
        self.assertGreater(e.occurred_at, 0)

    def test_correlation_ids_unique_per_event(self):
        a = FleetEvent(kind="K", group_id="g", generation=1, reason="r")
        b = FleetEvent(kind="K", group_id="g", generation=1, reason="r")
        self.assertNotEqual(a.correlation_id, b.correlation_id)

    def test_validation(self):
        with self.assertRaises(InvalidDefinition):
            FleetEvent(kind="", group_id="g", generation=1, reason="r")
        with self.assertRaises(InvalidDefinition):
            FleetEvent(kind="K", group_id="g", generation=0, reason="r")

    def test_observation_dedup_key_stringifies(self):
        # ints sneak in from sloppy producers; the key still dedups exactly
        self.assertEqual(observation_dedup_key("bravo", "999", "limit"),
                         observation_dedup_key("bravo", "999", "limit"))
        self.assertNotEqual(observation_dedup_key("bravo", "999", "limit"),
                            observation_dedup_key("bravo", "998", "limit"))


class EnumContractTests(unittest.TestCase):
    """The locked vocabularies stay spelled exactly as the spec writes
    them — the hub UI, persistence, and events all pin these literals."""

    def test_member_states(self):
        self.assertEqual(sorted(MemberEligibility.ALL),
                         ["cooling", "eligible", "paused", "retired", "waiting"])

    def test_assignment_states(self):
        self.assertEqual(sorted(AssignmentStatus.ALL),
                         ["assigned", "cancelled", "completed", "failed",
                          "queued", "running", "suspended"])

    def test_execution_modes(self):
        self.assertEqual(sorted(ExecutionMode.ALL),
                         ["group_managed", "independent"])

    def test_strategies(self):
        self.assertEqual(sorted(AssignmentStrategy.ALL), ["manual", "relay"])

    def test_transition_modes_suspend_deferred(self):
        self.assertEqual(sorted(TransitionMode.ALL), ["drain"])

    def test_field_classes(self):
        self.assertEqual(sorted(FieldClass.ALL),
                         ["boundary", "hot", "structural"])

    def test_cooldown_vocab(self):
        self.assertEqual(sorted(CooldownScopeType.ALL),
                         ["account", "group", "task"])
        self.assertEqual(sorted(CooldownSource.ALL),
                         ["manual", "observed", "server"])


if __name__ == "__main__":
    unittest.main()
