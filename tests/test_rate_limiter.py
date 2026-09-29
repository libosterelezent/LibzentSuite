"""Rate limiter: rules, token math, two-layer caps, priority, jitter."""
from __future__ import annotations

import asyncio
import unittest

from core.rate_limiter import (
    ManualClock,
    Priority,
    RateLimiterService,
    RateRule,
    RateRuleError,
)


class TestRateRule(unittest.TestCase):
    def test_parse_valid(self) -> None:
        rule = RateRule.parse("8/12s")
        self.assertEqual((rule.actions, rule.window), (8, 12.0))
        self.assertEqual(str(rule), "8/12.0s")
        self.assertAlmostEqual(rule.refill_per_second, 8 / 12)

    def test_parse_invalid(self) -> None:
        for bad in ("abc", "0/12", "8/0", "8/", "/12", -1):
            with self.assertRaises(RateRuleError):
                RateRule.parse(bad)

    def test_jitter_bounds_validated(self) -> None:
        with self.assertRaises(RateRuleError):
            RateRule(5, 10, jitter=9.0)


class TestTokenMath(unittest.TestCase):
    def test_refill_with_manual_clock(self) -> None:
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "2/2"})
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNone(limiter.try_acquire("message", "alpha"))
        wait = limiter.next_wait("message", "alpha")
        self.assertGreater(wait, 0.99)
        self.assertLessEqual(wait, 1.01)
        clock.advance(1.0)
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))

    def test_global_layer_caps_all_accounts(self) -> None:
        clock = ManualClock()
        limiter = RateLimiterService(
            clock=clock,
            default_rules={"message": "3/1"},
            account_rules={("alpha", "message"): "5/1"},
        )
        for _ in range(3):
            self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        # alpha's own bucket still has tokens, but the shared global layer is empty
        self.assertIsNone(limiter.try_acquire("message", "alpha"))
        wait = limiter.next_wait("message", "alpha")
        self.assertGreater(wait, 0.32)
        self.assertLessEqual(wait, 0.34)
        clock.advance(0.34)
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))

    def test_other_account_blocked_by_global_layer(self) -> None:
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "1/5"})
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNone(limiter.try_acquire("message", "beta"))  # global bucket drained
        self.assertGreaterEqual(limiter.next_wait("message", "beta"), 4.9)

    def test_account_override_precedence(self) -> None:
        limiter = RateLimiterService(
            default_rules={"message": "8/12s"},
            account_rules={("alpha", "message"): "1/10s"},
        )
        self.assertEqual(limiter.rule_for("message", "alpha").actions, 1)
        self.assertEqual(limiter.rule_for("message", "beta").actions, 8)
        self.assertEqual(limiter.rule_for("message").actions, 8)
        limiter.set_account_rule("alpha", "message", "2/4s")
        self.assertEqual(limiter.rule_for("message", "alpha").actions, 2)
        self.assertTrue(limiter.clear_account_rule("alpha", "message"))
        self.assertIsNone(limiter.override_for("alpha", "message"))
        self.assertEqual(limiter.rule_for("message", "alpha").actions, 8)

    def test_set_default_rule_rebuilds_global_bucket(self) -> None:
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "2/1"})
        limiter.try_acquire("message", "alpha")
        limiter.try_acquire("message", "beta")
        self.assertIsNone(limiter.try_acquire("message", "alpha"))
        limiter.set_default_rule("message", "10/1s")  # fresh bucket with capacity 10
        for _ in range(8):
            self.assertIsNotNone(limiter.try_acquire("message", "alpha"))

    def test_stats_shape(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "3/10s"})
        limiter.try_acquire("message", "alpha")
        stats = limiter.stats()
        self.assertIn("message", stats["actions"])
        info = stats["actions"]["message"]
        self.assertEqual(info["default_rule"], "3/10.0s")
        self.assertEqual(info["global_bucket"]["capacity"], 3.0)
        self.assertLess(info["global_bucket"]["tokens"], 3.0)

    def test_unknown_action_raises(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/1"})
        with self.assertRaises(RateRuleError):
            limiter.rule_for("react")


class TestJitter(unittest.TestCase):
    def test_jitter_bounds(self) -> None:
        limiter = RateLimiterService(
            default_rules={"message": RateRule(5, 10, jitter=0.5)}, jitter_seed=7
        )
        for _ in range(50):
            jittered = limiter.jittered_wait("message", "alpha", 1.0)
            self.assertGreaterEqual(jittered, 1.0)
            self.assertLessEqual(jittered, 1.5)

    def test_zero_jitter_is_identity(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "5/10"})
        self.assertEqual(limiter.jittered_wait("message", "alpha", 1.25), 1.25)


class TestAsyncAcquire(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_blocks_until_refill(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/0.35"})
        first = limiter.try_acquire("message", "alpha")
        self.assertIsNotNone(first)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await limiter.acquire("message", "alpha")
        self.assertGreaterEqual(loop.time() - started, 0.30)

    async def test_priority_ordering(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/0.4"})
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        order: list[str] = []

        async def waiter(tag: str, priority: Priority) -> None:
            await limiter.acquire("message", "alpha", priority=priority)
            order.append(tag)

        low = asyncio.create_task(waiter("low", Priority.LOW))
        await asyncio.sleep(0.05)
        high = asyncio.create_task(waiter("high", Priority.HIGH))
        await asyncio.gather(low, high)
        self.assertEqual(order, ["high", "low"])


if __name__ == "__main__":
    unittest.main()
