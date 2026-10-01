"""TaskScheduler — centralized queue, per-account lanes, retry/timeout/cancel.

The ONE place outbound work is dispatched (INTEGRATION "Send pipeline"):

    submit() -> per-account worker lane (concurrency = 1 per account)
             -> limiter.acquire(action, account, plugin, priority)
             -> sender(task)                       [AccountManager.send_hook]
             -> audit record + task history        [terminal transition]

Ported discipline (from the audited ``git show HEAD:core/send_engine.py``):
a token bucket paces the RATE of actions but does not prevent two Discord
requests being IN FLIGHT for one account at the same moment — concurrent
API calls are exactly what flags selfbot accounts. Each account therefore
gets ONE serialized worker ("lane"): a task is popped from that account's
priority heap, granted, sent, and only then does the lane pick the next one.

Worker model (decision):
  * Lanes are spawned lazily on the first submit for an account and live
    until stop(); strong references are kept so no worker is ever GC'd
    mid-flight.
  * Within one account the heap orders by (priority, submission seq), so a
    HIGH task submitted after a LOW one still runs first.

Cross-account fairness (decision):
  * A shared ``_GrantGate`` orders ENTRY into ``limiter.acquire`` globally by
    (priority, seq): when several accounts have runnable work at once, the
    higher-priority task's acquire starts first and the limiter — whose own
    waiter heap is priority-aware — grants it first. The gate batches
    same-tick arrivals with one ``sleep(0)`` hop before releasing the best
    candidate, which is what lets a late-submitted HIGH task overtake an
    already-registered LOW one. It only sequences the *entry*; the acquires
    themselves run concurrently (per-account pacing stays independent), so
    two accounts still send in parallel.

Failure containment (from ``git show HEAD:core/scheduler.py``): workers
swallow every sender exception into a task failure — the scheduler NEVER
crashes. History-repo and audit failures are likewise isolated (try/except
+ log).

Retry classification (INTEGRATION contract): ``ServerRateLimit`` is
reported to the limiter then retried; ``ConnectionError`` /
``TimeoutError`` / ``asyncio.TimeoutError`` retry (timeout marks the final
status "timed_out"); ``FlowBlocked`` and everything else fail immediately
with the reason.
Retries sleep ``min(retry_backoff * 2 ** (attempts - 1), max_backoff)``
through the ``_sleep`` seam (deterministic tests) and are capped at
``1 + max_retries`` total attempts.
"""
from __future__ import annotations

import asyncio
import re
import heapq
import inspect
import itertools
import math
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

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

from .tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_TIMED_OUT,
    TERMINAL_STATUSES,
    Task,
)

#: how many finished-task rows/dicts we keep in memory for tasks()/history()
_FINISHED_KEEP = 500

#: actions whose terminal transition is mirrored into the audit trail
_AUDITED_ACTIONS = ("message", "dm", "reaction", "button")


class _GrantGate:
    """Shared priority turnstile ordering ENTRY into limiter.acquire.

    When several account lanes are runnable at once, each must pass through
    this gate before calling ``limiter.acquire``. Waiters are released in
    (priority, submission seq) order — HIGH before NORMAL before LOW, FIFO
    within a priority — so the limiter's waiters register (and are granted)
    in global priority order.

    Mechanics:
      * ``enter()`` parks the caller on a future pushed onto a heap and
        kicks a release cycle.
      * A release cycle waits one event-loop hop (``sleep(0)``) first: all
        lanes that became runnable in the same tick register before the
        best candidate is chosen, which is what makes late-submitted
        higher-priority tasks overtake already-registered lower ones.
      * Exactly ONE waiter is released per cycle; each released waiter
        re-kicks the gate as it proceeds (``finally``), advancing the chain.
        The gate therefore never serializes the acquires themselves — only
        their starting order — keeping per-account pacing independent.
      * Cancelled waiters are skipped (their futures are done) and their
        ``finally`` still kicks, so a stopping scheduler can never wedge the
        chain.
    """

    def __init__(self) -> None:
        self._heap: List[tuple] = []  # (priority, seq, future)
        self._cycle_pending = False
        self._cycles: set = set()  # strong refs (create_task GC hazard)

    async def enter(self, priority: int, seq: int) -> None:
        """Wait until it is this (priority, seq) pair's turn to acquire."""
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (int(priority), int(seq), future))
        self._kick()
        try:
            await future
        finally:
            # whether I proceed, get cancelled, or raise: advance the chain
            self._kick()

    def _kick(self) -> None:
        """Ensure a release cycle is scheduled while waiters remain."""
        if self._cycle_pending or not self._heap:
            return
        self._cycle_pending = True
        cycle = asyncio.get_running_loop().create_task(self._release_cycle())
        self._cycles.add(cycle)
        cycle.add_done_callback(self._cycles.discard)

    async def _release_cycle(self) -> None:
        """After one loop hop, release the current best waiter."""
        try:
            await asyncio.sleep(0)  # batch same-tick arrivals first
            self._release_min()
        finally:
            self._cycle_pending = False

    def _release_min(self) -> None:
        """Resolve (once) the highest-priority, oldest pending waiter."""
        while self._heap:
            _, _, future = heapq.heappop(self._heap)
            if not future.done():
                future.set_result(None)
                return


