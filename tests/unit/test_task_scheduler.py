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
    """Dict-backed TaskHistoryRepository (async add/update/recent), fault-togglable."""

    def __init__(self) -> None:
        self.rows = {}
        self.add_calls = []
        self.update_calls = []
        self.fail = False

    async def add(self, entry):
        if self.fail:
            raise RuntimeError("task history db down")
        self.add_calls.append(dict(entry))
        self.rows[entry["id"]] = dict(entry)
        return entry["id"]

    async def update(self, task_id, **fields):
        if self.fail:
            raise RuntimeError("task history db down")
        self.update_calls.append((task_id, fields))
        self.rows[task_id].update(fields)

    async def recent(self, limit=50, account=None):
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

    async def test_server_rate_limit_retries_once_even_with_retries_zero(self) -> None:
        """429 contract: server-provided limits ALWAYS earn one retry even
        when the task was submitted with retries=0 (the limiter cooldown,
        not the caller's budget, paces it)."""
        task = self.scheduler.submit("alpha", "p", "message", retries=0)
        self.sender.outcomes.append(("raise", ServerRateLimit(1.0, True)))
        await self.settle(0.2)
        self.assertEqual(task.status, "completed")
        self.assertEqual(len(self.sender.calls), 2)  # failed once, retried
        self.assertEqual(task.attempts, 1)
        self.assertEqual(self.sleeps, [0.5])
        self.assertEqual(self.limiter.reports, [("alpha", "message", 1.0, True)])


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


