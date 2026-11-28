"""Unit tests for core/rate_limit_manager/policies.py (A4).

Covers: RatePolicy validation + __str__, scope parsing (level/owner rules),
set_policy / set_policy_values / clear_policy incl. the safety floor, and the
persistence round-trip against a fake repo returning real RatePolicyRecord
rows (plus dict-shaped rows).
"""

from __future__ import annotations

import unittest
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

from libzentsuite.core.errors import RatePolicyError, UnsafePolicyError
from libzentsuite.core.event_bus import EventBus
from libzentsuite.core.rate_limit_manager import MIN_ACTION_INTERVAL, RateLimitManager, RatePolicy
from libzentsuite.core.rate_limit_manager.policies import parse_scope, policy_from_row
from libzentsuite.database.models import RatePolicyRecord


# ----------------------------------------------------------------- test doubles
class StubLogger:
    """LogService-shaped capture double (never used for assertions by default)."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []

    def _add(self, severity: str, source: str, message: str, **ctx: Any) -> None:
        self.entries.append((severity, source, message))

    def info(self, source: str, message: str, **ctx: Any) -> None:
        self._add("INFO", source, message, **ctx)

    def warning(self, source: str, message: str, **ctx: Any) -> None:
        self._add("WARNING", source, message, **ctx)

    def error(self, source: str, message: str, **ctx: Any) -> None:
        self._add("ERROR", source, message, **ctx)


class FakePolicyRepo:
    """List-based RatePolicyRepository double backed by real RatePolicyRecords
    (async methods, like the real repository)."""

    def __init__(self, rows: Iterable[RatePolicyRecord] | None = None) -> None:
        self.rows: list[RatePolicyRecord] = list(rows or [])
        self.set_calls: list[tuple[str, str | None, str, float, float, float]] = []
        self.delete_calls: list[tuple[str, str | None, str]] = []

    async def list(self) -> list[RatePolicyRecord]:
        return list(self.rows)

    def set(
        self,
        level: str,
        owner: str | None,
        action: str,
        interval: float,
        jitter_min: float,
        jitter_max: float,
    ) -> None:
        self.set_calls.append((level, owner, action, interval, jitter_min, jitter_max))
        for i, row in enumerate(self.rows):
            if (row.level, row.owner, row.action) == (level, owner, action):
                self.rows[i] = RatePolicyRecord(
                    level, owner, action, interval, jitter_min, jitter_max
                )
                return
        self.rows.append(RatePolicyRecord(level, owner, action, interval, jitter_min, jitter_max))

    def delete(self, level: str, owner: str | None, action: str) -> bool:
        self.delete_calls.append((level, owner, action))
        for i, row in enumerate(self.rows):
            if (row.level, row.owner, row.action) == (level, owner, action):
                del self.rows[i]
                return True
        return False


class DictRowRepo:
    """Repo double returning plain dict rows (both row shapes must be accepted)."""

    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        self.rows: list[dict[str, Any]] = list(rows)
        self.set_calls: list[tuple[Any, ...]] = []
        self.delete_calls: list[tuple[Any, ...]] = []

    async def list(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def set(self, *args: Any) -> None:
        self.set_calls.append(args)

    def delete(self, *args: Any) -> bool:
        self.delete_calls.append(args)
        return False


def make_manager(repo: Any = None, **kwargs: Any) -> RateLimitManager:
    return RateLimitManager(EventBus(), StubLogger(), repo, **kwargs)


# ------------------------------------------------------------ RatePolicy value
class TestRatePolicy(unittest.TestCase):
    """RatePolicy: frozen value object + structural validation."""

    def test_str_with_jitter(self) -> None:
        self.assertEqual(str(RatePolicy("message", 2.5, 0.1, 0.5)), "2.50s +0.10..0.50s")

    def test_str_without_jitter(self) -> None:
        self.assertEqual(str(RatePolicy("message", 2.0)), "2.00s")

    def test_str_zero_min_jitter_still_shown(self) -> None:
        self.assertEqual(str(RatePolicy("m", 1.5, 0.0, 0.5)), "1.50s +0.00..0.50s")

    def test_int_values_coerced_to_float(self) -> None:
        policy = RatePolicy("m", 2, 0, 1)
        self.assertIsInstance(policy.interval, float)
        self.assertEqual(policy.interval, 2.0)
        self.assertEqual(policy.jitter_min, 0.0)

    def test_action_is_stripped(self) -> None:
        self.assertEqual(RatePolicy("  poke  ", 2.0).action, "poke")

    def test_frozen(self) -> None:
        policy = RatePolicy("m", 2.0)
        with self.assertRaises(Exception):  # noqa: B017 -- frozen-policy refusal, any failure kind
            policy.interval = 5.0  # type: ignore[misc]

    def assert_bad(self, **kwargs: Any) -> None:
        kwargs.setdefault("action", "message")
        kwargs.setdefault("interval", 2.0)
        with self.assertRaises(RatePolicyError):
            RatePolicy(kwargs.pop("action"), **kwargs)

    def test_bad_action(self) -> None:
        bad_actions: tuple[Any, ...] = ("", "   ", None, 42, b"poke")
        for action in bad_actions:
            with self.assertRaises(RatePolicyError):
                RatePolicy(action, 2.0)

    def test_bad_interval(self) -> None:
        bad_intervals: tuple[Any, ...] = (
            0,
            -1,
            -0.001,
            float("nan"),
            float("inf"),
            -float("inf"),
            "2",
        )
        for interval in bad_intervals:
            with self.assertRaises(RatePolicyError):
                RatePolicy("m", interval)

    def test_interval_cap(self) -> None:
        self.assert_bad(interval=86400.1)
        self.assert_bad(interval=1e9)
        RatePolicy("m", 86400.0)  # exactly at the cap is allowed

    def test_bad_jitter(self) -> None:
        self.assert_bad(jitter_min=-0.1)
        self.assert_bad(jitter_min=0.5, jitter_max=0.2)  # max < min
        self.assert_bad(jitter_max=3600.5)  # over cap
        self.assert_bad(jitter_min=float("nan"))
        self.assert_bad(jitter_max=float("nan"))

    def test_nonfinite_and_bool_jitter_pairs_rejected(self) -> None:
        """inf/-inf jitter bounds are rejected explicitly (NaN-poisoning
        guard extended to every non-finite shape); bools are NOT numbers
        here, even when they would coerce into range."""
        self.assert_bad(jitter_min=float("inf"))
        self.assert_bad(jitter_max=float("inf"))
        self.assert_bad(jitter_min=float("-inf"), jitter_max=1.0)
        self.assert_bad(jitter_min=True, jitter_max=2.0)  # bool, not number
        self.assert_bad(jitter_max=False)  # bool, not number
        self.assert_bad(jitter_min=0.0, jitter_max=float("inf"))
        # degenerate-but-finite pairs remain legal
        RatePolicy("m", 2.0, 0.0, 0.0)
        RatePolicy("m", 2.0, 0.5, 0.5)

    def test_jitter_caps_allowed_at_boundary(self) -> None:
        RatePolicy("m", 2.0, 0.0, 3600.0)
        RatePolicy("m", 2.0, 0.4, 0.4)  # min == max is a valid (degenerate) range


# ------------------------------------------------------------------ scopes
class TestParseScope(unittest.TestCase):
    """parse_scope: level/owner validation mirroring RatePolicyRecord rules."""

    def test_global_owner_none(self) -> None:
        self.assertEqual(parse_scope("global", None), ("global", None))

    def test_global_empty_owner_normalized(self) -> None:
        self.assertEqual(parse_scope("global", ""), ("global", None))

    def test_global_with_owner_rejected(self) -> None:
        with self.assertRaises(RatePolicyError):
            parse_scope("global", "alpha")

    def test_unknown_level_rejected(self) -> None:
        for level in ("Global", "fleet", "", None, 1):
            with self.assertRaises(RatePolicyError):
                parse_scope(level, None)

    def test_owner_required_for_non_global(self) -> None:
        for level in ("account", "plugin", "account_plugin"):
            for owner in (None, "", "   "):
                with self.assertRaises(RatePolicyError):
                    parse_scope(level, owner)

    def test_account_and_plugin_owners_stripped(self) -> None:
        self.assertEqual(parse_scope("account", " alpha "), ("account", "alpha"))
        self.assertEqual(parse_scope("plugin", " exporter "), ("plugin", "exporter"))

    def test_account_plugin_owner_shape(self) -> None:
        self.assertEqual(
            parse_scope("account_plugin", "alpha/exporter"),
            ("account_plugin", "alpha/exporter"),
        )
        for bad in ("alpha", "alpha/soul/extra", "/exporter", "alpha/", "a//b", "/"):
            with self.assertRaises(RatePolicyError):
                parse_scope("account_plugin", bad)


class TestPolicyFromRow(unittest.TestCase):
    """policy_from_row: attribute rows AND dict rows, invalid rows rejected."""

    def test_record_shaped_row(self) -> None:
        level, owner, policy = policy_from_row(
            RatePolicyRecord("account", "alpha", "message", 3.0, 0.2, 0.8)
        )
        self.assertEqual((level, owner), ("account", "alpha"))
        self.assertEqual(policy.interval, 3.0)
        self.assertEqual(policy.jitter_min, 0.2)

    def test_dict_shaped_row(self) -> None:
        level, owner, policy = policy_from_row(
            {
                "level": "plugin",
                "owner": "roll",
                "action": "fire",
                "interval": 2.5,
                "jitter_min": 0.1,
                "jitter_max": 0.6,
            }
        )
        self.assertEqual((level, owner), ("plugin", "roll"))
        self.assertEqual(policy.jitter_max, 0.6)

    def test_dict_row_missing_jitter_defaults(self) -> None:
        _, _, policy = policy_from_row(
            {
                "level": "global",
                "owner": None,
                "action": "poke",
                "interval": 2.0,
            }
        )
        self.assertEqual(policy.jitter_min, 0.0)
        self.assertEqual(policy.jitter_max, 0.0)

    def test_invalid_row_raises(self) -> None:
        with self.assertRaises(RatePolicyError):
            policy_from_row({"level": "bogus", "owner": None, "action": "x", "interval": 2})
        with self.assertRaises(RatePolicyError):
            policy_from_row({"level": "account", "owner": None, "action": "x", "interval": 2})


# ------------------------------------------------------- set/clear + the floor
class TestSetPolicy(unittest.TestCase):
    """set_policy / set_policy_values / clear_policy incl. safety floor."""

    def test_set_policy_values_global_roundtrip(self) -> None:
        manager = make_manager()
        policy = manager.set_policy_values("message", 2.5, 0.1, 0.5)
        self.assertIsInstance(policy, RatePolicy)
        self.assertEqual(policy.interval, 2.5)
        stored = manager.policies()
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            stored[0],
            {
                "level": "global",
                "owner": None,
                "action": "message",
                "interval": 2.5,
                "jitter": [0.1, 0.5],
            },
        )

    def test_set_policy_accepts_rate_policy_object(self) -> None:
        manager = make_manager()
        policy = manager.set_policy(RatePolicy("poke", 4.0), level="account", owner="alpha")
        self.assertEqual(policy.action, "poke")
        self.assertEqual(manager.policies()[0]["level"], "account")

    def test_set_policy_rejects_non_policy(self) -> None:
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            manager.set_policy({"action": "x", "interval": 2.0})  # type: ignore[arg-type]

    def test_floor_rejection_message_mentions_1_5(self) -> None:
        manager = make_manager()
        with self.assertRaises(UnsafePolicyError) as ctx:
            manager.set_policy_values("message", 0.03)
        self.assertIn("1.5", str(ctx.exception))
        # UnsafePolicyError is a RatePolicyError (interfaces render it the same)
        self.assertIsInstance(ctx.exception, RatePolicyError)
        self.assertEqual(manager.policies(), [])  # nothing stored on refusal

    def test_interval_exactly_at_floor_allowed(self) -> None:
        # "poke" carries no learned default — floor semantics in isolation
        manager = make_manager()
        manager.set_policy_values("poke", MIN_ACTION_INTERVAL)
        self.assertEqual(manager.effective("poke")["interval"], MIN_ACTION_INTERVAL)

    def test_floor_applies_at_every_level(self) -> None:
        manager = make_manager()
        for level, owner in (
            ("global", None),
            ("account", "alpha"),
            ("plugin", "exporter"),
            ("account_plugin", "alpha/exporter"),
        ):
            with self.assertRaises(UnsafePolicyError):
                manager.set_policy_values("msg", 0.03, level=level, owner=owner)

    def test_bad_values_rejected_before_storage(self) -> None:
        manager = make_manager()
        for kwargs in (
            {"interval": 0},
            {"interval": -2},
            {"interval": float("nan")},
            {"interval": 1e9},
            {"jitter_min": 5, "jitter_max": 1},
        ):
            with self.assertRaises(RatePolicyError):
                manager.set_policy_values(
                    "message",
                    kwargs.get("interval", 2.0),
                    kwargs.get("jitter_min", 0.0),
                    kwargs.get("jitter_max", 0.0),
                )
        self.assertEqual(manager.policies(), [])

    def test_bad_scope_rejected(self) -> None:
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            manager.set_policy_values("message", 2.0, level="fleet")
        with self.assertRaises(RatePolicyError):
            manager.set_policy_values("message", 2.0, level="account")  # owner missing
        with self.assertRaises(RatePolicyError):
            manager.set_policy_values("message", 2.0, level="account_plugin", owner="alpha")

    def test_clear_policy_removes_memory_only(self) -> None:
        repo = FakePolicyRepo()
        manager = make_manager(repo)
        manager.set_policy_values("message", 3.0, level="account", owner="alpha")
        self.assertTrue(manager.clear_policy("account", "alpha", "message"))
        self.assertEqual(manager.policies(), [])
        self.assertFalse(manager.clear_policy("account", "alpha", "message"))
        # the ENGINE does not persist anymore (ControlAPI owns it)
        self.assertEqual(repo.set_calls, [])
        self.assertEqual(repo.delete_calls, [])

    def test_clear_policy_invalid_scope(self) -> None:
        manager = make_manager()
        with self.assertRaises(RatePolicyError):
            manager.clear_policy("bogus", None, "message")
        with self.assertRaises(RatePolicyError):
            manager.clear_policy("account", None, "message")

    def test_policies_sorted_deterministically(self) -> None:
        manager = make_manager()
        manager.set_policy_values("zzz", 2.0)
        manager.set_policy_values("message", 3.0, level="account", owner="beta")
        manager.set_policy_values("message", 4.0)
        manager.set_policy_values("message", 2.5, level="plugin", owner="roll")
        rows = manager.policies()
        self.assertEqual([r["action"] for r in rows], ["message", "message", "message", "zzz"])
        self.assertEqual([r["level"] for r in rows[:3]], ["global", "account", "plugin"])


# ------------------------------------------------------------- persistence
class TestPersistenceRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Stored policies load via ``await load_persisted()`` (boot path) —
    both row shapes; bad rows are skipped. The ENGINE no longer WRITES:
    set/clear are memory-only and persistence belongs to the ControlAPI."""

    async def test_load_rate_policy_records(self) -> None:
        repo = FakePolicyRepo(
            [
                RatePolicyRecord("global", None, "message", 3.0, 0.2, 0.8),
                RatePolicyRecord("account", "alpha", "message", 5.0),
            ]
        )
        manager = make_manager(repo)
        self.assertEqual(manager.policies(), [])  # nothing before load
        await manager.load_persisted()
        rows = manager.policies()
        self.assertEqual(len(rows), 2)
        self.assertEqual(manager.effective("message")["interval"], 3.0)
        self.assertEqual(manager.effective("message", account="alpha")["interval"], 5.0)

    async def test_load_dict_rows(self) -> None:
        repo = DictRowRepo(
            [
                {
                    "level": "plugin",
                    "owner": "roll",
                    "action": "fire",
                    "interval": 2.5,
                    "jitter_min": 0.1,
                    "jitter_max": 0.6,
                },
            ]
        )
        manager = make_manager(repo)
        await manager.load_persisted()
        self.assertEqual(manager.effective("fire", plugin="roll")["interval"], 2.5)

    async def test_invalid_rows_skipped_not_fatal(self) -> None:
        repo = DictRowRepo(
            [
                {"level": "bogus", "owner": None, "action": "x", "interval": 2.0},
                {"level": "global", "owner": None, "action": "ok", "interval": 4.0},
                {"level": "global", "owner": None, "action": "bad", "interval": "oops"},
            ]
        )
        manager = make_manager(repo)
        await manager.load_persisted()
        rows = manager.policies()
        self.assertEqual([r["action"] for r in rows], ["ok"])

    async def test_set_is_memory_only_repo_untouched(self) -> None:
        repo = FakePolicyRepo()
        manager = make_manager(repo)
        manager.set_policy_values("poke", 2.0, 0.1, 0.5, level="account_plugin", owner="a/p")
        self.assertEqual(
            repo.set_calls,
            [],  # persistence moved to the ControlAPI layer
        )
        self.assertEqual(repo.rows, [])
        # reload of an unchanged repo therefore sees nothing
        reloaded = make_manager(FakePolicyRepo(repo.rows))
        await reloaded.load_persisted()
        self.assertEqual(reloaded.policies(), [])

    async def test_load_persisted_without_repo_is_a_noop(self) -> None:
        manager = make_manager()
        await manager.load_persisted()  # must not raise
        self.assertEqual(manager.policies(), [])

    async def test_failing_repo_list_is_logged_not_fatal(self) -> None:
        class ExplodingRepo(FakePolicyRepo):
            async def list(self) -> list[RatePolicyRecord]:
                raise RuntimeError("db down")

        manager = make_manager(ExplodingRepo())
        await manager.load_persisted()
        policy = manager.set_policy_values("message", 2.0)
        self.assertEqual(policy.interval, 2.0)
        self.assertEqual(manager.policies()[0]["interval"], 2.0)


