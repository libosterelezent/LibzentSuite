"""FINAL-PHASE regression tests: G1 telemetry, G2 pressure, G5 repeat/
spikes, G6 dispatch cache equivalence, G7 artifact security, G8 chaos."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING

from libzentsuite.core.event_bus import Event, EventBus, MessageReceived
from libzentsuite.core.task_scheduler import TaskScheduler
from tests.unit.helpers import enforces_file_modes
from tests.unit.test_task_scheduler import (
    FakeAudit,
    FakeHistoryRepo,
    FakeLimiter,
    FakeLogger,
    FakeSender,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class DbTelemetryTests(unittest.IsolatedAsyncioTestCase):
    """Each test closes its database INSIDE the temp-dir scope: Windows
    cannot delete an open file, where POSIX forgives the unlink — a
    cleanup registered for after the ``with`` block hid that ordering."""

    async def test_off_by_default_zero_records(self) -> None:
        from libzentsuite.database.db import Database
        from libzentsuite.database.telemetry import TELEMETRY

        was = TELEMETRY.enabled
        TELEMETRY.enabled = False
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(str(Path(tmp) / "t.db"))
                await db.connect()
                try:
                    TELEMETRY.reset()
                    await db.kv.set("k", "v")
                    await db.kv.get("k")
                    snap = TELEMETRY.snapshot()
                    self.assertEqual(snap["requests"]["total"], 0)
                finally:
                    await db.close()
        finally:
            TELEMETRY.enabled = was

    async def test_enabled_populates_from_real_db(self) -> None:
        from libzentsuite.database.db import Database
        from libzentsuite.database.telemetry import TELEMETRY

        was = TELEMETRY.enabled
        TELEMETRY.enabled = True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(str(Path(tmp) / "t.db"))
                await db.connect()
                try:
                    TELEMETRY.reset()
                    for i in range(5):
                        await db.kv.set(f"k{i}", {"n": i})
                    await db.kv.get("k0")
                    snap = TELEMETRY.snapshot()
                    self.assertGreaterEqual(snap["requests"]["by_kind"]["transaction"], 5)
                    self.assertGreaterEqual(snap["requests"]["by_kind"]["commit"], 5)
                    self.assertGreaterEqual(snap["requests"]["by_kind"]["read"], 1)
                    self.assertIn("transaction_ms", snap)
                    self.assertIn("commit_ms", snap)
                    self.assertLessEqual(snap["commit_ms"]["max"], snap["transaction_ms"]["max"])
                finally:
                    await db.close()
        finally:
            TELEMETRY.enabled = was

    async def test_no_per_record_logging(self) -> None:
        from libzentsuite.database.db import Database
        from libzentsuite.database.telemetry import TELEMETRY

        was = TELEMETRY.enabled
        TELEMETRY.enabled = True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(str(Path(tmp) / "t.db"))
                await db.connect()
                try:
                    TELEMETRY.reset()
                    # assertNoLogs on the ROOT logger flags ANY debug record;
                    # scope to the framework tree (aiosqlite's own debug lines
                    # are third-party and not ours to police)
                    with self.assertNoLogs(logger="libzent", level="DEBUG"):
                        for i in range(50):
                            await db.kv.set(f"x{i}", i)
                finally:
                    await db.close()
        finally:
            TELEMETRY.enabled = was


class SchedulerPressureTests(unittest.IsolatedAsyncioTestCase):
    def _scheduler(self) -> TaskScheduler:
        return TaskScheduler(
            EventBus(),
            FakeLogger(),
            FakeLimiter(),
            FakeSender(),
            FakeAudit(),
            default_timeout=5.0,
            history_repo=FakeHistoryRepo(),
        )

    async def test_pressure_transitions_and_hwm(self) -> None:
        scheduler = self._scheduler()
        scheduler.start()
        try:
            gate = scheduler._limiter.block_acquires()
            for i in range(10):
                scheduler.submit(f"acct{i % 3}", "roll", "message", channel_id=100, content="x")
            # poll for LANES BLOCKED AT THE LIMITER, not depth: depth is
            # 10 the instant the synchronous submits return, so a depth
            # poll never yields and the assertions ran before any lane
            # had blocked (PR review 2026-11-21). Three blocked acquires
            # prove one lane per account is parked on the gate.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while scheduler._limiter.blocked_count < 3 and loop.time() < deadline:
                await asyncio.sleep(0.01)
            pressure = scheduler.pressure()
            self.assertEqual(pressure["depth"], 10)
            self.assertGreaterEqual(scheduler.stats()["queued_depth_hwm"], 10)
            self.assertIn(pressure["level"], ("ELEVATED", "PRESSURED", "CRITICAL"))
            self.assertEqual(pressure["accounts"], 3)
            gate.set()
            # poll the drain too — the fixed 300ms raced the paced
            # sends under load
            deadline = loop.time() + 15.0
            while scheduler.stats()["queued"] and loop.time() < deadline:
                await asyncio.sleep(0.05)
            stats = scheduler.stats()
            self.assertEqual(stats["queued"], 0)  # all drained
            self.assertGreaterEqual(stats["wait_p50_s"], 0.0)
            self.assertEqual(stats["pressure"], "NORMAL")
        finally:
            await scheduler.stop(drain=False)

    async def test_pressure_age_axis(self) -> None:
        import time as _time

        scheduler = self._scheduler()
        scheduler.start()
        try:
            gate = scheduler._limiter.block_acquires()
            task = scheduler.submit("alpha", "roll", "message", channel_id=1, content="x")
            # backdate: age axis — BOTH clocks (age math reads the
            # monotonic twin; the wall stamp is display/history only and
            # an NTP step must not latch the level high)
            task.submitted_at = _time.time() - 25.0
            task.submitted_mono = _time.monotonic() - 25.0
            await asyncio.sleep(0.05)
            self.assertEqual(scheduler.pressure()["level"], "CRITICAL")
            gate.set()
            await asyncio.sleep(0.2)
        finally:
            await scheduler.stop(drain=False)

    async def test_stats_survives_pressure_warning_and_reads_floor_property(self) -> None:
        import time as _time

        scheduler = self._scheduler()
        scheduler._limiter.floor_value = 10.0  # property, not a method
        scheduler.start()
        try:
            gate = scheduler._limiter.block_acquires()
            task = scheduler.submit("alpha", "roll", "message", channel_id=1, content="x")
            task.submitted_mono = _time.monotonic() - 25.0
            await asyncio.sleep(0.05)
            self.assertEqual(scheduler.pressure()["pacing_floor_s"], 10.0)
            scheduler._last_pressure_warn = 0.0
            scheduler.stats()  # used to raise TypeError
            gate.set()
            await asyncio.sleep(0.2)
        finally:
            await scheduler.stop(drain=False)

    async def test_stats_is_pure_observer(self) -> None:
        scheduler = self._scheduler()
        scheduler.start()
        try:
            scheduler.submit("alpha", "roll", "message", channel_id=1, content="x")
            await asyncio.sleep(0.1)
            before = scheduler.stats()
            for _ in range(3):
                scheduler.stats()
                scheduler.pressure()
            after = scheduler.stats()
            self.assertEqual(before["queued"], after["queued"])
            self.assertEqual(before["completed"], after["completed"])
        finally:
            await scheduler.stop(drain=False)


class DispatchCacheEquivalenceTests(unittest.IsolatedAsyncioTestCase):
    """G6-A' §37 pins: identical set, order, routing, snapshot-at-reach."""

    async def test_invocation_order_insertion_not_mro(self) -> None:
        bus = EventBus()
        order: list[str] = []
        bus.subscribe(Event, lambda e: order.append("E"))
        bus.subscribe(MessageReceived, lambda e: order.append("M1"))
        bus.subscribe(MessageReceived, lambda e: order.append("M2"))
        await bus.publish(MessageReceived(account="a", message_id=0, channel_id=1))
        await bus.publish(MessageReceived(account="a", message_id=1, channel_id=1))
        self.assertEqual(order, ["E", "M1", "M2"] * 2)

    async def test_subscribe_between_publishes_visible(self) -> None:
        bus = EventBus()
        seen: list[Event] = []
        bus.subscribe(Event, seen.append)
        await bus.publish(MessageReceived(account="a", message_id=0, channel_id=1))
        bus.subscribe(MessageReceived, seen.append)
        await bus.publish(MessageReceived(account="a", message_id=1, channel_id=1))
        self.assertEqual(len(seen), 3)

    async def test_self_unsubscribe_runs_this_event_only(self) -> None:
        bus = EventBus()
        seen: list[int] = []
        holder: dict[str, Callable[[], None]] = {}

        def selfremoving(event: Event) -> None:
            seen.append(1)
            holder["un"]()

        holder["un"] = bus.subscribe(MessageReceived, selfremoving)
        await bus.publish(MessageReceived(account="a", message_id=0, channel_id=1))
        await bus.publish(MessageReceived(account="a", message_id=1, channel_id=1))
        self.assertEqual(seen, [1])

    async def test_cache_hit_equals_uncached_dispatch(self) -> None:
        bus = EventBus()
        hits: list[str] = []
        bus.subscribe(Event, lambda e: hits.append("base"))
        bus.subscribe(MessageReceived, lambda e: hits.append("mr"))
        first = await bus.publish(
            MessageReceived(account="a", message_id=0, channel_id=1)
        )  # cache build
        second = await bus.publish(
            MessageReceived(account="a", message_id=1, channel_id=1)
        )  # cache hit
        self.assertEqual(len(first), len(second))
        self.assertEqual(hits, ["base", "mr"] * 2)


class BenchmarkReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_median_block(self) -> None:
        from libzentsuite.benchmark.cli import build_parser, run_suite

        args = build_parser().parse_args(
            ["--scenario", "log_burst", "--records", "100", "--repeat", "3", "--quiet"]
        )
        report = await run_suite(args)
        scen = report["scenarios"][0]
        self.assertEqual(scen["repeat"]["n"], 3)
        samples = scen["repeat"]["metrics"]["custom.records_per_s"]["samples"]
        self.assertEqual(len(samples), 3)

    async def test_profiled_repeat_refused(self) -> None:
        from libzentsuite.benchmark.cli import run_bench_cli

        with self.assertRaises(SystemExit):
            run_bench_cli(
                [
                    "--scenario",
                    "log_burst",
                    "--records",
                    "10",
                    "--repeat",
                    "3",
                    "--profile",
                    "--quiet",
                ]
            )
        with self.assertRaises(SystemExit):
            run_bench_cli(
                [
                    "--scenario",
                    "log_burst",
                    "--records",
                    "10",
                    "--capture-spikes",
                    "--save-baseline",
                    "--quiet",
                ]
            )

    async def test_run_mode_labels(self) -> None:
        from libzentsuite.benchmark.cli import _run_mode, build_parser

        base = build_parser().parse_args(["--scenario", "log_burst"])
        self.assertEqual(_run_mode(base), "BASELINE")
        prof = build_parser().parse_args(["--profile"])
        self.assertEqual(_run_mode(prof), "PROFILED")
        mem = build_parser().parse_args(["--tracemalloc"])
        self.assertEqual(_run_mode(mem), "MEMORY_PROFILED")
        spikes = build_parser().parse_args(["--capture-spikes"])
        self.assertEqual(_run_mode(spikes), "DEEP_DIAGNOSTIC")

    async def test_spike_capture_under_synthetic_stall(self) -> None:
        from libzentsuite.benchmark.sampler import SamplerSession, SpikeCapture

        capture = SpikeCapture(floor_ms=5.0, k_lag=1.0)
        session = SamplerSession(loop_interval_ms=5, spike_capture=capture)
        assert session.spike_capture is not None  # just constructed with it
        session.spike_capture.capture_ctx["fake"] = NS(stats=lambda: {"depth": 1})
        session.start()
        await asyncio.sleep(0.05)
        time_module_sleep = 0.08
        import time as _t

        _t.sleep(time_module_sleep)  # a real blocking stall > floor
        await asyncio.sleep(0.05)
        await session.stop()
        self.assertGreaterEqual(len(capture.captures), 1)
        self.assertEqual(capture.captures[0]["trigger"]["kind"], "lag")
        self.assertIn("fake", capture.captures[0])

    async def test_no_spikes_in_calm_run(self) -> None:
        # A calm history captures nothing. The lags run through the REAL
        # monitor -> hook -> capture path, but as synthetic samples: "the
        # machine was calm for 200 ms" is an assumption about the host, and
        # it is false under parallel load on a platform whose timer ticks
        # every ~15.6 ms (one late wake past 10x p50 fired a capture).
        from libzentsuite.benchmark.sampler import SamplerSession, SpikeCapture

        capture = SpikeCapture()
        session = SamplerSession(loop_interval_ms=5, spike_capture=capture)
        for lag_s in (0.0002, 0.0005, 0.001, 0.003, 0.0008) * 20:
            session.lag._record(lag_s)  # every sample far under the 25 ms floor
        self.assertEqual(capture.captures, [])


class ArtifactSecurityTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(enforces_file_modes(), "POSIX permission bits are not enforced here")
    async def test_json_report_mode_0600(self) -> None:
        from libzentsuite.benchmark.report import to_json

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "r.json")
            to_json({"schema": "sm-bench/1"}, path)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode & 0o077, 0)

    async def test_report_value_free_of_fake_tokens(self) -> None:
        """The sentinel scan: a full bench stack report contains neither
        the fake token nor live key material (G7 pin)."""
        from libzentsuite.benchmark.cli import build_parser, run_suite
        from libzentsuite.benchmark.report import to_json

        args = build_parser().parse_args(
            [
                "--scenario",
                "accounts_startstop",
                "--accounts",
                "2",
                "--cycles",
                "1",
                "--duration",
                "0.1",
                "--quiet",
            ]
        )
        report = await run_suite(args)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scan.json")
            to_json(report, path)
            with open(path) as fh:
                text = fh.read()
        self.assertNotIn("bench-tok", text)
        key = Path("state/credentials.key")
        if key.is_file():
            self.assertNotIn(key.read_text().strip(), text)


if __name__ == "__main__":
    unittest.main()
