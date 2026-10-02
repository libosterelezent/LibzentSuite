"""Multi-account isolation audit — regression tests for the confirmed
findings (K1-K17) from the 10-subagent deep investigation.

Each test pins one minimal fix:
  K1  scheduler.cleanup_account lane teardown on account removal
  K2  stop-during-start disconnects the zombie client (abort branch)
  K3  in-flight reconnect cannot resurrect a stopped account
  K4  _start_failed never regresses STOPPED / emits spurious AccountFailed
  K5  start() cancels a live supervisor before spawning a new one
  K6  remove_account deactivates plugins of a never-stopped FAILED account
  K7  cancelled/failed connect() closes the LIBRARY client (session leak)
  K8  shared IdentifyGate spaces concurrent IDENTIFYs (factory-owned)
  K10 lane-config kv rows that decode to dicts load correctly
  K11 roll loop treats the 30s task-ack timeout as transient
  K12 ServiceLogger stdlib surface forwards account= context
  K13 RealClientFactory builds per-account adapter loggers
  K14 settings/profile mirrors are evicted on account removal
  K15 rate-engine residue + account policies are pruned on removal
  K16 soulcopy skips a second trigger while a flow is running
"""
from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from adapters.discord.client import IdentifyGate, RealClientFactory, RealDiscordClient
from core.account_manager import AccountStatus
from core.errors import AccountError, SchedulerError
from core.event_bus import AccountConnected, AccountFailed, EventBus, MessageReceived
from core.logging_service import LogService, ServiceLogger
from core.profile_manager.manager import ProfileManager
from core.rate_limit_manager.engine import RateLimitManager
from core.task_scheduler import TaskScheduler

from tests.unit.helpers import make_manager
from tests.unit.test_account_manager import _fast_reconnect_settings
from tests.unit.test_rate_engine import StubLogger
from tests.unit.test_rate_lane import FakeKv
from tests.unit.test_roll import CHANNEL, OWNER_ID, RollServices
from tests.unit.test_soulcopy import TRIGGER_CHANNEL, make_plugin
from tests.unit.test_task_scheduler import (
    FakeAudit,
    FakeHistoryRepo,
    FakeLimiter,
    FakeLogger,
    FakeSender,
)

LOG = logging.getLogger("t-isolation")


# --------------------------------------------------------------------- K1
class SchedulerCleanupAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_tears_down_only_that_accounts_lane(self) -> None:
        scheduler = TaskScheduler(
            EventBus(), FakeLogger(), FakeLimiter(), FakeSender(), FakeAudit(),
            default_timeout=5.0, retry_backoff=0.5, history_repo=FakeHistoryRepo(),
        )
        scheduler.start()
        try:
            for account in ("alpha", "beta"):
                scheduler.submit(account, "p", "message", channel_id=1,
                                 content="x")
            await asyncio.sleep(0.1)  # both lanes spawn + drain the task
            self.assertIn("alpha", scheduler._lanes)
            self.assertIn("beta", scheduler._lanes)

            await scheduler.cleanup_account("alpha")

            self.assertNotIn("alpha", scheduler._lanes)
            self.assertNotIn("alpha", scheduler._lane_events)
            self.assertNotIn("alpha", scheduler._queues)
            self.assertIn("beta", scheduler._lanes)  # sibling untouched
        finally:
            await scheduler.stop(drain=False)

    async def test_cleanup_is_idempotent_and_safe_for_unknown(self) -> None:
        scheduler = TaskScheduler(
            EventBus(), FakeLogger(), FakeLimiter(), FakeSender(), FakeAudit(),
            default_timeout=5.0, history_repo=FakeHistoryRepo(),
        )
        scheduler.start()
        try:
            await scheduler.cleanup_account("never-existed")  # no raise
            await scheduler.cleanup_account("never-existed")  # idempotent
        finally:
            await scheduler.stop(drain=False)