# -------------------------------------------------------- effective composition
class TestEffectiveComposition(unittest.TestCase):
    """effective(): max-interval composition across all four levels."""

    def setUp(self) -> None:
        self.manager = make_manager()
        self.manager.set_policy_values("poke", 2.0, 0.0, 1.0)  # global
        self.manager.set_policy_values("poke", 3.0, 0.5, 0.5, level="account", owner="alpha")
        self.manager.set_policy_values("poke", 2.5, 0.2, 0.9, level="plugin", owner="exporter")
        self.manager.set_policy_values(
            "poke", 4.0, 0.1, 0.4, level="account_plugin", owner="alpha/exporter"
        )

    def test_all_four_levels_compose_to_max(self) -> None:
        eff = self.manager.effective("poke", account="alpha", plugin="exporter")
        self.assertEqual(eff["interval"], 4.0)
        self.assertEqual(eff["jitter"], [0.5, 1.0])  # max of mins, max of maxes
        self.assertEqual(
            [s["level"] for s in eff["sources"]], ["global", "account", "plugin", "account_plugin"]
        )
        for source in eff["sources"]:
            self.assertEqual(set(source), {"level", "owner", "interval"})

    def test_partial_matches_compose(self) -> None:
        # alpha + a different plugin: global + account only
        eff = self.manager.effective("poke", account="alpha", plugin="roll")
        self.assertEqual(eff["interval"], 3.0)
        self.assertEqual(len(eff["sources"]), 2)
        # another account + exporter: global + plugin
        # (on "poke": no learned default — stored values compose alone)
        self.assertEqual(
            self.manager.effective("poke", account="beta", plugin="exporter")["interval"], 2.5
        )
        # bare action: global only
        self.assertEqual(self.manager.effective("poke")["interval"], 2.0)

    def test_empty_case_falls_back_to_unstudied_default(self) -> None:
        eff = self.manager.effective("unknown-action")
        self.assertEqual(eff["interval"], 1.5)  # max(floor, 1.2 unstudied default)
        self.assertEqual(eff["jitter"], [0.0, 0.0])
        self.assertEqual(eff["sources"], [])
        self.assertEqual(eff["action"], "unknown-action")

    def test_unknown_action_for_account_uses_unstudied_default(self) -> None:
        # "poke" now has a LEARNED default (rate study); a genuinely
        # unconfigured action paces at the unstudied default, clamped
        # up to the 1.5 s floor
        fresh = make_manager()
        eff = fresh.effective("other", account="alpha", plugin="exporter")
        self.assertEqual(eff["interval"], 1.5)
        self.assertEqual(eff["sources"], [])

    def test_learned_default_applies_without_policy(self) -> None:
        # "message" carries the LEARNED default (rate study); "poke" (an
        # unconfigured action) stays at the floor
        fresh = make_manager()
        eff = fresh.effective("message")
        self.assertEqual(eff["interval"], 2.6)  # rate-study derived
        self.assertEqual(eff["sources"], [])  # a default, not a policy
        self.assertEqual(fresh.effective("other")["interval"], 1.5)

    def test_losing_level_jitter_cannot_speed_up_winner(self) -> None:
        """MAX-across-levels: account_plugin LOSES to global on interval,
        and its jitter only ever ADDS a pause on top of the winner — the
        effective pacing can never get FASTER than global alone (interval
        stays the max; jitter bounds only widen upward via max-of-maxes)."""
        self.manager.set_policy_values("fast", 10.0, 0.0, 0.0)  # global wins
        self.manager.set_policy_values("fast", 2.0, 1.0, 4.0, level="account_plugin", owner="a/p")
        eff = self.manager.effective("fast", account="a", plugin="p")
        self.assertEqual(eff["interval"], 10.0)
        self.assertEqual(eff["jitter"], [1.0, 4.0])
        # a sub-level with NO jitter cannot shrink the winner's jitter either
        self.manager.set_policy_values("fast2", 10.0, 0.5, 2.0, level="global", owner=None)
        self.manager.set_policy_values("fast2", 3.0, level="account", owner="a")
        eff2 = self.manager.effective("fast2", account="a")
        self.assertEqual(eff2["interval"], 10.0)
        self.assertEqual(eff2["jitter"], [0.5, 2.0])

    def test_slash_owner_at_account_level_no_account_plugin_collision(self) -> None:
        """An account literally named 'a/b' stores at the account level and
        never collides with the account_plugin key 'a/b' (account 'a' +
        plugin 'b'): distinct storage, precise matching."""
        self.manager.set_policy_values("m", 2.0, level="account", owner="a/b")
        self.manager.set_policy_values("m", 5.0, level="account_plugin", owner="a/b")
        m_rows = [r for r in self.manager.policies() if r["action"] == "m"]
        self.assertEqual(len(m_rows), 2)
        self.assertEqual(
            self.manager.effective("m", account="a", plugin="b")["interval"],
            5.0,
        )
        self.assertEqual(self.manager.effective("m", account="a/b")["interval"], 2.0)
        # "a/b" + plugin "b" would need owner "a/b/b" — no accidental match
        self.assertEqual(
            self.manager.effective("m", account="a/b", plugin="b")["sources"],
            [{"level": "account", "owner": "a/b", "interval": 2.0}],
        )