# ----------------------------------------------- stop single-flight (round 2)
class TestStopSingleFlight(SchedulerTestBase):
    async def test_joiner_raises_owner_failure_and_retry_heals(self) -> None:
        """Owner cancelled mid-drain with a joiner parked on the latch: the
        joiner re-raises the owner's failure (fail-loud, no swallow), the
        scheduler is NOT marked stopped, and a retry stop() completes."""
        gate = asyncio.Event()
        in_sender = asyncio.Event()

        async def gated(task):
            in_sender.set()
            await gate.wait()

        self.scheduler._sender = gated
        self.scheduler.submit("alpha", "p", "message")
        await in_sender.wait()
        owner = asyncio.create_task(self.scheduler.stop(drain=True, timeout=30.0))
        await asyncio.sleep(0.02)  # owner parked in the drain loop
        joiner = asyncio.create_task(self.scheduler.stop(drain=True))
        await asyncio.sleep(0.02)  # joiner parked on the pending latch
        self.assertTrue(self.scheduler._stop_latch is not None
                        and not self.scheduler._stop_latch.done())

        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        with self.assertRaises(SchedulerError):
            await joiner  # joined a stop whose owner died: loud, not silent
        self.assertFalse(self.scheduler._stopped)  # retry must be possible
        self.assertFalse(self.scheduler._stopping)

        await self.scheduler.stop(drain=False)  # retry completes the teardown
        self.assertTrue(self.scheduler._stopped)

    async def test_latch_replaced_while_old_pending(self) -> None:
        """J parked on L1; L1's owner fails; a fresh stop() takes L2 and
        SUCCEEDS; J still reports the stale L1 failure (documented
        single-flight semantics: joiners inherit their owner's fate)."""
        gate = asyncio.Event()
        in_sender = asyncio.Event()

        async def gated(task):
            in_sender.set()
            await gate.wait()

        self.scheduler._sender = gated
        self.scheduler.submit("alpha", "p", "message")
        await in_sender.wait()
        owner1 = asyncio.create_task(self.scheduler.stop(drain=True, timeout=30.0))
        await asyncio.sleep(0.02)
        joiner = asyncio.create_task(self.scheduler.stop(drain=True))
        await asyncio.sleep(0.02)
        owner1.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner1
        owner2 = asyncio.create_task(self.scheduler.stop(drain=False))
        await owner2  # L1 failed -> L2 owned this fresh stop and completed
        self.assertTrue(self.scheduler._stopped)
        with self.assertRaises(SchedulerError):
            await joiner  # stale-but-loud: J's owner died under it

    async def test_joiner_cancelled_leaves_owner_intact(self) -> None:
        gate = asyncio.Event()
        in_sender = asyncio.Event()

        async def gated(task):
            in_sender.set()
            await gate.wait()
            return None

        self.scheduler._sender = gated
        self.scheduler.submit("alpha", "p", "message")
        await in_sender.wait()
        owner = asyncio.create_task(self.scheduler.stop(drain=True, timeout=30.0))
        await asyncio.sleep(0.02)
        joiner = asyncio.create_task(self.scheduler.stop(drain=True))
        await asyncio.sleep(0.02)
        joiner.cancel()  # cancelling a JOINER must never cancel the teardown
        with self.assertRaises(asyncio.CancelledError):
            await joiner
        self.assertFalse(self.scheduler._stop_latch.done())  # owner still live
        gate.set()
        await owner
        self.assertTrue(self.scheduler._stopped)

    async def test_owner_recancelled_during_abort_still_heals(self) -> None:
        """Double-cancel the owner mid-_abort_stop (a sender that stalls the
        reap): state stays consistent and a later stop() retries clean."""
        gate = asyncio.Event()
        in_sender = asyncio.Event()

        async def stubborn(task):
            in_sender.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)  # stall the abort's lane reap
                raise

        self.scheduler._sender = stubborn
        task = self.scheduler.submit("alpha", "p", "message")
        await in_sender.wait()
        owner = asyncio.create_task(self.scheduler.stop(drain=True, timeout=30.0))
        await asyncio.sleep(0.02)
        owner.cancel()   # lands inside the teardown
        await asyncio.sleep(0.02)
        owner.cancel()   # lands inside _abort_stop's bounded reap
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertFalse(self.scheduler._stopped)
        self.assertFalse(self.scheduler._stopping)

        await self.scheduler.stop(drain=False, timeout=5.0)
        self.assertTrue(self.scheduler._stopped)
        self.assertTrue(task.is_terminal)

    async def test_start_rejected_while_stopping(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message")
        self.sender.outcomes.append(("sleep", 0.08))
        owner = asyncio.create_task(self.scheduler.stop(drain=True, timeout=5.0))
        await asyncio.sleep(0.01)
        with self.assertRaises(SchedulerError):
            self.scheduler.start()  # re-arm mid-stop forbidden
        await owner
        self.scheduler.start()  # after completion: re-arm works
        late = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.1)
        self.assertEqual(late.status, "completed")

    async def test_joining_stop_runs_no_second_sweep_after_rearm(self) -> None:
        """The single-flight hazard: a drain=True stop finishing followed by
        a still-joined drain=False stop must NOT re-cancel tasks submitted
        after start() re-armed the scheduler."""
        task = self.scheduler.submit("alpha", "p", "message")
        self.sender.outcomes.append(("sleep", 0.08))
        owner = asyncio.create_task(self.scheduler.stop(drain=True, timeout=5.0))
        await asyncio.sleep(0.01)
        joiner = asyncio.create_task(self.scheduler.stop(drain=False))
        await owner
        await joiner  # joined the completed stop: success, no own sweep
        self.assertEqual(task.status, "completed")  # drained, not cancelled
        self.scheduler.start()
        after = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.1)
        self.assertEqual(after.status, "completed")  # no stale re-cancel


# --------------------------------------------- exactly-once races (round 2)
class TestExactlyOnceRaces(SchedulerTestBase):
    async def test_timeout_retry_then_success_records_exactly_once(self) -> None:
        task = self.scheduler.submit("alpha", "p", "message", timeout=0.05, retries=1)
        self.sender.outcomes.extend([("sleep", 0.5), ("ok", None)])
        await self.settle(0.4)
        self.assertEqual(task.status, "completed")
        self.assertEqual(len(self.repo.add_calls), 1)  # one row, two sends
        self.assertEqual(len(self.audit.calls), 1)     # one audit row
        self.assertEqual(self.audit.calls[0]["note"], "plugin=p task=1 attempts=2")

    async def test_cancel_matching_between_terminal_and_audit_keeps_exactly_once(self) -> None:
        orig_add = self.repo.add

        async def slow_add(entry):
            await asyncio.sleep(0.05)  # widen the send-done -> audit-emit window
            return await orig_add(entry)

        self.repo.add = slow_add
        task = self.scheduler.submit("alpha", "p", "message")
        self.assertTrue(await self.wait_until(lambda: task.status == "completed"))
        # sweep the task between its terminal transition and the audit emit:
        # terminal tasks must be skipped (no double terminal, no lost audit)
        self.assertEqual(self.scheduler.cancel_matching(account="alpha"), 0)
        self.assertFalse(self.scheduler.cancel(task.id))
        await self.settle(0.2)
        self.assertEqual(task.status, "completed")
        self.assertEqual(len(self.repo.add_calls), 1)
        self.assertEqual(len(self.audit.calls), 1)
        self.assertEqual(len(self.recorder.of(TaskCompleted)), 1)


