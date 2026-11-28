"""Regression suite for the logging feedback-loop class (10-agent round).

Historical bug: LogService.record mirrored to stdlib; StdlibBridge re-ingested
the mirrors -> every log line echoed ~143x (recursion-limit bounded,
RecursionError swallowed) -> ~23s boot from ~60k sqlite commits.

The suite pins the invariant 'N source events == N canonical LogService
events' plus the coordinator-confirmed hardening: stderr-exactly-once for
bridge-ingested records, severity mapping, failure observability, bridge
detach, and shutdown sink ordering.

Queued persistence (spec §29): record() never touches SQLite — DB writes
flow through start_persistence()/stop_persistence() and an async drain
worker. The DB-facing tests here drive that lifecycle with asyncio.run;
the dedicated matrix lives in tests/unit/test_queued_persistence.py.

Process-global hazard: the stdlib logger tree is shared by the whole test
process — setUp strips bridges and installs a counting probe, tearDown
RESTORES the original handlers (strip-only was fragile). configure_stderr is
NEVER called here (its module-global latch would poison later tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import sqlite3
import threading
import unittest
from typing import Any

from libzentsuite.core.logging_service import (
    LogEntry,
    LogService,
    ServiceLogger,
    StdlibBridge,
    attach_stdlib_bridge,
    detach_stdlib_bridge,
)


class CountingHandler(logging.Handler):
    """Probe: counts formatted messages that reached the tree's handlers."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self.messages.append(record.getMessage())


