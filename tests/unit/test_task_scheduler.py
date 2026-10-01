"""Unit tests for core.task_scheduler: lanes, priority, retry, cancel, history.

All timing-sensitive paths use the patched ``scheduler._sleep`` seam for
backoff and small real sleeps for lane scheduling; a gated fake limiter and
a holdable fake sender make checkpoints deterministic.
"""
from __future__ import annotations

import asyncio
import unittest
from collections import defaultdict, deque
from dataclasses import dataclass

from adapters.base import DiscordMessage
from core.errors import FlowBlocked, SchedulerError, ServerRateLimit
from core.event_bus import (
    Event,
    EventBus,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
    TaskQueued,
    TaskStarted,
)
from core.priority import Priority
from core.task_scheduler import Task, TaskScheduler


# ----------------------------------------------------------------- fakes
@dataclass
class FakeGrant:
    """Limiter grant with the attributes the scheduler reads."""

    action: str = ""
    account: str = ""
    plugin: str = ""
    wait_s: float = 0.0
    jitter_s: float = 0.0
    reason: str = ""


class FakeLimiter:
    """Duck-typed RateLimitManager: records acquires, optional gate, reports."""

    def __init__(self, wait_s: float = 0.0, jitter_s: float = 0.0) -> None:
        self.wait_s = wait_s
        self.jitter_s = jitter_s
        self.acquire_calls = []   # (action, account, plugin, priority)
        self.acquire_order = []   # account per acquire, in entry order
        self.reports = []         # report_server_limit args
        self.gate: asyncio.Event | None = None

    async def acquire(self, action, account, plugin, priority):
        self.acquire_calls.append((action, account, plugin, int(priority)))
        self.acquire_order.append(account)
        if self.gate is not None and not self.gate.is_set():
            await self.gate.wait()
        return FakeGrant(
            action=action, account=account, plugin=plugin,
            wait_s=self.wait_s, jitter_s=self.jitter_s,
        )

    def report_server_limit(self, account, action, retry_after, global_=False):
        self.reports.append((account, action, retry_after, global_))

    def block_acquires(self) -> asyncio.Event:
        """Make every future acquire wait until the returned event is set."""
        self.gate = asyncio.Event()
        return self.gate


class FakeSender:
    """Duck-typed sender: order + per-account in-flight tracking + outcomes."""

    def __init__(self) -> None:
        self.calls = []
        self.inflight = defaultdict(int)
        self.max_inflight = defaultdict(int)
        self.max_total = 0
        self.outcomes = deque()   # ("ok", obj|None) | ("sleep", s) | ("raise", exc)
        self.hold: asyncio.Event | None = None

    async def __call__(self, task: Task):
        self.calls.append(task)
        self.inflight[task.account] += 1
        self.max_inflight[task.account] = max(
            self.max_inflight[task.account], self.inflight[task.account]
        )
        self.max_total = max(self.max_total, sum(self.inflight.values()))
        try:
            if self.hold is not None and not self.hold.is_set():
                await self.hold.wait()
            if self.outcomes:
                outcome = self.outcomes.popleft()
            else:
                outcome = ("ok", None)
            kind = outcome[0]
            if kind == "ok":
                return outcome[1] if outcome[1] is not None else self._message(task)
            if kind == "sleep":
                await asyncio.sleep(outcome[1])
                return self._message(task)
            if kind == "raise":
                raise outcome[1]
            raise AssertionError(f"unknown scripted outcome {outcome!r}")
        finally:
            self.inflight[task.account] -= 1

    @staticmethod
    def _message(task: Task) -> DiscordMessage:
        return DiscordMessage(
            id=task.id * 100, channel_id=task.channel_id or 0,
            author_id=1, content=task.content or "",
        )


class FakeAudit:
    """Captures audit.record kwargs."""

    def __init__(self) -> None:
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)
        return dict(kwargs)


class FakeLogger:
    """LogService-style logger: info(source, message)."""

    def __init__(self) -> None:
        self.entries = []

    def info(self, source, message, **ctx):
        self.entries.append(("INFO", source, message))

    def warning(self, source, message, **ctx):
        self.entries.append(("WARNING", source, message))

    def error(self, source, message, **ctx):
        self.entries.append(("ERROR", source, message))


