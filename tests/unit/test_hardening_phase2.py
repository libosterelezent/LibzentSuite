"""Phase-2 hardening regressions (lifecycle races, cascade, dashboard, CLI).

Every test here pins a defect found in the phase-2 audit; each failed on
the pre-audit code.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from core.account_manager import DiscordCache
from core.account_manager.services import AccountPluginServices
from core.errors import AccountError, DatabaseError
from core.event_bus import EventBus, MessageReceived
from core.plugin_state import PluginStateService
from core.task_scheduler import Task, TaskScheduler
from database.db import Database
from tests.unit.helpers import FakeScheduler, default_snapshot, make_manager


class _TmpCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-p2-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


# ---------------------------------------------------------------- scheduler
class SchedulerStopSingleFlight(_TmpCase):
    async def test_concurrent_stops_join_and_resume_survives(self) -> None:
        from tests.unit.test_task_scheduler import FakeAudit, FakeLimiter, FakeLogger

        bus = EventBus()
        started = asyncio.Event()

        async def slow_sender(task: Task) -> None:
            started.set()
            await asyncio.sleep(10)  # drains time out in tests below

        sched = TaskScheduler(
            bus, FakeLogger(), FakeLimiter(), slow_sender, FakeAudit(), default_timeout=5.0
        )
        sched.start()
        sched.submit("alpha", "p", "message", channel_id=1, content="x")
        await started.wait()
        drain = asyncio.create_task(sched.stop(drain=True, timeout=10.0))
        await asyncio.sleep(0.05)
        estop = asyncio.create_task(sched.stop(drain=False))
        await asyncio.wait({drain, estop}, timeout=8.0)
        self.assertTrue(drain.done() and not drain.exception())
        self.assertTrue(estop.done() and not estop.exception())
        # re-arm, then verify the resumed scheduler accepts and runs tasks
        sched.start()
        ran = asyncio.Event()

        async def ok_sender(task: Task) -> None:
            ran.set()

        sched._sender = ok_sender
        sched.submit("alpha", "p", "message", channel_id=1, content="y")
        await asyncio.wait_for(ran.wait(), timeout=2.0)
        await sched.stop(drain=True, timeout=5.0)
        self.assertEqual(sched.stats()["completed"], 1)

    async def test_timeout_zero_means_no_budget(self) -> None:
        from tests.unit.test_task_scheduler import FakeAudit, FakeLimiter, FakeLogger

        ran = asyncio.Event()

        async def slow_but_fine(task: Task) -> None:
            await asyncio.sleep(0.15)  # would die instantly under timeout=0
            ran.set()

        sched = TaskScheduler(EventBus(), FakeLogger(), FakeLimiter(), slow_but_fine, FakeAudit())
        sched.start()
        sched.submit("alpha", "p", "poke", timeout=0)
        await asyncio.wait_for(ran.wait(), timeout=2.0)
        await sched.stop(drain=True, timeout=5.0)
        self.assertEqual(sched.stats()["completed"], 1)


# ------------------------------------------------------------ ack-timeout cancel
class TimeoutCancelsQueuedTask(_TmpCase):
    async def test_wait_for_task_cancels_on_timeout(self) -> None:
        class SlowScheduler(FakeScheduler):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled: list[int] = []

            def cancel(self, task_id: int) -> bool:
                self.cancelled.append(task_id)
                return True

        scheduler = SlowScheduler()
        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())
        services = AccountPluginServices(
            "alpha",
            "exporter",
            bus=EventBus(),
            scheduler=scheduler,
            credentials=None,
            cache=cache,
            settings_dict={},
            client_getter=lambda: None,
        )
        services.task_ack_timeout = 0.05
        with self.assertRaises(Exception) as ctx:  # SchedulerError family
            await services.send_message(100, "hi")
        self.assertIn("did not complete", str(ctx.exception))
        self.assertEqual(scheduler.cancelled, [scheduler.submitted[0].id])


# ------------------------------------------------- remove_account cascade/race
class RemoveAccountCascade(_TmpCase):
    async def test_plugin_state_kv_cascades_and_double_remove_is_clean(self) -> None:
        db = Database(self.dir / "c.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        state = PluginStateService(db.kv)
        await state.set_account("alpha", "marker", {"n": 1})
        await state.set_pair("alpha", "roll", "counter", 3)
        await state.set_plugin("roll", "shared", "keep-me")  # NOT account-scoped

        h = make_manager()
        await h.manager.add_account("alpha")
        # wire the kv-backed state service into the manager for the cascade
        h.manager._plugin_state = state
        await h.manager.remove_account("alpha")
        self.assertIsNone(await state.get_account("alpha", "marker"))
        self.assertIsNone(await state.get_pair("alpha", "roll", "counter"))
        self.assertEqual(await state.get_plugin("roll", "shared"), "keep-me")
        with self.assertRaises(AccountError):  # clean, not KeyError
            await h.manager.remove_account("alpha")

    async def test_remove_refuses_live_account_and_restores_registry(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        with self.assertRaises(AccountError):
            await h.manager.remove_account("alpha")
        self.assertIn("alpha", h.manager.names())  # registry not mutated
        await h.manager.stop("alpha")
        await h.manager.remove_account("alpha")
        self.assertNotIn("alpha", h.manager.names())


# ------------------------------------------------------- zombie client guards
class ZombieClientGuards(_TmpCase):
    async def test_stale_client_callbacks_are_ignored(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime
        first = runtime.client
        # simulate the supervisor building client #2 while #1 lingers
        second = h.factory("alpha", "tok-alpha")
        h.manager._wire_client("alpha", runtime, second)
        runtime.client = second
        from adapters.base import DiscordMessage

        def msg(i: int, text: str) -> DiscordMessage:
            return DiscordMessage(id=i, channel_id=1, author_id=2, content=text, dm=False)

        # the OLD client's callbacks must no longer publish
        with mock.patch.object(h.bus, "publish_nowait") as pub:
            first.on_message(msg(1, "x"))
            pub.assert_not_called()
        # the fresh client still works
        events: list[MessageReceived] = []
        h.bus.subscribe(MessageReceived, events.append)
        wired = second.on_message
        assert wired is not None  # set by _wire_client above
        wired(msg(2, "y"))
        for _ in range(5):  # publish_nowait defers via create_task
            await asyncio.sleep(0)
        self.assertEqual(len(events), 1)
        await h.manager.stop("alpha")

    async def test_drop_without_supervisor_is_failed_not_wedged(self) -> None:
        from core.account_manager.manager import AccountStatus
        from tests.unit.test_account_manager import _fast_reconnect_settings

        settings = _fast_reconnect_settings()
        settings.overrides["alpha"] = {"reconnect_enabled": False}
        h = make_manager(settings=settings)
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime
        self.assertIsNone(runtime.supervisor)
        runtime.client.drop("socket closed")
        await asyncio.sleep(0.15)
        self.assertEqual(runtime.status, AccountStatus.FAILED)
        # stale-start audit: lost is SET on the no-supervisor FAILED path —
        # the recovery signal (start() clears it before connecting, so the
        # restart below is never spuriously woken by it)
        self.assertTrue(runtime.lost.is_set())
        # start() can heal a FAILED account (no RECONNECTING wedge)
        await h.manager.start("alpha")
        self.assertEqual(runtime.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")


# ------------------------------------------------------------------- kv repo
class KvDeletePrefix(_TmpCase):
    async def test_delete_prefix_removes_only_matching(self) -> None:
        db = Database(self.dir / "k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        await db.kv.set("pstate:account:alpha:a", "1")
        await db.kv.set("pstate:account:alpha:b", "2")
        await db.kv.set("pstate:account:beta:a", "3")
        await db.kv.set("pstate:global:x", "4")
        removed = await db.kv.delete_prefix("pstate:account:alpha:")
        self.assertEqual(removed, 2)
        self.assertEqual(await db.kv.get("pstate:account:beta:a"), "3")
        self.assertEqual(await db.kv.get("pstate:global:x"), "4")
        with self.assertRaises(DatabaseError):
            await db.kv.delete_prefix("")


# ------------------------------------------------------------------ persist
class PersistQueueBoundedStop(_TmpCase):
    async def test_stop_counts_undrained_items(self) -> None:
        class StuckRepo:
            async def add_many(self, batch: list[dict[str, Any]]) -> None:
                await asyncio.sleep(30)  # simulates a locked DB

            async def add(self, item: dict[str, Any]) -> None:
                await asyncio.sleep(30)

        import core._persist_queue as pq

        q = pq.PersistQueue("test")
        await q.start(StuckRepo(), batch=50, interval=0.05)
        for i in range(20):
            q.put({"i": i})
        await asyncio.sleep(0.1)  # worker picks the batch up
        await q.stop(timeout=0.4)
        # StuckRepo never lands a row: everything queued was counted dropped
        # (the old assertion `>= 0` was a tautology)
        self.assertEqual(q.stats()["dropped"], 20)
        self.assertFalse(q.active)


# ------------------------------------------------------- (dashboard hardening tests removed with the dashboard interface)


class CliEmptyName(unittest.IsolatedAsyncioTestCase):
    async def test_empty_name_token_is_usage_error(self) -> None:
        from interfaces.cli.commands import UsageError, run_command

        with self.assertRaises(UsageError):
            await run_command(SimpleNamespace(), 'accounts stop ""')


if __name__ == "__main__":
    unittest.main()
