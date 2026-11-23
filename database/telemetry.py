"""FINAL-PHASE (G1/R1): boundary DB worker telemetry — R1 mitigation.

cProfile (``benchmark --profile``) covers MAIN-THREAD Python functions
only; the aiosqlite worker thread's cost shows up as process cpu_sys
with no attribution. This module adds WORKER_THREAD_TIMING measured at
the database/connection.py boundary — deliberately NOT a per-statement
sqlite profiler.

What the seams CAN see (and record):
  * TOTAL_DB_LATENCY — submit→result per read/standalone-execute op,
    exactly the latency the caller experienced (includes the worker
    queue wait, sqlite execution, the threadsafe hop and loop wakeup).
  * COMMIT_TIME — the centralized ``await conn.commit()`` calls
    (connection.py is the only committer in the codebase).
  * TRANSACTION_TIME — outermost ``write()`` body incl. its commit.
  * GUARD_WAIT — caller-side cross-task queuing on the connection.

What they CANNOT (documented, not faked): the queue-wait vs
sqlite-execution split per statement, and per-statement timings inside
a transaction (repositories submit directly through the yielded conn).
A phase-2 ``set_trace_callback`` hook could add true worker-side
statement instants — deliberately out of scope here.

Honesty contract: enabled==False means ZERO work (one attribute load +
bool test per op — no perf_counter, no contextmanager). No per-record
logging, ever; ``snapshot()`` is the only reader surface.
"""

from __future__ import annotations

import bisect
import threading
from collections import deque
from typing import Any

__all__ = ["TELEMETRY", "DbTelemetry"]

#: histogram edges in milliseconds (mirrors LoopLagMonitor's technique)
_BUCKETS_MS = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, float("inf"))

_KINDS = ("read", "write_stmt", "commit", "transaction", "guard_wait")

_RING = 1024  # tail forensics window (bounded)