# ------------------------------------------- backoff cancel latency (round 2)
class TestBackoffInterruption(SchedulerTestBase):
    async def test_cancel_during_backoff_interrupts_sleep(self) -> None:
        async def flaky(task):
            raise ConnectionError("net down")

        self.scheduler._sender = flaky
        self.scheduler._retry_backoff = 5.0
        del self.scheduler._sleep  # real sleep: prove the wake, not the seam
        task = self.scheduler.submit("alpha", "p", "message", retries=3)
        self.assertTrue(await self.wait_until(lambda: task.attempts == 1))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        self.assertTrue(self.scheduler.cancel(task.id))
        self.assertTrue(
            await self.wait_until(lambda: task.status == "cancelled", timeout=1.0)
        )
        self.assertLess(loop.time() - t0, 1.0)  # NOT the remaining ~5s backoff
        self.assertTrue(  # row + terminal event land via the detached tail
            await self.wait_until(
                lambda: self.repo.add_calls
                and self.repo.add_calls[0]["status"] == "cancelled"
                and self.recorder.of(TaskCancelled),
                timeout=2.0,
            )
        )

    async def test_cancel_matching_during_backoff_interrupts_sleep(self) -> None:
        async def flaky(task):
            raise ConnectionError("net down")

        self.scheduler._sender = flaky
        self.scheduler._retry_backoff = 5.0
        del self.scheduler._sleep
        task = self.scheduler.submit("alpha", "p", "message", retries=3)
        self.assertTrue(await self.wait_until(lambda: task.attempts == 1))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        self.assertEqual(self.scheduler.cancel_matching(account="alpha"), 1)
        self.assertTrue(
            await self.wait_until(lambda: task.status == "cancelled", timeout=1.0)
        )
        self.assertLess(loop.time() - t0, 1.0)

    async def test_backoff_without_cancel_still_sleeps_the_full_delay(self) -> None:
        self.scheduler._retry_backoff = 0.02
        del self.scheduler._sleep
        task = self.scheduler.submit("alpha", "p", "message", retries=1)
        self.sender.outcomes.append(("raise", ConnectionError("x")))
        await self.settle(0.1)
        self.assertEqual(task.status, "completed")
        self.assertEqual(len(self.sender.calls), 2)  # retry happened after backoff


# ------------------------------------------------ worker death (round 2)
class TestWorkerDeath(SchedulerTestBase):
    async def test_lane_death_orphans_queue_but_submit_respawns(self) -> None:
        """A lane task dying outside stop() (crash shape): the in-flight task
        is driven terminal by the worker's own handler, queued tasks wait,
        and the NEXT submit proves the pump is resurrected, not dead."""
        hold = asyncio.Event()
        self.sender.hold = hold
        t1 = self.scheduler.submit("alpha", "p", "message", channel_id=1)
        await self.settle(0.02)  # t1 running, held inside the sender
        t2 = self.scheduler.submit("alpha", "p", "message", channel_id=2)
        lane = self.scheduler._lanes["alpha"]
        lane.cancel()  # simulate worker death (NOT a scheduler stop)
        await asyncio.sleep(0.05)
        self.assertEqual(t1.status, "cancelled")  # in-flight task terminal
        self.assertEqual(t2.status, "queued")  # orphaned until a new submit
        self.assertTrue(lane.done())

        hold.set()
        t3 = self.scheduler.submit("alpha", "p", "message", channel_id=3)
        await self.settle(0.3)
        self.assertEqual(t2.status, "completed")  # respawned lane drains it
        self.assertEqual(t3.status, "completed")
        self.assertEqual([t.id for t in self.sender.calls], [t1.id, t2.id, t3.id])


