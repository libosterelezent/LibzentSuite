"""Round-2 audit pin: ``control_mention`` scenario checks can FAIL.

tests/simulation/test_scenarios.py pins pass+violation pairs for five of
the six scenarios; ``control_mention`` was only exercised through the
full-stack runner (where every check passing tells you nothing about
whether the checks could ever fail). These pins drive
``_checks_control_mention`` directly over fabricated (audit, world)
states, including the owner-gating violations and the void-guard.
"""
from __future__ import annotations

import unittest

from adapters.simulator.scenarios import (
    ACCOUNT_NAME,
    SCENARIOS,
)
from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_OWNER_ID,
    SimWorld,
)

STRANGER = 4242


def _world_with_flow():
    """Base world + an owner-driven status exchange already in history."""
    world = SimWorld()
    SCENARIOS["control_mention"].world_setup(world)
    world.add_user(STRANGER, "stranger")
    world.deliver(
        SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "<@2001> status")
    world.deliver(
        SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
        "alpha up 42s, tasks 0 queued",
    )
    return world


def _control_audit():
    return [{
        "account": ACCOUNT_NAME,
        "direction": "out",
        "action": "message",
        "ts": 10.0,
        "content": "status",
        "note": "plugin=control",
    }]


def _stranger_estop(world):
    return world.deliver(
        SYNTHETIC_GUILD_CHANNEL, STRANGER, "<@2001> estop")


class TestControlMentionChecksPure(unittest.TestCase):
    def setUp(self) -> None:
        self.checks = SCENARIOS["control_mention"].checks

    def test_passes_on_complete_owner_flow(self) -> None:
        world = _world_with_flow()
        _stranger_estop(world)
        self.assertEqual(self.checks(_control_audit(), world), [])

    def test_fails_when_owner_mention_has_no_reply(self) -> None:
        world = SimWorld()
        SCENARIOS["control_mention"].world_setup(world)
        world.add_user(STRANGER, "stranger")
        _stranger_estop(world)
        failures = self.checks([], world)
        self.assertTrue(
            any("no reply" in f for f in failures), failures)

    def test_fails_when_stranger_is_answered(self) -> None:
        world = _world_with_flow()
        estop = _stranger_estop(world)
        world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
            "EMERGENCY STOP engaged", reference_message_id=estop.id,
        )
        failures = self.checks(_control_audit(), world)
        self.assertTrue(
            any("non-owner mention was answered" in f for f in failures),
            failures,
        )
        self.assertTrue(
            any("replied after a non-owner mention" in f for f in failures),
            failures,
        )

    def test_flags_void_check_when_stranger_never_landed(self) -> None:
        world = _world_with_flow()  # no stranger estop delivered
        failures = self.checks(_control_audit(), world)
        self.assertTrue(
            any("never landed" in f for f in failures), failures)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