class TaskScheduler:
    """Centralized task queue with one serialized worker lane per account.

    Parameters:
        bus: EventBus for Task* events (publish_nowait, fire-and-forget).
        logger: LogService (``.info(source, message)``) or a stdlib logger.
        limiter: duck-typed RateLimitManager — needs async
            ``acquire(action, account, plugin, priority) -> Grant`` (Grant
            exposing ``wait_s``/``jitter_s``) and sync
            ``report_server_limit(account, action, retry_after, global_)``.
        sender: async callable executing one Task (AccountManager.send_hook).
        audit: optional AuditRecorder; terminal 'message'/'dm' tasks are
            recorded exactly once with their grant wait/jitter.
        default_timeout: sender budget per attempt when submit() omits one.
        retry_backoff / max_backoff: exponential backoff between attempts.
        history_repo: duck-typed TaskHistoryRepository (add/update/recent);
            one row per terminal task, best-effort.
    """

    def __init__(
        self,
        bus: EventBus,
        logger: Any,
        limiter: Any,
        sender: Callable[[Task], Awaitable[Any]],
        audit: Any = None,
        *,
        default_timeout: float = 30.0,
        retry_backoff: float = 0.5,
        max_backoff: float = 30.0,
        history_repo: Any = None,
    ) -> None:
        for label, value in (
            ("default_timeout", default_timeout),
            ("retry_backoff", retry_backoff),
            ("max_backoff", max_backoff),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{label} must be a number of seconds, got {value!r}")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{label} must be a finite value >= 0, got {value!r}")
        self._bus = bus
        self._logger = logger
        self._log_service_style = self._detect_log_style(logger)
        self._limiter = limiter
        self._sender = sender
        self._audit = audit
        self._default_timeout = float(default_timeout)
        self._retry_backoff = float(retry_backoff)
        self._max_backoff = float(max_backoff)
        self._history_repo = history_repo

        self._seq = itertools.count(1)
        self._tasks: Dict[int, Task] = {}  # live (queued/running) by id
        self._finished_rows: Deque[dict] = deque(maxlen=_FINISHED_KEEP)
        self._finished_tasks: Deque[Task] = deque(maxlen=_FINISHED_KEEP)
        self._counts: Dict[str, int] = {
            STATUS_COMPLETED: 0,
            STATUS_FAILED: 0,
            STATUS_CANCELLED: 0,
            STATUS_TIMED_OUT: 0,
        }
        self._queues: Dict[str, list] = {}  # account -> heap of (prio, id, task)
        self._lane_events: Dict[str, asyncio.Event] = {}
        self._lanes: Dict[str, asyncio.Task] = {}
        self._worker_refs: set = set()  # strong refs to spawned lane tasks
        self._gate = _GrantGate()
        self._started = False
        self._stopping = False
        self._stopped = False

    # ----------------------------------------------------------- lifecycle
    @staticmethod
    def _detect_log_style(logger: Any) -> bool:
        """True when logger.info's first parameter is the source (LogService)."""
        info = getattr(logger, "info", None)
        if info is None:
            return False
        try:
            params = list(inspect.signature(info).parameters)
        except (TypeError, ValueError):
            return False
        return bool(params) and params[0] == "source"

    def _log(self, severity: str, source: str, message: str) -> None:
        """Log through either LogService(source, msg) or stdlib logger(msg)."""
        method = getattr(self._logger, severity, None)
        if method is None:
            return
        try:
            if self._log_service_style:
                method(source, message)
            else:
                method("%s: %s", source, message)
        except Exception:  # logging must never break the pipeline
            pass

    def start(self) -> None:
        """Mark the scheduler ready (idempotent; RE-ARMS after stop()).

        Lanes are spawned lazily per account on first submit, so there is no
        central loop to launch here. Re-arming after stop() is the resume
        path for the global emergency stop: stop() already drove every task
        to a terminal state and reaped every lane, so a fresh arm starts
        from clean state (finished-task history is preserved). Attempts,
        counters and audit state are per-task — nothing stale carries over.
        """
        if self._stopping:
            raise SchedulerError("scheduler is stopping; wait for stop() to finish")
        self._stopped = False
        self._started = True

    async def stop(self, *, drain: bool = True, timeout: float = 10.0) -> None:
        """Stop the scheduler (idempotent).

        drain=True  waits (bounded by ``timeout``) for queued+running tasks
                    to finish normally, then tears the lanes down.
        drain=False cancels everything immediately: every live task gets a
                    terminal 'cancelled' status + TaskCancelled event and
                    every lane task is cancelled.

        Tasks still alive when a drain times out are cancelled as well —
        stop() never hangs and never leaves a task non-terminal.
        """
        if self._stopped:
            return
        self._stopping = True
        try:
            if drain:
                try:
                    budget = max(0.0, float(timeout))
                except (TypeError, ValueError):
                    budget = 10.0
                deadline = time.monotonic() + budget
                while self._tasks and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
            for task in list(self._tasks.values()):
                self._finish(task, STATUS_CANCELLED)
            for lane in list(self._lanes.values()):
                if not lane.done():
                    lane.cancel()
            if self._lanes:
                await asyncio.gather(*self._lanes.values(), return_exceptions=True)
        finally:
            self._stopped = True
            self._stopping = False

    # ------------------------------------------------------------- submit
    def submit(
        self,
        account: str,
        plugin: str,
        action: str,
        *,
        channel_id: Optional[int] = None,
        user_id: Optional[int] = None,
        content: Optional[str] = None,
        message_id: Optional[int] = None,
        priority: Priority = Priority.NORMAL,
        name: Optional[str] = None,
        timeout: Optional[float] = None,
        retries: int = 0,
    ) -> Task:
        """Validate, queue and publish one task; returns the live Task.

        Raises SchedulerError when the scheduler is stopped/stopping or any
        argument is invalid (human-readable message per ground rules).
        """
        if self._stopped or self._stopping:
            raise SchedulerError("scheduler is stopped; new tasks are rejected")
        for label, value in (("account", account), ("plugin", plugin), ("action", action)):
            if not isinstance(value, str) or not value.strip():
                raise SchedulerError(f"task {label} must be a non-empty string, got {value!r}")
        if content is not None and not isinstance(content, str):
            raise SchedulerError(f"content must be a string or None, got {type(content).__name__}")
        for label, value in (("channel_id", channel_id), ("user_id", user_id),
                             ("message_id", message_id)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise SchedulerError(f"task {label} must be an int or None, got {value!r}")
        if name is not None and not isinstance(name, str):
            raise SchedulerError(f"task name must be a string or None, got {name!r}")
        try:
            prio = Priority.coerce(priority)
        except ValueError as exc:
            raise SchedulerError(f"invalid priority: {exc}") from exc
        if timeout is None:
            timeout_s = self._default_timeout
        else:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise SchedulerError(f"timeout must be a number of seconds or None, got {timeout!r}")
            timeout_s = float(timeout)
            if not math.isfinite(timeout_s) or timeout_s < 0:
                raise SchedulerError(f"timeout must be a finite value >= 0 seconds, got {timeout!r}")
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise SchedulerError(f"retries must be an int >= 0, got {retries!r}")
        if retries < 0:
            raise SchedulerError(f"retries must be >= 0, got {retries!r}")

        task = Task(
            id=next(self._seq),
            name=(name.strip() if isinstance(name, str) and name.strip() else f"{plugin.strip()}:{action.strip()}"),
            account=account.strip(),
            plugin=plugin.strip(),
            action=action.strip(),
            channel_id=channel_id,
            user_id=user_id,
            content=content,
            message_id=message_id,
            priority=int(prio),
            timeout_s=timeout_s,
            max_retries=retries,
        )
        self._tasks[task.id] = task
        heapq.heappush(self._queues.setdefault(task.account, []), (int(task.priority), task.id, task))
        self._ensure_lane(task.account)
        self._log(
            "info",
            "scheduler",
            f"queued task {task.id} ({task.name}) account={task.account!r} "
            f"priority={prio.name} retries={task.max_retries}",
        )
        self._emit(TaskQueued(account=task.account, task_id=task.id, plugin=task.plugin, action=task.action))
        return task

    def _ensure_lane(self, account: str) -> None:
        """Spawn this account's serialized worker lane if it is not alive."""
        lane = self._lanes.get(account)
        if lane is not None and not lane.done():
            self._lane_events[account].set()
            return
        event = self._lane_events.get(account)
        if event is None:
            event = asyncio.Event()
            self._lane_events[account] = event
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - sync-context misuse
            raise SchedulerError(
                "submit() must be called from a running event loop "
                "(the scheduler drives its lanes with asyncio tasks)"
            ) from exc
        lane = loop.create_task(self._lane(account), name=f"scheduler-lane-{account}")
        self._lanes[account] = lane
        self._worker_refs.add(lane)
        lane.add_done_callback(self._worker_refs.discard)
        event.set()  # there is work by construction

    # ------------------------------------------------------------- cancel
    def cancel(self, task_id: int) -> bool:
        """Cancel one task by id.

        ``queued`` tasks cancel immediately (no sender call) and emit
        TaskCancelled; ``running`` tasks set the cooperative flag and the
        worker aborts at its next checkpoint (after the grant wait or
        after the sender returns), finishing the task as cancelled.

        Returns:
            False for unknown or already-terminal tasks.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status == STATUS_QUEUED:
            self._finish(task, STATUS_CANCELLED)
            return True
        if task.status == STATUS_RUNNING:
            task.cancelled = True
            return True
        return False

    def cancel_matching(
        self, *, account: Optional[str] = None, plugin: Optional[str] = None, action: Optional[str] = None
    ) -> int:
        """Cancel every live task matching ALL given filters.

        Counts both immediate (queued) and cooperative (running) cancels.
        No filters cancels every live task.
        """
        count = 0
        for task in list(self._tasks.values()):
            if account is not None and task.account != account:
                continue
            if plugin is not None and task.plugin != plugin:
                continue
            if action is not None and task.action != action:
                continue
            if task.status == STATUS_QUEUED:
                self._finish(task, STATUS_CANCELLED)
            elif task.status == STATUS_RUNNING:
                task.cancelled = True
            else:
                continue
            count += 1
        return count

    # ------------------------------------------------------------ queries
    def get(self, task_id: int) -> Optional[Task]:
        """The Task object for task_id (live first, then recently finished)."""
        task = self._tasks.get(task_id)
        if task is not None:
            return task
        for finished in reversed(self._finished_tasks):
            if finished.id == task_id:
                return finished
        return None

    def tasks(self, *, status: Optional[str] = None, account: Optional[str] = None) -> List[dict]:
        """Live queued/running tasks plus recently finished ones, newest last."""
        rows: List[dict] = [t.as_dict() for t in sorted(self._tasks.values(), key=lambda t: t.id)]
        rows.extend(self._finished_rows)
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        if account is not None:
            rows = [r for r in rows if r.get("account") == account]
        return rows

    def history(self, limit: int = 50, account: Optional[str] = None) -> List[dict]:
        """Finished tasks, newest last — repo-backed, else the memory tail."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        if self._history_repo is not None:
            try:
                return list(self._history_repo.recent(limit, account))
            except Exception:
                self._log("warning", "scheduler", "task history read failed; falling back to in-memory tail")
        rows = list(self._finished_rows)
        if account is not None:
            rows = [r for r in rows if r.get("account") == account]
        return rows[-limit:] if limit > 0 else []

    def stats(self) -> dict:
        """Scheduler counters from in-memory tallies (UI Overview page)."""
        live = list(self._tasks.values())
        durations = [
            float(r.get("duration_s") or 0.0)
            for r in self._finished_rows
            if r.get("status") == STATUS_COMPLETED
        ]
        return {
            "queued": sum(1 for t in live if t.status == STATUS_QUEUED),
            "running": sum(1 for t in live if t.status == STATUS_RUNNING),
            "completed": self._counts[STATUS_COMPLETED],
            "failed": self._counts[STATUS_FAILED],
            "cancelled": self._counts[STATUS_CANCELLED],
            "timed_out": self._counts[STATUS_TIMED_OUT],
            "avg_duration_s": round(sum(durations) / len(durations), 3) if durations else 0.0,
            "workers": sum(1 for t in self._lanes.values() if not t.done()),
        }

    # ---------------------------------------------------- worker machinery
    async def _lane(self, account: str) -> None:
        """One account's serialized execution lane (concurrency = 1).

        Drains the account's priority heap one task at a time: a task must
        fully terminate (including retries/backoff) before the next one for
        this account starts — never two in-flight requests per account.
        """
        event = self._lane_events[account]
        heap = self._queues[account]
        while True:
            if not heap:
                await event.wait()
                event.clear()
                continue
            _, _, task = heapq.heappop(heap)
            if task.status != STATUS_QUEUED or task.cancelled:
                continue  # cancelled while queued: already terminal
            try:
                await self._run_task(task)
            except asyncio.CancelledError:
                raise  # stop() is tearing the lane down
            except Exception:  # belt and braces: the lane must never die
                self._log("error", "scheduler", f"lane {account!r} worker error on task {task.id} (contained)")

    async def _sleep(self, seconds: float) -> None:
        """Retry-backoff seam (patched to instant in unit tests)."""
        await asyncio.sleep(seconds)

    def _outer_cancelled(self) -> bool:
        """Distinguish worker cancellation from sender-internal cancellation.

        True when the scheduler is stopping (we cancelled the lanes) or the
        current asyncio task has a pending outer cancellation. A sender that
        raises CancelledError on its own does NOT count — that is a failure.
        """
        if self._stopping:
            return True
        current = asyncio.current_task()
        if current is None:
            return False
        cancelling = getattr(current, "cancelling", None)  # 3.11+
        if callable(cancelling):
            try:
                return current.cancelling() > current.uncancel()
            except Exception:  # pragma: no cover - defensive
                return False
        return False

    def _emit(self, event: Event) -> None:
        """Fire-and-forget publish; never breaks the pipeline."""
        try:
            self._bus.publish_nowait(event)
        except RuntimeError:
            self._log("warning", "scheduler", f"no running loop; dropped event {event.name}")
        except Exception:
            self._log("warning", "scheduler", f"event publish failed for {event.name}")

    def _summarize(self, result: Any, task: Task) -> str:
        """Short (< 80 chars) success summary: result type name + channel id."""
        parts = [type(result).__name__] if result is not None else ["ok"]
        result_id = getattr(result, "id", None)
        if isinstance(result_id, int):
            parts.append(f"id={result_id}")
        channel = getattr(result, "channel_id", None)
        if channel is None:
            channel = task.channel_id
        if channel is not None:
            parts.append(f"ch={channel}")
        return " ".join(parts)[:79]

    async def _run_task(self, task: Task) -> None:
        """Drive one task from grant to terminal status (never raises).

        Attempt loop: grant (through the shared priority gate) -> sender
        under asyncio.wait_for -> classify -> retry with backoff or finish.
        Cooperative-cancel checkpoints sit before the grant, after the grant
        wait, and after the sender returns (a cancel there discards the
        result and finishes the task as cancelled).
        """
        started_mono: Optional[float] = None
        try:
            while True:
                # checkpoint 0: cancelled before we even ask for a grant
                # (a retry iteration legitimately re-enters here while RUNNING)
                if task.cancelled or task.status not in (STATUS_QUEUED, STATUS_RUNNING):
                    self._finish(task, STATUS_CANCELLED, started_mono=started_mono)
                    return

                await self._gate.enter(int(task.priority), task.id)
                try:
                    grant = await self._limiter.acquire(
                        task.action, task.account, task.plugin, task.priority
                    )
                except asyncio.CancelledError:
                    if self._outer_cancelled():
                        raise  # outer handler finishes the task as cancelled
                    self._finish(
                        task, STATUS_FAILED,
                        error="rate limiter acquire was cancelled",
                        started_mono=started_mono,
                    )
                    return
                except Exception as exc:
                    self._finish(
                        task, STATUS_FAILED,
                        error=f"rate limiter error: {type(exc).__name__}: {exc}",
                        started_mono=started_mono,
                    )
                    return
                task.grant_wait_ms = float(getattr(grant, "wait_s", 0.0) or 0.0) * 1000.0
                task.grant_jitter_ms = float(getattr(grant, "jitter_s", 0.0) or 0.0) * 1000.0

                # checkpoint 1: cancelled while waiting for the grant
                if task.cancelled or task.status not in (STATUS_QUEUED, STATUS_RUNNING):
                    self._finish(task, STATUS_CANCELLED, started_mono=started_mono)
                    return

                if task.status == STATUS_QUEUED:  # first attempt: queued -> running
                    task.status = STATUS_RUNNING
                    task.started_at = time.time()
                    started_mono = time.monotonic()
                    self._emit(TaskStarted(account=task.account, task_id=task.id, action=task.action))

                timed_out = False
                retryable: Optional[BaseException] = None
                try:
                    task.sender_ran = True  # the API call is going out NOW
                    result = await asyncio.wait_for(self._sender(task), task.timeout_s)
                except asyncio.CancelledError:
                    if self._outer_cancelled():
                        raise  # worker cancellation (stop sweep)
                    self._finish(
                        task, STATUS_FAILED,
                        error="sender was cancelled before completing",
                        started_mono=started_mono,
                    )
                    return
                except asyncio.TimeoutError:
                    timed_out = True  # retryable; final status "timed_out"
                except ServerRateLimit as exc:
                    try:
                        self._limiter.report_server_limit(
                            task.account, task.action, exc.retry_after, exc.global_
                        )
                    except Exception:
                        self._log("warning", "scheduler", f"task {task.id}: report_server_limit failed (continuing)")
                    retryable = exc
                except (ConnectionError, TimeoutError) as exc:
                    retryable = exc
                except FlowBlocked as exc:
                    self._finish(
                        task, STATUS_FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                        started_mono=started_mono,
                    )
                    return
                except Exception as exc:
                    self._finish(
                        task, STATUS_FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                        started_mono=started_mono,
                    )
                    return
                else:
                    # checkpoint 2: cancelled while the sender ran -> discard
                    if task.cancelled:
                        self._finish(task, STATUS_CANCELLED, started_mono=started_mono)
                        return
                    self._finish(
                        task, STATUS_COMPLETED,
                        summary=self._summarize(result, task),
                        started_mono=started_mono,
                    )
                    return

                # retryable failure (timeout / ConnectionError / 429...)
                if task.cancelled:
                    self._finish(task, STATUS_CANCELLED, started_mono=started_mono)
                    return
                # Server-provided limits ALWAYS earn at least one retry (spec:
                # the framework respects server rate-limit information) — the
                # limiter cooldown paces the retry after Retry-After seconds.
                retry_budget = task.max_retries
                if isinstance(retryable, ServerRateLimit):
                    retry_budget = max(task.max_retries, 1)
                if task.attempts < retry_budget:
                    task.attempts += 1  # retries used (drives note + backoff)
                    delay = min(self._retry_backoff * (2 ** (task.attempts - 1)), self._max_backoff)
                    try:
                        await self._sleep(delay)
                    except asyncio.CancelledError:
                        if self._outer_cancelled():
                            raise
                        self._finish(task, STATUS_CANCELLED, started_mono=started_mono)
                        return
                    continue

                # retries exhausted -> terminal
                if timed_out:
                    self._finish(
                        task, STATUS_TIMED_OUT,
                        error=f"timed out after {task.timeout_s:g}s ({task.attempts + 1} attempt(s))",
                        started_mono=started_mono,
                    )
                else:
                    exc = retryable
                    self._finish(
                        task, STATUS_FAILED,
                        error=f"{type(exc).__name__}: {exc}" if exc is not None else "failed",
                        started_mono=started_mono,
                    )
                return
        except asyncio.CancelledError:
            # the worker itself was cancelled (stop()); guarantee a terminal
            # status even if the sweep has not reached this task yet
            self._finish(task, STATUS_CANCELLED, started_mono=started_mono)
            raise

    @staticmethod
    def _error_kind(error: str) -> str:
        """Exception CLASS NAME from an error string, when it has the
        ``"ClassName: message"`` shape every failure site produces.

        Used by the services layer to faithfully re-raise FlowBlocked
        subclasses across the task-ack seam (the flow-abort contract must
        survive: a plugin waiting on send_message receives TargetGone, not
        an opaque SchedulerError). "" when the shape doesn't match.
        """
        match = re.match(r"^([A-Z][A-Za-z0-9_]+): ", error)
        return match.group(1) if match else ""

    def _finish(
        self,
        task: Task,
        status: str,
        *,
        error: Optional[str] = None,
        summary: Optional[str] = None,
        started_mono: Optional[float] = None,
    ) -> None:
        """Apply ONE terminal transition: fields, memory, repo, audit, event.

        Exactly-once by construction: live tasks are removed from the
        registry here, and already-terminal tasks make this a no-op. History
        and audit failures are swallowed (logged) — recording must never
        break the send pipeline.
        """
        if task.status in TERMINAL_STATUSES:
            return
        task.status = status  # type: ignore[assignment]
        task.error = error
        task.result_summary = summary
        task.finished_at = time.time()
        duration = time.monotonic() - started_mono if started_mono is not None else 0.0

        row = task.as_dict()
        row["duration_s"] = round(duration, 3)
        self._tasks.pop(task.id, None)
        self._finished_rows.append(row)
        self._finished_tasks.append(task)
        self._counts[status] = self._counts.get(status, 0) + 1

        if self._history_repo is not None:
            try:
                self._history_repo.add(row)
            except Exception:
                self._log("warning", "scheduler", f"task {task.id} history write failed (continuing)")

        # AUDIT INTEGRITY: only record an outbound row when the task could
        # actually have touched the API. COMPLETED/FAILED/TIMED_OUT attempts
        # all invoked the sender; a CANCELLED task only did when it was
        # cancelled mid-send (checkpoint 2) — a cancel while still queued or
        # parked on the grant NEVER sent and must not appear in the trail
        # (the pacing checks read audit timestamps as real send times).
        auditable = status != STATUS_CANCELLED or task.sender_ran
        if self._audit is not None and auditable and task.action in _AUDITED_ACTIONS:
            note = f"plugin={task.plugin} task={task.id}"
            if task.attempts > 0:
                note += f" attempts={task.attempts + 1}"
            try:
                self._audit.record(
                    account=task.account,
                    direction="out",
                    action=task.action,
                    channel_id=task.channel_id,
                    content=task.content,
                    wait_ms=task.grant_wait_ms,
                    jitter_ms=task.grant_jitter_ms,
                    note=note,
                )
            except Exception:
                self._log("warning", "scheduler", f"task {task.id} audit record failed (continuing)")

        if status == STATUS_COMPLETED:
            self._emit(
                TaskCompleted(
                    account=task.account,
                    task_id=task.id,
                    duration_s=row["duration_s"],
                    summary=summary or "",
                )
            )
        elif status in (STATUS_FAILED, STATUS_TIMED_OUT):
            self._emit(TaskFailed(
                account=task.account, task_id=task.id,
                error=error or status,
                error_kind=self._error_kind(error or ""),
            ))
        elif status == STATUS_CANCELLED:
            self._emit(TaskCancelled(account=task.account, task_id=task.id))


__all__ = ["TaskScheduler"]
