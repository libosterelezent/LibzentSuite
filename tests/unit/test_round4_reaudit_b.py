"""Round-4 adversarial re-audit B: integration regressions across seams.

Covers the round-4 hunks verified by the /tmp/opencode probes:

* start_all: estop of a semaphore-parked account mid-sweep never leaves
  that account running (post-semaphore revalidation) and a cancelled
  sweep owner releases joiners with an aborted report (flight finally).
* LifecycleGovernor: stats()/retry_after() observe without consuming
  (peek), precheck stays the consuming gate.
* TaskScheduler x real RateLimitManager: a cooperative cancel aborts an
  acquire parked behind a clamped 86400s 429 within one loop hop and
  frees the account's lane for the next task.
* EventBus: a killed dispatcher is resurrected (drain + publish paths)
  and control-plane events are admitted above the flood cap (two-tier).
* main.py CLI matrix: --bench refuses live-stack flag combinations.
* roll plugin: rollstop+roll in ONE dispatch slice keeps the NEW loop
  alive (ownership-checked finally) and a legacy ``$custom`` command
  resolves to the default with a warning instead of disabling.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
import unittest
from pathlib import Path

from core.account_manager.lifecycle import (
    LifecycleGovernor,
    LifecyclePolicy,
)
from core.account_manager.manager import AccountStatus
from core.event_bus import (
    AccountDisconnected,
    Event,
    EventBus,
    MessageReceived,
)
from core.rate_limit_manager import RateLimitManager
from core.task_scheduler import TaskScheduler

from tests.unit.helpers import FakeClient, make_manager
from tests.unit.test_task_scheduler import (
    FakeAudit,
    FakeHistoryRepo,
    FakeLogger,
    FakeSender,
)

REPO = Path(__file__).resolve().parents[2]
_LOG = logging.getLogger("tests.round4_reaudit_b")

NAMES = [f"ra{i}" for i in range(1, 9)]


class _SlowFactory:
    """FakeClient factory whose connects sleep, so start_all lanes fill."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.inflight = 0
        self.connects: dict = {}

    def __call__(self, name: str, token: str) -> FakeClient:
        outer = self

        class _Slow(FakeClient):
            async def connect(self) -> None:
                outer.inflight += 1
                outer.connects[name] = outer.connects.get(name, 0) + 1
                try:
                    await asyncio.sleep(outer.delay)
                    await super().connect()
                finally:
                    outer.inflight -= 1

        client = _Slow()
        client.token = token  # type: ignore[attr-defined]
        return client


def _fleet_manager(delay: float = 2.0):
    tokens = {f"DISCORD_TOKEN_{n.upper()}": f"tok-{n}" for n in NAMES}
    h = make_manager(token_values=tokens)
    factory = _SlowFactory(delay)
    h.manager._client_factory = factory
    h.manager._lifecycle = LifecycleGovernor(
        policy=LifecyclePolicy(start_burst=50, stop_burst=64))
    return h, factory