class TestNoBridgeFeedbackLoop(unittest.TestCase):
    """Core invariant tests (original round, upgraded with restore)."""

    def setUp(self) -> None:
        self.root = logging.getLogger("libzent")
        self._saved_handlers = list(self.root.handlers)
        self._saved_level = self.root.level
        self._saved_propagate = self.root.propagate
        self.root.handlers[:] = [h for h in self._saved_handlers if not isinstance(h, StdlibBridge)]

    def tearDown(self) -> None:
        self.root.handlers[:] = self._saved_handlers
        self.root.setLevel(self._saved_level)
        self.root.propagate = self._saved_propagate

    def test_service_entries_never_echo_through_the_bridge(self) -> None:
        service = LogService()
        attach_stdlib_bridge(service)
        service.record("INFO", "accounts", "one line")
        tail = service.tail(100)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["message"], "one line")

    def test_service_logger_entries_never_echo(self) -> None:
        service = LogService()
        attach_stdlib_bridge(service)
        logger = ServiceLogger(service, "ratelimit")
        logger.info("policy set: %s", "2.0s")
        tail = service.tail(100)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["message"], "policy set: 2.0s")

    def test_many_entries_stay_linear(self) -> None:
        service = LogService()
        attach_stdlib_bridge(service)
        for i in range(200):
            service.record("INFO", "accounts", f"line {i}")
        self.assertEqual(len(service.tail(1000)), 200)

    def test_raw_stdlib_records_still_flow_in(self) -> None:
        service = LogService()
        attach_stdlib_bridge(service)
        logging.getLogger("libzent.alpha.exporter").info("exporter loaded (trigger=%s)", "export")
        tail = service.tail(10)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["source"], "alpha:exporter")
        self.assertIn("exporter loaded", tail[0]["message"])

    def test_bridge_attach_is_idempotent(self) -> None:
        service = LogService()
        first = attach_stdlib_bridge(service)
        second = attach_stdlib_bridge(service)
        self.assertIs(first, second)

    def test_detach_restores_pre_attach_propagate(self) -> None:
        """Round-3: attach pinned propagate=False forever; detach must
        restore the value observed at FIRST attach once the last bridge
        goes away."""
        from libzentsuite.core.logging_service import _BRIDGE_PRIOR_PROPAGATE

        service = LogService()
        self.root.propagate = True
        attach_stdlib_bridge(service)
        self.assertFalse(self.root.propagate)  # pinned while attached
        attach_stdlib_bridge(service)  # second attach keeps prior
        self.root.propagate = False  # bystander flips it
        detach_stdlib_bridge()
        self.assertTrue(self.root.propagate)  # first-attach value back
        self.assertNotIn("libzent", _BRIDGE_PRIOR_PROPAGATE)
        detach_stdlib_bridge()  # idempotent, no restore
        self.assertTrue(self.root.propagate)

    def test_attach_closes_quiet_mode_propagate_hole(self) -> None:
        service = LogService()
        attach_stdlib_bridge(service)
        self.assertFalse(self.root.propagate)

    def test_raw_stdlib_line_prints_exactly_once(self) -> None:
        """REGRESSION (residual defect found by the 10-agent round): a raw
        stdlib line reached stderr TWICE — once directly, once via the
        re-mirror of its bridge-ingested record. ingest_context suppresses
        the redundant mirror."""
        service = LogService()
        attach_stdlib_bridge(service)
        probe = CountingHandler()
        self.root.addHandler(probe)
        logging.getLogger("libzent.loader").info("discovered once")
        self.assertEqual(probe.messages.count("discovered once"), 1)

    def test_direct_records_still_mirror_once(self) -> None:
        """Mirror suppression must NOT go too far: direct LogService calls
        keep their single stderr mirror (managers have no other console)."""
        service = LogService()
        attach_stdlib_bridge(service)
        probe = CountingHandler()
        self.root.addHandler(probe)
        service.record("INFO", "accounts", "direct line")
        self.assertEqual(probe.messages.count("direct line"), 1)

    def test_concurrent_n_events_equal_n_records(self) -> None:
        """8 threads x 250 records: canonical count must be exactly 2000."""
        service = LogService()
        attach_stdlib_bridge(service)

        def burst(name: str) -> None:
            for i in range(250):
                service.record("INFO", name, f"m{i}")

        threads = [threading.Thread(target=burst, args=(f"t{t}",)) for t in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(service.tail(5000)), 2000)
        self.assertEqual(service.stats()["entries"], 2000)

    def test_hostile_handler_no_recursion(self) -> None:
        """A handler that LOGS inside emit() must not loop through the
        bridge. (It logs to an OUTSIDE logger: a handler that logs into its
        OWN tree self-recurses by stdlib semantics — that is not our bug;
        the invariant under test is that LogService never amplifies.)"""

        class Hostile(CountingHandler):
            def __init__(self) -> None:
                super().__init__()
                self.spawns = 0

            def emit(self, record: logging.LogRecord) -> None:
                self.spawns += 1
                logging.getLogger("outside.tree").info("spawned %d", self.spawns)
                super().emit(record)

        service = LogService()
        attach_stdlib_bridge(service)
        hostile = Hostile()
        self.root.addHandler(hostile)
        service.record("INFO", "a", "one")
        self.assertEqual(len(service.tail(50)), 1)  # canonical only
        self.assertEqual(hostile.spawns, 1)  # saw the mirror once
        self.root.removeHandler(hostile)

    def test_critical_maps_to_error(self) -> None:
        service = LogService()
        attach_stdlib_bridge(service)
        logging.getLogger("libzent.a").critical("die")
        self.assertEqual(service.tail(1)[0]["severity"], "ERROR")
        logger = ServiceLogger(service, "x")
        logger.log(50, "fatal-ish")
        self.assertEqual(service.tail(1)[0]["severity"], "ERROR")

    def test_db_failure_silent_counted_single_attempt(self) -> None:
        class BoomRepo:
            def __init__(self) -> None:
                self.calls = 0

            async def add(self, entry: dict[str, Any]) -> None:
                self.calls += 1
                raise sqlite3.OperationalError("disk full")

        async def scenario() -> tuple[LogService, BoomRepo]:
            service = LogService()
            repo = BoomRepo()
            with contextlib.redirect_stderr(io.StringIO()):  # silence the burst one-liner
                await service.start_persistence(repo)
                service.record("ERROR", "db", "x")  # must not raise
                await service.stop_persistence()  # drain (the one add fails)
            return service, repo

        service, repo = asyncio.run(scenario())
        self.assertEqual(repo.calls, 1)  # exactly one attempt
        self.assertEqual(len(service.tail(10)), 1)  # canonical entry kept
        self.assertEqual(service.stats()["db_failures"], 1)  # observable

    def test_subscriber_failure_isolated_and_counted(self) -> None:
        service = LogService()
        seen: list[LogEntry] = []

        def exploding(_entry: LogEntry) -> None:
            """Deliberately raises: subscriber isolation under test."""
            raise ZeroDivisionError("division by zero")

        service.subscribe(exploding)  # raises
        service.subscribe(seen.append)  # still runs
        service.record("INFO", "dash", "x")
        self.assertEqual(seen and len(seen), 1)
        self.assertEqual(len(service.tail(5)), 1)
        self.assertEqual(service.stats()["subscriber_failures"], 1)

    def test_exception_logging(self) -> None:
        service = LogService()
        logger = ServiceLogger(service, "sched")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("task failed")
        self.assertIn("ValueError: boom", service.tail(1)[0]["message"])

    def test_third_party_record_via_bridge(self) -> None:
        service = LogService()
        bridge = StdlibBridge(service)
        record = logging.LogRecord(
            "discord.http", logging.ERROR, "f", 1, "rate limited", None, None
        )
        bridge.emit(record)
        entry = service.tail(1)[0]
        self.assertEqual(entry["source"], "ext:discord.http")
        self.assertEqual(entry["severity"], "ERROR")

    def test_hostile_payload_never_raises(self) -> None:
        """REGRESSION (audit round 4): record()'s degraded-mode fallback
        itself called str(source) — an object with a broken __str__ as BOTH
        source and message escaped the 'never raises' contract."""

        class Hostile:
            def __str__(self) -> str:
                raise RuntimeError("unprintable")

        service = LogService()
        bad_severity: Any = Hostile()  # wrong shape on purpose
        bad_source: Any = Hostile()  # wrong shape on purpose
        bad_message: Any = Hostile()  # wrong shape on purpose
        out = service.record(bad_severity, bad_source, bad_message)
        self.assertEqual(out["severity"], "INFO")
        self.assertEqual(out["source"], "<unloggable source>")
        self.assertEqual(out["message"], "<unloggable message>")

    def test_ring_eviction_and_volume(self) -> None:
        service = LogService(capacity=100)
        for i in range(250):
            service.record("INFO", "v", f"m{i}")
        self.assertEqual(len(service.tail(1000)), 100)
        self.assertEqual(service.tail(1)[0]["message"], "m249")

    def test_large_message_handled(self) -> None:
        """Messages are capped at 16k chars (+ a visible truncation marker)
        so ring memory and DB rows stay bounded per entry."""
        service = LogService()
        service.record("INFO", "a", "A" * 100_000)
        entry = service.tail(1)[0]
        self.assertLessEqual(len(entry["message"]), 16_040)
        self.assertIn("(+", entry["message"])  # truncation is visible

    def test_bridge_ingested_record_single_db_row(self) -> None:
        class CountingRepo:
            def __init__(self) -> None:
                self.rows: list[dict[str, Any]] = []

            async def add(self, entry: dict[str, Any]) -> None:
                self.rows.append(entry)

        async def scenario() -> CountingRepo:
            service = LogService()
            repo = CountingRepo()
            await service.start_persistence(repo)
            attach_stdlib_bridge(service)
            logging.getLogger("libzent.loader").info("once to db")
            await service.stop_persistence()  # drain before the repo could close
            return repo

        repo = asyncio.run(scenario())
        self.assertEqual(len(repo.rows), 1)

    def test_detach_stdlib_bridge(self) -> None:
        service_a = LogService()
        attach_stdlib_bridge(service_a)
        self.assertGreaterEqual(detach_stdlib_bridge(), 1)
        logging.getLogger("libzent.a").info("post-detach")
        self.assertEqual(service_a.tail(10), [])  # no ingestion anymore
        # a fresh attach binds the NEW service (stale-bridge hazard closed)
        service_b = LogService()
        attach_stdlib_bridge(service_b)
        logging.getLogger("libzent.b").info("for b")
        self.assertEqual(len(service_b.tail(10)), 1)
        self.assertEqual(service_a.tail(10), [])
        detach_stdlib_bridge()

    def test_stop_persistence_detaches_sink(self) -> None:
        """Shutdown ordering (spec §29): stop_persistence() BEFORE
        db.close(); records after the stop stay in the ring only."""

        class CountingRepo:
            def __init__(self) -> None:
                self.rows: list[dict[str, Any]] = []

            async def add(self, entry: dict[str, Any]) -> None:
                self.rows.append(entry)

        async def scenario() -> tuple[LogService, CountingRepo]:
            service = LogService()
            repo = CountingRepo()
            await service.start_persistence(repo)
            service.record("INFO", "a", "before")
            await service.stop_persistence()  # the off-switch now
            service.record("INFO", "a", "after")  # no worker -> not persisted
            return service, repo

        service, repo = asyncio.run(scenario())
        self.assertEqual(len(repo.rows), 1)
        self.assertEqual(len(service.tail(10)), 2)  # ring unaffected


