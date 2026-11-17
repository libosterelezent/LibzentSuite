"""Round-3 re-audit A (read-only auditor): probes pinning suspicions from the
hunk review of `git diff c32f9c2` — each test either verifies a round-3 change
is SOUND or pins a defect (marked expectedFailure where the defect stands).

Findings keyed as RA-<n>; see the audit report for file:line evidence.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.event_bus import EventBus
from core.plugin_state import PluginStateService
from core.rate_limit_manager.engine import ManualClock, RateLimitManager
from core.settings_manager import Setting, SettingsManager
from core.task_scheduler.scheduler import TaskScheduler
from database.db import Database

_LOG = logging.getLogger("tests.round3_reaudit_a")


# ---------------------------------------------------------------------------
# helpers (self-contained — no dependency on the round-3 test edits)
# ---------------------------------------------------------------------------
class _StubLogger:
    def __call__(self, severity: str, message: str, **ctx: Any) -> None:
        pass

    def warning(self, *a: Any, **k: Any) -> None:
        pass

    def error(self, *a: Any, **k: Any) -> None:
        pass

    def info(self, *a: Any, **k: Any) -> None:
        pass

    def exception(self, *a: Any, **k: Any) -> None:
        pass


class _InstantLimiter:
    class _Grant:
        wait_s: float = 0.0
        jitter_s: float = 0.0

    async def acquire(
        self, action: str, account: str | None, plugin: str | None, priority: Any
    ) -> _InstantLimiter._Grant:
        return self._Grant()

    def report_server_limit(self, *a: Any, **k: Any) -> None:
        pass


async def _settle(interval: float = 0.02, rounds: int = 3) -> None:
    for _ in range(rounds):
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# RA-1: plugin_state single-JSON — string values that LOOK like JSON
# ---------------------------------------------------------------------------
class _MiniKv:
    """The KvRepository contract, verbatim (set JSON-encodes, get
    tolerant-decodes with raw-string fallback)."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def set(self, key: str, value: Any) -> None:
        import json

        self.rows[key] = json.dumps(value)

    async def get(self, key: str, default: Any = None) -> Any:
        import json

        raw = self.rows.get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    async def delete(self, key: str) -> bool:
        return self.rows.pop(key, None) is not None

    async def delete_prefix(self, p: str) -> int:
        gone = [k for k in self.rows if k.startswith(p)]
        for k in gone:
            del self.rows[k]
        return len(gone)


def _state_service() -> Any:
    """PluginStateService over the duck-typed _MiniKv double (KvRepository
    is a concrete class, so the fake binds through Any)."""
    kv: Any = _MiniKv()
    return PluginStateService(kv)


