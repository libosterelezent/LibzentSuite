"""RestBucketManager + RestGate: Discord header intake, 429s, single-flight."""
from __future__ import annotations

import asyncio
import unittest

from core.rest_buckets import (
    RestBucketManager,
    RestFeedback,
    RestGate,
)


def fb(status=200, **headers):
    return RestFeedback(status=status, headers=headers)


class TestHeaderIntake(unittest.TestCase):
    def test_full_bot_headers_keyed_by_bucket(self) -> None:
        manager = RestBucketManager("alpha")
        decision = manager.after_call("message", fb(
            200,
            **{"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "3",
               "X-RateLimit-Reset-After": "1.5", "X-RateLimit-Bucket": "abcd1234",
               "X-RateLimit-Scope": "shared"},
        ))
        self.assertEqual(decision["mode"], "headers")
        self.assertEqual(decision["remaining"], 3.0)
        snap = manager.snapshot()
        self.assertEqual(snap["buckets"][0]["bucket"], "abcd1234")
        self.assertEqual(snap["buckets"][0]["remaining"], 3.0)
        self.assertEqual(snap["buckets"][0]["limit"], 5)

    def test_route_maps_to_bucket(self) -> None:
        manager = RestBucketManager("alpha")
        manager.set_route_spacing("message", 0.0)
        manager.after_call("message", fb(200, **{"X-RateLimit-Bucket": "b1", "X-RateLimit-Remaining": "2", "X-RateLimit-Reset-After": "1"}))
        self.assertEqual(manager.wait_before("message"), 0.0)
        # same bucket id from a different route shares the state
        manager.after_call("message:chan2", fb(200, **{"X-RateLimit-Bucket": "b1", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "5"}))
        self.assertGreater(manager.wait_before("message"), 4.0)

    def test_bucket_refills_after_reset(self) -> None:
        manager = RestBucketManager("alpha")
        manager.set_route_spacing("message", 0.0)
        manager.after_call("message", fb(200, **{"X-RateLimit-Bucket": "b", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.05"}))
        import time

        time.sleep(0.08)
        self.assertEqual(manager.wait_before("message"), 0.0)

    def test_sparse_user_token_headers(self) -> None:
        """User tokens only return Retry-After/Global/Scope on 429s — intake
        must not crash and must record the observation."""
        manager = RestBucketManager("alpha")
        decision = manager.after_call("message", fb(200, **{"X-RateLimit-Scope": "user"}))
        self.assertEqual(decision["mode"], "sparse")
        self.assertEqual(manager.snapshot()["headers_seen"], 1)

    def test_429_body_retry_after_sets_cooldown(self) -> None:
        manager = RestBucketManager("alpha")
        decision = manager.after_call("message", RestFeedback(
            status=429,
            headers={"Retry-After": "2.5", "X-RateLimit-Global": "false", "X-RateLimit-Scope": "user"},
            retry_after_body=2.5,
        ))
        self.assertEqual(decision["source"], "429")
        self.assertAlmostEqual(decision["cooldown"], 2.5, places=1)
        self.assertGreater(manager.wait_before("message"), 2.0)

    def test_global_429_invokes_callback(self) -> None:
        seen: list[float] = []
        manager = RestBucketManager("alpha", on_global_cooldown=seen.append)
        manager.after_call("message", RestFeedback(
            status=429,
            headers={"Retry-After": "3", "X-RateLimit-Global": "true"},
            retry_after_body=3.0,
        ))
        self.assertEqual(seen, [3.0])
        self.assertGreater(manager.snapshot()["global_cooldown_s"], 2.0)

    def test_absurd_reset_after_capped(self) -> None:
        from core.rest_buckets import MAX_TRUSTED_RESET_AFTER

        manager = RestBucketManager("alpha")
        decision = manager.after_call("message", fb(200, **{
            "X-RateLimit-Bucket": "b", "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset-After": "999999",
        }))
        self.assertLessEqual(decision["reset_after"], MAX_TRUSTED_RESET_AFTER)


class TestRestGate(unittest.IsolatedAsyncioTestCase):
    async def test_single_flight_serializes(self) -> None:
        manager = RestBucketManager("alpha")
        manager.set_route_spacing("fetch", 0.0)
        gate = RestGate(manager)
        inflight = 0
        peak = 0

        async def action(tag):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.03)
            inflight -= 1
            return tag

        async def run_one(tag: int) -> int:
            async with gate.call("fetch", lambda: action(tag)) as action_run:
                return await action_run()

        results = await asyncio.gather(*[run_one(i) for i in range(4)])
        self.assertEqual(peak, 1)  # ERROR-AUDIT: single in-flight REST call
        self.assertEqual(sorted(results), [0, 1, 2, 3])

    async def test_gate_applies_header_driven_wait(self) -> None:
        manager = RestBucketManager("alpha")
        manager.set_route_spacing("fetch", 0.0)
        gate = RestGate(manager)
        manager.after_call("fetch", fb(200, **{"X-RateLimit-Bucket": "f", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "0.15"}))
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with gate.call("fetch", lambda: asyncio.sleep(0, result="x")) as action:
            result = await action()
        self.assertEqual(result, "x")
        self.assertGreaterEqual(loop.time() - started, 0.13)

    async def test_action_exception_propagates_and_lock_released(self) -> None:
        manager = RestBucketManager("alpha")
        manager.set_route_spacing("fetch", 0.0)
        gate = RestGate(manager)

        async def boom():
            raise RuntimeError("rest exploded")

        with self.assertRaises(RuntimeError):
            async with gate.call("fetch", boom) as action:
                await action()
        # lock released: a second call proceeds (and its error surfaces too)
        async def ok():
            return "fine"

        async with gate.call("fetch", ok) as action:
            self.assertEqual(await action(), "fine")


if __name__ == "__main__":
    unittest.main()