# ------------------------------------------------------ chaos stress (round 2)
class TestStressExactlyOnce(SchedulerTestBase):
    async def test_120_tasks_random_outcomes_cancels_and_stop(self) -> None:
        import random

        rng = random.Random(0xC0FFEE)
        accounts = ["acc1", "acc2", "acc3", "acc4"]
        tasks = []
        for i in range(120):
            task = self.scheduler.submit(
                rng.choice(accounts),
                "pl",
                rng.choice(["message", "dm", "poke"]),
                channel_id=i,
                retries=rng.choice([0, 0, 1, 2]),
                **({"timeout": 0.04} if rng.random() < 0.3 else {}),
            )
            tasks.append(task)
            r = rng.random()
            if r < 0.25:
                self.sender.outcomes.append(("sleep", 0.06))
            elif r < 0.45:
                self.sender.outcomes.append(("raise", ConnectionError("net down")))
            elif r < 0.55:
                self.sender.outcomes.append(("raise", ServerRateLimit(0.01, False)))
            elif r < 0.60:
                self.sender.outcomes.append(("raise", FlowBlocked("x", "gone")))
            else:
                self.sender.outcomes.append(("ok", None))
            if rng.random() < 0.15:
                self.scheduler.cancel(task.id)
            if rng.random() < 0.05:
                self.scheduler.cancel_matching(account=rng.choice(accounts))

        await self.scheduler.stop(drain=True, timeout=10.0)
        await self.settle(0.05)  # publish_nowait tasks run one hop after tails

        for task in tasks:
            self.assertTrue(task.is_terminal, f"task {task.id} not terminal")
        ids = [c["id"] for c in self.repo.add_calls]
        self.assertEqual(len(ids), len(tasks))  # exactly one row per task
        self.assertEqual(len(set(ids)), len(ids))  # no duplicates, any ordering
        self.assertEqual(set(ids), {t.id for t in tasks})  # nothing dropped
        self.assertEqual(self.scheduler._inflight_tails, 0)
        terminal_events = self.recorder.of(TaskCompleted, TaskFailed, TaskCancelled)
        self.assertEqual(len(terminal_events), len(tasks))  # one event each
        self.assertEqual(len({e.task_id for e in terminal_events}), len(tasks))
        audited = {
            t.id for t in tasks
            if t.action in ("message", "dm") and t.sender_ran
        }
        self.assertEqual(len(self.audit.calls), len(audited))
        for account in accounts:
            self.assertLessEqual(self.sender.max_inflight[account], 1)  # lanes held


# ------------------------------------------------------ history + stats
class TestHistoryAndStats(SchedulerTestBase):
    async def test_history_recorded_once_per_terminal_task(self) -> None:
        t1 = self.scheduler.submit("alpha", "p", "message")              # completes
        await self.settle(0.02)
        t2 = self.scheduler.submit("alpha", "p", "message", retries=1)   # retries once
        # self-contained outcome list: attempt 1 raises, attempt 2 succeeds —
        # never depends on when LATER tasks append their own outcomes
        self.sender.outcomes.extend([("raise", ConnectionError("x")), ("ok", None)])
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

        # scheduler.history() is repo-backed (awaited)
        self.assertEqual([r["id"] for r in await self.scheduler.history()], [t1.id, t2.id, t3.id])
        self.assertEqual(
            [r["id"] for r in await self.scheduler.history(account="alpha")], [t1.id, t2.id]
        )

    async def test_history_repo_failure_never_breaks_pipeline(self) -> None:
        self.repo.fail = True
        task = self.scheduler.submit("alpha", "p", "message")
        await self.settle()
        self.assertEqual(task.status, "completed")
        self.assertEqual(self.repo.add_calls, [])
        # read fails too -> in-memory fallback
        self.assertEqual([r["id"] for r in await self.scheduler.history()], [task.id])

    async def test_history_memory_fallback_without_repo(self) -> None:
        scheduler = TaskScheduler(
            self.bus, FakeLogger(), FakeLimiter(), FakeSender(), None
        )
        scheduler.start()
        try:
            task = scheduler.submit("alpha", "p", "message")
            await asyncio.sleep(0.05)
            self.assertEqual(task.status, "completed")
            self.assertEqual([r["id"] for r in await scheduler.history()], [task.id])
            self.assertEqual(await scheduler.history(limit=0), [])
        finally:
            await scheduler.stop(drain=False)

    async def test_stats_counts_and_avg_duration(self) -> None:
        self.scheduler.submit("alpha", "p", "message")                              # ok
        await self.settle(0.02)
        self.scheduler.submit("alpha", "p", "message", retries=1)                    # retry -> ok
        self.sender.outcomes.extend([("raise", ConnectionError("x")), ("ok", None)])
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


