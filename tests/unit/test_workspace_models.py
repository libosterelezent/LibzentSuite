"""Workspace domain model tests (W1) — pure, zero connections.

Covers the carried fleet vocabulary (renamed one-to-one), the new W1
vocabulary (channels, chains, signals, presets, declarations), and
the exact-message validation discipline (every rejection names the
field — never a bare 'invalid').
"""

from __future__ import annotations

import unittest

from libzentsuite.core.workspace import (
    ChainDefinition,
    ChainRun,
    ChainRunStatus,
    ChainStep,
    ChannelBinding,
    ChannelDescriptor,
    ChannelValidity,
    CooldownRecord,
    CooldownScopeType,
    ExecutionMode,
    FollowSpec,
    InvalidDefinition,
    MemberEligibility,
    PresetRecord,
    Provenance,
    RoutingPolicy,
    SignalIdentity,
    StepExecution,
    TaskStatus,
    WorkspaceEvent,
    WorkspaceTask,
    effective_cooldown,
    validate_plugin_declarations,
)


class CarriedVocabularyTests(unittest.TestCase):
    """The correctness core carried from fleet RENAMED, not changed:
    values, sets and fences must match the fleet module one-to-one
    (the W5 test migration depends on this equivalence)."""

    def test_member_eligibility_states_pinned(self) -> None:
        # carried from fleet verbatim (W5 deleted the module; the
        # VALUES are the contract now)
        self.assertEqual(
            MemberEligibility.ALL,
            frozenset({"eligible", "waiting", "paused", "cooling", "retired"}),
        )
        self.assertEqual(
            MemberEligibility.LIVE,
            frozenset({"eligible", "waiting", "paused", "cooling"}),
        )

    def test_task_status_states_pinned(self) -> None:
        self.assertEqual(
            TaskStatus.ALL,
            frozenset(
                {"queued", "assigned", "running", "completed", "suspended", "failed", "cancelled"}
            ),
        )
        self.assertEqual(TaskStatus.ALWAYS_TERMINAL, frozenset({"completed", "cancelled"}))

    def test_execution_mode_renames_group_managed(self) -> None:
        # v10 migrates fleet "group_managed" -> workspace "managed"
        self.assertEqual(ExecutionMode.MANAGED, "managed")
        self.assertNotIn("group_managed", ExecutionMode.ALL)

    def test_generation_fence_carried(self) -> None:
        task = WorkspaceTask(
            id="t1", workspace="alpha", task_id="grind", account="acct", generation=3
        )
        self.assertTrue(task.matches_generation(3))
        self.assertFalse(task.matches_generation(4))

    def test_cooldown_record_carried_with_new_scopes(self) -> None:
        rec = CooldownRecord(
            scope_type=CooldownScopeType.WORKSPACE,
            scope_id="alpha",
            reason="observed limit",
            started_at=10.0,
            expires_at=20.0,
        )
        self.assertTrue(rec.is_active(now=15.0))
        self.assertFalse(rec.is_active(now=25.0))
        with self.assertRaises(InvalidDefinition):
            CooldownRecord(
                scope_type="group",  # the fleet-era name must NOT validate
                scope_id="alpha",
                reason="x",
                started_at=0.0,
                expires_at=1.0,
            )

    def test_effective_cooldown_most_restrictive(self) -> None:
        a = CooldownRecord("member", "acct", "short", 0.0, 10.0)
        b = CooldownRecord("workspace", "alpha", "long", 0.0, 30.0)
        self.assertIs(effective_cooldown([a, b], now=5.0), b)
        self.assertIsNone(effective_cooldown([a], now=15.0))