class FakeHistoryRepo:
    """Dict-backed TaskHistoryRepository (add/update/recent), fault-togglable."""

    def __init__(self) -> None:
        self.rows = {}
        self.add_calls = []
        self.update_calls = []
        self.fail = False

    def add(self, entry):
        if self.fail:
            raise RuntimeError("task history db down")
        self.add_calls.append(dict(entry))
        self.rows[entry["id"]] = dict(entry)
        return entry["id"]

    def update(self, task_id, **fields):
        if self.fail:
            raise RuntimeError("task history db down")
        self.update_calls.append((task_id, fields))
        self.rows[task_id].update(fields)

    def recent(self, limit=50, account=None):
        if self.fail:
            raise RuntimeError("task history db down")
        rows = sorted(self.rows.values(), key=lambda r: r["id"])
        if account:
            rows = [r for r in rows if r.get("account") == account]
        return rows[-limit:] if limit > 0 else []


class EventRecorder:
    """Subscribes to the Event base so it sees every event on the bus."""

    def __init__(self, bus: EventBus) -> None:
        self.seen = []
        self._unsub = bus.subscribe(Event, self._on)

    def _on(self, event) -> None:
        self.seen.append(event)

    def of(self, *types):
        return [e for e in self.seen if isinstance(e, types)]

    def close(self) -> None:
        self._unsub()


