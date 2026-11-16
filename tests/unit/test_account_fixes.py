"""Regression tests for the stale-start / observability audit fixes.

Covers the audited defects in core/account_manager/{manager,lifecycle,
cache,services}.py:

  (a) newer-start protection — a stale start (superseded by stop + a
      NEWER start) must never tear down the newer start's client/status/
      plugins (connect-park and activate-park windows),
  (b) start_all: a CANCELLED sweep resolves its flight so joiners
      complete instead of hanging on the shielded future forever,
  (c) start() clears/cancels a surviving prior-generation supervisor
      even when reconnect is DISABLED (drop then settles FAILED, not
      wedged RECONNECTING),
  (d) lifecycle stats()/retry_after() are side-effect-free observability
      reads (no start-bucket tokens consumed, no counters written),
  (e) dm_channel_for recipient index correctness + scan fallback,
  (f) resolve_dm_channel bounds client.open_dm (hung open resolves
      within timeout+ε with a ConnectionError),
  plus: remove_account identity guards before the early destructive
  steps, collect_messages prompt failure surfacing, and the fetch_user
  live-fetch row refresh (upsert).
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest import mock

from core.account_manager import AccountStatus
from core.account_manager.cache import DiscordCache, resolve_dm_channel
from core.account_manager.lifecycle import (
    LifecycleGovernor,
    LifecyclePolicy,
)
from core.account_manager.services import AccountPluginServices
from core.credentials import Credentials
from core.errors import AccountError
from core.event_bus import AccountFailed, EventBus, MessageReceived
from tests.unit.helpers import FakeSettings, default_snapshot, make_manager

if TYPE_CHECKING:
    from collections.abc import Callable


async def _poll_until(
    predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.01
) -> None:
    """Bounded wait until predicate() is true (AssertionError on timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached within budget")
        await asyncio.sleep(interval)


def _fast_reconnect_settings() -> FakeSettings:
    return FakeSettings(
        reconnect_enabled=True,
        reconnect_initial_delay=0.02,
        reconnect_max_delay=0.1,
        reconnect_multiplier=1.5,
    )


def _parking_factory(manager: Any, gates: list[asyncio.Event]) -> Callable[[str, str], Any]:
    """Wrap the manager's factory: every client's connect() parks on its
    own gate until the test sets it (test_final_phase.py pattern)."""
    original_factory = manager._client_factory

    def factory(name: str, token: str) -> Any:
        client = original_factory(name, token)
        gate = asyncio.Event()
        gates.append(gate)
        orig_connect = client.connect

        async def parked_connect() -> None:
            await gate.wait()
            await orig_connect()

        client.connect = parked_connect
        return client

    return factory


# ---------------------------------------------------------------- (a)
class NewerStartProtectionTests(unittest.IsolatedAsyncioTestCase):
    """Flagship: a stale start must not fight the newer start that
    superseded it (generation bumped by START, stop_requested cleared)."""

    async def test_stale_start_in_connect_leaves_newer_client_untouched(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        gates: list[asyncio.Event] = []
        h.manager._client_factory = _parking_factory(h.manager, gates)

        # start A parks inside client.connect()
        task_a = asyncio.create_task(h.manager.start("alpha"))
        await _poll_until(lambda: len(gates) == 1)
        client_a = h.factory.clients[0]

        # a stop completes while A is parked, then start B installs its
        # own client and runs to completion
        await h.manager.stop("alpha")
        task_b = asyncio.create_task(h.manager.start("alpha"))
        await _poll_until(lambda: len(gates) == 2)
        client_b = h.factory.clients[1]
        gates[1].set()  # B connects
        await _poll_until(lambda: h.manager.get("alpha").runtime.status == AccountStatus.RUNNING)
        rt = h.manager.get("alpha").runtime
        self.assertIs(rt.client, client_b)

        # A resumes STALE (its connect succeeds late) — the old code
        # discarded rt.client here, killing B's live gateway session
        gates[0].set()
        await asyncio.wait_for(task_a, timeout=2.0)  # completes, not hangs

        self.assertIs(rt.client, client_b)  # B's client untouched
        self.assertTrue(client_b.connected)  # and still connected
        self.assertFalse(client_a.connected)  # A released its own client
        self.assertEqual(rt.status, AccountStatus.RUNNING)  # not clobbered
        self.assertFalse(rt.stop_requested)
        # the stale abort was never classified as an account failure
        self.assertEqual(h.collector.of(AccountFailed), [])
        await asyncio.wait_for(task_b, timeout=2.0)
        await h.manager.stop("alpha")

    async def test_stale_start_in_activate_never_deactivates_newer_plugins(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha", plugins=["exporter"])
        gate = asyncio.Event()
        original = h.plugins.activate_account
        calls: list[str] = []

        async def gated_activate(account: str, factory: Any) -> None:
            calls.append(account)
            if len(calls) == 1:
                await gate.wait()  # A parks mid-activation
            return await original(account, factory)

        # deliberately swap in the gated double to park mid-activation
        h.plugins.activate_account = gated_activate  # type: ignore[method-assign,assignment]
        task_a = asyncio.create_task(h.manager.start("alpha"))
        await _poll_until(lambda: len(calls) == 1)

        await h.manager.stop("alpha")
        deactivated_after_stop = list(h.plugins.deactivated)

        # B runs to FULL completion (activate, publish, supervisor)
        task_b = asyncio.create_task(h.manager.start("alpha"))
        await asyncio.wait_for(task_b, timeout=2.0)
        rt = h.manager.get("alpha").runtime
        client_b = rt.client
        self.assertEqual(rt.status, AccountStatus.RUNNING)
        self.assertIsNotNone(rt.supervisor)

        # A resumes stale — the old code called _abort_stale_start here:
        # status STOPPED over B's RUNNING, deactivate over B's plugins,
        # client discard over B's live gateway
        gate.set()
        await asyncio.wait_for(task_a, timeout=2.0)

        self.assertEqual(rt.status, AccountStatus.RUNNING)  # not clobbered
        self.assertIs(rt.client, client_b)  # client intact
        self.assertTrue(client_b.connected)
        self.assertIsNotNone(rt.supervisor)  # still driven
        assert rt.supervisor is not None  # narrowing for the .done() read
        self.assertFalse(rt.supervisor.done())
        self.assertEqual(h.plugins.deactivated, deactivated_after_stop)  # B's plugins spared
        await h.manager.stop("alpha")


# ---------------------------------------------------------------- (b)
class StartAllCancelledSweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_sweep_completes_joiner(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        gates: list[asyncio.Event] = []
        h.manager._client_factory = _parking_factory(h.manager, gates)

        primary = asyncio.create_task(h.manager.start_all(stagger=0))
        await _poll_until(lambda: h.manager._start_all_flight is not None)
        joiner = asyncio.create_task(h.manager.start_all(stagger=0))
        await asyncio.sleep(0.05)  # joiner parks on the shielded flight

        primary.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(primary, timeout=2.0)

        # the joiner must COMPLETE (observe the cancellation), not hang
        _done, pending = await asyncio.wait({joiner}, timeout=2.0)
        self.assertFalse(pending, "joiner hung on the cancelled sweep")
        self.assertTrue(joiner.cancelled())
        self.assertIsNone(h.manager._start_all_flight)

        # cleanup: release the swept child's parked connect, then stop
        for gate in gates:
            gate.set()
        await asyncio.sleep(0.05)
        await h.manager.stop("alpha")


# ---------------------------------------------------------------- (c)
class ReconnectDisabledRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_with_reconnect_disabled_clears_old_supervisor(self) -> None:
        settings = _fast_reconnect_settings()
        h = make_manager(settings=settings)
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        rt = h.manager.get("alpha").runtime
        old_supervisor = rt.supervisor
        assert old_supervisor is not None  # just started; keeps .done() reads typed
        self.assertIsNotNone(old_supervisor)

        # disable reconnect, then drop: the supervisor stands the account
        # down FAILED (reconnect disabled) but stays ALIVE, parked on
        # ``lost`` — rt.supervisor still points at it
        settings.overrides["alpha"] = {"reconnect_enabled": False}
        rt.client.drop("bye")
        await _poll_until(lambda: rt.status == AccountStatus.FAILED, timeout=1.0)
        self.assertFalse(old_supervisor.done())  # alive but parked

        # restart with reconnect still disabled: the old supervisor must
        # be cancelled and cleared (the old code left it pointing)
        await h.manager.start("alpha")
        self.assertEqual(rt.status, AccountStatus.RUNNING)
        self.assertIsNone(rt.supervisor)
        await _poll_until(lambda: old_supervisor.done(), timeout=1.0)

        # a drop now settles FAILED (not wedged RECONNECTING forever)
        rt.client.drop("again")
        await _poll_until(lambda: rt.status == AccountStatus.FAILED, timeout=1.0)
        self.assertTrue(rt.lost.is_set())  # recovery signal armed
        await h.manager.stop("alpha")


# ---------------------------------------------------------------- (d)
class _ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LifecycleStatsSideEffectFreeTests(unittest.TestCase):
    def test_stats_does_not_consume_start_bucket_tokens(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(
            policy=LifecyclePolicy(start_burst=1, restart_cooldown_s=0.0),
            clock=clock,
        )
        gov.commit("alpha", "stop", ok=True)  # pacer exists; cooldown 0
        stats = gov.stats()  # the observability read
        self.assertEqual(stats["counters"]["decisions"], 0)  # wrote nothing
        # a start is still allowed IMMEDIATELY after the stats() call
        self.assertTrue(gov.precheck("alpha", "start").allowed)
        # ...and that was the REAL decision consuming the one token
        self.assertFalse(gov.precheck("alpha", "start").allowed)

    def test_retry_after_neither_consumes_tokens_nor_writes_counters(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(policy=LifecyclePolicy(start_burst=2), clock=clock)
        self.assertEqual(gov.retry_after("beta", "start"), 0.0)
        self.assertEqual(gov.counters["decisions"], 0)
        for _ in range(2):
            self.assertTrue(gov.precheck("beta", "start").allowed)
        self.assertFalse(gov.precheck("beta", "start").allowed)

    def test_stats_reports_cooldown_with_single_evaluation(self) -> None:
        clock = _ManualClock()
        gov = LifecycleGovernor(clock=clock)
        gov.commit("alpha", "stop", ok=True)  # 10s restart cooldown
        clock.advance(4.0)
        stats = gov.stats()
        self.assertAlmostEqual(stats["cooldowns"]["alpha"], 6.0, delta=0.1)
        # the peek did not advance/consume anything: still ~6s left
        self.assertAlmostEqual(gov.retry_after("alpha", "start"), 6.0, delta=0.1)


class LifecycleStatsManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_start_allowed_right_after_stats(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        gov = LifecycleGovernor(policy=LifecyclePolicy(start_burst=1), clock=_ManualClock())
        h.manager._lifecycle = gov
        stats = h.manager.lifecycle_stats()
        self.assertEqual(stats["counters"]["decisions"], 0)
        await h.manager.start("alpha")  # NOT rate-limited after the poll
        self.assertEqual(h.manager.get("alpha").runtime.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")


# ---------------------------------------------------------------- (e)
class DmChannelIndexTests(unittest.TestCase):
    def test_record_dm_invalidate_roundtrip_through_index(self) -> None:
        cache = DiscordCache("alpha")
        cache.record_dm(5000, 999)
        self.assertEqual(cache.dm_channel_for(999), 5000)
        cache.invalidate_channel(5000, "closed")
        self.assertIsNone(cache.dm_channel_for(999))  # tombstone honored
        cache.record_dm(5001, 999)  # re-open maps the NEW channel
        self.assertEqual(cache.dm_channel_for(999), 5001)
        self.assertIsNone(cache.dm_channel_for(888))  # unknown recipient

    def test_ingest_snapshot_populates_index(self) -> None:
        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())  # dm 9001 -> recipient 777
        self.assertEqual(cache.dm_channel_for(777), 9001)

    def test_recipient_change_drops_the_stale_index_entry(self) -> None:
        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())
        cache.record_dm(9001, 888)  # channel 9001 now claims recipient 888
        self.assertEqual(cache.dm_channel_for(888), 9001)
        self.assertIsNone(cache.dm_channel_for(777))  # scan semantics kept

    def test_fallback_scan_for_rows_predating_the_index(self) -> None:
        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())
        cache._dm_index.pop(777, None)  # simulate a pre-index row
        self.assertEqual(cache.dm_channel_for(777), 9001)  # scan finds it

    def test_deleted_index_entry_falls_back_to_scan(self) -> None:
        cache = DiscordCache("alpha")
        cache.record_dm(5000, 999)
        cache.record_dm(5001, 999)  # index now maps 999 -> 5001
        cache.invalidate_channel(5001, "closed")  # index entry is dead
        # live alternative with the same recipient still resolvable
        self.assertEqual(cache.dm_channel_for(999), 5000)


# ---------------------------------------------------------------- (f)
class OpenDmBoundedTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_open_dm_resolves_within_timeout(self) -> None:
        from core.account_manager import cache as cache_mod

        class HungClient:
            async def open_dm(self, user_id: int) -> None:
                await asyncio.Event().wait()  # never resolves

        cache = DiscordCache("alpha")
        loop = asyncio.get_running_loop()
        with mock.patch.object(cache_mod, "_OPEN_DM_TIMEOUT_S", 0.2):
            started = loop.time()
            with self.assertRaises(ConnectionError) as ctx:
                await resolve_dm_channel(cache, HungClient(), 42, "alpha", None)
            elapsed = loop.time() - started
        self.assertIn("timed out", str(ctx.exception))
        self.assertGreaterEqual(elapsed, 0.15)  # honored the bound...
        self.assertLess(elapsed, 1.5)  # ...within timeout + ε

    async def test_fast_open_dm_still_resolves(self) -> None:
        class FastClient:
            async def open_dm(self, user_id: int) -> int:
                return 7000 + user_id

        cache = DiscordCache("alpha")
        self.assertEqual(await resolve_dm_channel(cache, FastClient(), 5, "alpha", None), 7005)
        self.assertEqual(cache.dm_channel_for(5), 7005)


# ------------------------------------------------- bonus: remove guards
class _GatedPlugins:
    """Delegating plugin-manager double that parks deactivate_account."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.park = asyncio.Event()

    async def deactivate_account(self, account: str) -> Any:
        await self.park.wait()
        return await self._inner.deactivate_account(account)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)


class RemoveIdentityGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_recreate_during_deactivate_skips_scheduler_cascade(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        gated = _GatedPlugins(h.plugins)
        h.manager._plugin_manager = gated
        remover = asyncio.create_task(h.manager.remove_account("alpha"))
        await asyncio.sleep(0.05)  # removal parked inside deactivate

        # a same-named account is re-added during the await window (the
        # repo row is popped first: the removal only deletes it at the end)
        h.repo.records.pop("alpha", None)
        await h.manager.add_account("alpha")

        gated.park.set()
        out = await remover
        self.assertFalse(out.get("removed", True))
        self.assertTrue(out.get("recreated"))
        # the NEW account's scheduler lanes / queued tasks were untouched
        self.assertEqual(h.scheduler.cancel_calls, [])
        self.assertIsNotNone(await h.repo.get("alpha"))  # DB row intact
        # and the new account is fully usable
        await h.manager.start("alpha")
        self.assertEqual(h.manager.get("alpha").runtime.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")


# ------------------------------------------------- bonus: services fixes
def _services(
    bus: EventBus, client_getter: Callable[[], Any] | None = None, api_cache_repo: Any | None = None
) -> AccountPluginServices:
    return AccountPluginServices(
        "alpha",
        "exporter",
        bus=bus,
        scheduler=None,
        credentials=Credentials.from_values({}),
        cache=DiscordCache("alpha"),
        client_getter=client_getter,
        api_cache_repo=api_cache_repo,
    )


class CollectMessagesPromptFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_raising_check_surfaces_without_parking_the_idle_window(self) -> None:
        bus = EventBus()
        services = _services(bus)

        def bad_check(_event: MessageReceived) -> bool:
            raise KeyError("missing field")

        pending = asyncio.create_task(services.collect_messages(bad_check, 5.0))
        await asyncio.sleep(0.02)
        await bus.publish(
            MessageReceived(account="alpha", message_id=1, channel_id=100, author_id=1, content="x")
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(AccountError) as ctx:
            await asyncio.wait_for(pending, timeout=1.0)
        self.assertIn("missing field", str(ctx.exception))
        # surfaced promptly: nowhere near the 5s idle window
        self.assertLess(loop.time() - started, 1.0)


class _MemApiCache:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], Any] = {}

    async def get(self, account: str, kind: str, key: str) -> Any:
        return self.rows.get((account, kind, key))

    async def set(self, account: str, kind: str, key: str, value: Any) -> None:
        self.rows[(account, kind, key)] = value


class FetchUserRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_fetch_upserts_fresh_row(self) -> None:
        repo = _MemApiCache()
        names = iter(["old-name", "fresh-name"])

        class Client:
            async def fetch_user(self, user_id: int) -> SimpleNamespace:
                return SimpleNamespace(id=user_id, name=next(names))

        services = _services(EventBus(), client_getter=lambda: Client(), api_cache_repo=repo)

        user = await services.fetch_user(7)
        self.assertEqual(user.name, "old-name")
        self.assertEqual(repo.rows[("alpha", "user", "7")], {"id": 7, "name": "old-name"})

        # the only invalidation path for a user row is its deletion — the
        # next live fetch must UPSERT the fresh name (refresh), never
        # serve/keep the stale one
        del repo.rows[("alpha", "user", "7")]
        user2 = await services.fetch_user(7)
        self.assertEqual(user2.name, "fresh-name")
        self.assertEqual(repo.rows[("alpha", "user", "7")]["name"], "fresh-name")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