# ------------------------------------------------------------ K2 / K3 / K4
class StopDuringStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_before_client_assignment_disconnects_zombie(self) -> None:
        """K2: stop() completes while start() is parked in token resolution —
        the freshly connected client must be DISCONNECTED, not left live."""
        h = make_manager()
        await h.manager.add_account("alpha")
        gate = asyncio.Event()
        original = h.manager._resolve_token

        async def parked_resolve(record):
            await gate.wait()
            return await original(record)

        h.manager._resolve_token = parked_resolve
        start_task = asyncio.create_task(h.manager.start("alpha"))
        await asyncio.sleep(0.05)          # start() is parked pre-assignment
        await h.manager.stop("alpha")      # completes: rt.client was None
        gate.set()                         # start() resumes and CONNECTS
        out = await start_task

        rt = h.manager.get("alpha").runtime
        self.assertEqual(out["status"], "stopped")
        self.assertEqual(rt.status, AccountStatus.STOPPED)
        self.assertIsNone(rt.client)                      # no zombie reference
        self.assertFalse(h.factory.clients[0].connected)  # gateway closed

    async def test_aborted_start_never_publishes_account_failed(self) -> None:
        """K4: an intentional abort must not classify as a credential failure."""
        h = make_manager()
        await h.manager.add_account("alpha")
        rt = h.manager.get("alpha").runtime
        rt.stop_requested = True
        rt.status = AccountStatus.STOPPED
        h.manager._start_failed(
            "alpha", rt, AccountError("live client connected without a user (bad token?)"))
        self.assertEqual(rt.status, AccountStatus.STOPPED)   # no regression
        self.assertEqual(h.collector.of(AccountFailed), [])  # no event

    async def test_in_flight_reconnect_cannot_resurrect_stopped_account(self) -> None:
        """K3: stop() landing while the supervisor is INSIDE connect() must
        tear the fresh client down instead of flipping back to RUNNING."""
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        await h.collector.wait_for(AccountConnected, 1)

        gate = asyncio.Event()
        original = h.manager._resolve_token

        async def parked_resolve(record):
            await gate.wait()
            return await original(record)

        h.manager._resolve_token = parked_resolve
        h.manager.get("alpha").runtime.client.drop("socket closed")
        await asyncio.sleep(0.15)          # supervisor enters connect(), parks
        await h.manager.stop("alpha")      # completes while parked
        gate.set()                         # reconnect resumes and CONNECTS
        await asyncio.sleep(0.2)

        rt = h.manager.get("alpha").runtime
        self.assertEqual(rt.status, AccountStatus.STOPPED)     # not RUNNING
        self.assertIsNone(rt.client)                           # torn down
        self.assertFalse(h.factory.clients[-1].connected)      # disconnected
        self.assertEqual(                                       # one connect only
            len(h.collector.of(AccountConnected)), 1)
        await asyncio.sleep(0.1)  # no late resurrection either
        self.assertEqual(rt.status, AccountStatus.STOPPED)


# --------------------------------------------------------------------- K5
class DualSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_cancels_live_supervisor_before_spawning(self) -> None:
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt = h.manager.get("alpha").runtime
        old = rt.supervisor
        self.assertIsNotNone(old)
        self.assertFalse(old.done())

        rt.status = AccountStatus.FAILED  # stand-down leaves it alive+polling
        await h.manager.start("alpha")     # restart from FAILED is legal

        self.assertIsNot(rt.supervisor, old)
        await asyncio.sleep(0.05)
        self.assertTrue(old.done())        # K5: no orphaned dual supervisor
        self.assertFalse(rt.supervisor.done())
        await h.manager.stop("alpha")


