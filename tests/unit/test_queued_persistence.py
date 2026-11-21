"""Queued persistence matrix (spec §29) — LogService, AuditRecorder, scheduler.

The invariant under test: application/event paths NEVER touch SQLite
synchronously. record() enqueues onto a thread-safe queue; one async drain
worker owns every ``repo.add`` await; failures are counted, never raised;
overflow is dropped and counted; stop drains within a bounded timeout.

Standalone: no database, no event bus fixtures beyond minimal fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sqlite3
import time
import unittest
from typing import Any

from core.audit import AuditRecorder
from core.event_bus import EventBus
from core.logging_service import LogService
from core.task_scheduler import TaskScheduler


# ----------------------------------------------------------------- fakes
class AsyncLogRepo:
    """Counting LogRepository: records every awaited add, optional delay."""

    def __init__(self, delay: float = 0.0) -> None:
        self.added: list[dict[str, Any]] = []
        self.delay = delay

    async def add(self, entry: dict[str, Any]) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.added.append(dict(entry))


class FailingLogRepo:
    """Every add raises (disk full shape)."""

    def __init__(self) -> None:
        self.calls = 0

    async def add(self, entry: dict[str, Any]) -> None:
        self.calls += 1
        raise sqlite3.OperationalError("disk full")


class AsyncAuditRepo:
    """Counting AuditRepository (async add/recent)."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    async def add(self, entry: dict[str, Any]) -> None:
        self.added.append(dict(entry))

    async def recent(self, limit: int = 100, account: str | None = None) -> list[dict[str, Any]]:
        return []


class FakeLogger:
    def info(self, source: str, message: str, **ctx: Any) -> None:
        pass

    def warning(self, source: str, message: str, **ctx: Any) -> None:
        pass

    def error(self, source: str, message: str, **ctx: Any) -> None:
        pass


class FakeLimiter:
    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None

    async def acquire(
        self, action: str, account: str | None, plugin: str | None, priority: Any
    ) -> FakeGrant:
        if self.gate is not None and not self.gate.is_set():
            await self.gate.wait()
        return FakeGrant()

    def report_server_limit(
        self, account: str, action: str, retry_after: float, global_: bool = False
    ) -> None:
        pass


class FakeGrant:
    wait_s = 0.0
    jitter_s = 0.0


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, task: Any) -> None:
        self.calls.append(task)


