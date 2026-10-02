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
from typing import Any, Dict, List, Optional

#: default cap on queued-but-undrained items
MAX_INFLIGHT = 10_000


class PersistQueue:
    """Thread-safe producer queue + one async batching drain worker."""

    def __init__(self, label: str, *, max_inflight: int = MAX_INFLIGHT) -> None:
        self._label = label
        self._max = int(max_inflight)
        self._q: "queue.SimpleQueue[Dict[str, Any]]" = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._worker: Optional["asyncio.Task"] = None
        self._stopping = False
        self._repo: Any = None
        self._batch = 50
        self._interval = 0.25
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
        self._repo = repo
        self._batch = max(1, int(batch))
        self._interval = max(0.0, float(interval))
        self._stopping = False
        self._worker = asyncio.get_running_loop().create_task(
            self._run(), name=f"{self._label}-persistence")
        return self._worker

    async def stop(self, timeout: float = 5.0) -> None:
        """Stop accepting, finish the in-flight batch, drain the rest."""
        worker = self._worker
        if worker is None:
            return
        self._worker = None  # put() callers stop enqueueing from now on
        self._stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        await asyncio.wait({worker}, timeout=min(0.5, max(0.0, float(timeout))))
        worker.cancel()  # no-op when it already exited gracefully
        await asyncio.gather(worker, return_exceptions=True)
        while loop.time() < deadline:
            batch = self._next_batch()
            if not batch:
                return
            await self._write(batch)
            await asyncio.sleep(0)  # yield between batches (big drains)

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
        for item in batch:
            try:
                await repo.add(item)
                self._notified = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self._lock:
                    self.db_failures += 1
                self._notify(exc)

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