class ChannelModelTests(unittest.TestCase):
    def test_descriptor_requires_digit_ids(self) -> None:
        with self.assertRaises(InvalidDefinition):
            ChannelDescriptor(channel_id="not-digits")
        with self.assertRaises(InvalidDefinition):
            ChannelDescriptor(channel_id="500", guild_id="abc")

    def test_only_available_is_usable(self) -> None:
        for state in ChannelValidity.ALL:
            desc = ChannelDescriptor(channel_id="500", validity=state)
            self.assertEqual(desc.usable, state == ChannelValidity.AVAILABLE)

    def test_binding_validates_ids(self) -> None:
        self.assertIsInstance(
            ChannelBinding(account="a", plugin="p", channel_id="500"), ChannelBinding
        )
        with self.assertRaises(InvalidDefinition):
            ChannelBinding(account="a", plugin="p", channel_id="500.0")


class ChainModelTests(unittest.TestCase):
    def _steps(self) -> tuple[ChainStep, ...]:
        return (
            ChainStep(
                position=1,
                plugin="grinder",
                on={"plugin": "grinder", "task_action": "message", "event": "task_completed"},
            ),
            ChainStep(
                position=2,
                plugin="cleaner",
                on={"signal": "grinder:claim_done"},
                gate={"setting": "auto_clean", "equals": True},
                emit="done",
            ),
        )

    def test_valid_definition(self) -> None:
        chain = ChainDefinition(name="claim-cleanup", steps=self._steps())
        self.assertEqual([s.position for s in chain.steps], [1, 2])
        self.assertEqual(chain.on_skip, "continue")

    def test_specific_account_requires_target(self) -> None:
        with self.assertRaises(InvalidDefinition):
            ChainStep(
                position=1,
                plugin="p",
                routing=RoutingPolicy.SPECIFIC_ACCOUNT,
                on={"plugin": "p", "task_action": "message"},
            )
        step = ChainStep(
            position=1,
            plugin="p",
            routing=RoutingPolicy.SPECIFIC_ACCOUNT,
            target="acct",
            on={"plugin": "p", "task_action": "message"},
        )
        self.assertEqual(step.target, "acct")

    def test_step_trigger_must_be_exactly_one(self) -> None:
        with self.assertRaises(InvalidDefinition):  # neither
            ChainStep(position=1, plugin="p")
        with self.assertRaises(InvalidDefinition):  # both
            ChainStep(
                position=1,
                plugin="p",
                on={"signal": "a:b", "task_action": "message", "plugin": "p"},
            )
        with self.assertRaises(InvalidDefinition):  # action without plugin
            ChainStep(position=1, plugin="p", on={"task_action": "message"})

    def test_positions_unique_and_ascending(self) -> None:
        with self.assertRaises(InvalidDefinition):
            ChainDefinition(
                name="x",
                steps=(
                    ChainStep(position=2, plugin="p", on={"task_action": "message"}),
                    ChainStep(position=1, plugin="q", on={"task_action": "message"}),
                ),
            )

    def test_empty_chain_rejected(self) -> None:
        with self.assertRaises(InvalidDefinition):
            ChainDefinition(name="x", steps=())

    def test_run_captures_snapshot(self) -> None:
        chain = ChainDefinition(name="x", steps=self._steps())
        run = ChainRun(id="r1", chain_id="c1", workspace="alpha", definition=chain)
        self.assertEqual(run.status, ChainRunStatus.RUNNING)
        self.assertEqual(run.definition, chain)
        with self.assertRaises(InvalidDefinition):
            # deliberately wrong runtime type: the model must reject it
            ChainRun(
                id="r1",
                chain_id="c1",
                workspace="alpha",
                definition="not-a-snapshot",  # type: ignore[arg-type]
            )

    def test_step_execution_identity(self) -> None:
        ex = StepExecution(run_id="r1", position=1, attempt=2, target="acct")
        self.assertEqual(ex.outcome, "")
        with self.assertRaises(InvalidDefinition):
            StepExecution(run_id="r1", position=0)
        with self.assertRaises(InvalidDefinition):
            StepExecution(run_id="r1", position=1, outcome="flying")


