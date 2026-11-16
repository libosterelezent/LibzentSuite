"""Unit tests for core/rate_limit_manager/warnings.py (A4).

Covers the warning taxonomy (aggressive / no_jitter / narrow_jitter / floor),
the danger>warn>info ordering, and the manager's warnings() integration.
"""
from __future__ import annotations

import unittest

from core.event_bus import EventBus
from core.rate_limit_manager import MIN_ACTION_INTERVAL, RateLimitManager, RatePolicy
from core.rate_limit_manager.warnings import (
    describe_scope,
    evaluate_policies,
    evaluate_policy,
    floor_warning,
    sort_warnings,
)


class StubLogger:
    """LogService-shaped capture double."""

    def __init__(self) -> None:
        self.entries = []

    def info(self, source, message, **ctx):
        self.entries.append(("INFO", source, message))

    def warning(self, source, message, **ctx):
        self.entries.append(("WARNING", source, message))

    def error(self, source, message, **ctx):
        self.entries.append(("ERROR", source, message))


def make_manager():
    return RateLimitManager(EventBus(), StubLogger(), None)


def codes(warnings, level=None):
    return [w["code"] for w in warnings if level is None or w["level"] == level]


class TestEvaluatePolicy(unittest.TestCase):
    """Pure-function verdicts for one policy."""

    def test_below_floor_is_danger(self):
        policy = RatePolicy("m", 0.2, 0.5, 2.0)  # hand-edited below the floor
        out = evaluate_policy(policy, "global", None, 0.5)
        self.assertEqual(codes(out), ["below_floor"])
        self.assertEqual(out[0]["level"], "danger")
        self.assertIn("0.20s", out[0]["message"])
        self.assertIn("0.50s", out[0]["message"])

    def test_at_floor_is_clean(self):
        # at the floor exactly is legal (the setter enforces >= floor)
        self.assertEqual(
            evaluate_policy(RatePolicy("m", 0.5, 0.5, 2.0), "global", None, 0.5), []
        )

    def test_fast_but_above_floor_is_clean(self):
        """Discord-driven pacing: intervals between the floor and 3s are a
        LEGITIMATE configuration — the old 'aggressive' lint fired on them
        forever (residual noise). Only below-floor is a warning now."""
        for interval in (0.05, 0.5, 1.0, 2.0, 2.99):
            self.assertEqual(
                evaluate_policy(RatePolicy("m", interval, 0.5, 2.0),
                                "global", None, 0.05), [],
                f"interval {interval}s above floor must be clean")

    def test_no_jitter(self):
        out = evaluate_policy(RatePolicy("m", 4.0), "account", "alpha", 0.05)
        self.assertEqual(codes(out), ["no_jitter"])
        self.assertEqual(out[0]["level"], "info")

    def test_narrow_jitter(self):
        out = evaluate_policy(RatePolicy("m", 4.0, 0.5, 0.6), "global", None, 0.05)
        self.assertEqual(codes(out), ["narrow_jitter"])
        self.assertEqual(out[0]["level"], "info")
        # exactly at the spread threshold is fine
        self.assertEqual(
            evaluate_policy(RatePolicy("m", 4.0, 0.5, 0.8), "global", None, 0.05), []
        )

    def test_wide_jitter_sane_interval_is_clean(self):
        self.assertEqual(
            evaluate_policy(RatePolicy("m", 4.0, 0.5, 2.0), "global", None, 0.05), []
        )

    def test_below_floor_and_narrow_jitter_coexist(self):
        out = evaluate_policy(RatePolicy("m", 0.2, 0.1, 0.2), "global", None, 0.5)
        self.assertEqual(codes(out), ["below_floor", "narrow_jitter"])

    def test_entry_shape(self):
        out = evaluate_policy(RatePolicy("m", 2.0), "global", None, 0.05)
        for entry in out:
            self.assertEqual(set(entry), {"level", "code", "message"})
            self.assertIsInstance(entry["message"], str)


class TestFloorAndSorting(unittest.TestCase):
    """The always-present floor entry + danger>warn>info ordering."""

    def test_floor_warning_shape(self):
        entry = floor_warning(MIN_ACTION_INTERVAL)
        self.assertEqual(entry["level"], "info")
        self.assertEqual(entry["code"], "floor")
        self.assertIn("1.50s", entry["message"])

    def test_floor_not_prepended_to_evaluation(self):
        """No residual always-on entries: a clean configuration evaluates
        to an EMPTY list (the floor lives in the Safety Floor card)."""
        self.assertEqual(evaluate_policies([], 1.5), [])
        self.assertEqual(
            evaluate_policies(
                [(RatePolicy("m", 2.0, 0.5, 2.0), "global", None)], 1.5), [])

    def test_sort_warnings_danger_first(self):
        entries = [
            {"level": "info", "code": "no_jitter", "message": "b"},
            {"level": "warn", "code": "aggressive", "message": "c"},
            {"level": "danger", "code": "below_floor", "message": "a"},
            {"level": "info", "code": "floor", "message": "d"},
        ]
        ordered = sort_warnings(entries)
        self.assertEqual([e["level"] for e in ordered],
                         ["danger", "warn", "info", "info"])

    def test_sort_unknown_level_lasts(self):
        ordered = sort_warnings([
            {"level": "bizarre", "code": "x", "message": ""},
            {"level": "info", "code": "floor", "message": ""},
        ])
        self.assertEqual(ordered[-1]["level"], "bizarre")

    def test_describe_scope(self):
        self.assertEqual(describe_scope("global", None), "global")
        self.assertEqual(describe_scope("account", "alpha"), "account alpha")
        self.assertEqual(describe_scope("plugin", "roll"), "plugin roll")
        self.assertEqual(describe_scope("account_plugin", "a/p"), "a/p")


class TestManagerWarnings(unittest.TestCase):
    """RateLimitManager.warnings(): stored policies reviewed + sorted."""

    def test_empty_manager_is_clean(self):
        """No residual entries — an empty policy set yields no warnings
        (the floor is documented by the Safety Floor card, not here)."""
        self.assertEqual(make_manager().warnings(), [])

    def test_one_entry_per_stored_policy(self):
        manager = make_manager()
        manager.set_policy_values("message", 2.0)                       # no_jitter + faster_than_learned
        manager.set_policy_values("poke", 4.0, 0.5, 2.0)                # clean
        manager.set_policy_values("dm", 3.0, 0.5, 0.6, level="account", owner="alpha")
        warnings = manager.warnings()
        self.assertEqual(
            codes(warnings), ["faster_than_learned", "narrow_jitter", "no_jitter"])
        # entries sort danger > warn > info
        levels = [w["level"] for w in warnings]
        self.assertEqual(levels, ["warn", "info", "info"])
        # the message identifies the owning scope
        scoped = [w for w in warnings if "alpha" in w["message"]]
        self.assertEqual(len(scoped), 1)

    def test_hand_edited_below_floor_surfaces_as_danger(self):
        manager = make_manager()
        manager.set_policy_values("message", 2.0, 0.5, 2.0)   # legal at the 1.5s floor
        manager.set_floor(2.5)                                 # then the floor rises above it
        warnings = manager.warnings()
        self.assertEqual(codes(warnings), ["below_floor", "faster_than_learned"])
        self.assertEqual(warnings[0]["level"], "danger")
        self.assertIn("2.50s", warnings[0]["message"])


if __name__ == "__main__":
    unittest.main()
