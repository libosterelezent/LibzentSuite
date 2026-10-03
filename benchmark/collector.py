"""PerfCollector: opt-in stage timing registry (zero cost when unused)."""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List


class PerfCollector:
    """Registers wall-times per label; p50/p95/max per snapshot.

    Created ONLY by benchmark harness code — nothing in the production
    paths imports or consults it (profiling is opt-in by construction).
    """

    def __init__(self, alloc_hook: Any = None) -> None:
        self._records: Dict[str, List[float]] = {}
        self._errors: Dict[str, int] = {}
        self._counts: Dict[str, int] = {}
        # FINAL-PHASE (F6): optional stage-exit allocation hook (the
        # tracemalloc bench mode); None = zero overhead when unused.
        self._alloc_hook = alloc_hook

    @contextmanager
    def stage(self, label: str) -> Iterator["PerfCollector"]:
        import tracemalloc
        enter_snap = (tracemalloc.take_snapshot()
                      if self._alloc_hook is not None
                      and tracemalloc.is_tracing() else None)
        start = time.perf_counter()
        try:
            yield self
        except Exception:
            self._errors[label] = self._errors.get(label, 0) + 1
            raise
        finally:
            self.record_call(label, time.perf_counter() - start)
            if enter_snap is not None and self._alloc_hook is not None:
                try:  # snapshot AFTER the timer stops: hook cost stays out
                    self._alloc_hook(label, enter_snap,
                                     tracemalloc.take_snapshot())
                except Exception:
                    pass

    def timed(self, label: str) -> Callable:
        """Decorator for sync AND async functions."""
        collector = self

        def decorator(fn: Callable) -> Callable:
            if asyncio.iscoroutinefunction(fn):
                async def async_wrapper(*a: Any, **k: Any) -> Any:
                    with collector.stage(label):
                        return await fn(*a, **k)
                return async_wrapper
            def sync_wrapper(*a: Any, **k: Any) -> Any:
                with collector.stage(label):
                    return fn(*a, **k)
            return sync_wrapper
        return decorator

    def record_call(self, label: str, seconds: float,
                    ok: bool = True) -> None:
        self._records.setdefault(label, []).append(seconds)
        self._counts[label] = self._counts.get(label, 0) + 1
        if not ok:
            self._errors[label] = self._errors.get(label, 0) + 1

    @staticmethod
    def _pct(values: List[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(int(q * len(ordered)), len(ordered) - 1)
        return round(ordered[idx], 6)

    def snapshot(self) -> Dict[str, Any]:
        return {
            label: {
                "count": len(times),
                "total_s": round(sum(times), 4),
                "p50_s": self._pct(times, 0.50),
                "p95_s": self._pct(times, 0.95),
                "max_s": round(max(times), 6),
                "errors": self._errors.get(label, 0),
            }
            for label, times in self._records.items()
        }
