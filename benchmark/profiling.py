"""FINAL-PHASE (F6): cProfile and tracemalloc bench modes.

Opt-in, isolated from normal benchmark numbers (profiled runs carry
``profiled: true`` and are excluded from compare verdicts); zero overhead
when unused. Output surfaces are value-free by construction: pstats
records (filename, lineno, funcname) + call counts; tracemalloc records
file:lineno + byte counts — neither captures argument values, locals, or
allocated object contents (F7-audited; benchmark stacks use fake tokens
only, and the live .env is never even parsed by a bench process).
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import tracemalloc
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple


@contextmanager
def cprofile_scope(dump_path: str) -> Iterator[cProfile.Profile]:
    """Enable cProfile around the block; dump .pstats on exit."""
    prof = cProfile.Profile()
    prof.enable()
    try:
        yield prof
    finally:
        prof.disable()
        try:
            os.makedirs(os.path.dirname(dump_path) or ".", exist_ok=True)
            prof.dump_stats(dump_path)
        except OSError:
            pass


def pstats_top(prof: cProfile.Profile, n: int) -> Dict[str, List[dict]]:
    """Top-N functions by cumulative and by own time (value-free)."""
    stream = io.StringIO()
    stats = pstats.Stats(prof, stream=stream)
    rows = []
    for (filename, lineno, funcname), (cc, nc, tt, ct, _callers) \
            in stats.stats.items():
        rows.append({
            "site": f"{_short_path(filename)}:{lineno}({funcname})",
            "calls": nc, "own_s": round(tt, 5), "cum_s": round(ct, 5)})
    return {
        "top_cumulative": sorted(rows, key=lambda r: -r["cum_s"])[:n],
        "top_tottime": sorted(rows, key=lambda r: -r["own_s"])[:n],
    }


def _short_path(path: str) -> str:
    """Repo-relative path when possible (stable across machines)."""
    cwd = os.getcwd()
    if path.startswith(cwd + os.sep):
        return path[len(cwd) + 1:]
    return path


def top_sites(diffs: List[Any], n: int) -> List[dict]:
    """Top allocation sites from snapshot diffs (file:lineno + bytes)."""
    rows = []
    for stat in diffs:
        frame = stat.traceback[0]
        filename = frame.filename
        if any(part in filename for part in (
                "tracemalloc", "pstats", os.path.join("benchmark", ""))):
            continue  # harness noise
        rows.append({
            "site": f"{_short_path(filename)}:{frame.lineno}",
            "size_kb": round(stat.size / 1024.0, 1),
            "count": stat.count})
    rows.sort(key=lambda r: -r["size_kb"])
    return rows[:n]


def make_alloc_hook(trace_top: int) -> Tuple[Any, Dict[str, List[dict]]]:
    """A stage-exit snapshot hook + its accumulation dict."""
    stage_allocs: Dict[str, List[dict]] = {}

    def hook(label: str, before: Any, after: Any) -> None:
        try:
            stage_allocs[label] = top_sites(
                after.compare_to(before, "lineno"), trace_top)
        except Exception:
            pass

    return hook, stage_allocs


def trace_reset() -> None:
    """Per-scenario attribution reset (clear_traces + reset_peak — the
    peak counter is NOT reset by clear_traces alone)."""
    import gc
    gc.collect()
    tracemalloc.clear_traces()
    tracemalloc.reset_peak()