class TestJitterCountsIntoPacingStamps(unittest.IsolatedAsyncioTestCase):
    """Round-3 wire-spacing semantics: the jitter draw is made BEFORE
    admission and the pacing clock is stamped at grant + jitter — the
    expected WIRE instant. Consecutive wire sends on one key stay >=
    interval apart no matter how the draws differ: a smaller second draw
    can no longer compress (or invert) the gap."""

    async def test_wire_gap_holds_interval_despite_jitter(self) -> None:
        from tests.unit.test_rate_engine import make_manager

        interval = 10.0
        manager = make_manager(interval=interval, jitter=(1.0, 4.0), jitter_seed=123)
        first = await manager.acquire("poke")
        grant1 = manager._clock.now() - first.jitter_s
        wire1 = manager._clock.now()
        second = await manager.acquire("poke")
        grant2 = manager._clock.now() - second.jitter_s
        wire2 = manager._clock.now()
        self.assertGreaterEqual(second.jitter_s, 1.0)
        self.assertLessEqual(second.jitter_s, 4.0)
        # grant spacing: the next grant waits a full interval from the
        # previous stamp (grant + jitter), so the grant gap is >= interval
        # and exactly interval + first_draw
        self.assertAlmostEqual(grant2 - grant1, interval + first.jitter_s, places=6)
        # wire spacing: consecutive wire sends stay >= interval apart, and
        # equal interval + the SECOND draw (the first draw is already part
        # of the stamp the second grant waited out)
        self.assertGreaterEqual(wire2 - wire1, interval - 1e-9)
        self.assertAlmostEqual(wire2 - wire1, interval + second.jitter_s, places=6)


if __name__ == "__main__":
    unittest.main()