# ------------------------------------------- stop hardening (round 3)
class TestFailedStopHardening(SchedulerTestBase):
    async def test_owner_cancelled_stop_persists_terminal_state(self) -> None:
        """TORN TAIL regression: a stop owner cancelled while the sweep's
        history write is in flight must not tear the terminal transition —
        the tail runs shielded, the abort joins it, and rows/audit/events
        all land even though _apply_terminal already removed the task."""
        gate = asyncio.Event()
        orig_add = self.repo.add

        async def blocked_add(entry):
            await gate.wait()  # simulate a slow aiosqlite write
            return await orig_add(entry)

        self.repo.add = blocked_add
        t1 = self.scheduler.submit("alpha", "p", "message")
        t2 = self.scheduler.submit("alpha", "p", "message")  # queued behind
        await self.settle(0.02)  # t1 completed, its tail parked on the gate

        owner = asyncio.create_task(self.scheduler.stop(drain=False, timeout=0.0))
        await asyncio.sleep(0.05)  # owner parked: t1 terminal, row not landed
        owner.cancel()
        gate.set()  # DB becomes responsive again
        with self.assertRaises(asyncio.CancelledError):
            await owner
        await self.settle(0.1)

        self.assertEqual(t1.status, "completed")
        self.assertEqual(t2.status, "cancelled")
        self.assertEqual({r["id"] for r in self.repo.add_calls}, {t1.id, t2.id})
        completed = self.recorder.of(TaskCompleted)
        cancelled = self.recorder.of(TaskCancelled)
        self.assertEqual([e.task_id for e in completed], [t1.id])
        self.assertEqual([e.task_id for e in cancelled], [t2.id])
        self.assertEqual(len(self.scheduler._tasks), 0)
        # retry stop() completes the teardown and heals the fault
        await self.scheduler.stop(drain=False)
        self.assertTrue(self.scheduler._stopped)

    async def test_failed_stop_rejects_submit_and_leaves_no_stale_tasks(self) -> None:
        """FAILED STOP re-opened submissions (round 3): after an
        owner-cancelled estop the scheduler is FAULTED — submit() rejects
        (no respawned lane may drain tasks the estop was cancelling) until
        start() explicitly re-arms."""
        hold = asyncio.Event()
        entered = asyncio.Event()
        db = asyncio.Event()

        async def held(task):
            entered.set()
            await hold.wait()

        orig_add = self.repo.add

        async def blocked_add(entry):
            await db.wait()  # keep the teardown parked mid-sweep
            return await orig_add(entry)

        self.scheduler._sender = held
        self.repo.add = blocked_add
        t1 = self.scheduler.submit("alpha", "p", "message")
        self.assertTrue(await self.wait_until(entered.is_set))
        t2 = self.scheduler.submit("alpha", "p", "message")  # queued behind

        owner = asyncio.create_task(self.scheduler.stop(drain=False, timeout=0.0))
        await asyncio.sleep(0.05)  # sweep parked inside the blocked history write
        owner.cancel()
        db.set()
        hold.set()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        await self.settle(0.1)

        self.assertTrue(t1.is_terminal)               # abort drove it terminal
        self.assertEqual(t2.status, "cancelled")      # NOT left for a respawn
        self.assertTrue(self.scheduler._stop_failed)
        with self.assertRaises(SchedulerError) as ctx:
            self.scheduler.submit("alpha", "p", "message")
        self.assertIn("interrupted", str(ctx.exception))

        self.scheduler.start()  # explicit re-arm retires the fault
        late = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.1)
        self.assertEqual(late.status, "completed")

    async def test_cancelled_running_task_duration_uses_wall_clock(self) -> None:
        """CANCELLED rows persisted duration 0.0 despite wall
        started_at/finished_at: cancel paths without a monotonic start must
        fall back to the wall-clock diff."""
        hold = asyncio.Event()
        entered = asyncio.Event()

        async def held(task):
            entered.set()
            await hold.wait()

        self.scheduler._sender = held
        task = self.scheduler.submit("alpha", "p", "message")
        self.assertTrue(await self.wait_until(entered.is_set))
        await asyncio.sleep(0.03)  # measurable wall time mid-send
        await self.scheduler.stop(drain=False)
        row = self.repo.rows[task.id]
        self.assertEqual(row["status"], "cancelled")
        self.assertGreaterEqual(row["duration_s"], 0.02)


