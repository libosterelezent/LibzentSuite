#!/usr/bin/env python3
"""bench_db — event-loop stall benchmark: sync inline sqlite3 vs queued
aiosqlite persistence (spec §40).

WHAT IT MEASURES
----------------
Two harnesses write the SAME 1_000 records into the SAME ``logs`` table
schema (tmp file, WAL on — same pragmas as production):

  legacy   — the OLD pattern: synchronous sqlite3 calls executed INLINE on
             the event loop (one execute+commit per record, exactly how the
             pre-conversion LogService db sink behaved);
  queued   — the NEW pattern: core.logging_service.LogService.record() ->
             SimpleQueue -> ONE async drain worker awaiting the aiosqlite
             repository (start_persistence), i.e. today's production path.

While each harness produces its records, a ticker task sleeps ~1ms per
tick and records the gap between wakes. The MAX gap (minus the sleep
target) approximates the longest continuous period the event loop was
unavailable to other work — the number that matters for pacing accuracy
and websocket heartbeats.

Reported per harness: wall time, max loop stall, median loop stall,
per-record cost. Numbers are machine-dependent (disk cache, CPU, load);
run 3x and compare medians before drawing conclusions.

Usage:
    python3 scripts/bench_db.py [--records 1000] [--repeat 3]
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.logging_service import LogService  # noqa: E402
from database.db import Database  # noqa: E402

LOGS_DDL = """
CREATE TABLE IF NOT EXISTS logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    severity TEXT NOT NULL,
    source   TEXT NOT NULL,
    account  TEXT,
    plugin   TEXT,
    task_id  INTEGER,
    event    TEXT,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_ts ON logs (ts DESC);
