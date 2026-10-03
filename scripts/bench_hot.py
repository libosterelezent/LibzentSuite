#!/usr/bin/env python3
"""bench_hot — hot-path micro-benchmarks for the framework's per-event core.

STDLIB ONLY: imports project modules that are themselves dependency-free
(event_bus, rate engine with a ManualClock, PersistQueue with a no-op repo,
dashboard _dumps, adapters.model) — no aiosqlite/cryptography needed, so
this runs in any checkout with a bare interpreter.

WHAT IT MEASURES (per-op cost of code that executes for EVERY message /
task / UI tick in production):

  bus-fanout        EventBus.publish of 1000 MessageReceived events to
                    10 sync subscribers (dispatch + isolation overhead).
  rate-acquire      1000 RateLimitManager.acquire cycles under a ManualClock
                    advanced past the pacing interval (pure engine overhead:
                    heap bookkeeping, clock stamps, grant construction — no
                    real sleeping, no lane).
  persist-queue     10_000 records put() through core._persist_queue into a
                    no-op repository and drained by the worker (queue +
                    batching machinery only — NOT disk throughput; use
                    scripts/bench_db.py for real SQLite numbers).
  dashboard-dumps   interfaces.dashboard.server._dumps on a large synthetic
                    "stats" payload (500 log entries, 100 accounts, 100
                    tasks, 200 policies) — the once-per-second WS broadcast
                    serialization.
  model-convert     adapters.model.convert_message on a fat fake message
                    (1500-char content, 2 embeds with fields/author/footer,
                    5 action-row components with buttons, 3 attachments,
                    8 reactions, a resolved reply reference).

MEASURED (repo checkout, Python 3.14.8, PRoot/Android guest, quiet loop,
median of 5 runs — your machine WILL differ; rerun before trusting):

  bus-fanout        1000 publishes x10 subs   ~ 2.9 ms total     ~ 2.9 us/publish
  rate-acquire      1000 cycles               ~ 14  ms total     ~ 14  us/cycle
  persist-queue     10k put+drain             ~ 8   ms total     ~ 0.8 us/record
  dashboard-dumps   200 iterations            ~ 1.6 ms each      ~ 1.6 ms/iter
                                                           (~188 KB JSON payload)
  model-convert     1000 conversions          ~ 113 ms total    ~ 113 us/convert

Usage:
    python3 scripts/bench_hot.py            run all, print table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.event_bus import EventBus, MessageReceived                     # noqa: E402
from core.rate_limit_manager.engine import (                             # noqa: E402
    ManualClock, RateLimitManager,
)
from core._persist_queue import PersistQueue                             # noqa: E402
from interfaces.dashboard.server import _dumps                           # noqa: E402
from adapters.model import convert_message                               # noqa: E402

N_EVENTS = 1_000
N_SUBSCRIBERS = 10
N_ACQUIRES = 1_000
N_RECORDS = 10_000
N_DUMPS = 200
N_CONVERTS = 1_000


# --------------------------------------------------------------- helpers
class BenchResult:
    def __init__(self, name: str, ops: int, times: list[float], note: str = ""):
        self.name = name
        self.ops = ops
        self.times = times  # wall seconds, one per repeat
        self.note = note

    @property
    def best_us_per_op(self) -> float:
        return min(self.times) / self.ops * 1e6

    @property
    def median_us_per_op(self) -> float:
        return statistics.median(self.times) / self.ops * 1e6

    def row(self) -> str:
        total_ms = statistics.median(self.times) * 1e3
        best = self.best_us_per_op
        if best >= 1000.0:
            val, unit = best / 1000.0, "ms/op"
        else:
            val, unit = best, "us/op"
        return (f"{self.name:<16}{self.ops:>10} ops   "
                f"median total {total_ms:>9.2f} ms   "
                f"best {val:>9.2f} {unit}"
                f"   {self.note}")


def timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


# ------------------------------------------------------- 1. bus fanout
def bench_bus(repeat: int) -> BenchResult:
    hits = [0]

    def handler(event) -> None:
        hits[0] += 1

    bus = EventBus()
    for _ in range(N_SUBSCRIBERS):
        bus.subscribe(MessageReceived, handler)
    events = [MessageReceived(message_id=i, channel_id=1, author_id=2,
                              content=f"msg {i}", dm=False) for i in range(N_EVENTS)]

    async def _once() -> float:
        hits[0] = 0
        t0 = time.perf_counter()
        for e in events:
            await bus.publish(e)  # awaited dispatch (the paced hot path)
        return time.perf_counter() - t0

    times = [asyncio.run(_once()) for _ in range(repeat)]
    assert hits[0] == N_EVENTS * N_SUBSCRIBERS, "handler did not fire"
    return BenchResult("bus-fanout", N_EVENTS, times,
                       note=f"(x{N_SUBSCRIBERS} subs = {N_EVENTS * N_SUBSCRIBERS} handler calls)")


# ----------------------------------------------- 2. rate acquire cycle
async def _rate_once() -> float:
    clock = ManualClock()
    mgr = RateLimitManager(bus=None, logger=None, clock=clock, jitter_seed=1)

    async def run() -> None:
        for _ in range(N_ACQUIRES):
            clock.advance(2.0)  # past every pacing clock -> zero real wait
            grant = await mgr.acquire("message", account="acct")
            assert grant.wait_s == 0.0

    t0 = time.perf_counter()
    await run()
    return time.perf_counter() - t0


def bench_rate(repeat: int) -> BenchResult:
    times = [asyncio.run(_rate_once()) for _ in range(repeat)]
    return BenchResult("rate-acquire", N_ACQUIRES, times,
                       note="(ManualClock; no sleep, no lane)")


# ------------------------------------------------- 3. persist queue
class NullRepo:
    """No-op repository: measures queue+batch machinery, not the disk."""

    def __init__(self) -> None:
        self.rows = 0

    async def add_many(self, entries: list[dict]) -> int:
        self.rows += len(entries)
        return len(entries)

    async def add(self, entry: dict) -> int:
        self.rows += 1
        return 1


async def _persist_once() -> tuple[float, int]:
    pq = PersistQueue("bench")
    repo = NullRepo()
    await pq.start(repo, batch=50, interval=0.05)
    entries = [{"i": i, "message": "x" * 80} for i in range(N_RECORDS)]
    t0 = time.perf_counter()
    for e in entries:
        pq.put(e)
    await pq.stop(timeout=30.0)
    wall = time.perf_counter() - t0
    assert repo.rows == N_RECORDS, f"drained {repo.rows}/{N_RECORDS}"
    assert pq.dropped == 0, f"dropped {pq.dropped}"
    return wall, repo.rows


def bench_persist(repeat: int) -> BenchResult:
    times = []
    for _ in range(repeat):
        wall, _ = asyncio.run(_persist_once())
        times.append(wall)
    return BenchResult("persist-queue", N_RECORDS, times,
                       note="(no-op repo; disk: see bench_db.py)")


# ---------------------------------------------- 4. dashboard _dumps
def _big_stats_payload() -> dict:
    accounts = [{
        "name": f"account-{i:03d}", "status": "running", "latency_ms": 42 + i,
        "uptime_s": 3600 + i, "cached_guilds": 12, "cached_channels": 340 + i,
        "cached_dms": 27, "plugins": [{"name": "soulcopy", "enabled": True},
                                      {"name": "roll", "enabled": False}],
        "profiles": ["collector"],
    } for i in range(100)]
    tasks = [{
        "id": i, "name": f"task-{i}", "account": f"account-{i % 100:03d}",
        "plugin": "soulcopy", "action": "message", "status": "completed",
        "duration_s": 2.5, "attempts": 1, "wait_ms": 1500.0, "jitter_ms": 300.0,
        "summary": "copied 3 chunks",
    } for i in range(100)]
    policies = [{
        "action": "message", "level": "account", "owner": f"account-{i % 100:03d}",
        "interval": 2.0 + i * 0.01, "jitter": [0.1, 0.5],
        "sources": [{"level": "global", "owner": None, "interval": 2.0}],
    } for i in range(200)]
    logs = [{
        "ts": 1759500000.0 + i, "severity": "INFO", "source": "scheduler",
        "account": f"account-{i % 100:03d}", "plugin": "soulcopy",
        "task_id": i, "event": "task.completed",
        "message": f"task {i} completed after pacing wait (chunk 1/3)",
    } for i in range(500)]
    return {"type": "stats", "accounts": accounts, "tasks": tasks,
            "ratelimit": {"floor": 1.5, "policies": policies,
                          "cooldowns": [], "counters": {"grants": 1234}},
            "logs": logs}


def bench_dumps(repeat: int) -> BenchResult:
    payload = _big_stats_payload()
    # sanity: the payload must serialize (NaN/inf would crash _dumps)
    assert isinstance(_dumps(payload), str)
    times = []
    for _ in range(repeat):
        times.append(timed(lambda: [_dumps(payload) for _ in range(N_DUMPS)]))
    return BenchResult("dashboard-dumps", N_DUMPS, times,
                       note=f"(payload ~{len(_dumps(payload)) // 1024} KB JSON)")


# ------------------------------------------------- 5. model conversion
class _NS:
    """Attribute bag standing in for a library object."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fat_message():
    author = _NS(id=111222333, name="benchuser", bot=False)
    channel = _NS(id=444555666, name="general", type="text")
    guild = _NS(id=777888999, name="bench guild", member_count=500)
    embeds = [
        _NS(
            title="Mudae character", description="claims: 1",
            url="https://example.invalid/x",
            author=_NS(name="mudae", url="https://example.invalid/a"),
            footer=_NS(text=f"footer text {i}", icon_url="https://e.invalid/f.png"),
            fields=[_NS(name=f"field{k}", value=f"value {k}" * 4, inline=True)
                    for k in range(6)],
            color=0x7289DA,
        ) for i in range(2)
    ]
    components = [
        _NS(type=1, children=[
            _NS(type=2, style=1, custom_id=f"btn-{r}-{b}", label=f"react {b}",
                disabled=False) for b in range(5)
        ]) for r in range(5)
    ]
    attachments = [
        _NS(id=1000 + i, filename=f"card{i}.png",
            url=f"https://cdn.invalid/card{i}.png", size=250_000 + i,
            content_type="image/png") for i in range(3)
    ]
    reactions = [
        _NS(count=3 + i, me=False,
            emoji=_NS(id=None, name=f"emoji{i}", animated=False))
        for i in range(8)
    ]
    referenced = _NS(id=999111, content="reply target", author=author,
                     channel=channel, guild=guild, attachments=[], embeds=[],
                     components=[], reactions=[])
    reference = _NS(message_id=999111, channel_id=444555666, guild_id=777888999,
                    resolved=referenced)
    return _NS(
        id=123456789, content="lorem ipsum dolor sit amet " * 48,
        author=author, channel=channel, guild=guild,
        embeds=embeds, components=components, attachments=attachments,
        reactions=reactions, reference=reference, referenced_message=None,
        created_at=None,
    )