# ------------------------------------------------ lane containment (round 3)
class TestLaneContainment(SchedulerTestBase):
    async def test_lane_worker_error_drives_task_failed(self) -> None:
        """The belt-and-braces lane catch used to strand the task RUNNING
        forever: it must log the exception detail AND drive the task
        terminal FAILED (history row + TaskFailed event included)."""

        async def broken(task):
            raise RuntimeError("worker blew up")

        orig_run = self.scheduler._run_task
        self.scheduler._run_task = broken
        task = self.scheduler.submit("alpha", "p", "message")
        self.assertTrue(await self.wait_until(
            lambda: task.is_terminal and self.repo.add_calls
            and self.recorder.of(TaskFailed)
        ))
        self.assertEqual(task.status, "failed")
        self.assertIn("RuntimeError: worker blew up", task.error)
        self.assertEqual(len(self.repo.add_calls), 1)
        self.assertEqual(self.repo.add_calls[0]["status"], "failed")
        failed = self.recorder.of(TaskFailed)
        self.assertEqual([e.task_id for e in failed], [task.id])
        self.assertEqual(failed[0].error_kind, "RuntimeError")
        self.assertEqual(self.scheduler.stats()["running"], 0)
        # the lane itself survives for the next task
        self.scheduler._run_task = orig_run
        nxt = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.1)
        self.assertEqual(nxt.status, "completed")


# -------------------------------------------- pre-send failures (round 3)
class TestPreSendFailureNoAudit(SchedulerTestBase):
    async def test_limiter_failure_never_audited(self) -> None:
        """PHANTOM AUDIT ROWS: FAILED paths where acquire() raised or was
        cancelled never invoked the sender — no outbound audit row may
        exist for a send that did not happen."""

        async def exploding(action, account, plugin, priority):
            raise RuntimeError("limiter db gone")

        async def cancelling(action, account, plugin, priority):
            raise asyncio.CancelledError()

        self.limiter.acquire = exploding
        t1 = self.scheduler.submit("alpha", "p", "message")
        self.assertTrue(await self.wait_until(lambda: t1.is_terminal))
        self.limiter.acquire = cancelling
        t2 = self.scheduler.submit("alpha", "p", "message")
        await self.settle(0.1)

        self.assertEqual(t1.status, "failed")
        self.assertIn("rate limiter error", t1.error)
        self.assertEqual(t2.status, "failed")
        self.assertIn("acquire was cancelled", t2.error)
        self.assertEqual(self.sender.calls, [])       # nothing was sent
        self.assertEqual(self.audit.calls, [])        # ...so nothing audited
        self.assertEqual(len(self.repo.add_calls), 2)  # history still written


# ---------------------------------------------------- estop sweep (round 3)
class TestEstopSweep(SchedulerTestBase):
    async def test_zero_sender_calls_after_stop_returns(self) -> None:
        """Estop barrier contract: with one send held mid-flight and five
        tasks queued, stop(drain=False) cancels everything and ZERO sender
        calls may happen after stop() returns."""
        hold = asyncio.Event()
        entered = asyncio.Event()
        calls = []

        async def held(task):
            calls.append(task)
            entered.set()
            await hold.wait()

        self.scheduler._sender = held
        t1 = self.scheduler.submit("alpha", "p", "message")
        self.assertTrue(await self.wait_until(entered.is_set))
        queued = [self.scheduler.submit("alpha", "p", "message") for _ in range(5)]

        await self.scheduler.stop(drain=False)  # estop racing the held send

        self.assertEqual(len(calls), 1)  # only the pre-stop in-flight send
        for task in [t1, *queued]:
            self.assertEqual(task.status, "cancelled")
        self.assertEqual(self.scheduler.stats()["workers"], 0)
        self.assertEqual(self.scheduler._inflight_tails, 0)
        await self.settle(0.1)  # nothing may fire after stop() returned
        self.assertEqual(len(calls), 1)