class StartAllEstopSweepTests(unittest.IsolatedAsyncioTestCase):
    """Round-4 hunk: re-validate AFTER the semaphore wait (manager.py)."""

    async def test_estop_of_parked_account_wins_over_sweep(self) -> None:
        h, factory = _fleet_manager(delay=2.0)
        for n in NAMES:
            await h.manager.add_account(n)
        h.collector.close()

        sweep = asyncio.create_task(h.manager.start_all(stagger=0.05))
        deadline = time.monotonic() + 8.0
        parked: list = []
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            if factory.inflight >= 4:
                parked = [n for n in NAMES if not factory.connects.get(n)]
                if len(parked) >= 3:
                    break
        self.assertGreaterEqual(len(parked), 3, "no parked accounts pre-estop")
        target = parked[-1]

        out = h.manager.emergency_stop(target)
        report = await asyncio.wait_for(sweep, 30.0)

        self.assertTrue(out["stop_initiated"])
        self.assertIn(target, report["overtaken"], report)
        self.assertNotIn(target, report["started"], report)
        rt = h.manager.get(target).runtime
        self.assertIsNot(rt.status, AccountStatus.RUNNING)
        self.assertIsNone(rt.client)
        others = [n for n in NAMES if n != target]
        self.assertEqual(
            sorted(report["started"]), sorted(others), report)
        for n in others:
            self.assertIs(h.manager.get(n).runtime.status,
                          AccountStatus.RUNNING, n)
        await h.manager.stop_all()

    async def test_cancelled_sweep_owner_releases_joiner_bounded(self) -> None:
        h, factory = _fleet_manager(delay=3.0)
        for n in NAMES:
            await h.manager.add_account(n)
        h.collector.close()

        owner = asyncio.create_task(h.manager.start_all(stagger=0.05))
        deadline = time.monotonic() + 8.0
        while factory.inflight < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        joiner = asyncio.create_task(h.manager.start_all(stagger=0.05))
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        owner.cancel()
        try:
            report = await asyncio.wait_for(joiner, 5.0)
        except asyncio.TimeoutError:
            self.fail("joiner hung after the sweep owner was cancelled")
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertTrue(report.get("joined"), report)
        self.assertTrue(report.get("aborted"), report)
        await h.manager.stop_all()


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class GovernorPurityTests(unittest.TestCase):
    """Round-4: stats()/retry_after() observe via peek(), never consume."""

    def test_stats_never_drains_start_bucket(self) -> None:
        clock = _Clock()
        gov = LifecycleGovernor(policy=LifecyclePolicy(), clock=clock)
        first = gov.precheck("alpha", "start")
        self.assertTrue(first.allowed, first)
        gov.commit("alpha", "start", ok=True)
        before = gov._start_bucket._tokens
        for _ in range(5):
            gov.stats()
        self.assertAlmostEqual(gov._start_bucket._tokens, before)
        # the consuming path still grants the next start
        again = gov.precheck("alpha", "start")
        self.assertTrue(again.allowed, again)

    def test_precheck_still_consumes_and_cooldowns(self) -> None:
        clock = _Clock()
        gov = LifecycleGovernor(policy=LifecyclePolicy(), clock=clock)
        refused = sum(
            1 for _ in range(20)
            if not gov.precheck("beta", "start").allowed
        )
        self.assertGreater(refused, 0, "precheck stopped consuming tokens")
        clock.t += 300.0  # full refill
        self.assertTrue(gov.precheck("gamma", "start", status="failed").allowed)
        gov.commit("gamma", "start", ok=False)
        inside = gov.precheck("gamma", "start", status="failed")
        self.assertFalse(inside.allowed, inside)
        self.assertEqual(inside.reason, "account_cooldown")
        # stop-based cooldowns stay observable through the read-only path
        clock.t += 300.0
        self.assertTrue(gov.precheck("delta", "start").allowed)
        gov.commit("delta", "start", ok=True)
        gov.commit("delta", "stop", ok=True)
        self.assertFalse(gov.precheck("delta", "start").allowed)
        cooldowns = gov.stats()["cooldowns"]
        self.assertEqual(cooldowns["delta"]["reason"], "account_cooldown")


class SchedulerParkedAcquireTests(unittest.IsolatedAsyncioTestCase):
    """Round-4 F6-1: cancel aborts an acquire parked behind a 429."""

    async def test_cancel_aborts_day_long_park_and_frees_lane(self) -> None:
        bus = EventBus()
        engine = RateLimitManager(bus=None, logger=FakeLogger())
        sender = FakeSender()
        scheduler = TaskScheduler(
            bus, FakeLogger(), engine, sender, FakeAudit(),
            default_timeout=5.0, retry_backoff=0.5,
            history_repo=FakeHistoryRepo())
        scheduler.start()
        try:
            engine.report_server_limit("alpha", "message", 90000.0)
            t1 = scheduler.submit("alpha", "p", "message", channel_id=1)
            for _ in range(400):
                if ("alpha", "message") in engine._pending:
                    break
                await asyncio.sleep(0.005)
            self.assertIn(("alpha", "message"), engine._pending)

            t0 = time.monotonic()
            self.assertTrue(scheduler.cancel(t1.id))
            for _ in range(300):
                if t1.status == "cancelled":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(t1.status, "cancelled")
            self.assertLess(time.monotonic() - t0, 1.5)
            for _ in range(300):
                if ("alpha", "message") not in engine._pending:
                    break
                await asyncio.sleep(0.005)
            self.assertNotIn(("alpha", "message"), engine._pending)
            self.assertEqual(sender.calls, [])

            # lane freed: next task on the account proceeds after the lift
            engine._account_cooldowns.clear()
            engine._wake_waiters()
            t2 = scheduler.submit("alpha", "p", "message", channel_id=2)
            for _ in range(600):
                if t2.status == "completed":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(t2.status, "completed")
            self.assertEqual([t.id for t in sender.calls], [t2.id])
        finally:
            await scheduler.stop(drain=False, timeout=1.0)