# ----------------------------------------------------------------- base
class SchedulerTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.recorder = EventRecorder(self.bus)
        self.limiter = FakeLimiter()
        self.sender = FakeSender()
        self.audit = FakeAudit()
        self.repo = FakeHistoryRepo()
        self.scheduler = TaskScheduler(
            self.bus, FakeLogger(), self.limiter, self.sender, self.audit,
            default_timeout=5.0, retry_backoff=0.5, history_repo=self.repo,
        )
        self.sleeps = []
        self.patched_sleep = self.scheduler._sleep

        async def instant_sleep(seconds):
            self.sleeps.append(seconds)

        self.scheduler._sleep = instant_sleep
        self.scheduler.start()

    async def asyncTearDown(self) -> None:
        if not self.scheduler._stopped:
            await self.scheduler.stop(drain=False, timeout=1.0)
        self.recorder.close()

    async def settle(self, seconds: float = 0.05) -> None:
        """Let lanes, publish tasks and handlers run to completion."""
        await asyncio.sleep(seconds)

    async def wait_until(self, predicate, timeout: float = 2.0) -> bool:
        """Poll a predicate until true or timeout (deterministic-ish waits)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.005)
        return predicate()


# ------------------------------------------------------- lifecycle + events
class TestLifecycle(SchedulerTestBase):
    async def test_submit_lifecycle_events_in_order(self) -> None:
        task = self.scheduler.submit(
            "alpha", "soulcopy", "message", channel_id=123, content="hello"
        )
        await self.settle()
        self.assertEqual(task.status, "completed")
        self.assertEqual(
            [type(e).__name__ for e in self.recorder.of(TaskQueued, TaskStarted, TaskCompleted)],
            ["TaskQueued", "TaskStarted", "TaskCompleted"],
        )
        queued = self.recorder.of(TaskQueued)[0]
        self.assertEqual((queued.account, queued.task_id, queued.plugin, queued.action),
                         ("alpha", task.id, "soulcopy", "message"))
        completed = self.recorder.of(TaskCompleted)[0]
        self.assertEqual(completed.task_id, task.id)
        self.assertGreaterEqual(completed.duration_s, 0.0)
        self.assertIn("DiscordMessage", task.result_summary)
        self.assertIn("ch=123", task.result_summary)
        self.assertLess(len(task.result_summary), 80)
        self.assertEqual([t.id for t in self.sender.calls], [task.id])
        self.assertEqual(task.attempts, 0)
        self.assertEqual(task.error, None)
        self.assertIsNotNone(task.started_at)
        self.assertIsNotNone(task.finished_at)

    async def test_grant_wait_and_jitter_stored_and_audited(self) -> None:
        self.limiter.wait_s = 0.25
        self.limiter.jitter_s = 0.1
        task = self.scheduler.submit("alpha", "p", "message", channel_id=9, content="x")
        await self.settle()
        self.assertEqual(task.grant_wait_ms, 250.0)
        self.assertEqual(task.grant_jitter_ms, 100.0)
        self.assertEqual(len(self.audit.calls), 1)
        call = self.audit.calls[0]
        self.assertEqual(call["account"], "alpha")
        self.assertEqual(call["direction"], "out")
        self.assertEqual(call["action"], "message")
        self.assertEqual(call["channel_id"], 9)
        self.assertEqual(call["content"], "x")
        self.assertEqual(call["wait_ms"], 250.0)
        self.assertEqual(call["jitter_ms"], 100.0)
        self.assertEqual(call["note"], "plugin=p task=1")  # no attempts suffix on first-try success

    async def test_default_name_and_get(self) -> None:
        task = self.scheduler.submit("alpha", "soulcopy", "dm", user_id=42, content="hi")
        await self.settle()
        self.assertEqual(task.name, "soulcopy:dm")
        self.assertIs(self.scheduler.get(task.id), task)   # live
        self.assertIs(self.scheduler.get(task.id), task)   # finished fallback keeps the object
        self.assertIsNone(self.scheduler.get(9999))


# --------------------------------------------------- serialization + fairness
class TestLanes(SchedulerTestBase):
    async def test_one_account_tasks_are_serialized(self) -> None:
        for i in range(3):
            self.scheduler.submit("alpha", "p", "message", channel_id=1, content=f"m{i}")
            self.sender.outcomes.append(("sleep", 0.03))
        await self.settle(0.5)
        self.assertEqual(self.sender.max_inflight["alpha"], 1)
        self.assertEqual([t.content for t in self.sender.calls], ["m0", "m1", "m2"])

    async def test_two_accounts_run_concurrently(self) -> None:
        self.scheduler.submit("a1", "p", "message", channel_id=1)
        self.scheduler.submit("a2", "p", "message", channel_id=2)
        self.sender.outcomes.extend([("sleep", 0.08), ("sleep", 0.08)])
        await self.settle(0.5)
        self.assertEqual(self.sender.max_inflight["a1"], 1)
        self.assertEqual(self.sender.max_inflight["a2"], 1)
        self.assertEqual(self.sender.max_total, 2)  # truly parallel across accounts
        self.assertEqual(len(self.sender.calls), 2)

    async def test_priority_high_submitted_after_low_runs_first(self) -> None:
        low = self.scheduler.submit("alpha", "p", "message", content="low", priority=Priority.LOW)
        high = self.scheduler.submit("alpha", "p", "message", content="high", priority=Priority.HIGH)
        self.sender.outcomes.extend([("sleep", 0.01), ("sleep", 0.01)])
        await self.settle(0.3)
        self.assertEqual([t.content for t in self.sender.calls], ["high", "low"])
        self.assertEqual(high.status, "completed")
        self.assertEqual(low.status, "completed")

    async def test_cross_account_grant_priority_order(self) -> None:
        gate = self.limiter.block_acquires()
        low = self.scheduler.submit("alpha", "p", "message", priority=Priority.LOW)
        high = self.scheduler.submit("beta", "p", "message", priority=Priority.HIGH)
        await self.settle(0.1)
        # the shared grant gate lets the HIGH task enter limiter.acquire first
        self.assertEqual(self.limiter.acquire_order, ["beta", "alpha"])
        gate.set()
        await self.settle(0.2)
        self.assertEqual(low.status, "completed")
        self.assertEqual(high.status, "completed")

    async def test_limiter_receives_full_context(self) -> None:
        self.scheduler.submit("alpha", "soulcopy", "message", priority=Priority.HIGH)
        await self.settle()
        self.assertEqual(self.limiter.acquire_calls, [("message", "alpha", "soulcopy", 0)])


# ------------------------------------------------------------------ cancel
class TestCancel(SchedulerTestBase):
    async def test_cancel_queued_task(self) -> None:
        gate = self.limiter.block_acquires()
        t1 = self.scheduler.submit("alpha", "p", "message", channel_id=1)
        await self.settle(0.02)  # t1 picked up, blocked in acquire
        t2 = self.scheduler.submit("alpha", "p", "message", channel_id=2)
        t3 = self.scheduler.submit("alpha", "p", "message", channel_id=3)

        self.assertTrue(self.scheduler.cancel(t2.id))
        self.assertEqual(t2.status, "cancelled")
        self.assertFalse(self.scheduler.cancel(t2.id))  # already terminal
        await self.settle(0.01)  # let the fire-and-forget publish task run
        cancelled_events = self.recorder.of(TaskCancelled)
        self.assertEqual([e.task_id for e in cancelled_events], [t2.id])

        gate.set()
        await self.settle(0.3)
        self.assertEqual([t.id for t in self.sender.calls], [t1.id, t3.id])
        self.assertEqual(t1.status, "completed")
        self.assertEqual(t3.status, "completed")

    async def test_cancel_at_grant_checkpoint_skips_sender(self) -> None:
        gate = self.limiter.block_acquires()
        task = self.scheduler.submit("alpha", "p", "message", channel_id=5, content="late")
        await self.wait_until(lambda: len(self.limiter.acquire_calls) == 1)

        self.assertTrue(self.scheduler.cancel(task.id))
        self.assertEqual(task.status, "cancelled")  # immediate, no worker ran it

        gate.set()  # the parked worker wakes and aborts at its checkpoint
        await self.settle(0.1)
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(len(self.recorder.of(TaskCancelled)), 1)
        self.assertEqual(len(self.repo.add_calls), 1)
        self.assertEqual(self.repo.add_calls[0]["status"], "cancelled")
        # AUDIT INTEGRITY (regression): a cancel that NEVER invoked the
        # sender must not write an outbound audit row — audit timestamps
        # are read as real send times by the pacing checks
        self.assertEqual(self.audit.calls, [])

    async def test_cancel_mid_send_is_audited(self) -> None:
        """Cancelled AFTER the sender ran (checkpoint 2): the API call went
        out, so the outbound audit row must exist."""
        task = self.scheduler.submit("alpha", "p", "message", channel_id=1)
        self.sender.outcomes.append(("sleep", 0.08))
        await self.settle(0.02)  # running inside the sender
        self.assertTrue(self.scheduler.cancel(task.id))
        await self.settle(0.3)
        self.assertEqual(len(self.audit.calls), 1)
        self.assertEqual(self.audit.calls[0]["direction"], "out")

    async def test_cancel_running_discards_result_after_sender_returns(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", channel_id=1)
        self.sender.outcomes.append(("sleep", 0.08))
        await self.settle(0.02)  # task is running inside the sender
        self.assertTrue(self.scheduler.cancel(task.id))
        await self.settle(0.3)
        self.assertEqual(len(self.sender.calls), 1)   # sender did run...
        self.assertEqual(task.status, "cancelled")    # ...but the result is discarded
        self.assertIsNone(task.result_summary)
        self.assertEqual(len(self.recorder.of(TaskCompleted)), 0)

    async def test_cancel_matching_counts_queued_and_running(self) -> None:
        gate = self.limiter.block_acquires()
        t1 = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.02)               # running (blocked in acquire)
        t2 = self.scheduler.submit("alpha", "p", "message")   # queued
        t3 = self.scheduler.submit("alpha", "other", "poke")  # queued, other plugin
        t4 = self.scheduler.submit("beta", "p", "message")    # other account

        self.assertEqual(self.scheduler.cancel_matching(account="alpha", plugin="p"), 2)
        self.assertEqual(t1.status, "cancelled")   # running -> immediate here (still queued-status)
        t2_ref = self.scheduler.get(t2.id)
        gate.set()
        await self.settle(0.3)
        self.assertEqual(t2_ref.status, "cancelled")
        self.assertEqual(t3.status, "completed")
        self.assertEqual(t4.status, "completed")

    async def test_cancel_unknown_task(self) -> None:
        self.assertFalse(self.scheduler.cancel(424242))


# ------------------------------------------------------------------ retry
class TestRetry(SchedulerTestBase):
    async def test_retry_on_server_rate_limit(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", channel_id=1, retries=2)
        self.sender.outcomes.append(("raise", ServerRateLimit(2.0, True)))
        await self.settle(0.2)

        self.assertEqual(self.limiter.reports, [("alpha", "message", 2.0, True)])
        self.assertEqual(len(self.sender.calls), 2)   # failed once, retried, succeeded
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.attempts, 1)
        self.assertEqual(self.sleeps, [0.5])           # first backoff = retry_backoff
        self.assertEqual(len(self.audit.calls), 1)     # audited exactly once (terminal)
        self.assertEqual(self.audit.calls[0]["note"], "plugin=p task=1 attempts=2")

    async def test_retry_on_connection_error(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", retries=1)
        self.sender.outcomes.append(("raise", ConnectionError("socket exploded")))
        await self.settle(0.2)
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.attempts, 1)
        self.assertEqual(self.sleeps, [0.5])
        self.assertEqual(len(self.sender.calls), 2)

    async def test_backoff_doubles_and_caps(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", retries=3)
        self.sender.outcomes.extend([
            ("raise", ConnectionError("boom 1")),
            ("raise", ConnectionError("boom 2")),
            ("raise", ConnectionError("boom 3")),
        ])
        await self.settle(0.2)
        self.assertEqual(self.sleeps, [0.5, 1.0, 2.0])
        self.assertEqual(task.attempts, 3)
        self.assertEqual(task.status, "completed")
        self.assertEqual(len(self.sender.calls), 4)

    async def test_no_retry_on_flow_blocked(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", retries=5)
        self.sender.outcomes.append(("raise", FlowBlocked("alpha", "channel gone")))
        await self.settle(0.1)
        self.assertEqual(task.status, "failed")
        self.assertIn("channel gone", task.error)
        self.assertIn("FlowBlocked", task.error)
        self.assertEqual(task.attempts, 0)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.sleeps, [])
        failed = self.recorder.of(TaskFailed)
        self.assertEqual([e.task_id for e in failed], [task.id])
        self.assertIn("channel gone", failed[0].error)

    async def test_no_retry_on_unexpected_exception(self) -> None:
        task = self.scheduler.submit("alpha", "p", "poke", retries=3)
        self.sender.outcomes.append(("raise", KeyError("no such channel")))
        await self.settle(0.1)
        self.assertEqual(task.status, "failed")
        self.assertIn("KeyError", task.error)
        self.assertEqual(len(self.sender.calls), 1)

    async def test_timeout_marks_timed_out(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", timeout=0.05)
        self.sender.outcomes.append(("sleep", 0.5))
        await self.settle(0.3)
        self.assertEqual(task.status, "timed_out")
        self.assertIn("timed out", task.error)
        failed = self.recorder.of(TaskFailed)
        self.assertEqual([e.task_id for e in failed], [task.id])

    async def test_timeout_retries_then_times_out(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", timeout=0.05, retries=1)
        self.sender.outcomes.extend([("sleep", 0.5), ("sleep", 0.5)])
        await self.settle(0.4)
        self.assertEqual(task.status, "timed_out")
        self.assertEqual(len(self.sender.calls), 2)
        self.assertEqual(task.attempts, 1)
        self.assertEqual(self.sleeps, [0.5])


# ------------------------------------------------------------------- stop
class TestStop(SchedulerTestBase):
    async def test_stop_drain_true_waits_for_tasks(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message")
        self.sender.outcomes.append(("sleep", 0.05))
        await self.scheduler.stop(drain=True, timeout=2.0)
        self.assertEqual(task.status, "completed")
        self.assertEqual(self.scheduler.stats()["workers"], 0)
        with self.assertRaises(SchedulerError):
            self.scheduler.submit("alpha", "p", "message")
        await self.scheduler.stop(drain=False)  # idempotent

    async def test_stop_drain_false_cancels_everything(self) -> None:
        gate = self.limiter.block_acquires()
        t1 = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.02)              # blocked inside acquire
        t2 = self.scheduler.submit("alpha", "p", "message")
        t3 = self.scheduler.submit("beta", "p", "message")

        await self.scheduler.stop(drain=False)
        for task in (t1, t2, t3):
            self.assertEqual(task.status, "cancelled")
        cancelled_ids = {e.task_id for e in self.recorder.of(TaskCancelled)}
        self.assertEqual(cancelled_ids, {t1.id, t2.id, t3.id})
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.scheduler.stats()["workers"], 0)
        with self.assertRaises(SchedulerError):
            self.scheduler.submit("alpha", "p", "message")

    async def test_stop_drain_timeout_still_terminates(self) -> None:
        gate = self.limiter.block_acquires()
        task = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.02)
        await self.scheduler.stop(drain=True, timeout=0.15)  # drain cannot finish
        self.assertEqual(task.status, "cancelled")
        gate.set()
        await self.settle(0.05)


# ------------------------------------------------------ history + stats
class TestHistoryAndStats(SchedulerTestBase):
    async def test_history_recorded_once_per_terminal_task(self) -> None:
        t1 = self.scheduler.submit("alpha", "p", "message")              # completes
        await self.settle(0.02)
        t2 = self.scheduler.submit("alpha", "p", "message", retries=1)   # retries once
        self.sender.outcomes.append(("raise", ConnectionError("x")))
        await self.settle(0.05)
        t3 = self.scheduler.submit("beta", "p", "message")
        self.sender.outcomes.append(("raise", FlowBlocked("beta", "nope")))
        await self.settle(0.1)

        self.assertEqual(len(self.repo.add_calls), 3)
        self.assertEqual([c["id"] for c in self.repo.add_calls], [t1.id, t2.id, t3.id])
        row2 = self.repo.rows[t2.id]
        self.assertEqual(row2["status"], "completed")
        self.assertEqual(row2["attempts"], 1)
        self.assertIsNotNone(row2["duration_s"])
        self.assertEqual(self.repo.rows[t3.id]["status"], "failed")
        self.assertIn("nope", self.repo.rows[t3.id]["error"])

        # scheduler.history() is repo-backed
        self.assertEqual([r["id"] for r in self.scheduler.history()], [t1.id, t2.id, t3.id])
        self.assertEqual([r["id"] for r in self.scheduler.history(account="alpha")], [t1.id, t2.id])

    async def test_history_repo_failure_never_breaks_pipeline(self) -> None:
        self.repo.fail = True
        task = self.scheduler.submit("alpha", "p", "message")
        await self.settle()
        self.assertEqual(task.status, "completed")
        self.assertEqual(self.repo.add_calls, [])
        # read fails too -> in-memory fallback
        self.assertEqual([r["id"] for r in self.scheduler.history()], [task.id])

    async def test_history_memory_fallback_without_repo(self) -> None:
        scheduler = TaskScheduler(
            self.bus, FakeLogger(), FakeLimiter(), FakeSender(), None
        )
        scheduler.start()
        try:
            task = scheduler.submit("alpha", "p", "message")
            await asyncio.sleep(0.05)
            self.assertEqual(task.status, "completed")
            self.assertEqual([r["id"] for r in scheduler.history()], [task.id])
            self.assertEqual(scheduler.history(limit=0), [])
        finally:
            await scheduler.stop(drain=False)

    async def test_stats_counts_and_avg_duration(self) -> None:
        self.scheduler.submit("alpha", "p", "message")                              # ok
        await self.settle(0.02)
        self.scheduler.submit("alpha", "p", "message", retries=1)                    # retry -> ok
        self.sender.outcomes.append(("raise", ConnectionError("x")))
        await self.settle(0.05)
        self.scheduler.submit("alpha", "p", "message")                              # FlowBlocked
        self.sender.outcomes.append(("raise", FlowBlocked("alpha", "blocked")))
        await self.settle(0.05)
        self.scheduler.submit("alpha", "p", "message", timeout=0.05)                 # timeout
        self.sender.outcomes.append(("sleep", 0.5))
        await self.settle(0.5)

        stats = self.scheduler.stats()
        self.assertEqual(stats["completed"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["timed_out"], 1)
        self.assertEqual(stats["cancelled"], 0)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["running"], 0)
        self.assertGreaterEqual(stats["avg_duration_s"], 0.0)
        self.assertLess(stats["avg_duration_s"], 1.0)
        self.assertGreaterEqual(stats["workers"], 1)  # idle lanes stay alive

    async def test_tasks_listing_live_then_finished_newest_last(self) -> None:
        hold = asyncio.Event()
        self.sender.hold = hold
        t1 = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.02)               # t1 running (held in sender)
        t2 = self.scheduler.submit("alpha", "p", "message")   # queued behind
        live = self.scheduler.tasks()
        self.assertEqual([r["id"] for r in live], [t1.id, t2.id])
        self.assertEqual([r["status"] for r in live], ["running", "queued"])

        hold.set()
        await self.settle(0.2)
        rows = self.scheduler.tasks()
        self.assertEqual([r["id"] for r in rows], [t1.id, t2.id])
        self.assertEqual([r["status"] for r in rows], ["completed", "completed"])
        self.assertEqual([r["id"] for r in self.scheduler.tasks(account="beta")], [])
        self.assertEqual(
            [r["id"] for r in self.scheduler.tasks(status="failed")], []
        )

    async def test_audit_only_for_message_and_dm(self) -> None:
        self.scheduler.submit("alpha", "p", "message")
        self.scheduler.submit("alpha", "p", "dm", user_id=7)
        self.scheduler.submit("alpha", "p", "poke")
        await self.settle(0.2)
        self.assertEqual([c["action"] for c in self.audit.calls], ["message", "dm"])
        self.assertEqual(len(self.repo.add_calls), 3)  # history records everything


# ------------------------------------------------------------- validation
class TestValidation(SchedulerTestBase):
    def test_submit_rejects_bad_fields(self) -> None:
        cases = [
            {"account": "", "plugin": "p", "action": "message"},
            {"account": "  ", "plugin": "p", "action": "message"},
            {"account": 123, "plugin": "p", "action": "message"},
            {"account": "a", "plugin": "", "action": "message"},
            {"account": "a", "plugin": "p", "action": None},
            {"account": "a", "plugin": "p", "action": "message", "content": 42},
            {"account": "a", "plugin": "p", "action": "message", "channel_id": "x"},
            {"account": "a", "plugin": "p", "action": "message", "user_id": True},
            {"account": "a", "plugin": "p", "action": "message", "timeout": -1},
            {"account": "a", "plugin": "p", "action": "message", "timeout": "soon"},
            {"account": "a", "plugin": "p", "action": "message", "retries": -2},
            {"account": "a", "plugin": "p", "action": "message", "retries": "many"},
            {"account": "a", "plugin": "p", "action": "message", "priority": "banana"},
            {"account": "a", "plugin": "p", "action": "message", "name": 7},
        ]
        for kwargs in cases:
            with self.subTest(**kwargs), self.assertRaises(SchedulerError):
                self.scheduler.submit(**kwargs)

    async def test_submit_accepts_timeout_zero_and_named_tasks(self) -> None:
        task = self.scheduler.submit("a", "p", "poke", timeout=0, name="custom", retries=0)
        self.assertEqual(task.timeout_s, 0.0)
        self.assertEqual(task.name, "custom")
        self.assertEqual(task.priority, int(Priority.NORMAL))

    async def test_start_rearms_after_stop(self) -> None:
        """REGRESSION (resume path): the global emergency stop leaves the
        scheduler stopped; start() must RE-ARM it (fresh submits accepted)
        — stop() already drove every task terminal, so nothing is stale."""
        await self.scheduler.stop(drain=False)
        with self.assertRaises(SchedulerError):
            self.scheduler.submit("alpha", "p", "message", channel_id=1)
        self.scheduler.start()
        task = self.scheduler.submit("alpha", "p", "message", channel_id=1)
        await self.settle(0.1)
        self.assertEqual(task.status, "completed")

    def test_constructor_rejects_bad_config(self) -> None:
        bus, logger, limiter, sender = self.bus, FakeLogger(), FakeLimiter(), FakeSender()
        with self.assertRaises(ValueError):
            TaskScheduler(bus, logger, limiter, sender, default_timeout=-1)
        with self.assertRaises(ValueError):
            TaskScheduler(bus, logger, limiter, sender, retry_backoff=-0.5)
        with self.assertRaises(ValueError):
            TaskScheduler(bus, logger, limiter, sender, max_backoff=float("nan"))


if __name__ == "__main__":
    unittest.main()
