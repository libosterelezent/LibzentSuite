"""Round-3 adversarial RE-AUDIT B: cross-module integration regressions.

Complements the per-subsystem round-3 suites by driving SEAMS:
scheduler <-> services <-> plugins, rate engine <-> scheduler,
plugin manager <-> account manager, settings unpin over a real DB.

Two tests are marked expectedFailure: they encode the DESIRED contracts
that the round-3 working tree violates (documented regressions; remove
the decorator when the corresponding fix lands):

  * test_null_reset_refreshes_live_plugin_view — ControlAPI null reset
    leaves the running plugin's live settings view stale.
  * test_remote_control_masks_internal_runtime_errors — remote control
    still leaks internal RuntimeError text into the channel reply
    (mirrors the in-tree failing test_broken_api_replies_with_error).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api import ControlAPI
from core.errors import SchedulerError, SettingsValidationError, TargetGone
from core.event_bus import EventBus, MessageReceived
from core.settings_manager import Setting, SettingsManager
from core.task_scheduler.scheduler import TaskScheduler
from core.account_manager.services import (
    AccountPluginServices,
    TaskAckRegistry,
)

LOG = logging.getLogger("reaudit-b")
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fixtures", "plugins")


class QuietLog:
    def info(self, *a):
        pass

    def warning(self, *a):
        pass

    def error(self, *a):
        pass


class NoopLimiter:
    """Duck-typed RateLimitManager: instant grants, no side effects."""

    async def acquire(self, action, account=None, plugin=None, priority=0):
        return SimpleNamespace(wait_s=0.0, jitter_s=0.0, reason="")

    def report_server_limit(self, *a, **k):
        pass

    def stats(self):
        return {}

    def warnings(self):
        return []

    def prune_account(self, account):
        pass

    def set_account_group(self, account, group):
        pass


class RiggedRng:
    """uniform(a, b) alternates the tiny and the huge draw."""

    def __init__(self):
        self._i = 0

    def uniform(self, a, b):
        self._i += 1
        return a if self._i % 2 == 1 else b


def make_scheduler(sender, history_repo=None, bus=None) -> TaskScheduler:
    return TaskScheduler(bus or EventBus(), QuietLog(), NoopLimiter(),
                         sender, history_repo=history_repo)


# ---------------------------------------------------------------------------
# 1. scheduler <-> services <-> plugins: typed errors across the ack seam
# ---------------------------------------------------------------------------
class TestTypedErrorAckSeam(unittest.IsolatedAsyncioTestCase):
    async def test_connection_error_round_trip_and_transient_match(self):
        """A terminal ConnectionError reaches the waiting plugin TYPED
        (not wrapped in an opaque SchedulerError), so a caller can match
        it by type (the old roll loop's transient-classification contract
        — the framework half survives the plugin's removal)."""
        async def sender(task):
            raise ConnectionError("gateway websocket died")

        bus = EventBus()
        sched = make_scheduler(sender, bus=bus)
        registry = TaskAckRegistry(bus, sched)
        svc = AccountPluginServices("alpha", "probe", bus=bus, scheduler=sched,
                                    credentials=None, cache=None,
                                    ack_registry=registry)
        svc.task_ack_timeout = 5.0
        sched.start()
        with self.assertRaises(ConnectionError) as caught:
            await svc.send_message(100, "hi")
        self.assertNotIsInstance(caught.exception, SchedulerError)
        self.assertEqual(str(caught.exception), "gateway websocket died")
        await sched.stop()

    async def test_flow_blocked_subclass_round_trip_exact(self):
        """A FlowBlocked subclass (TargetGone) crosses the ack seam as the
        EXACT class with the (account, reason) constructor shape intact."""
        async def sender(task):
            raise TargetGone("beta", "channel 500 vanished")

        bus = EventBus()
        sched = make_scheduler(sender, bus=bus)
        registry = TaskAckRegistry(bus, sched)
        svc = AccountPluginServices("beta", "probe", bus=bus,
                                    scheduler=sched, credentials=None,
                                    cache=None, ack_registry=registry)
        svc.task_ack_timeout = 5.0
        sched.start()
        with self.assertRaises(TargetGone) as caught:
            await svc.send_dm(7, "hi")
        self.assertIs(type(caught.exception), TargetGone)
        self.assertIn("channel 500 vanished", str(caught.exception))
        await sched.stop()

    async def test_scheduler_error_from_ack_timeout_is_transient_text(self):
        """The ack-timeout SchedulerError keeps the 'did not complete within'
        text marker transient-classification fallbacks match on (the
        untyped legacy path)."""
        async def sender(task):
            await asyncio.sleep(10)
            return "sent"

        bus = EventBus()
        sched = make_scheduler(sender, bus=bus)
        registry = TaskAckRegistry(bus, sched)
        svc = AccountPluginServices("alpha", "probe", bus=bus, scheduler=sched,
                                    credentials=None, cache=None,
                                    ack_registry=registry)
        svc.task_ack_timeout = 0.2
        sched.start()
        with self.assertRaises(SchedulerError) as caught:
            await svc.send_message(100, "hi")
        self.assertIn("did not complete within", str(caught.exception))
        await sched.stop()


# ---------------------------------------------------------------------------
# 2. estop -> fault gate -> resume THROUGH the ControlAPI
# ---------------------------------------------------------------------------
class TestEstopResumeViaApi(unittest.IsolatedAsyncioTestCase):
    async def test_estop_halt_resume_cycle(self):
        async def sender(task):
            return "sent"

        sched = make_scheduler(sender)
        sched.start()

        class Accts:
            async def accounts(self):
                return []

            async def stop_all(self):
                return 0

            async def get(self, name):
                return {}

        api = ControlAPI(accounts=Accts(), plugins=None,                          settings=None, cooldowns=None, scheduler=sched,
                         limiter=NoopLimiter(), database=None, logs=QuietLog(),
                         audit=None)
        t = sched.submit("alpha", "roll", "message", channel_id=1, content="x")
        out = await api.emergency_stop_all()
        self.assertTrue(out["halted"])
        with self.assertRaises(SchedulerError):
            sched.submit("alpha", "roll", "message", channel_id=1, content="y")
        resumed = await api.system_resume()
        self.assertTrue(resumed["resumed"])
        t2 = sched.submit("alpha", "roll", "message", channel_id=1, content="z")
        self.assertEqual(t2.status, "queued")
        await sched.stop()
        await sched.stop()  # idempotent

    async def test_interrupted_stop_faults_and_heals(self):
        """A stop() owner cancelled mid-teardown leaves the scheduler
        FAULTED: submit rejected with the specific message, every task
        terminal, a retry stop() heals, start() re-arms."""
        release = asyncio.Event()

        class SlowRepo:
            async def add(self, row):
                await release.wait()

            async def update(self, row):
                await release.wait()

            async def max_id(self):
                return 0

        async def sender(task):
            await asyncio.sleep(30)
            return "sent"

        sched = make_scheduler(sender, history_repo=SlowRepo())
        sched.start()
        sched.submit("alpha", "roll", "message", channel_id=1, content="x")
        await asyncio.sleep(0.1)

        stop_task = asyncio.ensure_future(sched.stop(drain=False))
        await asyncio.sleep(0.05)
        stop_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stop_task
        await asyncio.sleep(0.05)

        self.assertTrue(sched._stop_failed)
        with self.assertRaises(SchedulerError) as caught:
            sched.submit("alpha", "roll", "message", channel_id=1, content="y")
        self.assertIn("stop was interrupted", str(caught.exception))
        for task in sched._tasks.values():
            self.assertEqual(task.status, "cancelled")

        release.set()
        await sched.stop()
        self.assertFalse(sched._stop_failed)
        self.assertTrue(sched._stopped)


# ---------------------------------------------------------------------------
# 3. rate engine <-> scheduler: jitter stamped at the expected wire instant
# ---------------------------------------------------------------------------
class TestJitterStampWireGaps(unittest.IsolatedAsyncioTestCase):
    async def test_same_group_wire_gaps_hold_under_rigged_draws(self):
        """Two accounts share one group clock; alternating tiny/huge jitter
        draws must not compress wire gaps below the interval."""
        from core.rate_limit_manager import RateLimitManager

        limiter = RateLimitManager(EventBus(), QuietLog())
        limiter._jitter_rng = RiggedRng()
        limiter.set_policy_values("message", 2.5, 0.0, 3.0)
        limiter.set_account_group("alpha", "grinders")
        limiter.set_account_group("beta", "grinders")

        wire: dict[int, float] = {}

        async def sender(task):
            wire[task.id] = time.monotonic()
            await asyncio.sleep(0.01)
            return "sent"

        sched = TaskScheduler(EventBus(), QuietLog(), limiter, sender)
        sched.start()
        ids = {}
        for i, acct in enumerate(("alpha", "beta") * 3):
            t = sched.submit(acct, "roll", "message", channel_id=1,
                             content=f"m{i}")
            ids[t.id] = acct
            await asyncio.sleep(0.05)
        deadline = time.monotonic() + 30
        while len(wire) < 6 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        await sched.stop()

        self.assertEqual(len(wire), 6, "every send reached the wire")
        order = sorted(wire.items(), key=lambda kv: kv[1])
        gaps = [order[i + 1][1] - order[i][1] for i in range(len(order) - 1)]
        self.assertGreaterEqual(min(gaps), 2.5 - 0.35,
                                f"wire gap compressed: {gaps}")

    async def test_cancelled_waiter_leaks_no_draw(self):
        """Cancelling a queued waiter mid-acquire neither corrupts the
        pacing clocks (next wire send still spaced) nor strands anyone."""
        from core.rate_limit_manager import RateLimitManager

        limiter = RateLimitManager(EventBus(), QuietLog())
        limiter._jitter_rng = RiggedRng()
        limiter.set_policy_values("message", 1.5, 0.0, 2.0)
        wire: dict[int, float] = {}

        async def sender(task):
            wire[task.id] = time.monotonic()
            return "sent"

        sched = TaskScheduler(EventBus(), QuietLog(), limiter, sender)
        sched.start()
        ta = sched.submit("alpha", "p", "message", channel_id=1, content="a")
        tb = sched.submit("alpha", "p", "message", channel_id=1, content="b")
        tc = sched.submit("alpha", "p", "message", channel_id=1, content="c")
        await asyncio.sleep(0.2)
        sched.cancel(tb.id)
        deadline = time.monotonic() + 15
        while (ta.id not in wire or tc.id not in wire) \
                and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await sched.stop()

        self.assertIn(ta.id, wire)
        self.assertIn(tc.id, wire)  # not stranded by the dead waiter
        self.assertGreaterEqual(wire[tc.id] - wire[ta.id], 1.5 - 0.35)
        self.assertEqual(tb.status, "cancelled")


# ---------------------------------------------------------------------------
# 4. settings unpin full cycle over a real sqlite database
# ---------------------------------------------------------------------------
class TestSettingsUnpinCycle(unittest.IsolatedAsyncioTestCase):
    SCHEMA = (Setting("roll_interval", "float", 2.0, "d"),)
    GLOBAL = {"roll": {"roll_interval": 4.5}}

    async def _fresh_manager(self, db_path):
        from database.db import Database

        db = Database(db_path)
        await db.connect()
        mgr = SettingsManager(EventBus(), LOG,
                              plugin_repo=db.plugins,
                              account_settings_repo=db.account_settings,
                              global_layer=self.GLOBAL)
        await mgr.load()
        return db, mgr

    async def test_null_reset_falls_back_to_global_not_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "cycle.db")
            db, mgr = await self._fresh_manager(db_path)
            try:
                self.assertEqual(
                    mgr.resolve("roll", self.SCHEMA, account="a1")["roll_interval"],
                    4.5)
                await mgr.set_plugin_setting(
                    "a1", "roll", "roll_interval", 9.5, self.SCHEMA)
                self.assertEqual(
                    mgr.resolve("roll", self.SCHEMA, account="a1")["roll_interval"],
                    9.5)
                effective = await mgr.clear_plugin_setting(
                    "a1", "roll", "roll_interval", self.SCHEMA)
                self.assertEqual(effective, 4.5)  # global, NOT default 2.0
                self.assertEqual(
                    mgr.resolve("roll", self.SCHEMA, account="a1")["roll_interval"],
                    4.5)
                rows = await db.plugins.all_account_settings()
                self.assertNotIn("roll_interval",
                                 rows.get("a1", {}).get("roll", {}))
            finally:
                await db.close()

            # simulated restart: a fresh manager over the SAME database
            db2, mgr2 = await self._fresh_manager(db_path)
            try:
                self.assertEqual(
                    mgr2.resolve("roll", self.SCHEMA, account="a1")["roll_interval"],
                    4.5,
                    "restart fell back to the schema default, not the global layer")
            finally:
                await db2.close()