class _SmallBus(EventBus):
    MAX_QUEUE = 50
    CONTROL_RESERVE = 8


class BusResurrectionAndTwoTierTests(unittest.IsolatedAsyncioTestCase):
    """Round-4 F4: dispatcher resurrection + two-tier overflow."""

    async def test_dead_dispatcher_is_resurrected_and_delivers(self) -> None:
        for use_publish in (True, False):
            with self.subTest(publish_first=use_publish):
                bus = EventBus()
                got: list = []
                bus.subscribe(Event, got.append)
                bus.publish_nowait(MessageReceived(
                    account="a", message_id=1, channel_id=1,
                    author_id=1, content="backlog"))
                dispatcher = bus._dispatcher
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)
                self.assertTrue(bus._queue, "expected a backlog")
                if use_publish:
                    bus.publish_nowait(
                        AccountDisconnected(account="a", reason="stopped"))
                await asyncio.wait_for(bus.drain(timeout=3.0), 5.0)
                types = [type(e).__name__ for e in got]
                self.assertIn("MessageReceived", types)
                if use_publish:
                    self.assertIn("AccountDisconnected", types)

    async def test_control_plane_admitted_above_flood_cap(self) -> None:
        from core.event_bus import _HandlerList

        bus = _SmallBus()
        delivered: list = []

        def slow_sync(event) -> None:
            time.sleep(0.02)  # hold the dispatcher so the queue fills

        bus.subscribe(MessageReceived, slow_sync)
        bus.subscribe(AccountDisconnected, delivered.append)
        for i in range(bus.MAX_QUEUE * 3):
            bus.publish_nowait(MessageReceived(
                account="a", message_id=i, channel_id=1,
                author_id=1, content="flood"))
            if i % 25 == 0:
                await asyncio.sleep(0)
        deadline = time.monotonic() + 5.0
        while (len(bus._queue) < bus.MAX_QUEUE
               and time.monotonic() < deadline):
            await asyncio.sleep(0.01)
        self.assertGreaterEqual(bus._dropped, 1, "flood never dropped")
        bus.publish_nowait(AccountDisconnected(account="a", reason="stopped"))
        self.assertTrue(any(isinstance(e, AccountDisconnected)
                            for e in bus._queue),
                        "control event dropped at the flood cap")
        self.assertLessEqual(
            len(bus._queue), bus.MAX_QUEUE + bus.CONTROL_RESERVE)
        # let the drain run without the slow handler
        bus._subs[MessageReceived] = _HandlerList()
        bus._type_cache.clear()
        await asyncio.wait_for(bus.drain(timeout=5.0), 8.0)
        self.assertTrue(delivered, "AccountDisconnected never delivered")


class CliMatrixTests(unittest.TestCase):
    """Round-4 F7-3: --bench refuses live-stack combinations (exit 2)."""

    def _main(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "main.py"), *argv],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))

    def test_bench_conflicts_exit_2(self) -> None:
        for combo in (("--bench", "--sim", "soulcopy_basic"),
                      ("--bench", "--start-all")):
            with self.subTest(combo=combo):
                out = self._main(*combo)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertIn("--bench cannot be combined", out.stderr)

    def test_sim_list_still_works(self) -> None:
        out = self._main("--sim-list")
        self.assertEqual(out.returncode, 0)
        self.assertIn("soulcopy_basic", out.stdout)


class _RollServices:
    """Minimal per-plugin services facade driving the REAL roll plugin."""

    def __init__(self) -> None:
        self.sent: list = []
        self.account = "alpha"
        self.plugin = "roll"
        self._state: dict = {}

    @property
    def credentials(self):
        from core.credentials import Credentials
        return Credentials.from_values({
            "MUDAE_BOT_ID": "999999", "OWNER_USER_IDS": "1001"})

    @property
    def settings(self):
        return {}

    async def send_message(self, channel_id, content, *, priority=None):
        self.sent.append((channel_id, content))
        return None

    async def send_dm(self, user_id, content, *, priority=None):
        return None

    async def open_dm(self, user_id):
        return 1

    async def wait_for_message(self, check, timeout):
        return None

    async def collect_messages(self, check, idle_timeout):
        return []

    def publish(self, event):
        return None

    def validate_channel(self, channel_id):
        return True

    def describe_channel(self, channel_id):
        return "#grind"

    async def cooldown_set(self, key, duration, **kwargs):
        return None

    async def cooldown_remaining(self, key):
        return 0

    async def cooldown_active(self, key):
        return False

    async def cooldown_clear(self, key):
        return None

    async def cooldown_list(self):
        return []

    async def state_get(self, key, default=None):
        return self._state.get(key, default)

    async def state_set(self, key, value):
        self._state[key] = value


