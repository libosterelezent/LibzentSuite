"""Safety floor + warnings engine."""
from __future__ import annotations

import unittest

from core.rate_limiter import RateLimiterService, RateRule
from core.warnings import (
    AGGRESSIVE_INTERVAL,
    MIN_ACTION_INTERVAL,
    UnsafeRateRuleError,
    check_rule_safety,
    enforce_floor,
    evaluate_limiter,
    format_warnings,
)


class TestFloorEnforcement(unittest.TestCase):
    def test_floor_rejects_fast_rules(self) -> None:
        for rule in ("10/5s", "4/2s", "2/2s", "1/1s"):  # all < 1.5s/action
            with self.assertRaises(UnsafeRateRuleError) as ctx:
                enforce_floor("message", RateRule.parse(rule), account="alpha")
            self.assertIn("safety floor", str(ctx.exception))

    def test_floor_allows_safe_rules(self) -> None:
        enforce_floor("message", RateRule.parse("8/12s"))          # exactly 1.5s
        enforce_floor("message", RateRule.parse("1/1.5s"), account="alpha")
        enforce_floor("message", RateRule.parse("5/12s"), account="alpha")

    def test_error_message_mentions_target_and_hint(self) -> None:
        with self.assertRaises(UnsafeRateRuleError) as ctx:
            enforce_floor("message", RateRule.parse("10/5s"), account="alpha")
        message = str(ctx.exception)
        self.assertIn("alpha", message)
        self.assertIn("1.5s", message)


class TestWarningsEvaluation(unittest.TestCase):
    def test_comfortable_config_has_no_warnings(self) -> None:
        from core.rate_limiter import RateRule

        limiter = RateLimiterService(
            default_rules={"message": RateRule(3, 12, jitter=0.2)}, jitter_seed=1
        )
        limiter.set_delay("message", 0.8, 2.4)
        self.assertEqual(evaluate_limiter(limiter), [])

    def test_aggressive_rule_warns(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "5/8s"})  # 1.6s/action
        warnings = evaluate_limiter(limiter)
        self.assertTrue(any(w.code == "aggressive" for w in warnings))

    def test_missing_delay_and_jitter_flagged(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "5/12s"})
        codes = {w.code for w in evaluate_limiter(limiter)}
        self.assertIn("no_human_delay", codes)
        self.assertIn("no_jitter", codes)

    def test_narrow_delay_range_flagged(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "5/12s"})
        limiter.set_delay("message", 1.0, 1.1)
        codes = {w.code for w in evaluate_limiter(limiter)}
        self.assertIn("narrow_delay", codes)

    def test_per_account_override_reviewed(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "5/12s"})
        limiter.set_delay("message", 0.8, 2.4)
        limiter.set_account_rule("alpha", "message", "4/3s")  # 0.75s/action — danger
        warnings = evaluate_limiter(limiter)
        self.assertTrue(any(w.code == "below_floor" and "alpha" in w.message for w in warnings))

    def test_check_rule_safety_levels(self) -> None:
        self.assertEqual(check_rule_safety(RateRule.parse("10/5s")).level, "danger")
        self.assertEqual(check_rule_safety(RateRule.parse("4/8s")).level, "warn")
        self.assertIsNone(check_rule_safety(RateRule.parse("3/12s")))

    def test_format_warnings_empty_and_filled(self) -> None:
        self.assertIn("no safety warnings", format_warnings([])[0])
        filled = format_warnings(evaluate_limiter(RateLimiterService(default_rules={"message": "5/12s"})))
        self.assertTrue(any("no human-like delay" in line for line in filled))


if __name__ == "__main__":
    unittest.main()
