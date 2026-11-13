"""Benchmark scenarios (fakes/simulation only — never real Discord).

Every scenario runs N cycles inside the leak-protocol wrapper: per cycle
the harness records the post-quiesce end-state; the report renders the
series and a trend verdict (no arbitrary thresholds).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any

from benchmark.report import leak_verdict
from benchmark.stack import BenchStack, TemporaryWorkspace
from core.cooldowns import CooldownService
from core.event_bus import EventBus, MessageReceived
from core.logging_service import LogService
from core.plugin_manager import PluginManager
from core.settings_manager import SettingsManager
from database.db import Database

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from benchmark.collector import PerfCollector
    from benchmark.sampler import SamplerSession

OWNER = 1001
CHANNEL = 500

#: fixture plugins for the dispatch benchmark (the framework itself ships
#: with no bundled plugins, so the production plugins/ dir would attach
#: nothing and the fan-out measurement would lose its plugin leg)
FIXTURE_PLUGINS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "plugins"


@dataclass
class BenchScenario:
    name: str
    description: str
    default_cycles: int
    run: Callable[..., Awaitable[dict[str, Any]]]


# ------------------------------------------------------------------ shared
async def _cycle_report(
    scenario,
    session: SamplerSession,
    perf: PerfCollector,
    cycles: int,
    custom: dict[str, Any],
    teardown_ok: bool,
) -> dict[str, Any]:
    summary = session.summary()
    process_keys = (
        "rss_base_kb",
        "rss_peak_kb",
        "rss_end_kb",
        "cpu_user_s",
        "cpu_sys_s",
        "cpu_pct",
        "fd_peak",
    )
    out: dict[str, Any] = {
        "process": {k: summary[k] for k in process_keys if k in summary},
        "loop_lag_ms": summary.get("loop_lag_ms", {}),
        "tasks": summary.get("tasks", {}),
        "threads": summary.get("threads", {}),
        "stages": perf.snapshot(),
        "custom": custom,
        "leak": leak_verdict(
            getattr(scenario, "_rss_series", []), getattr(scenario, "_task_series", [])
        ),
    }
    out["passed"] = bool(teardown_ok and out["leak"]["verdict"] != "suspected-leak")
    return out


def _track_cycle(scenario, session: SamplerSession) -> None:
    if not hasattr(scenario, "_rss_series"):
        scenario._rss_series, scenario._task_series = [], []
    proc = session.process.summary()
    scenario._rss_series.append(proc["rss_end_kb"])
    scenario._task_series.append(session.tasks.live_tasks())


# ------------------------------------------------------------- scenarios
async def cold_boot(
    session: SamplerSession,
    perf: PerfCollector,
    *,
    duration: float = 2.0,
    cycles: int = 3,
    **_: Any,
) -> dict[str, Any]:
    """Full real bootstrap (migrations, seeding, wiring) + shutdown."""
    for _ in range(cycles):
        with TemporaryWorkspace() as ws:
            session.start()
            stack = await BenchStack.build(ws, perf=perf)
            await asyncio.sleep(duration)  # idle steady-state sample
            await stack.shutdown(perf=perf)
            await session.stop()
            await session.quiesce()
            _track_cycle(cold_boot, session)
    custom = {"idle_sample_s": duration}
    return await _cycle_report(cold_boot, session, perf, cycles, custom, True)


async def accounts_startstop(
    session: SamplerSession,
    perf: PerfCollector,
    *,
    accounts: int = 5,
    duration: float = 2.0,
    cycles: int = 3,
    **_: Any,
) -> dict[str, Any]:
    """Per-account start & stop cost at N accounts through the real stack."""
    names = [f"bench{i}" for i in range(accounts)]
    with TemporaryWorkspace() as ws:
        stack = await BenchStack.build(ws)
        # measuring the raw lifecycle path, not the governor's pacing
        stack.framework.accounts._lifecycle = None
        for name in names:
            await stack.add_account(name)
        for _ in range(cycles):
            session.start()
            with perf.stage("accounts.start"):
                for name in names:
                    await stack.framework.accounts.start(name)
            await asyncio.sleep(duration)
            with perf.stage("accounts.stop"):
                for name in names:
                    await stack.framework.accounts.stop(name)
            await session.stop()
            await session.quiesce()
            _track_cycle(accounts_startstop, session)
        live = sum(1 for c in stack.factory.clients if c.connected)
        with perf.stage("shutdown"):
            await stack.shutdown()
        custom = {
            "accounts": accounts,
            "clients_built": len(stack.factory.clients),
            "clients_disconnected": len(stack.factory.clients) - live,
        }
        return await _cycle_report(
            accounts_startstop, session, perf, cycles, custom, teardown_ok=live == 0
        )


async def restart_churn(
    session: SamplerSession, perf: PerfCollector, *, cycles: int = 8, **_: Any
) -> dict[str, Any]:
    """Leak-per-restart: start→settle→stop churn on one account."""
    with TemporaryWorkspace() as ws:
        stack = await BenchStack.build(ws)
        await stack.add_account("churn")
        # lifecycle governor OFF for churn measurement (we WANT the churn)
        stack.framework.accounts._lifecycle = None
        session.start()
        for _i in range(cycles):
            with perf.stage("restart"):
                await stack.framework.accounts.start("churn")
                await asyncio.sleep(0.05)
                await stack.framework.accounts.stop("churn")
            await session.quiesce()
            _track_cycle(restart_churn, session)
        await session.stop()
        live = sum(1 for c in stack.factory.clients if c.connected)
        with perf.stage("shutdown"):
            await stack.shutdown()
        custom = {"restarts": cycles, "clients_built": len(stack.factory.clients)}
        return await _cycle_report(
            restart_churn, session, perf, cycles, custom, teardown_ok=live == 0
        )


async def log_burst(
    session: SamplerSession,
    perf: PerfCollector,
    *,
    records: int = 10_000,
    severity: str = "INFO",
    **_: Any,
) -> dict[str, Any]:
    """LogService throughput + persistence drain health."""
    with TemporaryWorkspace() as ws:
        database = Database(ws.db_path)
        await database.connect()
        logs = LogService()
        logs.attach_db(database.logs)
        await logs.start_persistence(database.logs)
        session.start()
        t0 = time.perf_counter()
        with perf.stage("log.records"):
            for i in range(records):
                logs.record(
                    severity,
                    "bench",
                    f"benchmark record {i} " + "x" * 40,
                    account="bench" if i % 2 else None,
                )
        # drain-wait poll (bench_db technique): the persist queue must
        # actually empty — the tight record loop has no awaits, so the
        # loop-lag monitor also gets its first scheduling window here.
        with perf.stage("log.drain"):
            deadline = time.time() + 60.0
            while time.time() < deadline:
                if _queue_depth(logs) == 0:
                    await asyncio.sleep(0.05)  # let the worker finish
                    if _queue_depth(logs) == 0:
                        break
                await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - t0
        await session.stop()
        stats = logs.stats()
        await logs.stop_persistence()
        await database.close()
        await session.quiesce()
        _track_cycle(log_burst, session)
        custom = {
            "records": records,
            "records_per_s": round(records / max(elapsed, 1e-9), 1),
            "db_failures": stats.get("db_failures", 0),
            "dropped": stats.get("dropped", 0),
        }
        return await _cycle_report(
            log_burst, session, perf, 1, custom, teardown_ok=stats.get("db_failures", 0) == 0
        )


def _queue_depth(logs: LogService) -> int:
    q = getattr(logs, "_persist", None)
    inner = getattr(q, "_q", None)
    if inner is None:
        return 0
    return int(inner.qsize())


async def db_writes(
    session: SamplerSession, perf: PerfCollector, *, ops: int = 2000, **_: Any
) -> dict[str, Any]:
    """Per-repo write cost + loop stall under sustained persistence."""
    with TemporaryWorkspace() as ws:
        database = Database(ws.db_path)
        await database.connect()
        session.start()
        with perf.stage("db.kv_set"):
            for i in range(ops):
                await database.kv.set(f"bench:{i}", {"n": i, "pad": "p" * 20})
        with perf.stage("db.cooldown_set"):
            now = 1_000_000.0
            for i in range(ops):
                await database.cooldowns.upsert(
                    account="bench",
                    plugin="plug",
                    cooldown_key=f"k{i}",
                    started_at=now + i,
                    expires_at=now + i + 3600.0,
                    source="bench",
                )
        with perf.stage("db.log_add_many"):
            for batch in range(ops // 50):
                rows = [
                    {
                        "ts": now + batch,
                        "severity": "INFO",
                        "source": "bench",
                        "message": "x" * 40,
                        "account": "bench",
                    }
                ] * 50
                await database.logs.add_many(rows)
        await session.stop()
        await database.close()
        await session.quiesce()
        _track_cycle(db_writes, session)
        custom = {"ops_per_repo": ops}
        return await _cycle_report(db_writes, session, perf, 1, custom, True)


async def cooldown_heavy(
    session: SamplerSession, perf: PerfCollector, *, keys: int = 2000, **_: Any
) -> dict[str, Any]:
    """CooldownService hot path + persistence restore."""
    with TemporaryWorkspace() as ws:
        database = Database(ws.db_path)
        await database.connect()
        cooldowns = CooldownService(database.cooldowns)
        session.start()
        with perf.stage("cooldown.set"):
            for i in range(keys):
                await cooldowns.set("bench", "plug", f"k{i}", 60.0 + (i % 600))
        with perf.stage("cooldown.reads"):
            for i in range(keys * 2):
                await cooldowns.remaining("bench", "plug", f"k{i % keys}")
            await cooldowns.active_for("bench")
        with perf.stage("cooldown.restore"):
            fresh = CooldownService(database.cooldowns)
            restored = await fresh.restore()
        await session.stop()
        await database.close()
        await session.quiesce()
        _track_cycle(cooldown_heavy, session)
        custom = {
            "keys": keys,
            "restored_rows": int(restored.get("restored", 0)) if isinstance(restored, dict) else 0,
        }
        return await _cycle_report(cooldown_heavy, session, perf, 1, custom, True)


async def message_burst(
    session: SamplerSession,
    perf: PerfCollector,
    *,
    messages: int = 5000,
    burst: int = 200,
    waiters: int = 4,
    **_: Any,
) -> dict[str, Any]:
    """Inbound event volume: bus fan-out × plugin dispatch × waiters."""
    bus = EventBus()
    plugins = PluginManager(
        bus,
        __import__("logging").getLogger("bench"),
        SettingsManager(bus, __import__("logging").getLogger("bench")),
        plugin_dir=str(FIXTURE_PLUGINS),
    )
    plugins.discover()
    await plugins.attach_account("bench", allowed=None)
    await plugins.activate_account(
        "bench",
        lambda plugin: NS(
            account="bench",
            plugin=plugin,
            credentials=NS(bot_id=999, owner_user_ids=frozenset({OWNER})),
            settings={},
            logger=__import__("logging").getLogger("bench"),
            send_message=_noop_send,
            send_dm=_noop_send,
            publish=lambda e: None,
            state_get=_noop_state,
            state_set=_noop_state,
            cooldown_remaining=(lambda *a, **k: _noop_async(0.0)),
            validate_channel=lambda c: None,
            wait_for_message=_noop_wait,
            collect_messages=_noop_collect,
            describe_channel=lambda c: "#c",
        ),
    )
    # the C1 cost model: unfiltered waiter subscriptions × event flood
    waiter_subs = []
    for _ in range(waiters):
        waiter_subs.append(bus.subscribe(MessageReceived, lambda event: None))

    session.start()
    t0 = time.perf_counter()
    delivered = 0
    with perf.stage("messages.publish"):
        for i in range(messages):
            bus.publish_nowait(
                MessageReceived(
                    account="bench",
                    message_id=i,
                    channel_id=CHANNEL,
                    author_id=OWNER + 1,
                    content=f"chatter {i}",
                )
            )
            delivered += 1
            if delivered % burst == 0:
                await asyncio.sleep(0)  # let dispatch breathe
    with perf.stage("messages.drain"):
        await bus.drain(timeout=30.0)
    elapsed = time.perf_counter() - t0
    await session.stop()
    await session.quiesce()
    _track_cycle(message_burst, session)
    custom = {
        "messages": messages,
        "messages_per_s": round(messages / max(elapsed, 1e-9), 1),
        "waiters": waiters,
    }
    return await _cycle_report(message_burst, session, perf, 1, custom, True)


async def _noop_send(*a: Any, **k: Any) -> None:
    return None


async def _noop_state(*a: Any, **k: Any) -> None:
    return None


async def _noop_wait(*a: Any, **k: Any) -> None:
    await asyncio.sleep(3600)


async def _noop_collect(*a: Any, **k: Any) -> list[Any]:
    return []


def _noop_async(value: Any) -> Any:
    async def inner() -> Any:
        return value

    return inner()


BENCH_SCENARIOS: dict[str, BenchScenario] = {
    "cold_boot": BenchScenario("cold_boot", "real bootstrap.build + shutdown", 3, cold_boot),
    "accounts_startstop": BenchScenario(
        "accounts_startstop", "N-account start/stop through the real stack", 3, accounts_startstop
    ),
    "restart_churn": BenchScenario(
        "restart_churn", "repeated start/stop churn (leak series)", 8, restart_churn
    ),
    "log_burst": BenchScenario(
        "log_burst", "LogService record flood + persistence drain", 1, log_burst
    ),
    "db_writes": BenchScenario("db_writes", "kv/cooldown/log sustained writes", 1, db_writes),
    "cooldown_heavy": BenchScenario(
        "cooldown_heavy", "cooldown set/read/restore", 1, cooldown_heavy
    ),
    "message_burst": BenchScenario(
        "message_burst", "bus fan-out + plugin dispatch flood", 1, message_burst
    ),
}