class RollOwnershipAndLegacyTests(unittest.IsolatedAsyncioTestCase):
    """Round-4: ownership-checked finally + legacy command grandfather."""

    OWNER_ID = 1001
    CHANNEL = 500

    def _manager(self, overrides=None):
        from core.plugin_manager import PluginManager
        from core.settings_manager import SettingsManager
        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"),
                            global_layer={"roll": overrides or {}}),
            plugin_dir=str(REPO / "plugins"))
        manager.discover()
        return bus, manager

    def _msg(self, mid, content):
        return MessageReceived(
            account="alpha", message_id=mid, channel_id=self.CHANNEL,
            author_id=self.OWNER_ID, content=content)

    async def test_rollstop_roll_one_slice_keeps_new_loop(self) -> None:
        bus, manager = self._manager({"roll_interval": 1.5})
        await manager.attach_account("alpha", allowed={"roll"})
        services = _RollServices()
        await manager.activate_account("alpha", services)
        plugin = manager._accounts["alpha"]["roll"].instance

        bus.publish_nowait(self._msg(1, "roll"))
        await asyncio.sleep(0.3)
        first = len(services.sent)
        self.assertGreater(first, 0)

        bus.publish_nowait(self._msg(2, "rollstop"))
        bus.publish_nowait(self._msg(3, "roll"))
        await asyncio.sleep(3.5)
        self.assertTrue(plugin._rolling.get("alpha"),
                        "new roll loop killed by the old task's finally")
        new_task = plugin._tasks.get("alpha")
        self.assertIsNotNone(new_task)
        self.assertFalse(new_task.done())
        self.assertGreater(len(services.sent), first)

        bus.publish_nowait(self._msg(4, "rollstop"))
        await asyncio.sleep(0.5)
        self.assertFalse(plugin._rolling.get("alpha"))
        self.assertNotIn("alpha", plugin._tasks)
        await manager.deactivate_account("alpha")

    async def test_legacy_dollar_command_falls_back_with_warning(self) -> None:
        records: list = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("slaughtermudae.plugins.roll")
        cap = _Cap()
        logger.addHandler(cap)
        try:
            bus, manager = self._manager({"command": "$custom"})
            await manager.attach_account("alpha", allowed={"roll"})
            services = _RollServices()
            # legacy $-values must NOT disable the plugin at activation
            await manager.activate_account("alpha", services)
            state = {s["name"]: s
                     for s in manager.states("alpha")}["roll"]
            self.assertIsNone(state.get("last_error"), state)
            view = manager.live_settings("alpha", "roll")
            self.assertEqual(view.get("command"), "$wa")
            warnings = [r for r in records
                        if r.levelno == logging.WARNING
                        and "legacy roll command" in r.getMessage()]
            self.assertTrue(warnings, "no legacy migration warning logged")

            bus.publish_nowait(self._msg(1, "roll"))
            await asyncio.sleep(0.4)
            commands = {c for _, c in services.sent}
            self.assertIn("$wa", commands)
            self.assertNotIn("$custom", commands)
            bus.publish_nowait(self._msg(2, "rollstop"))
            await asyncio.sleep(0.3)
            await manager.deactivate_account("alpha")
        finally:
            logger.removeHandler(cap)
        # non-$ garbage is still rejected outright (no grandfather):
        # the plugin disables at activation with a settings: error
        bus2, manager2 = self._manager({"command": "banana"})
        await manager2.attach_account("alpha", allowed={"roll"})
        await manager2.activate_account("alpha", _RollServices())
        state2 = {s["name"]: s
                  for s in manager2.states("alpha")}["roll"]
        self.assertFalse(state2["enabled"], state2)
        self.assertTrue(state2["last_error"].startswith("settings:"), state2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
