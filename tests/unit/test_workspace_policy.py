"""WorkspacePolicy tests (W1) — the 7-check order, pure over facts.

Each check fires in order with a distinct reason; the NEW seventh
gate (plugin assigned/enabled/gate/channel) is the workspace-era
addition and must produce "visible skip" reasons.
"""

from __future__ import annotations

import unittest

from core.workspace import (
    CooldownRecord,
    CooldownScopeType,
    DispatchFacts,
    MemberEligibility,
    TaskStatus,
    WorkspacePolicy,
)

POLICY = WorkspacePolicy()


def facts(**overrides) -> DispatchFacts:
    """A fully-passing member dispatch; tests override one axis."""
    base: dict[str, object] = {
        "framework_safe": True,
        "workspace_name": "alpha",
        "workspace_enabled": True,
        "workspace_paused": False,
        "workspace_generation": 7,
        "member_eligibility": MemberEligibility.ELIGIBLE,
        "account_connected": True,
        "cooldowns": (),
        "now": 100.0,
        "task_status": TaskStatus.ASSIGNED,
        "task_held_by": "acct",
        "task_generation": 7,
        "prerequisites_met": True,
        "plugin_assigned": True,
        "plugin_enabled": True,
        "gate_passed": True,
        "channel_binding_usable": True,
        "account": "acct",
        "task_label": "grind-1",
    }
    base.update(overrides)
    return DispatchFacts(**base)


class OrderAndBoundariesTests(unittest.TestCase):
    def test_happy_path_allowed_with_no_retry_at(self) -> None:
        d = POLICY.evaluate(facts())
        self.assertTrue(d.allowed)
        self.assertIn("passed", d.reason)
        self.assertIsNone(d.retry_at)

    def test_non_member_untouched(self) -> None:
        d = POLICY.evaluate(facts(workspace_name=None))
        self.assertTrue(d.allowed)
        self.assertIn("not workspace-managed", d.reason)

    def test_framework_safety_first_even_when_all_else_broken(self) -> None:
        d = POLICY.evaluate(
            facts(framework_safe=False, workspace_paused=True, member_eligibility="paused")
        )
        self.assertFalse(d.allowed)
        self.assertIn("framework safety", d.reason)


class CarriedChecksTests(unittest.TestCase):
    def test_paused_beats_eligibility(self) -> None:
        d = POLICY.evaluate(facts(workspace_paused=True, member_eligibility="paused"))
        self.assertFalse(d.allowed)
        self.assertIn("paused", d.reason)

    def test_disabled(self) -> None:
        d = POLICY.evaluate(facts(workspace_enabled=False))
        self.assertIn("disabled", d.reason)

    def test_not_connected(self) -> None:
        d = POLICY.evaluate(facts(account_connected=False))
        self.assertIn("not connected", d.reason)

    def test_ineligible_member(self) -> None:
        d = POLICY.evaluate(facts(member_eligibility=MemberEligibility.COOLING))
        self.assertIn("cooling", d.reason)

    def test_cooldown_most_restrictive_wins_with_retry_at(self) -> None:
        short = CooldownRecord(CooldownScopeType.MEMBER, "acct", "short", 0.0, 110.0)
        long = CooldownRecord(CooldownScopeType.WORKSPACE, "alpha", "observed limit", 0.0, 240.0)
        d = POLICY.evaluate(facts(cooldowns=(short, long)))
        self.assertFalse(d.allowed)
        self.assertIn("workspace:alpha", d.reason)
        self.assertEqual(d.retry_at, 240.0)

    def test_expired_cooldown_ignored(self) -> None:
        expired = CooldownRecord(CooldownScopeType.MEMBER, "acct", "old", 0.0, 99.0)
        self.assertTrue(POLICY.evaluate(facts(cooldowns=(expired,))).allowed)

    def test_task_held_by_another(self) -> None:
        d = POLICY.evaluate(facts(task_held_by="someone-else"))
        self.assertIn("held by", d.reason)

    def test_task_not_assigned_status(self) -> None:
        d = POLICY.evaluate(facts(task_status=TaskStatus.RUNNING))
        self.assertIn("running", d.reason)

    def test_generation_fence(self) -> None:
        d = POLICY.evaluate(facts(task_generation=6, workspace_generation=7))
        self.assertFalse(d.allowed)
        self.assertIn("stale dispatch fenced", d.reason)

    def test_prerequisites(self) -> None:
        d = POLICY.evaluate(facts(prerequisites_met=False))
        self.assertIn("prerequisites", d.reason)


class NewPluginChannelGateTests(unittest.TestCase):
    """Check 7 — the 'if settings/plugins allows it' of the goal."""

    def test_plugin_not_assigned_visible_skip(self) -> None:
        d = POLICY.evaluate(facts(plugin_assigned=False))
        self.assertFalse(d.allowed)
        self.assertIn("visible skip", d.reason)
        self.assertIn("not assigned", d.reason)

    def test_plugin_disabled_visible_skip(self) -> None:
        d = POLICY.evaluate(facts(plugin_enabled=False))
        self.assertIn("disabled", d.reason)
        self.assertIn("visible skip", d.reason)

    def test_gate_denies_visible_skip(self) -> None:
        d = POLICY.evaluate(facts(gate_passed=False))
        self.assertIn("chain gate", d.reason)

    def test_channel_unusable_visible_skip_with_reason(self) -> None:
        d = POLICY.evaluate(
            facts(channel_binding_usable=False, channel_binding_reason="channel deleted")
        )
        self.assertFalse(d.allowed)
        self.assertIn("no usable channel binding", d.reason)
        self.assertIn("channel deleted", d.reason)

    def test_gate_runs_after_carried_checks(self) -> None:
        # a cooling member AND an unassigned plugin: the carried check
        # reports first (the order is the contract)
        d = POLICY.evaluate(
            facts(member_eligibility=MemberEligibility.COOLING, plugin_assigned=False)
        )
        self.assertIn("cooling", d.reason)


if __name__ == "__main__":
    unittest.main()
