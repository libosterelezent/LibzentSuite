"""SendEngine: the FarmLite serialized-lane concept for single accounts.

Core guarantee under test: an account's outbound requests are NEVER in
flight simultaneously — one engine task executes one coroutine at a time,
spaced by `prefixrate` (0 pauses the lane, FarmLite semantics).
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cli.commands import UsageError, run_command
from core.api import ControlAPI
from core.audit import AuditRecorder
from core.database import Database
from core.rate_limiter import Priority
from core.roles import RoleRegistry
from core.send_engine import SendEngine, SendEngineStopped, SendPolicy

from .helpers import TEST_CHANNEL, Harness


class TestSendEngineCore(unittest.IsolatedAsyncioTestCase):
    async def test_no_concurrent_execution(self) -> None:
        engine = SendEngine("alpha", prefixrate=0.0).start()
        engine.set_prefixrate(0.01)  # spacing tiny; concurrency is the target
        inflight = 0
        peak = 0

        async def job(index):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.03)
            inflight -= 1
            return index

        results = await asyncio.gather(*[
            engine.run(None, lambda i=i: job(i)) for i in range(6)
        ])
        self.assertEqual(peak, 1, "requests must never overlap")
        self.assertEqual(results, list(range(6)))
        await engine.stop()

    async def test_prefixrate_spacing_between_tasks(self) -> None:
        engine = SendEngine("alpha", prefixrate=0.08).start()
        stamps: list[float] = []

        async def job():
            stamps.append(asyncio.get_running_loop().time())

        await asyncio.gather(*[engine.run(None, job) for _ in range(3)])
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.07 for g in gaps), gaps)
        await engine.stop()

    async def test_priority_ordering(self) -> None:
        # pause the lane so both tasks queue BEFORE selection, then unpause:
        # the heap must pick HIGH first (deterministic, no pop-race)
        engine = SendEngine("alpha", prefixrate=0.0).start()
        order: list[str] = []

        async def job(tag):
            order.append(tag)

        low = asyncio.create_task(engine.run("low", lambda: job("low"), priority=Priority.LOW))
        await asyncio.sleep(0.02)
        high = asyncio.create_task(engine.run("high", lambda: job("high"), priority=Priority.HIGH))
        await asyncio.sleep(0.02)
        engine.set_prefixrate(0.01)  # unpause
        await asyncio.gather(low, high)
        self.assertEqual(order, ["high", "low"])
        await engine.stop()

    async def test_named_coalescing_latest_wins(self) -> None:
        engine = SendEngine("alpha", prefixrate=0.05).start()
        ran: list[str] = []

        async def job(tag):
            ran.append(tag)

        first = asyncio.create_task(engine.run("refresh", lambda: job("first")))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(engine.run("refresh", lambda: job("second")))
        with self.assertRaises(SendEngineStopped):
            await first  # invalidated by the newer same-name submission
        await second
        self.assertEqual(ran, ["second"])
        self.assertEqual(engine.skipped, 1)
        await engine.stop()

    async def test_pause_and_resume_with_prefixrate_zero(self) -> None:
        engine = SendEngine("alpha", prefixrate=0.05).start()
        engine.set_prefixrate(0)  # FarmLite: 0 pauses the lane
        done: list[str] = []

        async def job():
            done.append("x")

        task = asyncio.create_task(engine.run(None, job))
        await asyncio.sleep(0.15)
        self.assertEqual(done, [])  # paused — task waiting
        self.assertTrue(engine.stats()["paused"])
        engine.set_prefixrate(0.01)
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(done, ["x"])
        await engine.stop()

    async def test_task_exception_propagates_and_engine_survives(self) -> None:
        engine = SendEngine("alpha", prefixrate=0.01).start()

        async def boom():
            raise RuntimeError("send failed")

        with self.assertRaises(RuntimeError):
            await engine.run(None, boom)
        # engine still serves later work (FarmLite swallowed errors entirely)
        result = await engine.run(None, lambda: asyncio.sleep(0, result="ok"))
        self.assertEqual(result, "ok")
        await engine.stop()

    async def test_stop_fails_pending_submissions(self) -> None:
        engine = SendEngine("alpha", prefixrate=0.05).start()

        async def job():
            return "never"

        pending = asyncio.create_task(engine.run(None, job))
        await asyncio.sleep(0.01)
        await engine.stop()
        with self.assertRaises(SendEngineStopped):
            await pending

    async def test_stats_shape(self) -> None:
        engine = SendEngine("alpha", prefixrate=0.01).start()
        await engine.run(None, lambda: asyncio.sleep(0, result=1))
        stats = engine.stats()
        self.assertEqual(stats["account"], "alpha")
        self.assertTrue(stats["running"])
        self.assertEqual(stats["executed"], 1)
        self.assertGreaterEqual(stats["last_wait_ms"], 0)
        await engine.stop()

    def test_policy_parsing(self) -> None:
        policy = SendPolicy.from_dict({"enabled": True, "prefixrate": 2.0})
        self.assertEqual((policy.enabled, policy.prefixrate), (True, 2.0))
        self.assertEqual(SendPolicy().enabled, False)
        with self.assertRaises(ValueError):
            SendPolicy.from_dict({"bogus": 1})


class TestSessionIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_session_sends_never_overlap(self) -> None:
        harness = Harness(
            "tests/fixtures/plugins", ["alpha"],
            rate_rules={"message": "200/1s", "dm": "200/1s"},
            send_policy=SendPolicy(enabled=True, prefixrate=0.04),
        )
        await harness.container.start_account("alpha")
        session = harness.session("alpha")
        overlaps = []
        inflight = {"n": 0}
        original_send = harness.world.deliver

        def watch_deliver(channel_id, author_id, content, dm=None):
            inflight["n"] += 1
            if inflight["n"] > 1:
                overlaps.append(content)
            inflight["n"] -= 1
            return original_send(channel_id, author_id, content, dm)

        harness.world.deliver = watch_deliver
        await asyncio.gather(*[
            session.send_message(TEST_CHANNEL, f"msg-{i}") for i in range(5)
        ])
        self.assertEqual(overlaps, [])
        engine_stats = (await ControlAPI(
            harness.container, harness.limiter, harness.loader, harness.credentials
        ).status())["accounts"][0]["engine"]
        self.assertTrue(engine_stats["enabled"])
        self.assertEqual(engine_stats["executed"], 5)
        await harness.container.stop_account("alpha")

    async def test_engine_wait_recorded_in_audit(self) -> None:
        from core.audit import AuditRecorder as Recorder

        recorder = Recorder()
        harness = Harness(
            "tests/fixtures/plugins", ["alpha"], audit=recorder,
            rate_rules={"message": "200/1s", "dm": "200/1s"},
            send_policy=SendPolicy(enabled=True, prefixrate=0.05),
        )
        await harness.container.start_account("alpha")
        await harness.session("alpha").send_message(TEST_CHANNEL, "serialized")
        note = recorder.tail(1)[0].note or ""
        self.assertIn("engine_wait=", note)
        await harness.container.stop_account("alpha")

    async def test_disabled_policy_keeps_direct_path(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"])  # default: disabled
        await harness.container.start_account("alpha")
        status_engine = (await ControlAPI(
            harness.container, harness.limiter, harness.loader, harness.credentials
        ).status())["accounts"][0]["engine"]
        self.assertFalse(status_engine["enabled"])
        sent = await harness.session("alpha").send_message(TEST_CHANNEL, "direct")
        self.assertEqual(sent.content, "direct")
        await harness.container.stop_account("alpha")


class TestEngineParity(unittest.IsolatedAsyncioTestCase):
    async def test_cli_engine_and_prefixrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "s.db")
            harness = Harness("tests/fixtures/plugins", ["alpha"], state=database)
            api = ControlAPI(
                harness.container, harness.limiter, harness.loader,
                harness.credentials, state=database,
            )
            await api.start("alpha")
            text = await run_command(api, "engine on alpha")
            self.assertIn("prefixrate", text)
            text = await run_command(api, "set-prefixrate 0.5 alpha")
            self.assertIn("0.5", text)
            # persisted
            stored = json.loads(database.kv_get("engine:alpha"))
            self.assertEqual(stored, {"enabled": True, "prefixrate": 0.5})
            with self.assertRaises(UsageError):
                await run_command(api, "set-prefixrate banana")
            await api.stop("alpha")

    async def test_api_set_engine_toggles_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "s.db")
            harness = Harness("tests/fixtures/plugins", ["alpha"], state=database)
            api = ControlAPI(
                harness.container, harness.limiter, harness.loader,
                harness.credentials, state=database,
            )
            await api.start("alpha")
            stats = await api.set_engine("alpha", True, prefixrate=0.25)
            self.assertTrue(stats["running"])
            self.assertEqual(stats["prefixrate"], 0.25)
            stats = await api.set_engine("alpha", False)
            self.assertFalse(stats["enabled"])
            stored = json.loads(database.kv_get("engine:alpha"))
            self.assertFalse(stored["enabled"])
            await api.stop("alpha")


class TestEngineDashboardRoute(unittest.IsolatedAsyncioTestCase):
    async def test_engine_route_roundtrip(self) -> None:
        from dashboard.server import DashboardServer

        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "s.db")
            harness = Harness("tests/fixtures/plugins", ["alpha"], state=database)
            api = ControlAPI(
                harness.container, harness.limiter, harness.loader,
                harness.credentials, state=database,
            )
            server = DashboardServer(api, token=None, port=0)
            server._loop = asyncio.get_running_loop()
            await api.start("alpha")
            status, payload = await server._route(
                "POST", "/api/engine", {"account": "alpha", "enabled": True, "prefixrate": 0.75}, {}
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload["data"]["running"])
            self.assertEqual(payload["data"]["prefixrate"], 0.75)
            bad = await server._route(
                "POST", "/api/engine", {"account": "alpha", "enabled": "yes"}, {}
            )
            self.assertEqual(bad[0], 400)
            self.assertIn("boolean", bad[1]["error"])
            await api.stop("alpha")


if __name__ == "__main__":
    unittest.main()
