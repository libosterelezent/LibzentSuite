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
  K9  reconnect backoff jitter per account (anti-thundering-herd)
  K10 lane-config kv rows that decode to dicts load correctly
  K11 (REMOVED with the roll plugin) the roll loop's transient-ack
      handling — the framework half (typed errors across the task-ack
      seam) stays covered by tests/unit/test_round3_reaudit_b.py
  K12 ServiceLogger stdlib surface forwards account= context
  K13 RealClientFactory builds per-account adapter loggers
  K14 settings/profile mirrors are evicted on account removal
  K15 rate-engine residue + account policies are pruned on removal
  K16 (REMOVED with the exporter plugin) the in-flight trigger latch
  K17 live adapter declares edit/delete/reaction callbacks (undeclared
      attrs are read inside library dispatch, which swallows the error
      and silently drops the events)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import unittest
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any
from unittest import mock

from adapters.discord import client as client_module
from adapters.discord.client import IdentifyGate, RealClientFactory, RealDiscordClient
from core.account_manager import AccountStatus
from core.errors import AccountError
from core.event_bus import AccountConnected, AccountFailed, EventBus
from core.logging_service import LogService, ServiceLogger
from core.rate_limit_manager.engine import RateLimitManager
from core.task_scheduler import TaskScheduler
from tests.unit.helpers import FakeClientFactory, make_manager, make_task
from tests.unit.test_account_manager import _fast_reconnect_settings
from tests.unit.test_rate_engine import StubLogger
from tests.unit.test_task_scheduler import (
    FakeAudit,
    FakeHistoryRepo,
    FakeLimiter,
    FakeLogger,
    FakeSender,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from database.models import AccountRecord

LOG = logging.getLogger("t-isolation")


# --------------------------------------------------------------------- K1
class SchedulerCleanupAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_tears_down_only_that_accounts_lane(self) -> None:
        scheduler = TaskScheduler(
            EventBus(),
            FakeLogger(),
            FakeLimiter(),
            FakeSender(),
            FakeAudit(),
            default_timeout=5.0,
            retry_backoff=0.5,
            history_repo=FakeHistoryRepo(),
        )
        scheduler.start()
        try:
            for account in ("alpha", "beta"):
                scheduler.submit(account, "p", "message", channel_id=1, content="x")
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
            EventBus(),
            FakeLogger(),
            FakeLimiter(),
            FakeSender(),
            FakeAudit(),
            default_timeout=5.0,
            history_repo=FakeHistoryRepo(),
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
        """K2 + PHASE-4 generation: stop() completes while start() is parked
        in token resolution — the stale start must NEVER become the owner.
        With generation checks it aborts before even building the client
        (strictly better than the K2-era build-then-disconnect)."""
        h = make_manager()
        await h.manager.add_account("alpha")
        gate = asyncio.Event()
        original = h.manager._resolve_token

        async def parked_resolve(record: AccountRecord) -> str:
            await gate.wait()
            return await original(record)

        # deliberately swap in the gated double to park the stale start
        h.manager._resolve_token = parked_resolve  # type: ignore[method-assign]
        start_task = asyncio.create_task(h.manager.start("alpha"))
        await asyncio.sleep(0.05)  # start() is parked pre-assignment
        await h.manager.stop("alpha")  # completes: rt.client was None
        gate.set()  # start() resumes
        out = await start_task

        rt = h.manager.get("alpha").runtime
        self.assertEqual(out["status"], "stopped")
        self.assertEqual(rt.status, AccountStatus.STOPPED)
        self.assertIsNone(rt.client)
        self.assertEqual(h.factory.clients, [])  # stale start built NOTHING
        self.assertEqual(h.collector.of(AccountFailed), [])

    async def test_stop_during_connect_tears_down_fresh_client(self) -> None:
        """K2 window 2: stop() completes while start() is parked INSIDE
        connect() — the freshly connected client must be disconnected."""
        h = make_manager()
        await h.manager.add_account("alpha")
        gate = asyncio.Event()

        class ParkingFactory(FakeClientFactory):
            def __init__(self, inner: FakeClientFactory, park: asyncio.Event) -> None:
                self._inner = inner
                self._park = park
                self.clients = inner.clients

            def __call__(self, name: str, token: str) -> Any:
                client = self._inner(name, token)
                orig_disconnect = client.disconnect

                async def parking_disconnect() -> None:
                    await self._park.wait()  # park the OLD-client teardown?
                    await orig_disconnect()

                # park INSIDE connect instead: wrap connect
                orig_connect = client.connect

                async def parked_connect() -> None:
                    await orig_connect()
                    await self._park.wait()  # hold start() post-READY

                # deliberately wrap connect() to hold start() post-READY
                client.connect = parked_connect  # type: ignore[method-assign]
                return client

        h.manager._client_factory = ParkingFactory(h.factory, gate)
        start_task = asyncio.create_task(h.manager.start("alpha"))
        await asyncio.sleep(0.05)  # start() parked post-READY
        await h.manager.stop("alpha")  # completes (rt.client was SET —
        gate.set()  # its disconnect aborts us anyway)
        with contextlib.suppress(Exception):  # AccountError("start cancelled by stop") is expected
            await start_task
        rt = h.manager.get("alpha").runtime
        self.assertFalse(h.factory.clients[0].connected)  # gateway closed
        self.assertEqual(rt.status, AccountStatus.STOPPED)

    async def test_aborted_start_never_publishes_account_failed(self) -> None:
        """K4: an intentional abort must not classify as a credential failure."""
        h = make_manager()
        await h.manager.add_account("alpha")
        rt = h.manager.get("alpha").runtime
        rt.stop_requested = True
        rt.status = AccountStatus.STOPPED
        h.manager._start_failed(
            "alpha", rt, AccountError("live client connected without a user (bad token?)")
        )
        self.assertEqual(rt.status, AccountStatus.STOPPED)  # no regression
        self.assertEqual(h.collector.of(AccountFailed), [])  # no event

    async def test_stop_during_activation_does_not_leave_plugins_active(self) -> None:
        """stop() completing while start() is INSIDE activate_account must
        not leave the re-activated plugins live on a STOPPED account."""
        h = make_manager()
        await h.manager.add_account("alpha")
        gate = asyncio.Event()
        original = h.plugins.activate_account

        async def gated_activate(account: str, factory: Any) -> None:
            await gate.wait()
            return await original(account, factory)

        # deliberately swap in the gated double to park mid-activation
        h.plugins.activate_account = gated_activate  # type: ignore[method-assign,assignment]
        start_task = asyncio.create_task(h.manager.start("alpha"))
        while len(h.plugins.attached) < 2:  # add_account attach + start attach
            await asyncio.sleep(0.01)
        await h.manager.stop("alpha")
        deactivated_before = len(h.plugins.deactivated)
        gate.set()
        out = await start_task

        rt = h.manager.get("alpha").runtime
        self.assertEqual(out["status"], "stopped")
        self.assertEqual(rt.status, AccountStatus.STOPPED)
        self.assertGreater(len(h.plugins.activated), 0)  # raced past the stop
        self.assertGreater(len(h.plugins.deactivated), deactivated_before)  # and undone

    async def test_in_flight_reconnect_cannot_resurrect_stopped_account(self) -> None:
        """K3: stop() landing while the supervisor is INSIDE connect() must
        tear the fresh client down instead of flipping back to RUNNING."""
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        await h.collector.wait_for(AccountConnected, 1)

        gate = asyncio.Event()
        original = h.manager._resolve_token

        async def parked_resolve(record: AccountRecord) -> str:
            await gate.wait()
            return await original(record)

        # deliberately swap in the gated double to park the reconnect
        h.manager._resolve_token = parked_resolve  # type: ignore[method-assign]
        h.manager.get("alpha").runtime.client.drop("socket closed")
        await asyncio.sleep(0.15)  # supervisor enters connect(), parks
        await h.manager.stop("alpha")  # completes while parked
        gate.set()  # reconnect resumes and CONNECTS
        await asyncio.sleep(0.2)

        rt = h.manager.get("alpha").runtime
        self.assertEqual(rt.status, AccountStatus.STOPPED)  # not RUNNING
        self.assertIsNone(rt.client)  # torn down
        self.assertFalse(h.factory.clients[-1].connected)  # disconnected
        self.assertEqual(  # one connect only
            len(h.collector.of(AccountConnected)), 1
        )
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
        assert old is not None  # just started; keeps the .done() reads typed
        self.assertIsNotNone(old)
        self.assertFalse(old.done())

        rt.status = AccountStatus.FAILED  # stand-down leaves it alive+polling
        await h.manager.start("alpha")  # restart from FAILED is legal

        self.assertIsNot(rt.supervisor, old)
        await asyncio.sleep(0.05)
        self.assertTrue(old.done())  # K5: no orphaned dual supervisor
        assert rt.supervisor is not None
        self.assertFalse(rt.supervisor.done())
        await h.manager.stop("alpha")


# --------------------------------------------------------------------- K6
class RemoveFailedAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_of_failed_account_deactivates_plugins(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")  # plugins attach + activate
        self.assertIn("alpha", [a for a, _ in h.plugins.activated])
        rt = h.manager.get("alpha").runtime
        rt.status = AccountStatus.FAILED  # degraded, never stop()ed
        await h.manager.remove_account("alpha")
        self.assertIn("alpha", h.plugins.deactivated)  # K6: hooks fired
        self.assertIn("alpha", h.plugins.detached)


# ----------------------------------------------------------------- K7-K8-K13
class _FakeLibClient:
    """discord.Client stand-in exercising the adapter's connect() contract."""

    last_built: _FakeLibClient | None = None
    # registered at runtime by the adapter via event() (annotation-only)
    on_disconnect: Callable[[], Awaitable[None]]
    # assigned by tests to simulate library socket state (annotation-only)
    ws: NS

    def __init__(self, **kwargs: Any) -> None:  # Bot kwargs ride through now
        self.closed = False
        self.constructor_kwargs = kwargs
        self.user = NS(id=777)
        self.before_identify_hook = None
        self._park = asyncio.Event()

    def event(self, coro: Callable[..., Any]) -> Callable[..., Any]:
        setattr(self, coro.__name__, coro)
        return coro

    async def start(self, token: str) -> None:
        # NOTE: on_ready is NOT fired here — connect() must stay parked in
        # _ready.wait() so the tests can cancel a genuinely pending connect
        await self._park.wait()  # stay "connected" until closed

    async def close(self) -> None:
        self.closed = True
        self._park.set()


def _fake_discord_module() -> dict[str, Any]:
    """The fake discord world (Bot included) for sys.modules patching."""
    from tests.unit.helpers import fake_discord_sysmodules

    _FakeLibClient.last_built = None
    original_init = _FakeLibClient.__init__

    def recording_init(self: Any, **kwargs: Any) -> None:
        original_init(self, **kwargs)
        _FakeLibClient.last_built = self

    # deliberately swap __init__ to record the last client built
    _FakeLibClient.__init__ = recording_init  # type: ignore[method-assign]
    return fake_discord_sysmodules(_FakeLibClient)


class AdapterIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_connect_closes_library_client(self) -> None:
        """K7: reaping only the _run task leaks the HTTP sessions."""
        mod = _fake_discord_module()
        with mock.patch.dict(sys.modules, mod):
            client = RealDiscordClient("token-tok")
            task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)  # inside _ready wait / client.start
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(_FakeLibClient.last_built)
        lib_built = _FakeLibClient.last_built
        assert lib_built is not None
        self.assertTrue(lib_built.closed)  # session closed

    async def test_factory_threads_shared_identify_gate_and_logger(self) -> None:
        """K8/K13: every client gets the process-wide gate + account logger."""
        factory = RealClientFactory(identify_interval=0.05)
        alpha = factory("alpha", "tok-a")
        beta = factory("beta", "tok-b")
        self.assertIs(alpha._identify_gate, beta._identify_gate)
        self.assertEqual(alpha._logger.name, "libzent.adapter.alpha")
        self.assertEqual(beta._logger.name, "libzent.adapter.beta")

    async def test_identify_gate_spaces_concurrent_identifies(self) -> None:
        gate = IdentifyGate(interval=0.08)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.gather(gate.acquire(), gate.acquire(), gate.acquire())
        elapsed = loop.time() - start
        self.assertGreaterEqual(elapsed, 2 * 0.08)  # 3 identifies, 2 gaps

    async def test_connect_wires_gate_as_before_identify_hook(self) -> None:
        mod = _fake_discord_module()
        with mock.patch.dict(sys.modules, mod):
            gate = IdentifyGate(interval=0.01)
            client = RealDiscordClient("token-tok", identify_gate=gate)
            connect_task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)
            connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connect_task
        lib = _FakeLibClient.last_built
        self.assertIsNotNone(lib)
        assert lib is not None
        self.assertEqual(lib.before_identify_hook, gate.acquire)