class FakeAudit:
    """Sync audit sink — AuditRecorder.record() stays non-async by contract."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return dict(kwargs)


class AsyncHistoryRepo:
    """Async TaskHistoryRepository fake (add/recent awaited)."""

    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []

    async def add(self, entry: dict[str, Any]) -> Any:
        self.add_calls.append(dict(entry))
        return entry["id"]

    async def recent(self, limit: int = 50, account: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = list(self.add_calls)
        if account:
            rows = [r for r in rows if r.get("account") == account]
        return rows[-limit:] if limit > 0 else []


# ------------------------------------------------------------ LogService
class TestLogServiceQueuedPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_n_records_equal_n_repo_adds_exactly(self) -> None:
        """(a) One canonical persisted row per source event — no more, no
        less — after a full drain + stop."""
        service = LogService()
        repo = AsyncLogRepo()
        await service.start_persistence(repo, interval=0.01)
        for i in range(200):
            service.record("INFO", "a", f"m{i}")
        await service.stop_persistence(timeout=5.0)
        self.assertEqual(len(repo.added), 200)
        self.assertEqual([e["message"] for e in repo.added], [f"m{i}" for i in range(200)])

    async def test_record_never_blocks_on_sqlite(self) -> None:
        """(b) With a SLOW repo being drained by the worker, 1000 record()
        calls average under 2ms — the loop never waits on SQLite."""
        import gc

        service = LogService()
        repo = AsyncLogRepo(delay=0.2)  # each add costs 200ms on the worker
        await service.start_persistence(repo, batch=10, interval=0.01)
        await asyncio.sleep(0.05)  # worker is inside a slow add right now
        for _i in range(100):  # warmup: logger cache, ring pages
            service.record("INFO", "lat", "warmup")
        gc.collect()
        gc.disable()  # a GC pause is environment noise, not record() cost
        try:
            t0 = time.perf_counter()
            worst = 0.0
            for i in range(1000):
                call_start = time.perf_counter()
                service.record("INFO", "lat", f"m{i}")
                worst = max(worst, time.perf_counter() - call_start)
            elapsed = time.perf_counter() - t0
        finally:
            gc.enable()
        # The 2ms budget as a MEAN: a single OS descheduling hiccup on a
        # loaded box must not fail the suite, but the average must hold.
        self.assertLess(elapsed / 1000, 0.002)
        # Hard per-call ceiling: ONE blocked-on-SQLite record() would cost
        # >= 200ms here. 100ms tolerates OS descheduling under the suite's
        # parallel workers (observed 30ms call noise, 1s loop deschedules)
        # while staying half the blocked signature.
        self.assertLess(worst, 0.1)
        self.assertLess(elapsed, 5.0)  # blocking would cost ~200s here
        with contextlib.redirect_stderr(io.StringIO()):
            await service.stop_persistence(timeout=0.5)  # bounded: slow repo

    async def test_overflow_drops_counted_ring_unaffected(self) -> None:
        """(c) Queue capped at 10_000 in-flight: the excess is dropped FROM
        PERSISTENCE, counted in stats()['dropped']; ring/counters keep
        everything, and a stop drains the bounded remainder."""
        service = LogService()
        repo = AsyncLogRepo()
        await service.start_persistence(repo, interval=3600.0)  # worker parked
        await asyncio.sleep(0)  # let the worker reach its idle sleep
        for i in range(10_500):
            service.record("INFO", "of", f"m{i}")
        stats = service.stats()
        self.assertEqual(stats["dropped"], 500)
        self.assertEqual(stats["entries"], 10_500)  # counters unaffected
        self.assertEqual(len(service.tail(1)), 1)  # ring alive
        await service.stop_persistence(timeout=30.0)  # bounded inline drain
        self.assertEqual(len(repo.added), 10_000)  # cap, not 10_500

    async def test_failing_repo_counts_never_raises_into_record(self) -> None:
        """(d) A repo that always fails: every drain attempt is counted in
        db_failures, record() never raises, canonical entries survive."""
        service = LogService()
        repo = FailingLogRepo()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            await service.start_persistence(repo, interval=0.01)
            for i in range(5):
                service.record("ERROR", "db", f"x{i}")  # must not raise
            await service.stop_persistence()
        self.assertEqual(repo.calls, 5)  # one attempt each
        self.assertEqual(service.stats()["db_failures"], 5)  # observable
        self.assertEqual(len(service.tail(10)), 5)  # ring intact
        # failure-burst policy: at most ONE raw stderr one-liner
        self.assertEqual(stderr.getvalue().count("log persistence failing"), 1)

    async def test_stop_drains_everything_within_timeout(self) -> None:
        """(e) Records enqueued while the worker is parked idle are all
        flushed by stop_persistence() alone; stop is idempotent."""
        service = LogService()
        repo = AsyncLogRepo()
        await service.start_persistence(repo, interval=3600.0)  # parked
        await asyncio.sleep(0)
        for i in range(100):
            service.record("INFO", "dr", f"m{i}")
        await service.stop_persistence(timeout=5.0)
        self.assertEqual(len(repo.added), 100)
        await service.stop_persistence()  # idempotent, no error
        self.assertEqual(len(repo.added), 100)

    async def test_no_persistence_started_zero_extra_work(self) -> None:
        """(sim/headless boot) record() without start_persistence: queue
        unused, ring + stats behave exactly as before, no warnings."""
        service = LogService()
        stats_before = service.stats()
        for i in range(50):
            service.record("INFO", "sim", f"m{i}")
        self.assertEqual(len(service.tail(100)), 50)
        self.assertEqual(service.stats()["entries"], 50)
        self.assertEqual(service.stats()["dropped"], 0)
        self.assertEqual(stats_before.keys(), service.stats().keys())


# ---------------------------------------------------------- AuditRecorder
class TestAuditRecorderQueuedPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_n_records_equal_n_repo_adds_exactly(self) -> None:
        """(f) Same N==N contract as LogService."""
        recorder = AuditRecorder()
        repo = AsyncAuditRepo()
        await recorder.start_persistence(repo, interval=0.01)
        for i in range(120):
            recorder.record("alpha", "out", "message", 500, f"m{i}", wait_ms=1.5)
        await recorder.stop_persistence(timeout=5.0)
        self.assertEqual(len(repo.added), 120)
        self.assertEqual([e["content"] for e in repo.added], [f"m{i}" for i in range(120)])
        self.assertEqual([e["wait_ms"] for e in repo.added], [1.5] * 120)
        self.assertEqual(len(recorder.tail(10)), 10)  # ring independent of DB

    async def test_failing_repo_counted_never_raises(self) -> None:
        class Bomber:
            def __init__(self) -> None:
                self.calls = 0

            async def add(self, entry: dict[str, Any]) -> None:
                self.calls += 1
                raise RuntimeError("audit db down")

        recorder = AuditRecorder()
        bomber = Bomber()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            await recorder.start_persistence(bomber, interval=0.01)
            entry = recorder.record("alpha", "out", "message", 500, "fine")
            await recorder.stop_persistence()
        self.assertEqual(entry["content"], "fine")
        self.assertEqual(bomber.calls, 1)
        self.assertEqual(recorder.persistence_stats()["db_failures"], 1)
        self.assertEqual(stderr.getvalue().count("audit persistence failing"), 1)

    async def test_record_without_persistence_is_inert(self) -> None:
        recorder = AuditRecorder()
        entry = recorder.record("alpha", "out", "message", 500, "sim")
        self.assertEqual(entry["content"], "sim")
        self.assertEqual(len(recorder.tail(5)), 1)


# --------------------------------------------------- cooldowns (round 3)
class FlakyCooldownRepo:
    """In-memory cooldown repo whose clear()/clear_expired_for() can fail
    on demand, with call counters (the shapes database.repositories
    .cooldowns exposes)."""

    def __init__(self) -> None:
        # (account, plugin, profile, cooldown_key) -> row dict — the exact
        # key shape database.repositories.cooldowns persists under.
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.fail_clear = False
        self.clear_expired_for_calls = 0
        self.clear_calls = 0

    async def upsert(self, **kw: Any) -> None:
        key = (kw["account"], kw["plugin"], kw.get("profile", ""), kw["cooldown_key"])
        self.rows[key] = dict(kw)

    async def clear(self, a: str, p: str, k: str, profile: str = "") -> bool:
        self.clear_calls += 1
        if self.fail_clear:
            raise RuntimeError("db locked")
        return self.rows.pop((a, p, profile, k), None) is not None

    async def clear_expired_for(
        self, a: str, p: str, k: str, now: float, profile: str = ""
    ) -> bool:
        self.clear_expired_for_calls += 1
        if self.fail_clear:
            raise RuntimeError("db locked")
        row = self.rows.get((a, p, profile, k))
        if row and row["expires_at"] <= now:
            del self.rows[(a, p, profile, k)]
            return True
        return False

    async def clear_expired(self, now: float | None = None) -> int:
        return 0

    async def active_all(self, now: float | None = None) -> list[dict[str, Any]]:
        return list(self.rows.values())

    async def active_for(
        self, account: str, plugin: str | None = None, now: float | None = None
    ) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["account"] == account]

    async def get(
        self, a: str, p: str, k: str, now: float | None = None, profile: str = ""
    ) -> dict[str, Any] | None:
        row = self.rows.get((a, p, profile, k))
        return row if row and row["expires_at"] > now else None

    async def delete_account(self, account: str) -> int:
        return 0


class TestCooldownServiceFixes(unittest.IsolatedAsyncioTestCase):
    """Round-3 probes (probe_cooldowns.py) pinned: (a) a failed DB clear
    must not resurrect the cooldown on the next get(); (b) an expired
    memory row is re-checked against the repo ONCE and then dropped — no
    repeated clear_expired_for round-trips, no stale shadow over rows
    extended by another process; (c) set() survives numeric-string
    durations (the log used the raw value before the float() coercion)."""

    async def test_failed_db_clear_does_not_resurrect(self) -> None:
        from core.cooldowns import CooldownService

        repo = FlakyCooldownRepo()
        svc = CooldownService(repo)
        await svc.set("alfa", "roll", "wed", 60)
        self.assertTrue(await svc.is_active("alfa", "roll", "wed"))

        repo.fail_clear = True
        with self.assertRaises(RuntimeError):
            await svc.clear("alfa", "roll", "wed")  # DB delete fails
        # memory NOT popped first anymore: the cooldown survives intact...
        self.assertTrue(await svc.is_active("alfa", "roll", "wed"))
        # ...and the next get() cannot resurrect a half-cleared row
        self.assertIsNotNone(await svc.get("alfa", "roll", "wed"))
        repo.fail_clear = False
        self.assertTrue(await svc.clear("alfa", "roll", "wed"))
        self.assertIsNone(await svc.get("alfa", "roll", "wed"))

    async def test_expired_memory_row_rechecked_once_then_dropped(self) -> None:
        from core.cooldowns import CooldownService

        repo = FlakyCooldownRepo()
        clock = {"now": 1000.0}
        svc = CooldownService(repo, time_fn=lambda: clock["now"])
        await svc.set("beta", "p", "k", 10)
        clock["now"] += 999  # expired everywhere

        self.assertIsNone(await svc.get("beta", "p", "k"))
        self.assertNotIn(("beta", "p", "", "k"), svc._active)  # popped, not looming
        self.assertEqual(repo.clear_expired_for_calls, 1)
        self.assertNotIn(("beta", "p", "", "k"), repo.rows)  # lazily cleared

        # subsequent gets: plain memory miss -> one repo get, NO repeat of
        # the clear_expired_for round-trip on every call
        self.assertIsNone(await svc.get("beta", "p", "k"))
        self.assertIsNone(await svc.get("beta", "p", "k"))
        self.assertEqual(repo.clear_expired_for_calls, 1)

    async def test_expired_memory_row_adopts_extension_by_other_process(self) -> None:
        from core.cooldowns import CooldownService

        repo = FlakyCooldownRepo()
        clock = {"now": 1000.0}
        svc = CooldownService(repo, time_fn=lambda: clock["now"])
        await svc.set("beta", "p", "k", 10)
        clock["now"] += 999  # memory copy expired...
        # ...but another process EXTENDED the persisted row meanwhile
        repo.rows[("beta", "p", "", "k")]["expires_at"] = clock["now"] + 500

        row: Any = await svc.get("beta", "p", "k")
        self.assertIsNotNone(row)  # the extension wins over stale memory
        self.assertEqual(row["expires_at"], clock["now"] + 500)
        self.assertIn(("beta", "p", "", "k"), repo.rows)  # still persisted
        self.assertEqual(repo.clear_expired_for_calls, 1)  # scoped delete
        # and the adopted row serves subsequent gets from memory
        self.assertIsNotNone(await svc.get("beta", "p", "k"))
        self.assertEqual(repo.clear_expired_for_calls, 1)

    async def test_set_survives_numeric_string_duration(self) -> None:
        from core.cooldowns import CooldownService

        svc = CooldownService(FlakyCooldownRepo(), time_fn=lambda: 0.0)
        numeric_string: Any = "60"  # printf %f used to TypeError
        row = await svc.set("a", "p", "k", numeric_string)
        self.assertAlmostEqual(row["expires_at"], 60.0, places=6)
        self.assertAlmostEqual(await svc.remaining("a", "p", "k"), 60.0, places=6)


# ------------------------------------------------------------- scheduler
class TestSchedulerQueuedPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_write_awaited_and_audit_once(self) -> None:
        """(g) A completing task writes its history row through the AWAITED
        repo add (an un-awaited async fake would never record anything)
        and records exactly ONE outbound audit row."""
        audit = FakeAudit()
        repo = AsyncHistoryRepo()
        scheduler = TaskScheduler(
            EventBus(),
            FakeLogger(),
            FakeLimiter(),
            FakeSender(),
            audit,
            history_repo=repo,
        )
        scheduler.start()
        try:
            task = scheduler.submit("alpha", "p", "message", channel_id=9, content="x")
            for _ in range(200):  # settle deterministically
                if task.status == "completed" and repo.add_calls:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(task.status, "completed")
            self.assertEqual(len(repo.add_calls), 1)
            self.assertEqual(repo.add_calls[0]["id"], task.id)
            self.assertEqual(repo.add_calls[0]["status"], "completed")
            self.assertEqual(len(audit.calls), 1)
            self.assertEqual(audit.calls[0]["direction"], "out")
            self.assertEqual(audit.calls[0]["content"], "x")
        finally:
            await scheduler.stop(drain=False)

    async def test_sync_cancel_still_persists_via_scheduled_tail(self) -> None:
        """cancel() is a SYNC call site: the terminal status lands
        immediately, and the history row + TaskCancelled event follow via
        the scheduled async tail — never a synchronous repo call."""
        bus = EventBus()
        seen: list[str] = []

        def _on(event: Any) -> None:
            seen.append(type(event).__name__)

        from core.event_bus import Event

        bus.subscribe(Event, _on)
        audit = FakeAudit()
        repo = AsyncHistoryRepo()
        limiter = FakeLimiter()
        scheduler = TaskScheduler(
            bus, FakeLogger(), limiter, FakeSender(), audit, history_repo=repo
        )
        scheduler.start()
        try:
            limiter.gate = asyncio.Event()  # park the task in acquire
            task = scheduler.submit("alpha", "p", "message", channel_id=1)
            await asyncio.sleep(0.02)  # picked up by its lane
            self.assertTrue(scheduler.cancel(task.id))
            self.assertEqual(task.status, "cancelled")  # immediate (sync)
            self.assertEqual(repo.add_calls, [])  # nothing sync-written
            for _ in range(200):
                if repo.add_calls:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(len(repo.add_calls), 1)  # scheduled tail ran
            self.assertEqual(repo.add_calls[0]["status"], "cancelled")
            self.assertIn("TaskCancelled", seen)
            self.assertEqual(audit.calls, [])  # never sent -> no row
            limiter.gate.set()
        finally:
            await scheduler.stop(drain=False)

    async def test_drain_timeout_stop_does_not_drop_inflight_tail(self) -> None:
        """(round 2) A drain=True stop whose budget expires while a task's
        finish tail (history write + audit) is still in flight must join the
        tail barrier BEFORE cancelling lanes — the row and the audit record
        may not be silently dropped by the timeout path."""

        class SlowRepo:
            def __init__(self) -> None:
                self.adds: list[dict[str, Any]] = []

            async def add(self, entry: dict[str, Any]) -> Any:
                await asyncio.sleep(0.05)
                self.adds.append(dict(entry))
                return entry["id"]

        audit = FakeAudit()
        repo = SlowRepo()
        scheduler = TaskScheduler(
            EventBus(),
            FakeLogger(),
            FakeLimiter(),
            FakeSender(),
            audit,
            history_repo=repo,
        )
        scheduler.start()
        try:
            task = scheduler.submit("alpha", "p", "message", channel_id=1)
            for _ in range(200):
                if task.status == "completed":
                    break
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.005)  # tail now awaiting the slow add
            await scheduler.stop(drain=True, timeout=0.001)  # budget blows mid-write
            self.assertEqual(task.status, "completed")
            self.assertEqual(len(repo.adds), 1)  # history row NOT dropped
            self.assertEqual(len(audit.calls), 1)  # audit row NOT dropped
        finally:
            await scheduler.stop(drain=False)


if __name__ == "__main__":
    unittest.main()
