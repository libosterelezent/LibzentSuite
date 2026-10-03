"""Shared queued-persistence engine for the log and audit sinks.

``LogService`` and ``AuditRecorder`` previously carried ~100 duplicated
lines each of drain-worker machinery. This is the one implementation.

Contract (spec §29 — the record path never touches SQLite):

* :meth:`put` is thread-safe, never blocks, never raises. Above
  ``max_inflight`` queued items new ones are DROPPED from persistence only
  (and counted) so a wedged database can't balloon memory.
* ONE asyncio drain worker pulls up to ``batch`` items and writes them with
  the repository's ``add_many`` (ONE transaction + ONE prune per batch)
  when it has one, else item-by-item ``add``. If a batch write fails it is
  retried item-by-item so a single bad row cannot take its neighbours down;
  failures are counted and surface as one raw stderr line per burst
  (never through the logging machinery itself — that is the feedback loop
  this design exists to avoid).
* :meth:`stop` finishes the in-flight batch (bounded join), cancels a
  stuck worker, then drains whatever is left INLINE in original order
  (bounded by ``timeout``). A batch interrupted by cancellation is kept
  and written first, so shutdown loses nothing it had already dequeued.
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading

from ._asyncutil import outer_cancel_pending
from typing import Any, Dict, List, Optional

#: default cap on queued-but-undrained items
MAX_INFLIGHT = 10_000


class PersistQueue:
    """Thread-safe producer queue + one async batching drain worker."""

    def __init__(self, label: str, *, max_inflight: int = MAX_INFLIGHT) -> None:
        if int(max_inflight) < 1:
            raise ValueError(f"max_inflight must be >= 1, got {max_inflight!r}")
        self._label = label
        self._max = int(max_inflight)
        self._q: "queue.SimpleQueue[Dict[str, Any]]" = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._worker: Optional["asyncio.Task"] = None
        self._stopping = False
        self._stopped = False
        self._draining: Optional["asyncio.Future[None]"] = None
        self._repo: Any = None
        self._batch = 50
        self._interval = 0.25  # floor below: 0 would busy-spin the loop
        self._carry: List[Dict[str, Any]] = []  # dequeued, not yet written
        self._notified = False
        self.db_failures = 0
        self.dropped = 0

    # ----------------------------------------------------------- producers
    @property
    def active(self) -> bool:
        """True while a live worker is accepting records."""
        worker = self._worker
        return worker is not None and not worker.done()

    def put(self, item: Dict[str, Any]) -> None:
        """Enqueue one dict (advisory cap: threads may overshoot by a hair)."""
        if self._stopped:
            with self._lock:  # post-stop arrivals can never drain: count them
                self.dropped += 1
            return
        if self._q.qsize() >= self._max:
            with self._lock:
                self.dropped += 1
            return
        self._q.put(item)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"db_failures": self.db_failures, "dropped": self.dropped}

    # ----------------------------------------------------------- lifecycle
    async def start(self, repo: Any, *, batch: int = 50,
                    interval: float = 0.25) -> Optional["asyncio.Task"]:
        """Spawn the worker (idempotent: a live worker is returned as-is)."""
        if repo is None:
            return self._worker
        if self.active:
            return self._worker
        if self._stopping:
            # a stop is still draining: wait it out, else clearing _stopping
            # here would RESURRECT the old worker's loop and two workers
            # would share the queue/_carry (order + duplication bugs)
            draining = self._draining
            if draining is not None:
                await asyncio.wait({asyncio.shield(draining)}, timeout=5.0)
            self._stopping = False
        self._stopped = False
        self._repo = repo
        self._batch = max(1, int(batch))
        self._interval = max(0.05, float(interval))
        self._worker = asyncio.get_running_loop().create_task(
            self._run(), name=f"{self._label}-persistence")
        return self._worker

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop accepting, finish the in-flight batch, drain the rest.
        Single-flight: a concurrent stop JOINS instead of returning early
        while the first still drains (an early return let callers close the
        DB under the live drainer).

        Every remaining batch write is bounded by the time still left on
        the overall ``timeout`` budget — a slow/locked repository cannot
        stretch shutdown past it. Items still queued when the budget runs
        out are counted in ``dropped`` (visible via stats()).
        """
        if self._stopping:
            # join the in-flight teardown: bounded by the join budget
            await asyncio.wait({asyncio.shield(self._draining)}, timeout=timeout)
            return
        worker = self._worker
        if worker is None:
            return
        self._worker = None  # put() callers stop enqueueing from now on
        self._stopping = True
        self._draining = asyncio.get_running_loop().create_future()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        await asyncio.wait({worker}, timeout=min(0.5, max(0.0, float(timeout))))
        worker.cancel()  # no-op when it already exited gracefully
        await asyncio.gather(worker, return_exceptions=True)
        try:
            await self._drain_inline(loop, deadline)
        finally:
            if not self._draining.done():
                self._draining.set_result(None)
            self._stopping = False
            self._stopped = True

    async def _drain_inline(self, loop, deadline) -> None:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            batch = self._next_batch()
            if not batch:
                return
            try:
                await asyncio.wait_for(self._write(batch), timeout=remaining)
            except asyncio.CancelledError:
                # budget-exhaustion cancels the batch task; an OUTER cancel
                # (this stop() call itself being cancelled) must propagate —
                # swallowing it breaks asyncio.timeout/TaskGroup semantics
                with self._lock:  # count what never landed either way
                    self.dropped += len(self._carry) + self._q.qsize()
                self._drain_counted()
                self._carry = []
                if not outer_cancel_pending():
                    return  # our own wait_for timeout: clean stop
                raise
            except asyncio.TimeoutError:
                with self._lock:
                    self.dropped += len(self._carry) + self._q.qsize()
                self._drain_counted()
                self._carry = []
                return
            await asyncio.sleep(0)  # yield between batches (big drains)
        leftover = len(self._carry) + self._q.qsize()
        if leftover:
            with self._lock:
                self.dropped += leftover
            self._drain_counted()
            self._carry = []

    # --------------------------------------------------------------- drain
    def _next_batch(self) -> List[Dict[str, Any]]:
        """Carry-over first (order!), then up to ``batch`` queued items."""
        batch = self._carry
        self._carry = []
        while len(batch) < self._batch:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        return batch

    async def _run(self) -> None:
        while not self._stopping:
            batch = self._next_batch()
            if batch:
                await self._write(batch)
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(self._interval)

    async def _write(self, batch: List[Dict[str, Any]]) -> None:
        """Persist one batch; never raises except to propagate cancellation
        (the batch is then kept in ``_carry`` for the inline drain)."""
        repo = self._repo
        self._carry = batch  # visible to stop() if we are cancelled mid-write
        try:
            add_many = getattr(repo, "add_many", None)
            if add_many is not None and len(batch) > 1:
                try:
                    await add_many(batch)
                    self._notified = False  # burst over: next failure logs
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._write_each(repo, batch)  # isolate the bad row
            else:
                await self._write_each(repo, batch)
            self._carry = []
        except asyncio.CancelledError:
            raise

    async def _write_each(self, repo: Any, batch: List[Dict[str, Any]]) -> None:
        while batch:
            item = batch[0]
            try:
                await repo.add(item)
                self._notified = False
            except asyncio.CancelledError:
                raise  # batch[0] never landed: it stays for the re-drain
            except Exception as exc:
                with self._lock:
                    self.db_failures += 1
                self._notify(exc)
            # remove ONLY after add() returned/failed-for-good: a cancellation
            # mid-batch must not leave committed rows in _carry (the inline
            # drain would re-write them -> duplicate rows)
            batch.pop(0)

    def _drain_counted(self) -> None:
        """Empty the queue after counting it dropped (restart hygiene:
        a restarted worker must not re-write rows already counted lost)."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def _notify(self, exc: BaseException) -> None:
        """One raw stderr one-liner per failure BURST (reset on success)."""
        if self._notified:
            return
        self._notified = True
        try:
            sys.stderr.write(
                f"[slaughtermudae] {self._label} persistence failing: "
                f"{type(exc).__name__}: {exc} "
                "(further errors counted silently)\n")
        except Exception:
            pass  # even stderr must never break the drain


__all__ = ["PersistQueue", "MAX_INFLIGHT"]