# -------------------------------------------------------------------- K10
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
        sm._account_plugin["alpha"] = {"probe": {"trigger": "x"}}
        sm._account_framework["alpha"] = {"typing_probability": 0.5}
        sm.forget_account("alpha")
        self.assertNotIn("alpha", sm._account_plugin)
        self.assertNotIn("alpha", sm._account_framework)


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
# (REMOVED) SoulcopyInFlightLatchTests tested the exporter plugin's
# one-flow-at-a-time latch — plugin behavior that died with the plugin.
# The framework's dispatch isolation (one crash never kills siblings)
# stays covered by tests/unit/test_plugin_manager.py.


# -------------------------------------------------------------------- K11
# (REMOVED) RollAckTimeoutTests pinned the roll PLUGIN's loop semantics:
# the 30s task-ack timeout was classified transient and the loop backed
# off instead of crashing. That loop died with the plugin; the FRAMEWORK
# half of K11 — typed ConnectionError/TimeoutError reconstruction across
# the task-ack seam and the "did not complete within" SchedulerError
# text — stays covered by tests/unit/test_round3_reaudit_b.py
# (TestTypedErrorAckSeam).


# ------------------------------------------------------- M1: close codes
class DisconnectReasonTests(unittest.IsolatedAsyncioTestCase):
    """M1 pins, adapted to the audit's debounced-forward design: transient
    'disconnect' dispatches forward only after the resume window elapses
    (no on_resumed), and the reason carries the machine-parseable
    'close=NNNN' sentinel. The session is marked live first — a dispatch
    while not connected is a startup failure owned by connect()."""

    async def test_close_code_surfaces_in_disconnect_reason(self) -> None:
        """M1: 'gateway closed' alone can't discriminate cull vs network."""
        mod = _fake_discord_module()
        with (
            mock.patch.dict(sys.modules, mod),
            mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 0.05),
        ):
            client = RealDiscordClient("token-tok")
            seen: list[str] = []
            client.on_disconnect = seen.append
            connect_task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)
            lib = _FakeLibClient.last_built
            self.assertIsNotNone(lib)
            assert lib is not None
            client.connected = True  # session live: the debounce owns it
            lib.ws = NS(close_code=4007)
            await lib.on_disconnect()
            await asyncio.sleep(0.15)  # no resume landed: window elapsed
            connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connect_task
        self.assertEqual(seen, ["gateway closed (close=4007)"])

    async def test_missing_close_code_keeps_plain_reason(self) -> None:
        mod = _fake_discord_module()
        with (
            mock.patch.dict(sys.modules, mod),
            mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 0.05),
        ):
            client = RealDiscordClient("token-tok")
            seen: list[str] = []
            client.on_disconnect = seen.append
            connect_task = asyncio.create_task(client.connect())
            await asyncio.sleep(0.05)
            lib = _FakeLibClient.last_built
            assert lib is not None
            lib.ws = NS()  # no close_code attribute at all
            client.connected = True
            await lib.on_disconnect()
            await asyncio.sleep(0.15)  # debounce window elapsed
            connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connect_task
        self.assertEqual(seen, ["gateway closed"])


