"""Samplers: process metrics, event-loop lag, task census — stdlib only.

Design (PHASE-4 S6): every sampler is a loop task binding lazily to the
running loop, cheap enough to run continuously during a scenario and
STOPPED before any leak snapshot (the samplers are themselves tasks/RSS).

LoopLagMonitor generalizes scripts/bench_db.py's LoopStallMeter from
max/median to a bucketed histogram with p50/p95/p99/max. Resolution:
0.25 ms buckets below 100 ms, 1 ms to 2 s — O(1) memory for hour runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import resource
import threading
from bisect import bisect_left
from typing import Any

__all__ = ["LoopLagMonitor", "ProcessSampler", "SamplerSession", "TaskSampler"]


def _edges_ms() -> tuple[float, ...]:
    edges: list[float] = []
    v = 0.0
    while v < 100.0:
        edges.append(v)
        v += 0.25
    while v < 2000.0:
        edges.append(v)
        v += 1.0
    edges.append(float("inf"))
    return tuple(edges)


class ProcessSampler:
    """Periodic process-wide snapshot (default 100 ms).

    CPU from ``resource.getrusage`` (covers the aiosqlite thread + any
    dashboard threads — that IS framework cost). RSS from
    ``/proc/self/status`` on Linux with a ``ru_maxrss`` fallback.
    """

    def __init__(self, interval_s: float = 0.1) -> None:
        self.interval_s = float(interval_s)
        self._task: asyncio.Task[None] | None = None
        self._rss: list[int] = []
        self._fds: list[int] = []
        self._ru0: tuple[float, float] = (0.0, 0.0)
        self._t0: float = 0.0
        self._linux = os.path.isdir("/proc/self")
        self._on_rss: Any | None = None  # G5 spike hook (session-set)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._ru0 = self._rusage()
        self._t0 = asyncio.get_running_loop().time()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._sample()

    async def _run(self) -> None:
        while True:
            self._sample()
            await asyncio.sleep(self.interval_s)

    # -------------------------------------------------------------- probes
    def _rusage(self) -> tuple[float, float]:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        return (ru.ru_utime, ru.ru_stime)

    def _rss_kb(self) -> int | None:
        if self._linux:
            try:
                with open("/proc/self/status") as fh:
                    for line in fh:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1])
            except OSError:
                pass
        ru = resource.getrusage(resource.RUSAGE_SELF)
        kb = int(ru.ru_maxrss)
        return kb // 1024 if kb > 10_000_000 else kb  # macOS reports bytes

    def _fd_count(self) -> int:
        if self._linux:
            try:
                return len(os.listdir("/proc/self/fd"))
            except OSError:
                return -1
        return -1

    def _sample(self) -> None:
        rss = self._rss_kb()
        if rss is not None:
            if self._on_rss is not None and len(self._rss) >= 1:  # G5 hook
                with contextlib.suppress(Exception):
                    self._on_rss(self._rss[-1], rss)
            self._rss.append(rss)
        self._fds.append(self._fd_count())

    # -------------------------------------------------------------- output
    def summary(self) -> dict[str, Any]:
        ru1 = self._rusage()
        wall = max(asyncio.get_running_loop().time() - self._t0, 1e-9)
        cpu = (ru1[0] - self._ru0[0]) + (ru1[1] - self._ru0[1])
        rss = self._rss or [0]
        return {
            "rss_base_kb": rss[0],
            "rss_peak_kb": max(rss),
            "rss_end_kb": rss[-1],
            "cpu_user_s": round(ru1[0] - self._ru0[0], 3),
            "cpu_sys_s": round(ru1[1] - self._ru0[1], 3),
            "cpu_pct": round(cpu / wall * 100.0, 2),
            "fd_peak": max(self._fds) if self._fds else -1,
        }


class LoopLagMonitor:
    """Sleep-drift lag meter: lag = actual_wake - scheduled_wake."""

    EDGES_MS: tuple[float, ...] = _edges_ms()

    def __init__(self, interval_s: float = 0.005) -> None:
        self.interval_s = float(interval_s)
        self._task: asyncio.Task[None] | None = None
        self._hist: list[int] = [0] * len(self.EDGES_MS)
        self._count = 0
        self._max_s = 0.0
        self._on_lag: Any | None = None  # G5 spike hook (session-set)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # the final starving wake IS recorded before cancel; awaiting a
            # cancelled task re-raises CancelledError (BaseException — NOT
            # covered by Exception), so the original tuple is preserved
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self.interval_s)
            now = loop.time()
            self._record(now - last - self.interval_s)
            last = now

    def _record(self, lag_s: float) -> None:
        lag_s = max(0.0, lag_s)
        self._count += 1
        if lag_s > self._max_s:
            self._max_s = lag_s
        self._hist[bisect_left(self.EDGES_MS, lag_s * 1000.0)] += 1
        if self._on_lag is not None:  # G5 spike hook
            with contextlib.suppress(Exception):
                self._on_lag(lag_s)

    def _percentile_ms(self, q: float) -> float:
        if not self._count:
            return 0.0
        target = q * self._count
        cumulative = 0
        for i, n in enumerate(self._hist):
            cumulative += n
            if cumulative >= target:
                low = self.EDGES_MS[i]
                high = self.EDGES_MS[min(i + 1, len(self.EDGES_MS) - 1)]
                width = (high - low) if high != float("inf") else 1.0
                return round(low + width / 2.0, 3)
        return round(self._max_s * 1000.0, 3)

    def summary(self) -> dict[str, Any]:
        return {
            "p50_ms": self._percentile_ms(0.50),
            "p95_ms": self._percentile_ms(0.95),
            "p99_ms": self._percentile_ms(0.99),
            "max_ms": round(self._max_s * 1000.0, 3),
            "samples": self._count,
            "interval_ms": round(self.interval_s * 1000.0, 1),
        }


class TaskSampler:
    """Low-cadence asyncio task + thread census (default 1 s).

    ``asyncio.all_tasks()`` iterates the loop's weak set — calling it at
    10 Hz with thousands of tasks is itself measurable load, so the
    census runs at 1 Hz, decoupled from the process sampler.
    """

    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = float(interval_s)
        self._task: asyncio.Task[None] | None = None
        self._series: list[int] = []
        self._threads: list[int] = []

    def start(self) -> None:
        self._series.append(len(asyncio.all_tasks()))
        self._threads.append(threading.active_count())
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._series.append(self.live_tasks())
        self._threads.append(threading.active_count())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            self._series.append(self.live_tasks())
            self._threads.append(threading.active_count())

    @staticmethod
    def live_tasks() -> int:
        return sum(1 for t in asyncio.all_tasks() if not t.done())

    def summary(self) -> dict[str, Any]:
        return {
            "tasks": {
                "baseline": self._series[0] if self._series else 0,
                "peak": max(self._series) if self._series else 0,
                "end": self._series[-1] if self._series else 0,
            },
            "threads": {
                "baseline": self._threads[0] if self._threads else 0,
                "peak": max(self._threads) if self._threads else 0,
                "end": self._threads[-1] if self._threads else 0,
            },
        }


class SpikeCapture:
    """G5: debounced diagnostic snapshot on loop-lag / RSS spikes.

    Self-relative thresholds (no stored baseline at runtime): a lag sample
    > K× the run's own p50 (with a floor for stall-hunt mode) or an RSS
    jump > 32 MB between samples triggers ONE capture per 2s window, capped
    at 20. Snapshot content: trigger detail + any registered provider's
    stats() (scheduler/bus/logs) — sync-only, µs-ms, value-free. Spikes
    NEVER enter compare metrics (diagnostic only). Runs under
    --capture-spikes are labeled DEEP_DIAGNOSTIC and compare-excluded.
    """

    def __init__(
        self,
        *,
        k_lag: float = 10.0,
        floor_ms: float = 25.0,
        rss_jump_mb: float = 32.0,
        debounce_s: float = 2.0,
        cap: int = 20,
    ) -> None:
        self.k_lag = float(k_lag)
        self.floor_ms = float(floor_ms)
        self.rss_jump_mb = float(rss_jump_mb)
        self.debounce_s = float(debounce_s)
        self.cap = int(cap)
        self.captures: list[dict[str, Any]] = []
        self.capture_ctx: dict[str, Any] = {}  # providers (scenario-set)
        self._last_capture = 0.0
        import time as _time

        self._now = _time.monotonic

    # ------------------------------------------------------------ triggers
    def on_lag(self, monitor: LoopLagMonitor, lag_s: float) -> None:
        lag_ms = lag_s * 1000.0
        p50 = monitor._percentile_ms(0.50)
        if lag_ms <= max(self.k_lag * p50, self.floor_ms):
            return
        self._fire({"kind": "lag", "lag_ms": round(lag_ms, 2), "p50_ms": round(p50, 2)})

    def on_rss(self, prev_kb: int, now_kb: int) -> None:
        if (now_kb - prev_kb) / 1024.0 <= self.rss_jump_mb:
            return
        self._fire({"kind": "rss", "jump_mb": round((now_kb - prev_kb) / 1024.0, 1)})

    def _fire(self, trigger: dict[str, Any]) -> None:
        now = self._now()
        if now - self._last_capture < self.debounce_s:
            return
        if len(self.captures) >= self.cap:
            return
        self._last_capture = now
        capture: dict[str, Any] = {"t_s": round(now, 3), "trigger": trigger}
        for label, provider in self.capture_ctx.items():
            stats = getattr(provider, "stats", None)
            if callable(stats):
                with contextlib.suppress(Exception):
                    capture[label] = stats()
        self.captures.append(capture)


class SamplerSession:
    """One facade per scenario: owns all samplers + leak-snapshot helper."""

    def __init__(
        self,
        *,
        sampling_ms: int = 100,
        loop_interval_ms: int = 5,
        task_interval_ms: int = 1000,
        spike_capture: SpikeCapture | None = None,
    ) -> None:
        self.process = ProcessSampler(sampling_ms / 1000.0)
        self.lag = LoopLagMonitor(loop_interval_ms / 1000.0)
        self.tasks = TaskSampler(task_interval_ms / 1000.0)
        self.spike_capture = spike_capture
        if spike_capture is not None:
            capture = spike_capture
            monitor = self.lag

            def _on_lag(lag_s: float) -> None:
                capture.on_lag(monitor, lag_s)

            def _on_rss(prev_kb: int, now_kb: int) -> None:
                capture.on_rss(prev_kb, now_kb)

            self.lag._on_lag = _on_lag
            self.process._on_rss = _on_rss

    def start(self) -> None:
        gc.collect()
        self.process.start()
        self.lag.start()
        self.tasks.start()

    async def stop(self) -> None:
        await self.process.stop()
        await self.lag.stop()
        await self.tasks.stop()

    async def quiesce(self) -> None:
        """Between-cycle settle for honest leak snapshots."""
        gc.collect()
        await asyncio.sleep(0.15)
        gc.collect()

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        out.update(self.process.summary())
        out["loop_lag_ms"] = self.lag.summary()
        out.update(self.tasks.summary())
        return out