# --------------------------------------------------------------------- K6
class RemoveFailedAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_of_failed_account_deactivates_plugins(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")           # plugins attach + activate
        self.assertIn("alpha", [a for a, _ in h.plugins.activated])
        rt = h.manager.get("alpha").runtime
        rt.status = AccountStatus.FAILED         # degraded, never stop()ed
        await h.manager.remove_account("alpha")
        self.assertIn("alpha", h.plugins.deactivated)  # K6: hooks fired
        self.assertIn("alpha", h.plugins.detached)

    async def test_remove_evicts_mirrors_and_engine_residue(self) -> None:
        """K14/K15 wiring: the removal cascade reaches every mirror."""
        h = make_manager()
        await h.manager.add_account("alpha")
        h.profile_manager._assignment_cache["alpha"] = ["grinder"]
        settings = NS(forget_account=mock.MagicMock())
        limiter = NS(prune_account=mock.MagicMock())
        h.manager._settings = settings
        h.manager._limiter = limiter
        await h.manager.remove_account("alpha")
        settings.forget_account.assert_called_once_with("alpha")
        limiter.prune_account.assert_called_once_with("alpha")
        self.assertNotIn("alpha", h.profile_manager._assignment_cache)


# ----------------------------------------------------------------- K7-K8-K13
class _FakeLibClient:
    """discord.Client stand-in exercising the adapter's connect() contract."""

    last_built: "_FakeLibClient | None" = None

    def __init__(self) -> None:
        self.closed = False
        self.user = NS(id=777)
        self.before_identify_hook = None
        self._park = asyncio.Event()

    def event(self, coro):
        setattr(self, coro.__name__, coro)
        return coro

    async def start(self, token: str) -> None:
        # NOTE: on_ready is NOT fired here — connect() must stay parked in
        # _ready.wait() so the tests can cancel a genuinely pending connect
        await self._park.wait()  # stay "connected" until closed

    async def close(self) -> None:
        self.closed = True
        self._park.set()


def _fake_discord_module():
    mod = NS(Client=_FakeLibClient, DMChannel=type("DMChannel", (), {}))
    _FakeLibClient.last_built = None
    original_init = _FakeLibClient.__init__

    def recording_init(self):
        original_init(self)
        _FakeLibClient.last_built = self

    _FakeLibClient.__init__ = recording_init
    return mod


class AdapterIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_connect_closes_library_client(self) -> None:
        """K7: reaping only the _run task leaks the HTTP sessions."""
        mod = _fake_discord_module()
        with mock.patch.dict(sys.modules, {"discord": mod}):
            client = RealDiscordClient("token-tok")
            task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)     # inside _ready wait / client.start
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(_FakeLibClient.last_built)
        self.assertTrue(_FakeLibClient.last_built.closed)  # session closed

    async def test_factory_threads_shared_identify_gate_and_logger(self) -> None:
        """K8/K13: every client gets the process-wide gate + account logger."""
        factory = RealClientFactory(identify_interval=0.05)
        alpha = factory("alpha", "tok-a")
        beta = factory("beta", "tok-b")
        self.assertIs(alpha._identify_gate, beta._identify_gate)
        self.assertEqual(alpha._logger.name, "slaughtermudae.adapter.alpha")
        self.assertEqual(beta._logger.name, "slaughtermudae.adapter.beta")

    async def test_identify_gate_spaces_concurrent_identifies(self) -> None:
        gate = IdentifyGate(interval=0.08)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.gather(gate.acquire(), gate.acquire(), gate.acquire())
        elapsed = loop.time() - start
        self.assertGreaterEqual(elapsed, 2 * 0.08)  # 3 identifies, 2 gaps

    async def test_connect_wires_gate_as_before_identify_hook(self) -> None:
        mod = _fake_discord_module()
        with mock.patch.dict(sys.modules, {"discord": mod}):
            gate = IdentifyGate(interval=0.01)
            client = RealDiscordClient("token-tok", identify_gate=gate)
            connect_task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass
        lib = _FakeLibClient.last_built
        self.assertIsNotNone(lib)
        self.assertEqual(lib.before_identify_hook, gate.acquire)


# -------------------------------------------------------------------- K10
class LaneConfigDictRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_repo_shape_dict_row_loads(self) -> None:
        """KvRepository.get JSON-DECODES — the engine must accept a dict."""
        kv = FakeKv()
        kv.rows["ratelimit:lane"] = {"enabled": True, "prefixrate": 0.75}
        manager = RateLimitManager(
            EventBus(), StubLogger(), None, kv_repo=kv,
            lane_enabled=False, lane_prefixrate=1.5,
        )
        await manager.load_persisted()
        status = manager.lane_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["prefixrate"], 0.75)


# -------------------------------------------------------------------- K12
class ServiceLoggerContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdlib_surface_forwards_account_context(self) -> None:
        logs = LogService()
        logger = ServiceLogger(logs, "ratelimit")
        logger.info("server rate limit on 'message': cooling account", account="alpha")
        entry = logs.tail(1)[0]
        self.assertEqual(entry.get("account"), "alpha")  # was dropped pre-K12