class PluginStateStringRoundTrip(unittest.IsolatedAsyncioTestCase):
    """RA-1: `_set` stores RAW and `_get` json.loads any str — a string
    value which is itself valid JSON comes back as the DECODED object
    (silent type change). Pre-round-3 (double encoding) round-tripped
    every value correctly, so this is a regression."""

    async def test_json_object_string_comes_back_as_dict(self) -> None:
        state = _state_service()
        await state.set_global("payload", '{"a": 1}')
        self.assertEqual(await state.get_global("payload"), '{"a": 1}')

    async def test_numeric_string_comes_back_as_int(self) -> None:
        state = _state_service()
        await state.set_global("count_text", "123")
        self.assertEqual(await state.get_global("count_text"), "123")
        self.assertIsInstance(await state.get_global("count_text"), str)

    async def test_null_string_comes_back_as_none(self) -> None:
        state = _state_service()
        await state.set_global("marker", "null")
        self.assertEqual(await state.get_global("marker"), "null")

    async def test_plain_string_survives(self) -> None:
        state = _state_service()
        await state.set_global("note", "hello world")
        self.assertEqual(await state.get_global("note"), "hello world")

    async def test_legacy_double_encoded_row_still_decodes(self) -> None:
        import json

        kv: Any = _MiniKv()
        # a legacy row: the service pre-encoded the dict, kv encoded again
        await kv.set("pstate:global:old", json.dumps({"legacy": [1, 2]}))
        state = PluginStateService(kv)
        self.assertEqual(await state.get_global("old"), {"legacy": [1, 2]})

    async def test_real_repo_round_trip_non_json_types(self) -> None:
        """Non-string values must round-trip through the REAL repository
        (dict/bool/list) — the fix's primary goal."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "st.db")
            await db.connect()
            try:
                state = PluginStateService(db.kv)
                await state.set_pair("alpha", "roll", "userstack_available", False)
                self.assertIs(await state.get_pair("alpha", "roll", "userstack_available"), False)
                await state.set_pair("alpha", "roll", "stats", {"rolls": 3})
                self.assertEqual(await state.get_pair("alpha", "roll", "stats"), {"rolls": 3})
            finally:
                await db.close()


# ---------------------------------------------------------------------------
# RA-2: CLI `logs` severity exact-case — lowercase severity silently became
# an account filter (old behavior: .upper() matched it as a severity)
# ---------------------------------------------------------------------------
class LogsSeverityCase(unittest.IsolatedAsyncioTestCase):
    class _Api:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def logs_tail(
            self,
            limit: int,
            account: str | None = None,
            severity: str | None = None,
            without_account: bool = False,
        ) -> list[dict[str, Any]]:
            # without_account rides the alias's framework-view route;
            # these cases pin the account/severity split, not the view
            self.calls.append({"limit": limit, "account": account, "severity": severity})
            return []

    @unittest.expectedFailure
    async def test_lowercase_severity_still_filters_by_severity(self) -> None:
        from interfaces.cli.commands import run_command

        api = self._Api()
        await run_command(api, "logs 50 error")
        self.assertEqual(api.calls, [{"limit": 50, "account": None, "severity": "ERROR"}])

    async def test_lowercase_severity_now_account_filters(self) -> None:
        """Pin the (introduced) new behavior: `logs 50 error` silently
        account-filters instead of severity-filtering — see the report."""
        from interfaces.cli.commands import run_command

        api = self._Api()
        await run_command(api, "logs 50 error")
        self.assertEqual(api.calls, [{"limit": 50, "account": "error", "severity": None}])

    async def test_uppercase_severity_filters(self) -> None:
        from interfaces.cli.commands import run_command

        api = self._Api()
        await run_command(api, "logs 50 ERROR")
        self.assertEqual(api.calls, [{"limit": 50, "account": None, "severity": "ERROR"}])

    async def test_bare_lowercase_token_is_an_account_filter(self) -> None:
        """`logs error` was an ACCOUNT filter before round 3 too (the old
        detection needed >= 2 args) — pin that unchanged."""
        from interfaces.cli.commands import run_command

        api = self._Api()
        await run_command(api, "logs error")
        self.assertEqual(api.calls, [{"limit": 20, "account": "error", "severity": None}])


# ---------------------------------------------------------------------------
# RA-3: scheduler — stop() joiner racing a failed (aborted) stop owner
# ---------------------------------------------------------------------------
class StopJoinerRace(unittest.IsolatedAsyncioTestCase):
    def _scheduler(self, sender: Any, repo: Any) -> TaskScheduler:
        scheduler = TaskScheduler(
            EventBus(),
            _LOG,
            _InstantLimiter(),
            sender,
            None,
            history_repo=repo,
        )
        scheduler.start()
        return scheduler

    class _GateRepo:
        def __init__(self) -> None:
            self.add_calls: list[dict[str, Any]] = []
            self.gate: asyncio.Event | None = None

        async def add(self, entry: dict[str, Any]) -> Any:
            if self.gate is not None:
                await self.gate.wait()
            self.add_calls.append(dict(entry))
            return entry.get("id")

        async def max_id(self) -> int:
            return 0

    async def test_joiner_during_abort_recovers(self) -> None:
        """A joiner stop() that observes the FAILED latch while the owner's
        _abort_stop is still running must not corrupt state: tasks reach
        terminal states, and a later stop() completes the teardown."""
        gate = asyncio.Event()
        repo = self._GateRepo()
        repo.gate = gate

        async def sender(task: Any) -> str:
            return "ok"

        scheduler = self._scheduler(sender, repo)
        t1 = scheduler.submit("alpha", "p", "message")
        await _settle()
        # park the NEXT tail on the gate so the stop sweep blocks mid-write
        repo.gate = gate
        t2 = scheduler.submit("alpha", "p", "message")
        await _settle()
        self.assertTrue(scheduler._tail_refs)  # strong refs exist

        owner = asyncio.create_task(scheduler.stop(drain=False, timeout=0.0))
        await asyncio.sleep(0.05)  # owner parked inside the blocked sweep
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertTrue(scheduler._stop_failed)

        # a joiner arrives while _abort_stop may still be joining tails
        joiner = asyncio.create_task(scheduler.stop(drain=False, timeout=0.0))
        gate.set()  # unblock everything
        try:
            await asyncio.wait_for(joiner, 5.0)
        except asyncio.TimeoutError as exc:
            joiner.cancel()
            raise AssertionError("joiner stop hung after failed owner stop") from exc
        except Exception as exc:
            raise AssertionError(f"joiner stop raised {type(exc).__name__}: {exc}") from exc
        self.assertTrue(scheduler._stopped)
        self.assertTrue(t1.is_terminal)
        self.assertTrue(t2.is_terminal)

    async def test_start_rearms_faulted_gate(self) -> None:
        async def sender(task: Any) -> str:
            return "ok"

        gate = asyncio.Event()
        repo = self._GateRepo()
        repo.gate = gate  # park the sweep's history write mid-flight
        scheduler = self._scheduler(sender, repo)
        scheduler.submit("alpha", "p", "message")
        await _settle()
        owner = asyncio.create_task(scheduler.stop(drain=False, timeout=0.0))
        await asyncio.sleep(0.05)  # owner parked in the blocked sweep
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        gate.set()
        await _settle()
        # a task-less abort: fault must still gate submits and start() must
        # retire it (the estop-resume recovery path)
        from core.errors import SchedulerError

        with self.assertRaises(SchedulerError):
            scheduler.submit("a", "p", "message")
        scheduler.start()
        task = scheduler.submit("a", "p", "message")
        for _ in range(50):
            if task.is_terminal:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(task.is_terminal)
        await scheduler.stop(drain=False)

    async def test_start_does_not_leak_failed_latch_exception(self) -> None:
        """FIXED (round-3 re-audit): start() retrieves a FAILED latch's
        exception before retiring it (no GC 'never retrieved' stderr noise)."""
        gate = asyncio.Event()
        repo = self._GateRepo()
        repo.gate = gate

        async def sender(task: Any) -> str:
            return "ok"

        scheduler = self._scheduler(sender, repo)
        scheduler.submit("alpha", "p", "message")
        await _settle()
        owner = asyncio.create_task(scheduler.stop(drain=False, timeout=0.0))
        await asyncio.sleep(0.05)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        gate.set()
        await _settle()

        loop = asyncio.get_running_loop()
        unretrieved: list[Any] = []

        def handler(log: Any, context: dict[str, Any]) -> None:
            if "exception" in context and "never retrieved" in str(context.get("message", "")):
                unretrieved.append(context["exception"])

        loop.set_exception_handler(handler)
        try:
            scheduler.start()  # drops the failed latch
            import gc

            gc.collect()  # force the Future's __del__ path
            await _settle(0.02, 3)
        finally:
            loop.set_exception_handler(None)
        self.assertEqual(unretrieved, [])


# ---------------------------------------------------------------------------
# RA-4: rate engine — cancelled waiter must not leak its drawn jitter into
# the pacing/lane stamps (ManualClock advanced through the _sleep seam)
# ---------------------------------------------------------------------------
class _ClockAdvanceManager(RateLimitManager):
    """Deterministic manager: every timed wait advances the ManualClock."""

    #: constructed with a ManualClock in every test (the seam's point)
    _clock: ManualClock

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._clock.advance(seconds)

    async def _wait_either(self, seconds: float, key: tuple[str, str] | None = None) -> None:
        await self._sleep(seconds)


class _ParkManager(RateLimitManager):
    """Manual clock + REAL (short) sleeps: waiters park in the event loop
    (cancel-able) while the clock never advances on its own."""

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(0.01)


class CancelledWaiterJitter(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiter_does_not_stamp_clocks(self) -> None:
        manager = _ParkManager(
            EventBus(), _StubLogger(), None, clock=ManualClock(0.0), jitter_seed=3
        )
        manager.set_policy_values("message", 1.5, 0.3, 0.9)
        first = await manager.acquire("message", account="alpha")
        stamp_after_first = manager._last_fire[("alpha", "message")]
        self.assertGreater(first.jitter_s, 0.0)
        # a second waiter cancelled mid-wait must leave the stamp untouched
        waiter = asyncio.get_running_loop().create_task(manager.acquire("message", account="alpha"))
        await _settle(0.01, 3)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(manager._last_fire[("alpha", "message")], stamp_after_first)

    async def test_grant_stamps_include_jitter(self) -> None:
        manager = _ClockAdvanceManager(
            EventBus(), _StubLogger(), None, clock=ManualClock(0.0), jitter_seed=5
        )
        manager.set_policy_values("message", 1.5, 0.5, 0.5)
        grant = await manager.acquire("message", account="alpha")
        self.assertAlmostEqual(grant.jitter_s, 0.5, places=6)
        # stamp sits at grant + jitter (the expected wire instant)
        self.assertAlmostEqual(
            manager._last_fire[("alpha", "message")],
            manager._clock.now() - 0.5 + grant.jitter_s,
            places=6,
        )


# ---------------------------------------------------------------------------
# RA-5: settings clear round-trip through the REAL plugin repository
# ---------------------------------------------------------------------------
class SettingsClearRoundTrip(unittest.IsolatedAsyncioTestCase):
    SCHEMA = (Setting("trigger", "str", "default-trigger"), Setting("gain", "int", 1))

    async def test_clear_deletes_row_and_reloads_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            await db.connect()
            try:
                bus = EventBus()
                seen: list[Any] = []
                bus.subscribe(
                    __import__("core.event_bus", fromlist=["SettingChanged"]).SettingChanged,
                    seen.append,
                )
                mgr = SettingsManager(
                    bus,
                    _LOG,
                    plugin_repo=db.plugins,
                    global_layer={"exporter": {"trigger": "global-trigger"}},
                )
                await mgr.load()
                await mgr.set_plugin_setting("alpha", "exporter", "trigger", "acct", self.SCHEMA)
                effective = await mgr.clear_plugin_setting(
                    "alpha", "exporter", "trigger", self.SCHEMA
                )
                self.assertEqual(effective, "global-trigger")
                # fresh manager over the SAME db: the override row is gone
                mgr2 = SettingsManager(
                    EventBus(),
                    _LOG,
                    plugin_repo=db.plugins,
                    global_layer={"exporter": {"trigger": "global-trigger"}},
                )
                await mgr2.load()
                self.assertEqual(
                    mgr2.resolve("exporter", self.SCHEMA, account="alpha")["trigger"],
                    "global-trigger",
                )
                # exactly one SettingChanged for the clear, new_value global
                cleared = [e for e in seen if e.old_value == "acct"]
                self.assertEqual(len(cleared), 1)
                self.assertEqual(cleared[0].new_value, "global-trigger")
            finally:
                await db.close()


# ---------------------------------------------------------------------------
# RA-6: connection close rebirth — double close, then reuse
# ---------------------------------------------------------------------------
class ConnectionDoubleClose(unittest.IsolatedAsyncioTestCase):
    async def test_double_close_then_reuse_same_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "c.db")
            await db.connect()
            await db.kv.set("k", "v")
            await asyncio.gather(db.close(), db.close())
            self.assertFalse(db.connection.connected)
            await db.connect()
            self.assertEqual(await db.kv.get("k"), "v")
            await db.close()


# ---------------------------------------------------------------------------
# RA-7: identify gate — 1-account fleet keeps the configured interval
# ---------------------------------------------------------------------------
class IdentifyGateSanity(unittest.TestCase):
    def test_single_account_unchanged(self) -> None:
        from adapters.discord.client import IdentifyGate

        gate = IdentifyGate(5.0)
        self.assertEqual(gate.effective_interval, 5.0)
        gate.expected = 1
        self.assertEqual(gate.effective_interval, 5.0)

    def test_zero_and_negative_expected_clamped(self) -> None:
        from adapters.discord.client import IdentifyGate

        gate = IdentifyGate(5.0)
        gate.expected = 0
        self.assertEqual(gate.effective_interval, 5.0)
        gate.expected = -7
        self.assertEqual(gate.effective_interval, 5.0)


# ---------------------------------------------------------------------------
# RA-8: server-rate-limit intake clamp — MAX_INTERVAL passthrough sanity
# ---------------------------------------------------------------------------
class RetryAfterClamp(unittest.TestCase):
    def test_values_below_cap_pass_through(self) -> None:
        manager = RateLimitManager(EventBus(), _StubLogger(), None, clock=ManualClock(0.0))
        manager.report_server_limit("alpha", "message", 3600.0)
        self.assertEqual(manager.cooldown_remaining("alpha", "message"), 3600.0)

    def test_over_cap_clamped_to_max_interval(self) -> None:
        from core.rate_limit_manager.policies import MAX_INTERVAL

        manager = RateLimitManager(EventBus(), _StubLogger(), None, clock=ManualClock(0.0))
        manager.report_server_limit("alpha", "message", 1e9)
        self.assertEqual(manager.cooldown_remaining("alpha", "message"), MAX_INTERVAL)

    def test_none_action_rejected_not_stored(self) -> None:
        from core.errors import RatePolicyError

        manager = RateLimitManager(EventBus(), _StubLogger(), None, clock=ManualClock(0.0))
        no_action: Any = None  # runtime validation rejects a None action
        with self.assertRaises(RatePolicyError):
            manager.report_server_limit("alpha", no_action, 5.0)
        manager.report_server_limit("alpha", "message", 5.0)
        manager.stats()  # a None-keyed cooldown made sorted() raise TypeError


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
