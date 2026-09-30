"""SendEngine — the FarmLite serialized-lane concept, refined for SlaughterMudae.

Why it exists (user spec): token buckets pace the RATE of actions but do not
prevent a single account from having two Discord requests IN FLIGHT at the
same moment — concurrent API calls are exactly what flags selfbot accounts.
The old FarmLite `core/util/rate_limiter.py` solved this for ONE account with
a priority queue drained by a single engine task, spaced by `prefixrate`.

Refinements over the FarmLite original:
  * per-account engines (each selfbot gets its own singleton lane)
  * submitter-friendly: `submit()` returns/raises the coroutine's own
    outcome (FarmLite swallowed task exceptions; here they propagate to the
    caller while the engine itself stays alive)
  * coalescing is opt-in per NAME (latest-wins); anonymous submissions never
    collapse — two different messages must both send
  * graceful stop: pending submissions fail fast with SendEngineStopped
  * observability: executed count, queue depth, per-task wait + duration in
    stats; the account session feeds engine wait into the audit trail

Semantics preserved from FarmLite:
  * `prefixrate > 0` — sleep BEFORE executing each task (spacing, never
    concurrent)
  * `prefixrate == 0` — the lane PAUSES (submissions wait until a non-zero
    rate is set via set_prefixrate / the dashboard / the CLI)
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from .rate_limiter import Priority

LOG = logging.getLogger("slaughtermudae.engine")


class SendEngineStopped(RuntimeError):
    """Raised to pending submissions when the engine stops."""


@dataclass(frozen=True)
class SendPolicy:
    """Per-account engine configuration (FarmLite prefixrate policy)."""

    enabled: bool = False
    prefixrate: float = 1.5

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SendPolicy":
        data = data or {}
        allowed = {"enabled", "prefixrate"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown send_engine keys: {sorted(unknown)}")
        return cls(
            enabled=bool(data.get("enabled", False)),
            prefixrate=float(data.get("prefixrate", 1.5)),
        )

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "prefixrate": self.prefixrate}


@dataclass
class _SendTask:
    priority: int
    seq: int
    name: str
    factory: Callable[[], Awaitable[Any]]
    future: "asyncio.Future"
    queued_at: float
    cancelled: bool = False

    def __lt__(self, other: "_SendTask") -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


class SendEngine:
    """ONE-at-a-time execution lane for an account's outbound actions."""

    def __init__(
        self,
        account: str,
        prefixrate: float = 1.5,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.account = account
        self.prefixrate = float(prefixrate)
        self._logger = logger or logging.getLogger(f"slaughtermudae.engine.{account}")
        self._queue: list = []                 # heap of _SendTask
        self._by_name: Dict[str, _SendTask] = {}  # coalescing: latest per name
        self._current: Optional[_SendTask] = None   # popped, not yet finished
        self._seq = itertools.count()
        self._engine_task: Optional[asyncio.Task] = None
        self._running = False
        self._wake: Optional[asyncio.Event] = None      # new work arrived
        self._rate_event: Optional[asyncio.Event] = None  # prefixrate > 0 gate
        # stats
        self.executed = 0
        self.skipped = 0
        self.last_wait_seconds = 0.0
        self.last_duration_seconds = 0.0

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "SendEngine":
        """Spawn the engine loop (idempotent)."""
        if self._engine_task is None or self._engine_task.done():
            self._running = True
            self._wake = asyncio.Event()
            self._rate_event = asyncio.Event()
            if self.prefixrate > 0:
                self._rate_event.set()
            self._engine_task = asyncio.get_running_loop().create_task(
                self._run(), name=f"send-engine-{self.account}"
            )
        return self

    async def stop(self) -> None:
        """Stop the lane; pending submissions fail with SendEngineStopped."""
        self._running = False
        task = self._engine_task
        self._engine_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                pass
        for pending in list(self._queue):
            if not pending.future.done():
                pending.future.set_exception(
                    SendEngineStopped(f"send engine for {self.account!r} stopped")
                )
        if self._current is not None and not self._current.future.done():
            self._current.future.set_exception(SendEngineStopped("engine stopped mid-task"))
        self._current = None
        self._queue.clear()
        self._by_name.clear()

    # -------------------------------------------------------------- tuning
    def set_prefixrate(self, seconds: float) -> float:
        """Set the spacing between consecutive tasks.

        FarmLite semantics, preserved: 0 PAUSES the lane (submissions wait
        until a non-zero value is set); > 0 runs with that spacing.
        """
        seconds = float(seconds)
        if seconds < 0:
            raise ValueError(f"prefixrate must be >= 0, got {seconds}")
        self.prefixrate = seconds
        if self._rate_event is not None:
            if seconds > 0:
                self._rate_event.set()
            else:
                self._rate_event.clear()
        self._logger.info("prefixrate set to %.3fs for %s", seconds, self.account)
        return seconds

    # ------------------------------------------------------------ submission
    def submit(
        self,
        name: Optional[str],
        factory: Callable[[], Awaitable[Any]],
        priority: Priority | int = Priority.NORMAL,
    ) -> "asyncio.Future":
        """Queue one action; the future resolves with the action's outcome.

        name=None -> unique auto-name (never coalesced; use for sends).
        name set  -> coalescing: a newer submission with the same name
                     invalidates the queued older one (latest wins).

        NOTE: this returns a Future; prefer `await submit(...)` (async
        facade below) which also works with asyncio.create_task.
        """
        if not self._running or self._engine_task is None:
            raise SendEngineStopped(f"send engine for {self.account!r} is not running")
        task = _SendTask(
            priority=int(priority),
            seq=next(self._seq),
            name=name or f"anon-{next(self._seq)}",
            factory=factory,
            future=asyncio.get_running_loop().create_future(),
            queued_at=time.perf_counter(),
        )
        if name is not None:
            previous = self._by_name.get(name)
            if previous is not None and not previous.future.done():
                previous.cancelled = True  # latest wins (shadow-queue idea)
        self._by_name[task.name] = task
        import heapq

        heapq.heappush(self._queue, task)
        if self._wake is not None:
            self._wake.set()
        return task.future

    async def run(
        self,
        name: Optional[str],
        factory: Callable[[], Awaitable[Any]],
        priority: Priority | int = Priority.NORMAL,
    ) -> Any:
        """Async facade: submit + await the action's own outcome/exception."""
        return await self.submit(name, factory, priority)

    # ---------------------------------------------------------------- engine
    async def _run(self) -> None:
        import heapq

        try:
            while self._running:
                if self.prefixrate <= 0:
                    self._logger.info("prefixrate is 0 — lane paused, tasks waiting")
                    await self._rate_event.wait()
                if not self._queue:
                    await self._wake.wait()
                    self._wake.clear()
                    continue
                task = heapq.heappop(self._queue)
                if task.cancelled or task.future.done():
                    self.skipped += 1
                    if self._by_name.get(task.name) is task:
                        self._by_name.pop(task.name, None)
                    continue
                # NOTE: _by_name keeps the entry while the task is in flight —
                # a newer same-name submission must still invalidate it
                # (FarmLite shadow-queue behavior)
                self._current = task
                if self.prefixrate > 0:
                    # FarmLite spacing: sleep BEFORE every execution, so two
                    # requests can never be in flight simultaneously
                    await asyncio.sleep(self.prefixrate)
                # FarmLite validity re-check AFTER the spacing sleep: a newer
                # same-name submission (or a stop) may have invalidated it
                if task.cancelled or task.future.done():
                    self.skipped += 1
                    if not task.future.done():
                        task.future.set_exception(
                            SendEngineStopped(f"superseded by a newer '{task.name}' submission")
                        )
                    if self._by_name.get(task.name) is task:
                        self._by_name.pop(task.name, None)
                    self._current = None
                    continue
                self.last_wait_seconds = time.perf_counter() - task.queued_at
                started = time.perf_counter()
                try:
                    result = await task.factory()
                except asyncio.CancelledError:
                    if not task.future.done():
                        task.future.set_exception(
                            SendEngineStopped("engine cancelled mid-task")
                        )
                    raise
                except Exception as exc:
                    if not task.future.done():
                        task.future.set_exception(exc)
                else:
                    if not task.future.done():
                        task.future.set_result(result)
                finally:
                    self.last_duration_seconds = time.perf_counter() - started
                    self.executed += 1
                    self._current = None
                    if self._by_name.get(task.name) is task:
                        self._by_name.pop(task.name, None)
        except asyncio.CancelledError:
            current = self._current
            if current is not None and not current.future.done():
                current.future.set_exception(SendEngineStopped("engine cancelled mid-task"))
            self._current = None
            return

    # ----------------------------------------------------------------- stats
    def stats(self) -> Dict[str, Any]:
        live = [t for t in self._queue if not t.cancelled and not t.future.done()]
        return {
            "account": self.account,
            "enabled": True,
            "running": bool(self._running and self._engine_task and not self._engine_task.done()),
            "prefixrate": self.prefixrate,
            "paused": self.prefixrate <= 0,
            "queued": len(live),
            "executed": self.executed,
            "skipped": self.skipped,
            "last_wait_ms": round(self.last_wait_seconds * 1000, 1),
            "last_duration_ms": round(self.last_duration_seconds * 1000, 1),
        }