"""

PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL")


class LoopStallMeter:
    """Ticker-based max-continuous-unavailability meter (~1ms resolution,
    bounded by the OS timer granularity — treat results as approximate)."""

    def __init__(self, period: float = 0.001) -> None:
        self.period = period
        self.gaps: list[float] = []
        self._task: asyncio.Task[None] | None = None

    async def _run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not stop.is_set():
            await asyncio.sleep(self.period)
            now = loop.time()
            self.gaps.append(max(0.0, now - last - self.period))
            last = now

    def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._run(self._stop))

    async def stop(self) -> None:
        assert self._task is not None
        self._stop.set()
        # let the ticker wake ONE more time so the final gap (the block that
        # starved it) is recorded before cancellation
        try:
            await asyncio.wait_for(self._task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def summary(self) -> dict[str, float]:
        return {
            "max_stall_ms": (max(self.gaps) * 1000.0) if self.gaps else 0.0,
            "median_stall_ms": (statistics.median(self.gaps) * 1000.0) if self.gaps else 0.0,
        }


class BenchLogEntry(TypedDict):
    """One row of the ``logs`` table shape both harnesses write."""

    ts: float
    severity: str
    source: str
    account: str
    plugin: str
    task_id: int | None
    event: str
    message: str


def make_entry(i: int) -> BenchLogEntry:
    return {
        "ts": time.time(),
        "severity": "INFO" if i % 2 else "ERROR",
        "source": "bench",
        "account": "bench-account",
        "plugin": "benchplugin",
        "task_id": None,
        "event": "bench-event",
        "message": f"bench record {i} — lorem ipsum dolor sit amet",
    }


async def bench_legacy_inline(n: int, tmp: Path) -> dict[str, float]:
    """Synchronous sqlite3 execute+commit per record, INLINE on the loop."""
    path = tmp / f"legacy-{time.monotonic_ns()}.db"  # fresh file per run
    conn = sqlite3.connect(str(path))
    for pragma in PRAGMAS:
        conn.execute(pragma)
    conn.executescript(LOGS_DDL)
    conn.commit()

    meter = LoopStallMeter()
    loop = asyncio.get_running_loop()
    meter.start()
    await asyncio.sleep(0.02)  # baseline ticks BEFORE the blocking section,
    # so the first inline write window is actually measured (a ticker that
    # never ran before the block would report nothing)
    t0 = loop.time()
    try:
        for i in range(n):  # the old LogService sink: write-then-wait, per record
            e = make_entry(i)
            conn.execute(
                "INSERT INTO logs (ts, severity, source, account, plugin,"
                " task_id, event, message) VALUES (?,?,?,?,?,?,?,?)",
                (
                    e["ts"],
                    e["severity"],
                    e["source"],
                    e["account"],
                    e["plugin"],
                    e["task_id"],
                    e["event"],
                    e["message"],
                ),
            )
            conn.commit()
    finally:
        wall = loop.time() - t0
        await meter.stop()
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        conn.close()
    out = {"wall_s": wall, "records": count}
    out.update(meter.summary())
    return out


async def bench_queued_aiosqlite(n: int, tmp: Path) -> dict[str, float]:
    """LogService.record() -> queue -> one async drain worker (production)."""
    db = Database(tmp / f"queued-{time.monotonic_ns()}.db")  # fresh per run
    await db.connect()
    service = LogService()
    await service.start_persistence(db.logs)

    meter = LoopStallMeter()
    loop = asyncio.get_running_loop()
    meter.start()
    t0 = loop.time()
    for i in range(n):
        e = make_entry(i)
        service.record(
            e["severity"],
            e["source"],
            e["message"],
            account=e["account"],
            plugin=e["plugin"],
            event=e["event"],
        )
    # wait until every record hit the database (queue fully drained)
    deadline = loop.time() + 60.0
    while loop.time() < deadline:
        rows = await db.logs.recent(limit=n + 10)
        if len(rows) >= n:
            break
        await asyncio.sleep(0.01)
    wall = loop.time() - t0
    await meter.stop()
    await service.stop_persistence()
    out = {"wall_s": wall, "records": len(rows)}
    out.update(meter.summary())
    out["db_failures"] = service.stats().get("db_failures", 0)
    await db.close()
    return out


def render_table(results: list[tuple[str, dict[str, float]]]) -> str:
    header = (
        f"{'harness':<22}{'records':>8}{'wall':>9}{'max stall':>11}{'med stall':>11}{'per-rec':>9}"
    )
    lines = [header, "-" * len(header)]
    for name, r in results:
        per_rec_ms = (r["wall_s"] / r["records"] * 1000.0) if r["records"] else float("nan")
        lines.append(
            f"{name:<22}{r['records']:>8}{r['wall_s']:>8.3f}s"
            f"{r['max_stall_ms']:>10.2f}ms{r['median_stall_ms']:>10.3f}ms"
            f"{per_rec_ms:>8.3f}ms"
        )
    return "\n".join(lines)


async def main_async(records: int, repeat: int) -> int:
    print(
        f"bench_db — {records} persisted log records per harness, {repeat} run(s) each (spec §40)"
    )
    print(
        "stall = longest continuous period the event loop could not run "
        "other work (ticker, ~1ms resolution)\n"
    )
    all_results: list[tuple[str, dict[str, float]]] = []
    with tempfile.TemporaryDirectory(prefix="sm-bench-") as raw:
        tmp = Path(raw)
        for run in range(repeat):
            suffix = f" #{run + 1}" if repeat > 1 else ""
            legacy = await bench_legacy_inline(records, tmp)
            all_results.append((f"legacy inline{suffix}", legacy))
            queued = await bench_queued_aiosqlite(records, tmp)
            all_results.append((f"queued aiosqlite{suffix}", queued))
    print(render_table(all_results))
    print(
        "\nnotes: single-writer SQLite remains — the queued harness removes the\n"
        "EVENT-LOOP stall, not write serialization (see database/repository\n"
        "docstrings and docs/api/database.rst).\n"
        "Numbers vary by machine/disk/load; compare medians across several runs."
    )
    # surface drain failures loudly (a silent bench is a wrong bench)
    for name, r in all_results:
        if r.get("db_failures"):
            print(f"WARNING: {name} reported {r['db_failures']} db write failures")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--records", type=int, default=1_000)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    return asyncio.run(main_async(args.records, args.repeat))


if __name__ == "__main__":
    raise SystemExit(main())