# -------------------------------------------------------------------- K14
class MirrorForgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_forget_account(self) -> None:
        from core.settings_manager import SettingsManager

        sm = SettingsManager(EventBus(), LOG)
        sm._account_plugin["alpha"] = {"roll": {"trigger": "x"}}
        sm._account_framework["alpha"] = {"typing_probability": 0.5}
        sm.forget_account("alpha")
        self.assertNotIn("alpha", sm._account_plugin)
        self.assertNotIn("alpha", sm._account_framework)

    async def test_profile_manager_forget_account(self) -> None:
        pm = ProfileManager(object(), LOG, object())
        pm._assignment_cache["alpha"] = ["grinder"]
        pm.forget_account("alpha")
        self.assertNotIn("alpha", pm._assignment_cache)


# -------------------------------------------------------------------- K15
class EnginePruneTests(unittest.IsolatedAsyncioTestCase):
    async def test_prune_account_removes_only_that_accounts_residue(self) -> None:
        manager = RateLimitManager(EventBus(), StubLogger(), None)
        manager._last_fire[("alpha", "message")] = 123.0
        manager._last_fire[("beta", "message")] = 456.0
        manager._account_cooldowns[("alpha", "message")] = 999.0
        manager._account_cooldowns[("beta", "message")] = 998.0
        from core.rate_limit_manager.policies import RatePolicy

        manager.set_policy(RatePolicy(action="message", interval=3.0), "account", "alpha")
        manager.set_policy(RatePolicy(action="message", interval=4.0), "account", "beta")

        await manager.prune_account("alpha")

        self.assertNotIn(("alpha", "message"), manager._last_fire)
        self.assertIn(("beta", "message"), manager._last_fire)
        self.assertNotIn(("alpha", "message"), manager._account_cooldowns)
        self.assertIn(("beta", "message"), manager._account_cooldowns)
        self.assertNotIn(("account", "alpha", "message"), manager._policies)
        self.assertIn(("account", "beta", "message"), manager._policies)


# -------------------------------------------------------------------- K16
class SoulcopyInFlightLatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_trigger_skipped_while_flow_running(self) -> None:
        pages = ["Soulmates\n---------\n" + "\n".join(f"user{i}" for i in range(40))]
        manager, services = await make_plugin(
            pages, {"inter_chunk_delay": 0.05, "dm_read_timeout": 1.0})
        await manager.activate_account("alpha", services)  # dispatch needs active

        def trigger() -> None:
            manager._bus.publish_nowait(MessageReceived(
                account="alpha", message_id=500, channel_id=TRIGGER_CHANNEL,
                author_id=OWNER_ID, content="slcopy",
            ))

        trigger()
        await asyncio.sleep(0.02)  # first flow is now mid-run
        trigger()                  # second trigger arrives during the flow
        await asyncio.sleep(0.5)   # let the first flow finish

        # exactly ONE flow's worth of sends ($slud-s + one chunk); a second
        # interleaved flow would double them (K16)
        self.assertEqual(len(services.sent), 2)
        runtime = manager._accounts["alpha"]["soulcopy"]
        self.assertFalse(runtime.instance._in_flight)  # latch released


# -------------------------------------------------------------------- K11
class _AckTimeoutServices(RollServices):
    """RollServices whose FIRST send dies with the 30s task-ack timeout."""

    def __init__(self, responses=None):
        super().__init__(responses)
        self._failed_once = False

    def validate_channel(self, channel_id):
        return None  # probe succeeds immediately once the pause cycle runs

    async def send_message(self, channel_id, content, *, priority=2):
        if not self._failed_once:
            self._failed_once = True
            raise SchedulerError(
                "task 42 did not complete within 30s (cancelled)")
        return await super().send_message(channel_id, content, priority=priority)


class RollAckTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_timeout_pauses_then_resumes_instead_of_crashing(self):
        from core.plugin_manager import PluginManager
        from core.settings_manager import SettingsManager

        bus = EventBus()
        manager = PluginManager(
            bus, logging.getLogger("t"),
            SettingsManager(bus, logging.getLogger("t"), global_layer={"roll": {}}),
            plugin_dir="plugins",
        )
        manager.discover()
        await manager.attach_account("alpha", allowed={"roll"})
        services = _AckTimeoutServices()
        await manager.activate_account("alpha", services)
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=1, channel_id=CHANNEL,
            author_id=OWNER_ID, content="roll",
        ))
        await asyncio.sleep(0.3)
        runtime = manager._accounts["alpha"]["roll"]
        # the loop survived the ack-timeout (K11) and is still rolling
        self.assertTrue(runtime.instance._rolling.get("alpha"))
        # the transient branch parks one 5s probe cycle, then resumes —
        # a successful roll command must eventually go out
        await asyncio.sleep(5.2)
        self.assertTrue(any(cmd.startswith("$") for _, cmd, _ in services.sent))
        self.assertTrue(runtime.instance._rolling.get("alpha"))