class DbTelemetry:
    """Cumulative + ring telemetry for DB boundary timings (thread-safe:
    all record() call sites run on the event loop, but the lock makes the
    class safe under any future worker-side producer too)."""

    def __init__(self) -> None:
        self.enabled = False
        # FINAL-PHASE (G1): LZ_DB_TELEMETRY=1 enables live telemetry in
        # ANY process (debugging mode sets it); benchmark enables it
        # programmatically. Default remains OFF (zero overhead).
        import os as _os

        if _os.environ.get("LZ_DB_TELEMETRY", "").lower() in ("1", "true"):
            self.enabled = True
        self._lock = threading.Lock()
        self._counts: dict[str, int] = dict.fromkeys(_KINDS, 0)
        self._errors = dict.fromkeys(_KINDS, 0)
        self._hists: dict[str, list[int]] = {k: [0] * len(_BUCKETS_MS) for k in _KINDS}
        self._max_s: dict[str, float] = dict.fromkeys(_KINDS, 0.0)
        self._ring: deque[tuple[str, float, bool]] = deque(maxlen=_RING)
        self._worker_tid: int | None = None

    # ------------------------------------------------------------- control
    def set_worker_thread(self, thread: Any) -> None:
        """Capture the aiosqlite worker's kernel TID (thread CPU source)."""
        # aiosqlite < 0.22: the connection IS the worker Thread; >= 0.22:
        # it holds the thread as ``_thread``. The CPU sources (/proc/self/task,
        # psutil's thread list) need the KERNEL tid (native_id) — Python's
        # ``ident`` is not that.
        worker = getattr(thread, "_thread", thread)
        self._worker_tid = getattr(worker, "native_id", None) or getattr(worker, "ident", None)

    def record(self, kind: str, seconds: float, ok: bool = True) -> None:
        if not self.enabled:
            return
        ms = max(0.0, seconds * 1000.0)
        with self._lock:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            if not ok:
                self._errors[kind] = self._errors.get(kind, 0) + 1  # pyright: ignore[reportArgumentType,reportGeneralTypeIssues,reportAttributeAccessIssue,reportAssignmentType,reportCallIssue,reportIndexIssue,reportOperatorIssue,reportOptionalMemberAccess,reportOptionalOperand,reportReturnType]  # dynamic boundary
            self._hists[kind][bisect.bisect_left(_BUCKETS_MS, ms)] += 1
            if ms > self._max_s.get(kind, 0.0) * 1000.0:
                self._max_s[kind] = seconds
            # append UNCONDITIONALLY: deque(maxlen=...) evicts the OLDEST
            # entry on its own — the old len<maxlen guard froze the ring
            # at the FIRST 1024 records, so the "tail forensics window"
            # kept showing boot-time entries forever.
            self._ring.append((kind, round(ms, 3), ok))

    def reset(self) -> None:
        with self._lock:
            self._counts = dict.fromkeys(_KINDS, 0)
            self._errors = dict.fromkeys(_KINDS, 0)
            self._hists = {k: [0] * len(_BUCKETS_MS) for k in _KINDS}
            self._max_s = dict.fromkeys(_KINDS, 0.0)
            self._ring.clear()

    # -------------------------------------------------------------- output
    def _pct_ms(self, kind: str, q: float) -> float:
        hist = self._hists.get(kind) or []
        total = sum(hist)
        if not total:
            return 0.0
        target = q * total
        cumulative = 0
        for i, n in enumerate(hist):
            cumulative += n
            if cumulative >= target:
                # bisect_left puts a sample in bucket i when
                # edges[i-1] < ms <= edges[i]
                low = _BUCKETS_MS[i - 1] if i else 0.0
                high = _BUCKETS_MS[i]
                if high == float("inf"):
                    # open-ended overflow bucket: report its finite lower
                    # edge (inf is not JSON-serialisable and not a number
                    # worth showing)
                    return round(low, 3)
                return round(low + (high - low) / 2.0, 3)
        return round(self._max_s.get(kind, 0.0) * 1000.0, 3)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {
                "enabled": self.enabled,
                "requests": {
                    "total": sum(self._counts.values()),
                    "by_kind": dict(self._counts),
                    "errors": sum(self._errors.values()),
                },
                "ring_window": _RING,
            }
            for kind in _KINDS:
                if self._counts.get(kind):
                    out[f"{kind}_ms"] = {
                        "p50": self._pct_ms(kind, 0.50),
                        "p95": self._pct_ms(kind, 0.95),
                        "max": round(self._max_s.get(kind, 0.0) * 1000.0, 3),
                    }
            worker = self._worker_thread_cpu()
            if worker is not None:
                out["worker_thread_cpu"] = worker
            return out

    def _worker_thread_cpu(self) -> dict[str, Any] | None:
        """Worker-thread CPU: absolute user/system seconds, sampled twice by
        the caller for a delta.

        Platform-independent: ``psutil`` answers on every OS when installed;
        ``/proc`` is the Linux-only fallback for installs without it. The
        import stays lazy — telemetry is off (zero work) by default, so the
        production import graph never needs psutil."""
        tid = self._worker_tid
        if tid is None:
            return None
        try:
            import psutil
        except ImportError:
            psutil = None
        if psutil is not None:
            try:
                for entry in psutil.Process().threads():
                    if entry.id == tid:
                        return {
                            "available": True,
                            "user_s": round(entry.user_time, 3),
                            "sys_s": round(entry.system_time, 3),
                        }
            except psutil.Error:
                return {"available": False, "reason": "thread cpu unavailable"}
            return {"available": False, "reason": "worker thread not found"}
        try:
            with open(f"/proc/self/task/{tid}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            utime, stime = int(fields[11]), int(fields[12])
            hz = 100  # os.sysconf("SC_CLK_TCK") is universally 100 on Linux
            return {
                "available": True,
                "user_s": round(utime / hz, 3),
                "sys_s": round(stime / hz, 3),
            }
        except (OSError, ValueError, IndexError):
            return {"available": False, "reason": "requires psutil or /proc"}


TELEMETRY = DbTelemetry()