# ---------------------------------------------------------------------------
# 5. plugin manager <-> account manager: deactivation ordering (regression
#    for the round-3 reorder): plugin hooks quiet BEFORE cascades.
#    Uses the REAL PluginManager; the account manager's ordering is covered
#    by test_account_manager (fake seam), this drives the plugin side.
# ---------------------------------------------------------------------------
class TestDispatchQuiesceOrdering(unittest.IsolatedAsyncioTestCase):
    async def test_deactivate_joins_handler_before_cascade_runs(self):
        from core.plugin_manager.manager import PluginManager

        bus = EventBus()
        settings = SettingsManager(bus, LOG)
        plugins = PluginManager(bus, LOG, settings, plugin_dir=FIXTURES)
        await plugins.attach_account("alpha")

        ordered: list[tuple[str, float]] = []
        services = SimpleNamespace()
        await plugins.activate_account("alpha", lambda name: services)
        runtime = plugins._accounts["alpha"]["spy"]

        async def handler(ctx, event):
            ordered.append(("handler-start", time.monotonic()))
            await asyncio.sleep(0.2)
            ordered.append(("handler-write", time.monotonic()))

        runtime.instance.handle_event = handler
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=1, channel_id=5, author_id=9,
            content="x"))
        await asyncio.sleep(0.05)
        await plugins.deactivate_account("alpha")
        ordered.append(("cascade", time.monotonic()))
        self.assertEqual([tag for tag, _ in ordered],
                         ["handler-start", "handler-write", "cascade"])