# ------------------------------------------------- M2: flap-escalated backoff
class FlapBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_sessions_accumulate_flaps_and_recovery_continues(self) -> None:
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

        rt.flaps = [t for t in rt.flaps if __import__("time").monotonic() - t < 300.0]
        self.assertGreaterEqual(len(rt.flaps), 4)  # M2: flapping recorded
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
    calls: int = 0

    @classmethod
    async def get_api_properties(
        cls, session: Any, type_: str, *, proxy: Any = None, proxy_auth: Any = None
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
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


# -------------------------------------------------- round-2 audit findings
class RemoveDuringStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_while_connecting_refused_and_registry_intact(self) -> None:
        """remove_account racing a start parked BEFORE the status guard's
        sync section: CONNECTING is set without an await, so the refusal
        must fire and put the account back untouched."""
        h = make_manager()
        await h.manager.add_account("alpha")
        gate = asyncio.Event()
        original = h.manager._resolve_token

        async def parked_resolve(record: AccountRecord) -> str:
            await gate.wait()
            return await original(record)

        # deliberately swap in the gated double to park before assignment
        h.manager._resolve_token = parked_resolve  # type: ignore[method-assign]
        start_task = asyncio.create_task(h.manager.start("alpha"))
        await asyncio.sleep(0.02)
        with self.assertRaises(AccountError):
            await h.manager.remove_account("alpha")
        self.assertEqual(h.manager.names(), ["alpha"])
        gate.set()
        out = await start_task
        self.assertEqual(out["status"], "running")
        await h.manager.stop("alpha")


class OpenDmSingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_open_dm_and_lane_dm_send_share_one_open(self) -> None:
        """services.open_dm runs OUTSIDE the scheduler lane: without the
        per-account resolve lock it double-fetches the same DM (two REST
        opens) against a concurrent lane dm send."""
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        client = h.manager.get("alpha").runtime.client
        original: Callable[[int], Coroutine[Any, Any, int]] = client.open_dm

        async def slow_open(user_id: int) -> int:
            await asyncio.sleep(0.05)
            return await original(user_id)

        # deliberately wrap open_dm to slow the shared-flight window
        client.open_dm = slow_open
        services = h.manager.services_for("alpha", "exporter")
        resolved, _message = await asyncio.gather(
            services.open_dm(888),
            h.manager.send_hook(make_task(action="dm", user_id=888, content="hi")),
        )
        self.assertEqual(resolved, 9888)
        self.assertEqual(client.open_dm_calls, [888])  # exactly ONE open
        await h.manager.stop("alpha")


class BackgroundTaskReaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_estop_task_death_after_removal_is_logged_not_silent(self) -> None:
        """The estop background stop task dies when its account is removed
        before it runs — the failure must be logged (and retrieved), never
        a GC-time 'exception was never retrieved' surprise."""
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        captured: list[tuple[Any, ...]] = []

        class SpyLogger:
            def error(self, *args: Any) -> None:
                captured.append(args)

            def __getattr__(self, name: str) -> Any:
                return lambda *a, **k: None

        h.manager._logger = SpyLogger()
        rt = h.manager.get("alpha").runtime
        rt.status = AccountStatus.STOPPED  # quiescent-looking survivor
        h.manager.emergency_stop("alpha")
        await h.manager.remove_account("alpha")
        await asyncio.sleep(0.1)
        self.assertEqual(h.manager._background, set())
        flat = [str(a) for args in captured for a in args]
        self.assertTrue(any("estop-alpha" in part for part in flat), flat)


if __name__ == "__main__":
    unittest.main()