# ------------------------------------------------ error-kind seam (round 3)
class TestErrorKindSeam(SchedulerTestBase):
    async def test_flow_blocked_classes_survive_the_ack_seam(self) -> None:
        """Drive a REAL scheduler failure per FlowBlocked subclass: the
        published TaskFailed must carry error_kind == '<ClassName>' and
        TaskAckRegistry must reconstruct the SAME class for the waiter
        (pins the seam the services layer hardens)."""
        from core.account_manager.services import TaskAckRegistry
        from core.errors import AccessDenied, CacheInvalid, FetchRequired, TargetGone

        registry = TaskAckRegistry(self.bus, scheduler=self.scheduler)
        cases = (
            FlowBlocked("alpha", "flow dead end"),
            CacheInvalid("alpha", "cache evaporated"),
            TargetGone("alpha", "channel 500 vanished"),
            AccessDenied("alpha", "permissions revoked"),
            FetchRequired("alpha", "not cached"),
        )
        for exc in cases:
            with self.subTest(kind=type(exc).__name__):
                raised = exc

                async def failing(task, _exc=raised):
                    raise _exc

                self.scheduler._sender = failing
                task = self.scheduler.submit("alpha", "p", "message")
                future = registry.register(task.id)
                with self.assertRaises(type(exc)) as ctx:
                    await asyncio.wait_for(future, 2.0)
                self.assertTrue(task.is_terminal)
                events = [
                    e for e in self.recorder.of(TaskFailed) if e.task_id == task.id
                ]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].error_kind, type(exc).__name__)
                self.assertTrue(events[0].error.startswith(f"{type(exc).__name__}: "))
                self.assertIs(type(ctx.exception), type(exc))
                self.assertEqual(ctx.exception.account, "alpha")
                self.assertIn(exc.reason, ctx.exception.reason)

    async def test_non_flow_failure_maps_to_scheduler_error(self) -> None:
        from core.account_manager.services import TaskAckRegistry

        registry = TaskAckRegistry(self.bus, scheduler=self.scheduler)

        async def failing(task):
            raise KeyError("no such channel")

        self.scheduler._sender = failing
        task = self.scheduler.submit("alpha", "p", "poke")
        future = registry.register(task.id)
        with self.assertRaises(SchedulerError) as ctx:
            await asyncio.wait_for(future, 2.0)
        self.assertTrue(task.is_terminal)
        events = [e for e in self.recorder.of(TaskFailed) if e.task_id == task.id]
        self.assertEqual(events[0].error_kind, "KeyError")
        self.assertIn("no such channel", str(ctx.exception))


# ---------------------------------------------- restore sequence (round 3)
class TestRestoreSequence(SchedulerTestBase):
    async def test_sequence_continues_after_history_max_id(self) -> None:
        class MaxIdRepo(FakeHistoryRepo):
            async def max_id(self):
                return 41

        repo = MaxIdRepo()
        scheduler = TaskScheduler(
            self.bus, FakeLogger(), self.limiter, self.sender, None,
            history_repo=repo,
        )
        scheduler.start()
        try:
            self.assertEqual(await scheduler.restore_sequence(), 42)
            task = scheduler.submit("alpha", "p", "message")
            self.assertEqual(task.id, 42)
        finally:
            await scheduler.stop(drain=False)

    async def test_max_id_read_failure_seeds_collision_proof_ids(self) -> None:
        """On a max_id() read failure ids must NOT restart at 1: every add
        would fail its UNIQUE constraint for the whole session. The floor
        is seeded from the epoch — unreachable by any prior session."""
        import time

        class BrokenMaxId(FakeHistoryRepo):
            async def max_id(self):
                raise RuntimeError("history db locked")

        repo = BrokenMaxId()
        scheduler = TaskScheduler(
            self.bus, FakeLogger(), self.limiter, self.sender, None,
            history_repo=repo,
        )
        scheduler.start()
        try:
            nxt = await scheduler.restore_sequence()
            self.assertGreaterEqual(nxt, int(time.time()))
            task = scheduler.submit("alpha", "p", "message")
            self.assertEqual(task.id, nxt)
            await self.settle(0.05)
            self.assertEqual(len(repo.add_calls), 1)  # no swallowed collision
        finally:
            await scheduler.stop(drain=False)


if __name__ == "__main__":
    unittest.main()