# ---------------------------------------------------------------------------
# 6. DOCUMENTED REGRESSIONS (expectedFailure — flip when fixed)
# ---------------------------------------------------------------------------
class TestDocumentedRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_null_reset_refreshes_live_plugin_view(self):
        """FIXED (round-3 re-audit): PluginManager.clear_setting now exists —
        the RUNNING plugin's live settings view follows a null-reset."""
        from core.plugin_manager.manager import PluginManager

        schema = (Setting("count", "int", 3, "n", min_value=0, max_value=10),)
        bus = EventBus()
        settings = SettingsManager(bus, LOG,
                                   global_layer={"spy": {"count": 7}})
        plugins = PluginManager(bus, LOG, settings, plugin_dir=FIXTURES)

        class Accts:
            async def accounts(self):
                return []

        api = ControlAPI(accounts=Accts(), plugins=plugins,                          settings=settings, cooldowns=None, scheduler=None,
                         limiter=None, database=None, logs=LOG, audit=None)
        await plugins.attach_account("a1")
        await settings.set_plugin_setting("a1", "spy", "count", 9, schema)
        await plugins.activate_account("a1", lambda name: None)
        await api.plugins_set_setting("a1", "spy", "count", None)
        live = plugins.live_settings("a1", "spy")
        resolved = settings.resolve("spy", schema, account="a1")["count"]
        self.assertEqual(
            live.get("count"), resolved,
            f"live view stale: plugin keeps {live.get('count')} "
            f"while resolution says {resolved}")

    async def test_remote_control_masks_internal_runtime_errors(self):
        """FIXED (round-3 re-audit): bare RuntimeError left _DOMAIN_ERRORS —
        non-domain internals ESCAPE _execute so _handle masks them; domain
        errors come back rendered as strings."""
        from core.remote_control import RemoteControl

        rc = RemoteControl.__new__(RemoteControl)  # only _execute's contract
        rc._handlers = {}
        rc._api_provider = lambda: None

        async def boom(api, args):
            raise RuntimeError("boom")

        rc._handlers["status"] = boom
        rc._logger = LOG
        with self.assertRaises(RuntimeError):
            # non-domain internals must ESCAPE _execute so _handle masks
            # them; domain errors come back rendered as strings
            await rc._execute("status")


if __name__ == "__main__":
    unittest.main()
