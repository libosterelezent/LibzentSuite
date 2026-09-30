"""ADVERSARIAL RATE-LIMIT STRESS (audit 15/15, made permanent).

Attacks core/rate_limiter.py and its enforcement through
core/account.py (AccountSession.send_message) and core/api.py
(ControlAPI.set_limit floor) + dashboard/_route parity.

Coverage: throughput ceilings (no over-grant), two-layer global caps,
priority ordering without starvation, jitter/delay bounds and precedence,
safety-floor bypass attempts through every surface, session enforcement
ordering, edge cases (unknown actions, NaN amounts, non-finite rules/delays,
huge-delay wedging), and waiter cancellation hygiene.

Run with the suite: python3 -m unittest tests.test_rate_stress
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest

sys.path.insert(0, "/home/Liboster/Workspace/SlaughterMudae")

from core.account import AccountConfig, AccountSession, DiscordMessage
from core.api import ControlAPI
from core.events import EventBus
from core.rate_limiter import (
    GLOBAL_KEY,
    DelaySpec,
    ManualClock,
    Priority,
    RateLimiterService,
    RateRule,
    RateRuleError,
)
from core.warnings import (
    MIN_ACTION_INTERVAL,
    UnsafeRateRuleError,
    evaluate_limiter,
)
from dashboard.server import DashboardServer

ROOT = "/home/Liboster/Workspace/SlaughterMudae"


def drain(limiter: RateLimiterService, action: str, account: str | None = None, cap: int = 10_000):
    """Burn the bucket(s) dry via try_acquire (no pending waiters -> allowed)."""
    n = 0
    while limiter.try_acquire(action, account) is not None and n < cap:
        n += 1
    return n


# ---------------------------------------------------------------- 1. THROUGHPUT
class TestThroughput(unittest.IsolatedAsyncioTestCase):
    """100 concurrent acquires, REAL clock: no over-grant, all complete."""

    async def test_100_concurrent_no_overgrant(self) -> None:
        N, W = 25, 1.0                     # 25 grants / 1s window
        limiter = RateLimiterService(default_rules={"message": f"{N}/{W}s"})
        drained = drain(limiter, "message")            # remove burst allowance
        self.assertGreaterEqual(drained, N - 1)

        grants: list[float] = []

        async def waiter() -> None:
            g = await limiter.acquire("message", "alpha")
            grants.append(g.at)

        started = time.monotonic()
        await asyncio.wait_for(asyncio.gather(*[waiter() for _ in range(100)]), timeout=20.0)
        elapsed = time.monotonic() - started

        self.assertEqual(len(grants), 100, "all 100 must complete")
        ts = sorted(grants)
        # steady-state rate (exclude the very first positioning) — PRIMARY check
        span = ts[-1] - ts[0]
        rate = 99 / span
        cap = (N / W) * 1.15
        self.assertLessEqual(
            rate, cap,
            f"over-grant: observed {rate:.1f} grants/s, cap {cap:.2f} (N/W*1.15)",
        )
        # Sliding window: a token bucket may BANK up to `capacity` fractional
        # tokens under event-loop latency (observed min inter-grant delta
        # 0.035s < 0.04s), so the sound bound is the envelope
        # capacity + refill*1.15 per window — not the naive rate*1.15.
        for L in (0.5, 1.0, 2.0):
            for i, t in enumerate(ts):
                in_window = sum(1 for u in ts if t <= u < t + L)
                envelope = N + (N / W) * L * 1.15 + 2
                self.assertLessEqual(
                    in_window, envelope,
                    f"{in_window} grants inside {L}s window at t={t:.3f} "
                    f"(envelope {envelope:.1f}: burst bank + refill)",
                )
        # all grants distinct + correctly accounted
        self.assertEqual(len(set(ts)), 100)
        self.assertLess(span, elapsed + 0.001)


# ---------------------------------------------------------------- 2. TWO-LAYER
class TestTwoLayer(unittest.IsolatedAsyncioTestCase):
    """5 accounts on a tight GLOBAL bucket: the bus-wide cap must bind."""

    def test_combined_cap_deterministic_manual_clock(self) -> None:
        clock = ManualClock()
        limiter = RateLimiterService(
            clock=clock,
            default_rules={"message": "4/4s"},             # global: 1 grant/s
            account_rules={(a, "message"): "100/1s" for a in "abcde"},  # fast overrides
        )
        grants = 0
        for _ in range(100):                               # 10 simulated seconds
            clock.advance(0.1)
            for a in "abcde":
                if limiter.try_acquire("message", a) is not None:
                    grants += 1
        # global bucket can never yield more than capacity + refill = 4 + 10
        self.assertLessEqual(grants, 14, "global layer over-granted")
        self.assertGreaterEqual(grants, 12)
        # without the global cap the 5 overrides would allow ~5000 grants
        self.assertLess(grants, 60, "per-account overrides bypassed the global cap")

    async def test_combined_cap_real_clock_async(self) -> None:
        limiter = RateLimiterService(
            default_rules={"message": "10/2s"},            # global: 5/s
            account_rules={(a, "message"): "100/1s" for a in "abcde"},  # want 100/s each
        )
        drain(limiter, "message", "a")  # drain shared global bucket via any route
        got: dict[str, list[float]] = {a: [] for a in "abcde"}

        async def waiter(acct: str) -> None:
            for _ in range(4):
                g = await limiter.acquire("message", acct)
                got[acct].append(g.at)

        started = time.monotonic()
        await asyncio.wait_for(asyncio.gather(*[waiter(a) for a in "abcde"]), timeout=15.0)
        span = max(t for v in got.values() for t in v) - min(t for v in got.values() for t in v)
        rate = 19 / span                                   # 20 grants, minus first
        self.assertLessEqual(rate, 5.0 * 1.15, f"global cap broken: {rate:.2f}/s")
        for a in "abcde":
            self.assertEqual(len(got[a]), 4, f"account {a} starved")


# ---------------------------------------------------------------- 3. PRIORITY
class TestPriority(unittest.IsolatedAsyncioTestCase):
    """30 mixed-priority waiters on one slow bucket: strict order, no starvation."""

    async def test_mixed_priority_order_and_no_starvation(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/0.06s"})
        drain(limiter, "message", "alpha")
        results: list[tuple[float, int]] = []              # (grant.at, priority)

        async def waiter(prio: Priority) -> None:
            g = await limiter.acquire("message", "alpha", priority=prio)
            results.append((g.at, int(prio)))

        prios = [Priority.HIGH] * 10 + [Priority.NORMAL] * 10 + [Priority.LOW] * 10
        await asyncio.wait_for(
            asyncio.gather(*[waiter(p) for p in prios]), timeout=15.0
        )
        self.assertEqual(len(results), 30, "every waiter incl. LOW must finish")
        results.sort()
        last_high = max(at for at, p in results if p == 0)
        first_normal = min(at for at, p in results if p == 1)
        last_normal = max(at for at, p in results if p == 1)
        first_low = min(at for at, p in results if p == 2)
        self.assertLess(last_high, first_normal + 0.001, "NORMAL granted before HIGH drained")
        self.assertLess(last_normal, first_low + 0.001, "LOW granted before NORMAL drained")
        # monotone: priority never goes backwards over time
        seen = [p for _, p in results]
        self.assertEqual(seen, sorted(seen), "grant order violated (priority, seq)")

    async def test_late_high_preempts_queued_low(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/0.1s"})
        drain(limiter, "message", "alpha")
        times: list[float] = []

        async def waiter() -> None:
            g = await limiter.acquire("message", "alpha", priority=Priority.LOW)
            times.append(g.at)

        lows = [asyncio.create_task(waiter()) for _ in range(5)]
        await asyncio.sleep(0.15)                          # ~1 LOW granted
        high = asyncio.create_task(
            (lambda: limiter.acquire("message", "alpha", priority=Priority.HIGH))()
        )
        g = await asyncio.wait_for(high, timeout=5.0)
        lows_done = await asyncio.wait_for(asyncio.gather(*lows), timeout=5.0)
        after = [t for t in times if t > g.at]
        self.assertGreaterEqual(len(after), 3, "HIGH did not jump the LOW queue")


# ------------------------------------------------------------ 4. JITTER + DELAY
class TestJitterDelay(unittest.IsolatedAsyncioTestCase):
    def test_jitter_bounds_seeded(self) -> None:
        limiter = RateLimiterService(
            default_rules={"message": RateRule(5, 10, jitter=0.5)}, jitter_seed=7
        )
        for _ in range(200):
            j = limiter.jittered_wait("message", "alpha", 1.0)
            self.assertGreaterEqual(j, 1.0)
            self.assertLessEqual(j, 1.5)
        # max jitter 4.0 boundary
        big = RateLimiterService(
            default_rules={"message": RateRule(5, 10, jitter=4.0)}, jitter_seed=3
        )
        for _ in range(100):
            j = big.jittered_wait("message", None, 2.0)
            self.assertGreaterEqual(j, 2.0)
            self.assertLessEqual(j, 10.0)

    async def test_observed_acquire_wait_within_jitter_band(self) -> None:
        limiter = RateLimiterService(
            default_rules={"message": RateRule(1, 0.15, jitter=0.5)}, jitter_seed=5
        )
        drain(limiter, "message")
        t0 = time.monotonic()
        g = await asyncio.wait_for(limiter.acquire("message", "alpha"), timeout=2.0)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.148, "granted before tokens existed")
        self.assertLessEqual(elapsed, 0.15 * 1.5 + 0.08, "wait exceeded w*(1+j)")

    async def test_delay_sleep_bounds_and_grant_record(self) -> None:
        limiter = RateLimiterService(
            default_rules={"message": "200/1s"}, delay_seed=11
        )
        limiter.set_delay("message", 0.05, 0.12)
        for _ in range(6):
            t0 = time.monotonic()
            g = await asyncio.wait_for(limiter.acquire("message", "alpha"), timeout=2.0)
            dt = time.monotonic() - t0
            self.assertGreaterEqual(g.delay_applied, 0.05)
            self.assertLessEqual(g.delay_applied, 0.12)
            self.assertGreaterEqual(dt, g.delay_applied - 0.005, "delay sleep skipped")
            self.assertLessEqual(dt, g.delay_applied + 0.08)
        limiter.set_delay("message", 0.05, 0.05)           # min == max
        for _ in range(5):
            g = await limiter.acquire("message", "alpha")
            self.assertAlmostEqual(g.delay_applied, 0.05, places=6)

    async def test_account_delay_overrides_global(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "200/1s"}, delay_seed=2)
        limiter.set_delay("message", 0.10, 0.11)                   # global
        limiter.set_delay("message", 0.02, 0.03, account="beta")   # override
        self.assertEqual(str(limiter.delay_for("message", "beta")), "0.02s..0.03s")
        g_beta = await limiter.acquire("message", "beta")
        self.assertGreaterEqual(g_beta.delay_applied, 0.02)
        self.assertLessEqual(g_beta.delay_applied, 0.03)           # NOT the global range
        g_gamma = await limiter.acquire("message", "gamma")
        self.assertGreaterEqual(g_gamma.delay_applied, 0.10)
        self.assertLessEqual(g_gamma.delay_applied, 0.11)

    def test_negative_or_inverted_delay_bounds_rejected(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/10s"})
        with self.assertRaises(RateRuleError):
            limiter.set_delay("message", -0.1, 0.2)
        with self.assertRaises(RateRuleError):
            limiter.set_delay("message", 0.3, 0.1)
        with self.assertRaises(RateRuleError):
            DelaySpec(-1.0, -2.0)
        self.assertFalse(DelaySpec(0.0, 0.0).enabled)

    async def test_try_acquire_applies_no_delay(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "5/10s"})
        limiter.set_delay("message", 5.0, 6.0)            # would sleep 5s if applied
        t0 = time.monotonic()
        g = limiter.try_acquire("message", "alpha")
        dt = time.monotonic() - t0
        self.assertIsNotNone(g)
        self.assertEqual(g.delay_applied, 0.0)            # KB_17: sync path, no delay
        self.assertLess(dt, 0.05)

    def test_jitter_uses_effective_rule_not_max(self) -> None:
        # KB_05 claims "max of global/account jitters"; code uses the EFFECTIVE
        # (account override wins) rule's jitter. Document actual behavior:
        limiter = RateLimiterService(
            default_rules={"message": RateRule(5, 10, jitter=0.5)},
            account_rules={("alpha", "message"): RateRule(5, 10, jitter=0.0)},
            jitter_seed=9,
        )
        self.assertEqual(limiter.jittered_wait("message", "alpha", 1.0), 1.0)
        self.assertGreaterEqual(limiter.jittered_wait("message", "beta", 1.0), 1.0)


# -------------------------------------------------------------- 5. FLOOR BYPASS
class TestFloorBypass(unittest.IsolatedAsyncioTestCase):
    def _api(self, limiter: RateLimiterService) -> ControlAPI:
        return ControlAPI(None, limiter, None, None)      # set_limit needs only limiter

    async def test_unsafe_rules_rejected_via_api(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "8/12s"})
        api = self._api(limiter)
        attacks = [
            ("10/5s", None),          # 0.50s/action
            ("1/1.4s", None),         # 1.40s/action
            ("1/0.5s", None),         # 0.50s/action
            ("3/4.4s", None),         # 1.467s/action (float window trick)
            ("10/14.99s", None),      # 1.499s/action (closest float probe)
            ("2/2s", "alpha"),        # per-account route
            ("1/1.4s", "alpha"),      # per-account route
        ]
        for rule, account in attacks:
            with self.assertRaises(
                UnsafeRateRuleError,
                msg=f"set_limit accepted unsafe rule {rule!r} for {account!r}",
            ):
                await api.set_limit("message", rule, account)
        # fractional ACTIONS (0.9/1.3s) cannot parse -> rejected even earlier
        with self.assertRaises(RateRuleError):
            await api.set_limit("message", "0.9/1.3s", None)
        # limiter state untouched by every rejected attempt
        self.assertEqual(str(limiter.rule_for("message")), "8/12.0s")

    async def test_boundary_rule_exactly_at_floor_allowed(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "8/12s"})
        api = self._api(limiter)
        out = await api.set_limit("message", "1/1.5s")     # exactly MIN_ACTION_INTERVAL
        self.assertEqual(out["rule"], "1/1.5s")
        self.assertEqual(MIN_ACTION_INTERVAL, 1.5)

    def test_rate_rule_parse_is_permissive_by_design(self) -> None:
        # the PARSER accepts sub-floor rules (only the API enforces the floor)
        for rule in ("10/5s", "1/1.4s", "1/0.5s", "10/14.99s"):
            r = RateRule.parse(rule)
            self.assertIsInstance(r, RateRule)
        with self.assertRaises(RateRuleError):             # fractional actions unsupported
            RateRule.parse("0.9/1.3s")
        limiter = RateLimiterService(default_rules={"message": "8/12s"})
        applied = limiter.set_default_rule("message", "1/1.4s")   # direct = allowed
        self.assertEqual(str(applied), "1/1.4s")
        codes = [w.code for w in evaluate_limiter(limiter)]
        self.assertIn("below_floor", codes)                # KB_17: flagged, not refused

    async def test_dashboard_route_cannot_bypass_floor(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "8/12s"})
        api = self._api(limiter)
        server = DashboardServer(api, token=None, port=0)  # no start(): _route direct
        for body in (
            {"action": "message", "rule": "10/5s", "account": None},
            {"action": "message", "rule": "1/1.4s", "account": None},
            {"action": "message", "rule": "10/5s", "account": "alpha"},
            {"action": "message", "rule": "0.9/1.3s", "account": "alpha"},
        ):
            status, payload = await server._route("POST", "/api/limit", body, {})
            self.assertEqual(status, 400, f"route accepted {body}")
            self.assertFalse(payload["ok"])
            self.assertIn("error", payload)
        self.assertEqual(str(limiter.rule_for("message")), "8/12.0s")


# ----------------------------------------------------------- 6. SESSION ENFORCE
class TestSessionEnforcement(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_acquires_first_and_paces(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/0.2s"})

        class CountingClient:
            user_id = 42

            def __init__(self) -> None:
                self.sends: list[float] = []
                self.waits_at_send: list[float] = []
                self.on_message = None
                self.on_disconnect = None

            async def send_message(self, channel_id: int, content: str) -> DiscordMessage:
                # proof the limiter token was consumed BEFORE the client send
                self.waits_at_send.append(limiter.next_wait("message", "alpha"))
                self.sends.append(time.monotonic())
                return DiscordMessage(1, channel_id, 7, content)

        session = AccountSession(
            AccountConfig(name="alpha"), CountingClient(), EventBus(), limiter
        )
        t0 = time.monotonic()
        for i in range(5):
            await asyncio.wait_for(session.send_message(500, f"msg{i}"), timeout=5.0)
        total = time.monotonic() - t0
        self.assertEqual(len(session._client.sends), 5)
        # 5 sends on 1/0.2s: 4 enforced gaps -> >= 0.8s total (small sched slack)
        self.assertGreaterEqual(total, 0.78, "send_message bypassed the limiter")
        for w in session._client.waits_at_send:
            self.assertGreater(
                w, 0.15, "client send saw a full bucket: acquire ran AFTER send"
            )


# -------------------------------------------------------------- 7. EDGE CASES
class TestEdgeCases(unittest.IsolatedAsyncioTestCase):
    def test_unknown_action_rate_rule_error_everywhere(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/1s"})
        for fn in (
            lambda: limiter.rule_for("react"),
            lambda: limiter.next_wait("react", "alpha"),
            lambda: limiter.try_acquire("react", "alpha"),
            lambda: limiter.jittered_wait("react", "alpha", 1.0),
        ):
            with self.assertRaises(RateRuleError):
                fn()

    async def test_acquire_unknown_action_raises_not_keyerror(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/1s"})
        with self.assertRaises(RateRuleError):
            await asyncio.wait_for(limiter.acquire("react", "alpha"), timeout=2.0)
        await asyncio.sleep(0.02)                          # let finally{} prune run
        self.assertEqual(limiter.stats()["pending"], {}, "failed acquire leaked a pending entry")

    def test_manual_clock_negative_advance_no_token_exploit(self) -> None:
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "2/2s"})
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNone(limiter.try_acquire("message", "alpha"))
        clock.advance(-100.0)                              # time travel backwards
        self.assertIsNone(limiter.try_acquire("message", "alpha"))
        self.assertLessEqual(limiter.next_wait("message", "alpha"), 1.01)
        clock.advance(200.0)                               # ...and forwards again
        for _ in range(2):                                 # exactly capacity, no more
            self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNone(limiter.try_acquire("message", "alpha"))

    def test_amount_two_try_acquire_vs_acquire(self) -> None:
        # try_acquire: multi-token reservation honored on BOTH layers
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "5/10s"})
        g1 = limiter.try_acquire("message", "alpha", amount=2)
        g2 = limiter.try_acquire("message", "alpha", amount=2)
        self.assertIsNotNone(g1)
        self.assertIsNotNone(g2)
        self.assertEqual((g1.amount, g2.amount), (2.0, 2.0))
        self.assertIsNone(limiter.try_acquire("message", "alpha", amount=2))  # 1 left
        self.assertAlmostEqual(limiter.next_wait("message", "alpha", 2), 2.0, places=3)

    async def test_amount_two_acquire_consistent(self) -> None:
        # Was inconsistent pre-patch (acquire ignored amount, reserved 1 token).
        # Now amount flows through _Entry -> _grant_ready -> Grant.
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "5/10s"})
        g = await limiter.acquire("message", "alpha", amount=2)
        self.assertEqual(g.amount, 2.0, "acquire(amount=2) mis-reported the amount")
        snap = limiter.stats()["actions"]["message"]
        self.assertEqual(snap["global_bucket"]["tokens"], 3.0, "acquire(amount=2) charged 1 token")

    def test_amount_validation_rejects_nonfinite(self) -> None:
        # _validate_amount uses `amount <= 0` and `amount > capacity` — both
        # False for NaN, so NaN slips through and poisons the bucket.
        limiter = RateLimiterService(clock=ManualClock(), default_rules={"message": "8/12s"})
        with self.assertRaises(RateRuleError):
            limiter.try_acquire("message", "alpha", amount=float("nan"))
        with self.assertRaises(RateRuleError):
            limiter.try_acquire("message", "alpha", amount=float("inf"))

    def test_nan_amount_does_not_rearm_bucket_unlimited(self) -> None:
        # Frozen clock: after one amount=NaN take, tokens become NaN and every
        # later check `next_wait > 0` is False (NaN compare) -> endless grants.
        clock = ManualClock()
        limiter = RateLimiterService(clock=clock, default_rules={"message": "8/12s"})
        for _ in range(8):
            self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNone(limiter.try_acquire("message", "alpha"))          # drained
        # FIXED (stress audit): NaN amounts are rejected outright now
        with self.assertRaises(Exception):
            limiter.try_acquire("message", "alpha", amount=float("nan"))
        self.assertIsNone(limiter.try_acquire("message", "alpha"))          # still drained

    async def test_acquire_amount_nan_rejected(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "8/12s"})
        with self.assertRaises(RateRuleError):
            await asyncio.wait_for(
                limiter.acquire("message", "alpha", amount=float("nan")), timeout=2.0
            )

    def test_rate_rule_rejects_nonfinite_window(self) -> None:
        # regression guard for the concurrent patch (math.isfinite now checked)
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(RateRuleError):
                RateRule(1, bad)

    def test_delay_spec_rejects_nonfinite_bounds(self) -> None:
        # DelaySpec only checks < 0 and max < min; inf/nan pass both.
        for lo, hi in ((float("inf"), float("inf")), (float("nan"), 1.0), (0.0, float("inf"))):
            with self.assertRaises(RateRuleError, msg=f"DelaySpec({lo}, {hi}) accepted"):
                DelaySpec(lo, hi)

    async def test_api_and_dashboard_reject_nonfinite_delay(self) -> None:
        # Non-finite delay bounds return 200 OK; the delay is then silently
        # skipped (uniform(inf,inf)=nan -> `nan > 0` is False -> no sleep).
        limiter = RateLimiterService(default_rules={"message": "200/1s"})
        api = ControlAPI(None, limiter, None, None)
        server = DashboardServer(api, token=None, port=0)
        for body in (
            {"action": "message", "min": float("inf"), "max": float("inf")},
            {"action": "message", "min": float("nan"), "max": 1.0},
        ):
            status, payload = await server._route("POST", "/api/delay", body, {})
            self.assertEqual(status, 400, f"route accepted non-finite delay {body}: {payload}")
            self.assertFalse(payload["ok"])
        g = await limiter.acquire("message", "alpha")
        self.assertEqual(g.delay_applied, 0.0)          # no silent nan/inf recorded

    async def test_huge_finite_delay_cannot_wedge_acquires(self) -> None:
        # POST /api/delay {"min": 1e9, "max": 2e9} -> 200 OK -> every acquire
        # sleeps ~1.5e9s: fleet-wide send outage from ONE dashboard call.
        limiter = RateLimiterService(default_rules={"message": "200/1s"})
        api = ControlAPI(None, limiter, None, None)
        server = DashboardServer(api, token=None, port=0)
        status, payload = await server._route(
            "POST", "/api/delay", {"action": "message", "min": 1e9, "max": 2e9}, {}
        )
        self.assertEqual(
            status, 400, f"accepted 47-year delay range: {payload} (wedges every acquire)"
        )
        try:
            await asyncio.wait_for(limiter.acquire("message", "alpha"), timeout=0.6)
        except asyncio.TimeoutError:
            self.fail("acquire wedged after setting a 1e9..2e9 delay range via POST /api/delay")


    def test_stats_before_any_acquire_materializes_bucket(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "3/10s"})
        stats = limiter.stats()                            # must not raise
        info = stats["actions"]["message"]
        self.assertEqual(info["global_bucket"]["tokens"], 3.0)
        self.assertEqual(info["global_bucket"]["capacity"], 3.0)
        self.assertEqual(info["account_buckets"], {})
        self.assertEqual(stats["pending"], {})
        # SIDE EFFECT: the global bucket is materialized full of tokens
        self.assertIn((GLOBAL_KEY, "message"), limiter._buckets)
        json.dumps(stats)                                  # JSON-safe
        limiter.try_acquire("message", "alpha")            # still drains correctly
        self.assertEqual(limiter.stats()["actions"]["message"]["global_bucket"]["tokens"], 2.0)

    def test_clear_account_rule_re_arms_full_burst(self) -> None:
        # Clearing an override drops the account bucket: the next acquire
        # rebuilds it FULL under the inherited default -> instant burst.
        clock = ManualClock()
        limiter = RateLimiterService(
            clock=clock,
            default_rules={"message": "5/1s"},
            account_rules={("alpha", "message"): "1/100s"},
        )
        self.assertIsNotNone(limiter.try_acquire("message", "alpha"))
        self.assertIsNone(limiter.try_acquire("message", "alpha"))   # override exhausted
        self.assertTrue(limiter.clear_account_rule("alpha", "message"))
        # fresh bucket at full default capacity -> immediate grant(s)
        burst = sum(1 for _ in iter(lambda: limiter.try_acquire("message", "alpha"), None))
        self.assertGreaterEqual(burst, 4, "clear_account_rule did not re-arm a full bucket")


# -------------------------------------------------------------- 8. CANCELLATION
class TestCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiters_no_leak_no_starvation(self) -> None:
        limiter = RateLimiterService(default_rules={"message": "1/0.05s"})
        drain(limiter, "message", "alpha")
        grants: list[float] = []

        async def waiter() -> bool:
            g = await limiter.acquire("message", "alpha")
            grants.append(g.at)
            return True

        tasks = [asyncio.create_task(waiter()) for _ in range(15)]
        await asyncio.sleep(0.012)                         # all queued, none granted yet
        self.assertEqual(len(grants), 0)
        for t in tasks[:10]:
            t.cancel()
        outcomes = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
        )
        ok = [o for o in outcomes if o is True]
        cancelled = [o for o in outcomes if isinstance(o, asyncio.CancelledError)]
        self.assertEqual(len(ok), 5, "surviving waiters starved by cancellations")
        self.assertEqual(len(grants), 5)
        self.assertGreaterEqual(len(cancelled), 9)
        await asyncio.sleep(0.15)                          # let finally{} prune run
        pending = limiter.stats()["pending"]
        self.assertEqual(pending.get("alpha:message", 0), 0, "cancelled entries leaked in stats")
        self.assertEqual(pending, {})


if __name__ == "__main__":
    unittest.main(verbosity=2, failfast=False)