class SignalTests(unittest.TestCase):
    def test_namespacing_added_at_consumption(self) -> None:
        sig = SignalIdentity(account="a", plugin="grinder", name="claim_done")
        self.assertEqual(sig.namespaced, "grinder:claim_done")

    def test_bare_name_rule(self) -> None:
        with self.assertRaises(InvalidDefinition):
            SignalIdentity(account="a", plugin="grinder", name="grinder:claim_done")

    def test_payload_must_be_dict(self) -> None:
        with self.assertRaises(InvalidDefinition):
            # deliberately wrong runtime type: the model must reject it
            SignalIdentity(
                account="a",
                plugin="p",
                name="n",
                payload=["not", "a", "dict"],  # type: ignore[arg-type]
            )


class PresetModelTests(unittest.TestCase):
    def test_scope_owner_consistency(self) -> None:
        with self.assertRaises(InvalidDefinition):
            PresetRecord(
                preset_id="p",
                scope="workspace",
                plugin="x",
                name="n",
                version=1,
                schema_version=1,
                settings={},
            )
        with self.assertRaises(InvalidDefinition):
            PresetRecord(
                preset_id="p",
                scope="global",
                plugin="x",
                name="n",
                version=1,
                schema_version=1,
                settings={},
                owner_id="someone",
            )

    def test_uniqueness_scoped_to_owner(self) -> None:
        a = PresetRecord(
            preset_id="p1",
            scope="account",
            plugin="x",
            name=" Setup ",
            owner_id="acct",
            version=1,
            schema_version=1,
            settings={},
        )
        b = PresetRecord(
            preset_id="p2",
            scope="account",
            plugin="x",
            name="setup",
            owner_id="acct",
            version=1,
            schema_version=1,
            settings={},
        )
        other_owner = PresetRecord(
            preset_id="p3",
            scope="account",
            plugin="x",
            name="setup",
            owner_id="other",
            version=1,
            schema_version=1,
            settings={},
        )
        # normalized-name collision within one owner, distinct across owners
        self.assertEqual(a.uniqueness_key, b.uniqueness_key)
        self.assertNotEqual(a.uniqueness_key, other_owner.uniqueness_key)

    def test_provenance_shape(self) -> None:
        self.assertIsInstance(
            Provenance(source_preset_id="p", source_version=2, applied_at=1.0), Provenance
        )
        with self.assertRaises(InvalidDefinition):
            Provenance(source_preset_id="p", source_version=0, applied_at=1.0)


class DeclarationTests(unittest.TestCase):
    def test_clean_declarations(self) -> None:
        class Clean:
            EMITS = ("claim_done",)
            FOLLOWS = (FollowSpec(handler="on_claim", signal="grinder:claim_done"),)

            def on_claim(self) -> None: ...

        self.assertEqual(validate_plugin_declarations(Clean), [])

    def test_missing_handler_is_a_problem(self) -> None:
        class Broken:
            EMITS = ()
            FOLLOWS = (FollowSpec(handler="nope", task_action="message"),)

        problems = validate_plugin_declarations(Broken)
        self.assertEqual(len(problems), 1)
        self.assertIn("nope", problems[0])

    def test_namespaced_emit_is_a_problem(self) -> None:
        class Spoofy:
            EMITS = ("grinder:claim_done",)
            FOLLOWS = ()

        problems = validate_plugin_declarations(Spoofy)
        self.assertEqual(len(problems), 1)
        self.assertIn("namespacing", problems[0])

    def test_follow_spec_needs_exactly_one_trigger(self) -> None:
        with self.assertRaises(InvalidDefinition):
            FollowSpec(handler="h")  # neither
        with self.assertRaises(InvalidDefinition):
            FollowSpec(handler="h", signal="a:b", task_action="message")  # both


class EventEnvelopeTests(unittest.TestCase):
    def test_carried_envelope_shape(self) -> None:
        ev = WorkspaceEvent(kind="TaskAssigned", workspace="alpha", generation=2, reason="dispatch")
        self.assertIsNone(ev.task_id)
        with self.assertRaises(InvalidDefinition):
            WorkspaceEvent(kind="x", workspace="alpha", generation=0, reason="r")


if __name__ == "__main__":
    unittest.main()
