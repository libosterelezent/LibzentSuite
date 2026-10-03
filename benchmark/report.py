"""BenchReport: human table + JSON output + baseline compare."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

SCHEMA = "sm-bench/1"

#: documented noise tolerances (compare verdicts); a metric "regressed"
#: only when it exceeds the larger of the relative and absolute bands.
TOLERANCES = {
    "wall_s": (0.10, 0.2),     # (relative, absolute)
    "cpu_pct": (0.05, 1.0),
    "rss_base_kb": (0.01, 512.0),
    "rss_peak_kb": (0.01, 512.0),
    "lag_p50_ms": (0.0, 0.2),
    "lag_p95_ms": (0.15, 1.0),
    "lag_p99_ms": (0.15, 1.0),
    "lag_max_ms": (0.25, 5.0),
    "tasks_end": (0.0, 2.0),
    "threads_end": (0.0, 1.0),
}


def _flat_metrics(report: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    proc = report.get("process", {})
    lag = report.get("loop_lag_ms", {})
    tasks = report.get("tasks", {})
    threads = report.get("threads", {})
    custom = report.get("custom", {})
    for k in ("rss_base_kb", "rss_peak_kb", "cpu_pct"):
        if k in proc:
            out[k] = float(proc[k])
    out["lag_p50_ms"] = float(lag.get("p50_ms", 0))
    out["lag_p95_ms"] = float(lag.get("p95_ms", 0))
    out["lag_p99_ms"] = float(lag.get("p99_ms", 0))
    out["lag_max_ms"] = float(lag.get("max_ms", 0))
    out["tasks_end"] = float(tasks.get("end", 0))
    out["threads_end"] = float(threads.get("end", 0))
    stages = report.get("stages", {})
    total = sum(s.get("total_s", 0) for s in stages.values())
    out["wall_s"] = float(total)
    for k, v in custom.items():
        if isinstance(v, (int, float)):
            out[f"custom.{k}"] = float(v)
    return out


def _verdict(metric: str, before: float, after: float) -> str:
    rel, abs_ = TOLERANCES.get(metric, (0.15, 0.0))
    delta = after - before
    band = max(abs(before * rel), abs_)
    lower_is_better = metric.startswith(("rss_", "cpu_", "lag_", "wall_"))
    if abs(delta) <= band:
        return "unchanged"
    if delta > 0:
        return "regressed" if lower_is_better else "improved"
    return "improved" if lower_is_better else "regressed"


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    """Human before/after/Δ table across scenarios + metrics."""
    lines = ["BASELINE COMPARE", "=" * 78]
    overall_regressed = False
    for scen in after.get("scenarios", []):
        name = scen.get("name", "?")
        if scen.get("profiled"):
            lines.append(f"{name}: profiled run — excluded from compare")
            continue
        prev = next((s for s in before.get("scenarios", [])
                     if s.get("name") == name), None)
        if prev is None:
            lines.append(f"{name}: (no baseline)")
            continue
        if prev.get("profiled"):
            lines.append(f"{name}: baseline was a profiled run — skipped")
            continue
        lines.append(f"{name}:")
        b_flat, a_flat = _flat_metrics(prev), _flat_metrics(scen)
        for metric in sorted(set(b_flat) | set(a_flat)):
            b, a = b_flat.get(metric, 0.0), a_flat.get(metric, 0.0)
            verdict = _verdict(metric, b, a)
            if verdict == "regressed":
                overall_regressed = True
            lines.append(f"  {metric:<28} {b:>12.3f} -> {a:>12.3f}"
                         f"  [{verdict}]")
    lines.append("=" * 78)
    lines.append("OVERALL: REGRESSED — rerun with --repeat 3 (medians)"
                 if overall_regressed else "OVERALL: ok (within tolerances)")
    return "\n".join(lines)


def render_table(report: Dict[str, Any]) -> str:
    """One human block per scenario."""
    lines = ["FRAMEWORK BENCHMARK", "=" * 78]
    for scen in report.get("scenarios", []):
        lines.append(f"Scenario: {scen.get('name')}  "
                     f"(mode={scen.get('mode', '-')}, "
                     f"cycles={scen.get('cycles')}, "
                     f"passed={scen.get('passed')})")
        proc = scen.get("process", {})
        lines.append(f"  RSS: base={proc.get('rss_base_kb', '?')}kB "
                     f"peak={proc.get('rss_peak_kb', '?')}kB "
                     f"end={proc.get('rss_end_kb', '?')}kB")
        lines.append(f"  CPU: {proc.get('cpu_pct', '?')}% "
                     f"(u={proc.get('cpu_user_s')}s "
                     f"s={proc.get('cpu_sys_s')}s)")
        lag = scen.get("loop_lag_ms", {})
        lines.append(f"  Loop lag ms: p50={lag.get('p50_ms')} "
                     f"p95={lag.get('p95_ms')} p99={lag.get('p99_ms')} "
                     f"max={lag.get('max_ms')} ({lag.get('samples')} samples)")
        tasks = scen.get("tasks", {})
        threads = scen.get("threads", {})
        lines.append(f"  Tasks: {tasks}   Threads: peak={threads.get('peak')}"
                     f" end={threads.get('end')}")
        leak = scen.get("leak", {})
        if leak:
            lines.append(f"  Leak verdict: {leak.get('verdict')} "
                         f"(slope={leak.get('slope_kb_per_cycle', 0):.1f}"
                         " kB/cycle)")
            series = leak.get("rss_end_per_cycle_kb", [])
            lines.append(f"  RSS series: {series}")
        for label, stage in scen.get("stages", {}).items():
            lines.append(f"  stage {label}: n={stage.get('count')} "
                         f"total={stage.get('total_s')}s "
                         f"p95={stage.get('p95_s')}s "
                         f"max={stage.get('max_s')}s")
        for k, v in scen.get("custom", {}).items():
            lines.append(f"  {k}: {v}")
        lines.append("-" * 78)
    return "\n".join(lines)


def to_json(report: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def leak_verdict(rss_series: List[float], task_series: List[int]
                 ) -> Dict[str, Any]:
    """Trend-based leak verdict (S6 §3.3): no arbitrary thresholds.

    cycle 1 is warmup (allocator arenas, imports, sqlite page cache) and
    is excluded from the trend math but shown.
    """
    n = len(rss_series)
    if n < 3:
        return {"rss_end_per_cycle_kb": rss_series,
                "tasks_end_per_cycle": task_series,
                "slope_kb_per_cycle": 0.0,
                "verdict": "insufficient-cycles"}
    s = rss_series[1:]
    growth = s[-1] - s[0]
    noise = max(256.0, 2.0 * max((s[i] - s[i + 1] for i in range(len(s) - 1)),
                                 default=0.0))
    non_returning = all(
        max((s[i] - later for later in s[i + 1:]), default=0.0) <= noise
        for i in range(len(s) - 1))
    slope = growth / max(len(s) - 1, 1)
    if non_returning and growth > noise and n >= 5:
        verdict = "suspected-leak"
    elif growth <= noise:
        verdict = "stable"
    else:
        verdict = "inconclusive"
    return {"rss_end_per_cycle_kb": rss_series,
            "tasks_end_per_cycle": task_series,
            "slope_kb_per_cycle": round(slope, 1),
            "verdict": verdict}