class TestRound2Audit(unittest.TestCase):
    """Round-2 hostile coverage: foreign-thread bridge attach/detach, ts
    ordering under concurrent record(), hostile exc payloads, unknown-account
    filters, ring bounds with a stalled consumer, post-stop drops."""

    def setUp(self) -> None:
        self.root = logging.getLogger("libzent")
        self._saved_handlers = list(self.root.handlers)
        self._saved_level = self.root.level
        self._saved_propagate = self.root.propagate
        self.root.handlers[:] = [h for h in self._saved_handlers if not isinstance(h, StdlibBridge)]

    def tearDown(self) -> None:
        self.root.handlers[:] = self._saved_handlers
        self.root.setLevel(self._saved_level)
        self.root.propagate = self._saved_propagate

    def test_bridge_attach_detach_from_foreign_thread(self) -> None:
        service = LogService()

        def foreign() -> None:
            attach_stdlib_bridge(service)
            logging.getLogger("libzent.foreign").info("from thread")

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join()
        self.assertEqual(len(service.tail(10)), 1)  # ingested

        def foreign_detach() -> None:
            detach_stdlib_bridge()

        thread = threading.Thread(target=foreign_detach)
        thread.start()
        thread.join()
        logging.getLogger("libzent.foreign").info("after detach")
        self.assertEqual(len(service.tail(10)), 1)  # not ingested

    def test_record_from_threads_ts_monotonic_in_ring(self) -> None:
        """ts is captured under the ring lock: ring order and timestamp
        order agree even under concurrent record() from many threads."""
        service = LogService()
        received: list[LogEntry] = []
        service.subscribe(received.append)

        def burst(name: str) -> None:
            for i in range(200):
                service.record("INFO", name, f"m{i}")

        threads = [threading.Thread(target=burst, args=(f"t{t}",)) for t in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        tail = service.tail(10_000)
        self.assertEqual(len(tail), 1600)
        self.assertEqual(len(received), 1600)
        timestamps = [e["ts"] for e in tail]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_record_exc_instance_with_broken_str(self) -> None:
        class Unprintable(Exception):
            def __str__(self) -> str:
                raise RuntimeError("no")

        service = LogService()
        bad_message: Any = Unprintable()  # wrong shape on purpose
        out = service.record("ERROR", "sched", bad_message)
        self.assertEqual(out["severity"], "INFO")  # degraded path is INFO
        self.assertEqual(out["message"], "<unloggable message>")
        self.assertEqual(out["source"], "sched")
        self.assertEqual(len(service.tail(5)), 0)  # degraded: not canonical

    def test_unknown_account_plugin_and_severity_filters(self) -> None:
        service = LogService()
        service.record("INFO", "s", "a1", account="alpha")
        service.record("ERROR", "s", "a2", account="alpha", plugin="roll")
        self.assertEqual(service.tail(10, account="ghost"), [])
        self.assertEqual(len(service.tail(10, account="alpha")), 2)
        self.assertEqual(service.tail(10, plugin="ghost"), [])
        self.assertEqual(service.tail(10, plugin="roll")[0]["message"], "a2")
        self.assertEqual(service.tail(10, severity="error")[0]["message"], "a2")
        self.assertEqual(service.tail(10, source="other"), [])
        self.assertEqual(service.tail(0), [])

    def test_ring_bound_holds_when_consumer_stalls(self) -> None:
        """A slow CONSUMER (tail reader) cannot grow memory past the ring:
        the oldest entries evict and subscribers are still notified."""
        calls: list[LogEntry] = []
        service = LogService(capacity=50)
        service.subscribe(calls.append)
        for i in range(300):
            service.record("INFO", "stall", f"m{i}")  # consumer never reads
        tail = service.tail(1000)  # late first read
        self.assertEqual(len(tail), 50)
        self.assertEqual(tail[-1]["message"], "m299")
        self.assertEqual(tail[0]["message"], "m250")
        self.assertEqual(len(calls), 300)  # live tail never starved

    def test_post_stop_records_counted_dropped(self) -> None:
        class CountingRepo:
            def __init__(self) -> None:
                self.rows: list[dict[str, Any]] = []

            async def add(self, entry: dict[str, Any]) -> None:
                self.rows.append(entry)

        async def scenario() -> tuple[LogService, CountingRepo]:
            service = LogService()
            repo = CountingRepo()
            await service.start_persistence(repo, interval=0.01)
            service.record("INFO", "a", "kept")
            await service.stop_persistence()
            service.record("INFO", "a", "lost-1")
            service.record("WARNING", "a", "lost-2")
            return service, repo

        service, repo = asyncio.run(scenario())
        self.assertEqual(len(repo.rows), 1)
        self.assertEqual(len(service.tail(10)), 3)  # ring unaffected
        self.assertEqual(service.stats()["dropped"], 2)  # observable
        self.assertEqual(service.stats()["entries"], 3)


if __name__ == "__main__":
    unittest.main()


class StderrPipelineClaims(unittest.TestCase):
    """Review pass-3 #1: the listener stops only when the LAST live
    framework claim releases — an owner shutting down first must not
    silence a framework built after it."""

    def test_last_release_stops_first_does_not(self) -> None:
        import logging

        from libzentsuite.core import logging_service as m

        # snapshot + restore the process-global state (the pipeline AND
        # the stdlib logger it touches — configure_stderr flips
        # propagate, which the pipeline stop does not restore)
        users_before = m._STDERR_USERS
        listener_before = m._STDERR_LISTENER
        configured_before = m._STDERR_CONFIGURED
        lg = logging.getLogger("libzent")
        handlers_before = list(lg.handlers)
        propagate_before = lg.propagate
        try:
            m.stop_stderr_pipeline()  # known idle floor
            created = m.claim_stderr_pipeline()
            self.assertTrue(created, "first claim creates the pipeline")
            self.assertTrue(m.stderr_pipeline_active())
            borrowed = m.claim_stderr_pipeline()
            self.assertFalse(borrowed, "second claim borrows")
            m.release_stderr_pipeline()  # OWNER (first) shuts down
            self.assertTrue(
                m.stderr_pipeline_active(),
                "the live borrower keeps the pipeline after the owner exits",
            )
            m.release_stderr_pipeline()  # last claim releases
            self.assertFalse(m.stderr_pipeline_active())
            m.release_stderr_pipeline()  # stray release is a no-op
            self.assertFalse(m.stderr_pipeline_active())
        finally:
            m._STDERR_USERS = users_before
            m._STDERR_LISTENER = listener_before
            m._STDERR_CONFIGURED = configured_before
            lg.handlers[:] = handlers_before
            lg.propagate = propagate_before


class DiscordTreeCaptureTests(unittest.TestCase):
    """The contract that logs never interrupt the command interface:
    the fork's own logger tree (discord.*) bridges into the structured
    log instead of riding lastResort onto stderr (owner rule
    2026-11-17 — 'accounts start' used to flood the REPL with client
    warnings)."""

    def test_discord_tree_bridges_and_stops_propagation(self) -> None:
        import logging as _logging

        from libzentsuite.core.logging_service import (
            LogService,
            attach_stdlib_bridge,
            detach_stdlib_bridge,
        )

        saved_handlers = list(_logging.getLogger("discord").handlers)
        saved_propagate = _logging.getLogger("discord").propagate
        saved_level = _logging.getLogger("discord").level
        try:
            service = LogService()
            attach_stdlib_bridge(service, "discord")
            root = _logging.getLogger("discord")
            self.assertFalse(root.propagate)  # no lastResort leak
            self.assertTrue(any(isinstance(h, StdlibBridge) for h in root.handlers))
            _logging.getLogger("discord.client").warning("gateway hiccup (synthetic)")
            rows = service.tail(10, severity="WARNING")
            self.assertTrue(any("gateway hiccup" in r.get("message", "") for r in rows))
        finally:
            detach_stdlib_bridge("discord")
            root = _logging.getLogger("discord")
            root.handlers = saved_handlers
            root.propagate = saved_propagate
            root.setLevel(saved_level)