# ------------------------------------------------------- M1: close codes
class DisconnectReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_code_surfaces_in_disconnect_reason(self) -> None:
        """M1: 'gateway closed' alone can't discriminate cull vs network."""
        mod = _fake_discord_module()
        with mock.patch.dict(sys.modules, {"discord": mod}):
            client = RealDiscordClient("token-tok")
            seen: list = []
            client.on_disconnect = seen.append
            connect_task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)
            lib = _FakeLibClient.last_built
            self.assertIsNotNone(lib)
            lib.ws = NS(close_code=4007)
            await lib.on_disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass
        self.assertEqual(seen, ["gateway closed (code=4007)"])

    async def test_missing_close_code_keeps_plain_reason(self) -> None:
        mod = _fake_discord_module()
        with mock.patch.dict(sys.modules, {"discord": mod}):
            client = RealDiscordClient("token-tok")
            seen: list = []
            client.on_disconnect = seen.append
            connect_task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)
            lib = _FakeLibClient.last_built
            lib.ws = NS()  # no close_code attribute at all
            await lib.on_disconnect()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass
        self.assertEqual(seen, ["gateway closed"])


# ------------------------------------------------- M2: flap-escalated backoff
class FlapBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_sessions_accumulate_flaps_and_recovery_continues(self):
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt = h.manager.get("alpha").runtime

        for cycle in range(5):
            await h.collector.wait_for(AccountConnected, cycle + 1)
            client = rt.client
            if client is not None:
                client.drop("gateway closed (code=1000)")  # short-lived drop
        await h.collector.wait_for(AccountConnected, 6)  # still recovering

        rt.flaps = [t for t in rt.flaps
                    if __import__("time").monotonic() - t < 300.0]
        self.assertGreaterEqual(len(rt.flaps), 4)   # M2: flapping recorded
        self.assertEqual(rt.status.value, "running")  # never blocks recovery
        await h.manager.stop("alpha")

    async def test_long_session_clears_flap_history(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt = h.manager.get("alpha").runtime
        rt.flaps = [1.0, 2.0, 3.0, 4.0]
        rt.session_started = __import__("time").monotonic() - 120.0  # survived
        rt.client.drop("gateway closed (code=1000)")
        self.assertEqual(rt.flaps, [])  # healthy session resets the breaker
        await h.manager.stop("alpha")


# --------------------------------------------------- M3: headers fetch cache
class _CountingHeaders:
    calls = 0

    @classmethod
    async def get_api_properties(cls, session, type_, *, proxy=None,
                                 proxy_auth=None):
        _CountingHeaders.calls += 1
        return ({"browser_version": "130.0.0.0"}, {}, "encoded")


class HeadersCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_properties_fetched_once_per_process(self) -> None:
        import adapters.discord.client as adapter_mod

        adapter_mod._headers_patch_installed = False
        adapter_mod._headers_cache.clear()
        _CountingHeaders.calls = 0
        mod = NS(utils=NS(Headers=_CountingHeaders))
        adapter_mod._install_headers_fetch_cache(mod, ttl=600.0)

        first = await _CountingHeaders.get_api_properties(None, "web")
        second = await _CountingHeaders.get_api_properties(None, "web")
        self.assertEqual(_CountingHeaders.calls, 1)  # second hit was cached
        self.assertEqual(first, second)
        adapter_mod._headers_patch_installed = False  # don't leak the patch

    async def test_cache_expires_after_ttl(self) -> None:
        import adapters.discord.client as adapter_mod

        adapter_mod._headers_patch_installed = False
        adapter_mod._headers_cache.clear()
        _CountingHeaders.calls = 0
        mod = NS(utils=NS(Headers=_CountingHeaders))
        adapter_mod._install_headers_fetch_cache(mod, ttl=0.0)  # instant TTL

        await _CountingHeaders.get_api_properties(None, "web")
        await _CountingHeaders.get_api_properties(None, "web")
        self.assertEqual(_CountingHeaders.calls, 2)  # expired -> refetched
        adapter_mod._headers_patch_installed = False


if __name__ == "__main__":
    unittest.main()