def bench_model(repeat: int) -> BenchResult:
    fat = _fat_message()
    dto = convert_message(fat, dm=False)
    assert len(dto.embeds) == 2 and len(dto.buttons) >= 1
    times = []
    for _ in range(repeat):
        times.append(timed(lambda: [convert_message(fat, dm=False)
                                    for _ in range(N_CONVERTS)]))
    return BenchResult("model-convert", N_CONVERTS, times,
                       note="(fat message: embeds/components/reactions/ref)")


# ----------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(
        description="hot-path micro-benchmarks (stdlib only)")
    parser.add_argument("--repeat", type=int, default=5,
                        help="repeats per benchmark (default 5; persist uses max(3, repeat//2))")
    args = parser.parse_args()

    logging.disable(logging.CRITICAL)  # keep the loop quiet
    repeat = max(1, args.repeat)
    persist_repeat = max(3, repeat // 2) if repeat >= 3 else 3

    print(f"bench_hot — hot-path micro-benchmarks "
          f"(python {sys.version.split()[0]}, repeat={repeat})")
    results = [
        bench_bus(repeat),
        bench_rate(repeat),
        bench_persist(persist_repeat),
        bench_dumps(repeat),
        bench_model(repeat),
    ]
    print()
    for r in results:
        print(r.row())
    print("\nNumbers are machine-dependent; compare medians across runs. "
          "See the module docstring for the recorded baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
