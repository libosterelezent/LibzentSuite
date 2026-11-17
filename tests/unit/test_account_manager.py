"""Unit tests for core.account_manager: registry, lifecycle, supervisor,
send hook, details page shape and inbound audit."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import unittest
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from adapters.base import DiscordMessage
from core.account_manager import AccountManager, AccountStatus
from core.errors import AccountError, CredentialsError, SchedulerError, TargetGone
from core.event_bus import (
    AccountConnected,
    AccountDisconnected,
    AccountFailed,
    EventBus,
)
from core.task_scheduler import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    TaskScheduler,
)
from tests.unit.helpers import (
    FakeDetailsLimiter,
    FakeSettings,
    default_snapshot,
    make_manager,
    make_task,
)

STATUS_KEYS = {
    "id",
    "name",
    "status",
    "user_id",
    "enabled",
    "workspace",
    "assigned_plugins",
    "plugin_profiles",
    "latency_ms",
    "uptime_s",
    "last_error",
    "credential",
    "plugins",
}
DETAILS_KEYS = {"name", "status", "identity", "runtime", "plugins", "rate_limit", "credential"}

_TEST_LOG = logging.getLogger("tests.account_manager")


class _InstantLimiter:
    """Duck-typed limiter for the real-scheduler tests (instant grants)."""

    class _Grant:
        wait_s = 0.0
        jitter_s = 0.0

    async def acquire(
        self, action: str, account: str | None, plugin: str | None, priority: Any
    ) -> _InstantLimiter._Grant:
        return self._Grant()

    def report_server_limit(self, *args: Any, **kwargs: Any) -> None:
        pass


def _real_scheduler(bus: EventBus, sender: Any, **kwargs: Any) -> TaskScheduler:
    """A started real TaskScheduler wired to the given sender."""
    scheduler = TaskScheduler(bus, _TEST_LOG, _InstantLimiter(), sender, **kwargs)
    scheduler.start()
    return scheduler


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


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_account_happy_path(self) -> None:
        h = make_manager()
        out = await h.manager.add_account("alpha", workspace="g1")
        self.assertEqual(out["name"], "alpha")
        self.assertEqual(out["status"], "created")
        self.assertEqual(set(out), STATUS_KEYS)
        self.assertIsNotNone(await h.repo.get("alpha"))  # persisted
        self.assertEqual(h.manager.names(), ["alpha"])  # memory
        # capability model: no assignments -> an EMPTY allowed set (the
        # old "no profile = unrestricted (None)" default is gone)
        self.assertEqual(h.plugins.attached, [("alpha", set())])

    async def test_add_account_invalid_name(self) -> None:
        h = make_manager()
        bad_names: tuple[Any, ...] = ("", "ALPHA", "a b", "-x", "x" * 33, 5, None)
        for bad in bad_names:
            with self.assertRaises(AccountError):
                await h.manager.add_account(bad)

    async def test_add_account_duplicate(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        with self.assertRaises(AccountError) as ctx:
            await h.manager.add_account("alpha")
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(len(h.repo.records), 1)

    async def test_add_persists_before_memory(self) -> None:
        h = make_manager()
        h.repo.fail_on = "add"
        with self.assertRaises(RuntimeError):
            await h.manager.add_account("alpha")
        self.assertEqual(h.manager.names(), [])  # memory clean
        self.assertEqual(len(h.repo.records), 0)

    async def test_add_with_plugins_assigns_and_gates(self) -> None:
        h = make_manager()
        out = await h.manager.add_account("alpha", plugins=["exporter"])
        self.assertEqual(out["assigned_plugins"], ["exporter"])
        self.assertEqual(h.plugins.attached[-1], ("alpha", {"exporter"}))
        self.assertEqual(
            await h.account_plugins.plugins_of("alpha"), ["exporter"]
        )  # persisted capability rows

    async def test_add_with_bad_plugins_type_rejected_before_persist(self) -> None:
        h = make_manager()
        bad_plugins: tuple[Any, ...] = ("exporter", 7, ["exporter", 3], {"exporter"})
        for bad in bad_plugins:
            with self.assertRaises(AccountError):
                await h.manager.add_account("alpha", plugins=bad)
        self.assertEqual(h.manager.names(), [])
        self.assertEqual(len(h.repo.records), 0)
        self.assertEqual(h.account_plugins.rows, {})

    async def test_add_rejects_legacy_profiles_argument(self) -> None:
        h = make_manager()
        with self.assertRaises(ValueError) as ctx:
            await h.manager.add_account("alpha", profiles=["basic"])
        self.assertIn("profiles", str(ctx.exception))
        self.assertEqual(h.manager.names(), [])  # nothing created
        self.assertEqual(h.account_plugins.rows, {})

    async def test_get_unknown_and_names_sorted(self) -> None:
        h = make_manager()
        await h.manager.add_account("beta")
        await h.manager.add_account("alpha")
        self.assertEqual(h.manager.names(), ["alpha", "beta"])
        self.assertEqual(h.manager.get("beta").record.name, "beta")
        with self.assertRaises(AccountError):
            h.manager.get("ghost")
        unhashable_payload: Any = {"name": "alpha"}  # dashboard sends dicts
        with self.assertRaises(AccountError):  # unhashable dashboard payload
            h.manager.get(unhashable_payload)

    async def test_accounts_sorted_status_dicts(self) -> None:
        h = make_manager()
        await h.manager.add_account("beta")
        await h.manager.add_account("alpha")
        rows = await h.manager.accounts()
        self.assertEqual([r["name"] for r in rows], ["alpha", "beta"])
        for row in rows:
            self.assertEqual(set(row), STATUS_KEYS)
            self.assertEqual(row["status"], "created")

    async def test_accounts_reflect_plugin_states(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        h.plugins.states_map["alpha"] = [
            {
                "name": "exporter",
                "enabled": True,
                "settings": {"x": 1},
                "last_error": None,
                "error_count": 0,
            },
            {
                "name": "roll",
                "enabled": False,
                "settings": {},
                "last_error": "boom",
                "error_count": 2,
            },
        ]
        rows = await h.manager.accounts()
        self.assertEqual(rows[0]["plugins"]["exporter"]["enabled"], True)
        self.assertEqual(rows[0]["plugins"]["roll"]["error_count"], 2)
        self.assertEqual(rows[0]["plugins"]["roll"]["last_error"], "boom")

    async def test_plugin_states_failure_keeps_last_known(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        h.plugins.states_map["alpha"] = [
            {
                "name": "exporter",
                "enabled": True,
                "settings": {},
                "last_error": None,
                "error_count": 0,
            },
        ]
        await h.manager.accounts()
        h.plugins.fail_states = True
        rows = await h.manager.accounts()
        self.assertIn("exporter", rows[0]["plugins"])  # last known kept

    async def test_fail_closed_reattach_clears_stale_plugin_view(self) -> None:
        """Round-3: an account re-attached with ZERO plugins (its
        assignments emptied) must not keep showing its previous plugin
        rows as if they were still attached."""
        h = make_manager()
        await h.manager.add_account("alpha", plugins=["exporter"])
        h.plugins.states_map["alpha"] = [
            {
                "name": "exporter",
                "enabled": True,
                "settings": {},
                "last_error": None,
                "error_count": 0,
            },
        ]
        rows = await h.manager.accounts()
        self.assertIn("exporter", rows[0]["plugins"])  # truthful view

        # a transient assignment-repo failure: the refresh fail-closes to
        # ZERO assignments, so the next attach (the one every start()
        # performs) resolves the grant to NOTHING
        h.account_plugins.fail_on = "plugins_of"
        try:
            await h.manager._refresh_assignment("alpha")
        finally:
            h.account_plugins.fail_on = None
        self.assertEqual(h.manager.get("alpha").assigned_plugins, [])
        await h.manager._attach_plugins("alpha")
        self.assertEqual(h.plugins.attached[-1], ("alpha", set()))

        # the plugin manager now HAS the account — with zero plugins
        h.plugins.states_map["alpha"] = []
        rows = await h.manager.accounts()
        self.assertEqual(rows[0]["plugins"], {})  # stale view cleared

    async def test_unattached_account_keeps_last_known_plugin_view(self) -> None:
        """Empty states on an account the plugin manager does NOT have
        (not attached) keeps the last known view (unchanged semantics)."""
        h = make_manager()
        await h.manager.add_account("alpha")
        h.plugins.states_map["alpha"] = [
            {
                "name": "exporter",
                "enabled": True,
                "settings": {},
                "last_error": None,
                "error_count": 0,
            },
        ]
        await h.manager.accounts()
        h.plugins.states_map["alpha"] = []
        h.manager._plugin_attached.discard("alpha")  # simulate "not attached"
        rows = await h.manager.accounts()
        self.assertIn("exporter", rows[0]["plugins"])  # last known kept

    async def test_set_enabled_persists(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        out = await h.manager.set_enabled("alpha", False)
        self.assertFalse(out["enabled"])
        persisted = await h.repo.get("alpha")
        assert persisted is not None
        self.assertFalse(persisted.enabled)
        self.assertTrue((await h.manager.set_enabled("alpha", True))["enabled"])

    async def test_remove_stopped_account(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        out = await h.manager.remove_account("alpha")
        self.assertEqual(out, {"removed": "alpha", "existed": True})
        self.assertEqual(h.manager.names(), [])
        self.assertIsNone(await h.repo.get("alpha"))
        self.assertEqual(h.plugins.detached, ["alpha"])

    async def test_remove_while_running_refused(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        with self.assertRaises(AccountError) as ctx:
            await h.manager.remove_account("alpha")
        self.assertIn("stop account", str(ctx.exception))
        self.assertEqual(h.manager.names(), ["alpha"])  # untouched
        self.assertIsNotNone(await h.repo.get("alpha"))
        await h.manager.stop("alpha")


class StartStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_happy_path(self) -> None:
        h = make_manager()
        h.plugins.states_map["alpha"] = [
            {
                "name": "exporter",
                "enabled": True,
                "settings": {"k": 1},
                "last_error": None,
                "error_count": 0,
            },
        ]
        await h.manager.add_account("alpha")
        out = await h.manager.start("alpha")
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["user_id"], 555)
        self.assertGreaterEqual(out["uptime_s"], 0.0)
        await h.collector.wait_for(AccountConnected, 1)
        # plugin activation got a per-plugin services factory
        self.assertEqual(len(h.plugins.activated), 1)
        account_name, factory = h.plugins.activated[0]
        self.assertEqual(account_name, "alpha")
        services = factory("exporter")
        self.assertEqual((services.account, services.plugin), ("alpha", "exporter"))
        # cache ingested from the client's READY snapshot
        cache = h.manager.get("alpha").runtime.cache
        assert cache is not None
        cache.validate_channel(100)  # would raise TargetGone if not ingested
        self.assertEqual(cache.dms(), 1)
        # supervisor running (reconnect enabled by default settings)
        self.assertIsNotNone(h.manager.get("alpha").runtime.supervisor)
        await h.manager.stop("alpha")

    async def test_start_resolves_token_by_convention(self) -> None:
        h = make_manager(token_values={"DISCORD_TOKEN_ALPHA": "tok-alpha"})
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        client_zero: Any = h.factory.clients[0]  # factory stamps .token
        self.assertEqual(client_zero.token, "tok-alpha")
        await h.manager.stop("alpha")

    async def test_start_missing_token(self) -> None:
        h = make_manager(token_values={})
        await h.manager.add_account("alpha")
        with self.assertRaises(CredentialsError) as ctx:
            await h.manager.start("alpha")
        self.assertIn("no stored credential", str(ctx.exception))
        runtime = h.manager.get("alpha").runtime
        self.assertEqual(runtime.status, AccountStatus.MISSING_CREDENTIAL)
        self.assertTrue(runtime.last_error)
        await h.collector.wait_for(AccountFailed, 1)

    async def test_start_connect_failure(self) -> None:
        h = make_manager(client_kwargs={"fail_connect": True})
        await h.manager.add_account("alpha")
        with self.assertRaises(AccountError) as ctx:
            await h.manager.start("alpha")
        self.assertIn("failed to connect", str(ctx.exception))
        runtime = h.manager.get("alpha").runtime
        self.assertEqual(runtime.status, AccountStatus.FAILED)
        self.assertIsNotNone(runtime.last_error)
        failures = await h.collector.wait_for(AccountFailed, 1)
        self.assertTrue(failures[0].error)
        self.assertIsNone(runtime.supervisor)

    async def test_start_twice_refused(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        with self.assertRaises(AccountError):
            await h.manager.start("alpha")
        await h.manager.stop("alpha")

    async def test_stop_is_idempotent_and_publishes_once(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        client = h.manager.get("alpha").runtime.client
        out = await h.manager.stop("alpha")
        self.assertEqual(out["status"], "stopped")
        await self._drain()
        disconnected = h.collector.of(AccountDisconnected)
        self.assertEqual(len(disconnected), 1)
        self.assertEqual(disconnected[0].reason, "stopped")
        self.assertFalse(client.connected)
        self.assertEqual(h.plugins.deactivated, ["alpha"])
        first = h.manager.get("alpha").runtime
        self.assertIsNone(first.supervisor)
        await h.manager.stop("alpha")  # second stop: nothing re-fires
        await self._drain()
        self.assertEqual(len(h.collector.of(AccountDisconnected)), 1)
        self.assertEqual(h.plugins.deactivated, ["alpha"])
        self.assertEqual(h.manager.get("alpha").runtime.status, AccountStatus.STOPPED)

    async def test_stop_never_started_account(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        out = await h.manager.stop("alpha")
        self.assertEqual(out["status"], "stopped")
        await self._drain()
        self.assertEqual(h.collector.of(AccountDisconnected), [])  # never live

    async def test_start_all_skips_disabled_and_collects_failures(self) -> None:
        # only alpha and gamma have stored credentials; beta does not
        h = make_manager(
            token_values={
                "DISCORD_TOKEN_ALPHA": "tok-alpha",
                "DISCORD_TOKEN_GAMMA": "tok-gamma",
            }
        )
        await h.manager.add_account("alpha")
        await h.manager.add_account("beta")
        await h.manager.add_account("gamma")
        await h.manager.set_enabled("gamma", False)
        out = await h.manager.start_all()
        self.assertEqual(out["started"], ["alpha"])
        self.assertEqual(out["skipped"], ["gamma"])
        self.assertIn("beta", out["failed"])
        self.assertEqual(h.manager.get("beta").runtime.status, AccountStatus.MISSING_CREDENTIAL)
        await h.manager.stop_all()

    async def test_stop_all_logs_and_continues(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.add_account("beta")
        await h.manager.start("alpha")
        await h.manager.start("beta")
        out = await h.manager.stop_all()
        self.assertEqual(sorted(out["stopped"]), ["alpha", "beta"])
        self.assertEqual(out["failed"], {})
        for name in ("alpha", "beta"):
            self.assertEqual(h.manager.get(name).runtime.status, AccountStatus.STOPPED)

    async def test_emergency_stop_cancels_tasks_and_stops(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        for _ in range(3):
            h.scheduler.submit("alpha", "exporter", "message", channel_id=100, content="x")
        out = h.manager.emergency_stop("alpha")
        self.assertEqual(out["cancelled_tasks"], 3)
        self.assertTrue(out["stop_initiated"])
        self.assertEqual(h.scheduler.cancel_calls[-1]["account"], "alpha")
        runtime = h.manager.get("alpha").runtime
        for _ in range(200):  # bounded poll: the fired stop task finishes
            if runtime.status == AccountStatus.STOPPED:
                break
            await self._drain()
        self.assertEqual(runtime.status, AccountStatus.STOPPED)
        await h.collector.wait_for(AccountDisconnected, 1)

    @staticmethod
    async def _drain() -> None:
        """Let fire-and-forget publishes dispatch."""
        await asyncio.sleep(0.02)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_drop_triggers_reconnect_with_fresh_client(self) -> None:
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        first = h.manager.get("alpha").runtime.client
        await h.collector.wait_for(AccountConnected, 1)
        first.drop("socket closed")
        runtime = h.manager.get("alpha").runtime
        self.assertEqual(runtime.status, AccountStatus.RECONNECTING)
        await h.collector.wait_for(AccountConnected, 2)
        self.assertEqual(runtime.status, AccountStatus.RUNNING)
        self.assertIs(runtime.client, h.factory.clients[1])  # fresh client
        self.assertIsNot(runtime.client, first)
        cache = runtime.cache
        assert cache is not None
        cache.validate_channel(100)  # re-ingested on reconnect
        await h.manager.stop("alpha")
        self.assertEqual(len(h.factory.clients), 2)  # no extra reconnects

    async def test_revoked_token_on_reconnect_stands_down(self) -> None:
        """A credential rejected DURING a reconnect is abandoned once —
        the supervisor must not re-enter the loop (lost cleared) and
        hammer the login every tick."""
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        first = h.manager.get("alpha").runtime.client
        h.manager._client_factory = RejectingFactory()
        first.drop("socket closed")
        runtime = h.manager.get("alpha").runtime
        for _ in range(100):
            if runtime.status == AccountStatus.INVALID_CREDENTIAL:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(runtime.status, AccountStatus.INVALID_CREDENTIAL)
        built = len(h.manager._client_factory.clients)
        await asyncio.sleep(0.5)
        self.assertEqual(len(h.manager._client_factory.clients), built)
        self.assertFalse(runtime.lost.is_set())
        await h.manager.stop("alpha")

    def test_auth_marker_ignores_401_inside_names(self) -> None:
        from core.account_manager.manager import _looks_like_auth_error

        self.assertTrue(_looks_like_auth_error(Exception("HTTP 401 Unauthorized")))
        self.assertTrue(_looks_like_auth_error(Exception("Improper token passed")))
        self.assertFalse(
            _looks_like_auth_error(Exception("client for account 'alt401' did not connect"))
        )
        self.assertFalse(_looks_like_auth_error(Exception("user 1401234 gone")))

    async def test_drop_reason_recorded(self) -> None:
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        h.manager.get("alpha").runtime.client.drop("heartbeat timeout")
        await h.collector.wait_for(AccountConnected, 2)
        runtime = h.manager.get("alpha").runtime
        self.assertEqual(runtime.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")

    async def test_reconnect_disabled_by_account_settings(self) -> None:
        settings = _fast_reconnect_settings()
        settings.overrides["alpha"] = {
            "reconnect_enabled": False,
            "reconnect_initial_delay": 0.05,
        }
        h = make_manager(settings=settings)
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        self.assertIsNone(h.manager.get("alpha").runtime.supervisor)
        h.manager.get("alpha").runtime.client.drop("bye")
        await asyncio.sleep(0.2)
        runtime = h.manager.get("alpha").runtime
        # FAILED (not wedged RECONNECTING): start() stays usable for automation
        self.assertEqual(runtime.status, AccountStatus.FAILED)
        self.assertEqual(len(h.factory.clients), 1)  # never retried
        # stale-start audit: lost is SET on the no-supervisor FAILED path —
        # the recovery signal for a supervisor spawned later in the same
        # generation (start() clears it before connecting, so no stale wake)
        self.assertTrue(runtime.lost.is_set())
        # and the account can be started again
        await h.manager.start("alpha")
        self.assertEqual(runtime.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")

    async def test_stop_during_backoff_cancels_supervisor(self) -> None:
        settings = _fast_reconnect_settings()
        settings.overrides["alpha"] = {
            "reconnect_initial_delay": 30.0,
            "reconnect_max_delay": 30.0,
        }
        h = make_manager(settings=settings)
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        h.manager.get("alpha").runtime.client.drop("bye")
        await asyncio.sleep(0.05)
        await asyncio.wait_for(h.manager.stop("alpha"), timeout=3.0)
        runtime = h.manager.get("alpha").runtime
        self.assertEqual(runtime.status, AccountStatus.STOPPED)
        self.assertIsNone(runtime.supervisor)

    async def test_repeated_drops_backoff_recovers(self) -> None:
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        for _ in range(2):
            await h.collector.wait_for(AccountConnected, 1)
            h.manager.get("alpha").runtime.client.drop("flaky")
            await asyncio.sleep(0.05)
        await h.collector.wait_for(AccountConnected, 3)
        self.assertEqual(h.manager.get("alpha").runtime.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")

    async def test_backoff_overflow_never_kills_supervisor(self) -> None:
        """Round-3 (backoff-overflow zombie): a multiplier/exponent whose
        power overflows float must not crash the reconnect loop — the
        supervisor keeps retrying at the max delay instead of dying
        silently and leaving the account RECONNECTING forever."""
        settings = _fast_reconnect_settings()
        # NOTE: _reconnect_settings clamps max with a 0.1s floor — the
        # effective max here is 0.1, NOT the 0.05 written below.
        settings.overrides["alpha"] = {
            "reconnect_initial_delay": 0.01,
            "reconnect_max_delay": 0.05,
            "reconnect_multiplier": 1e300,  # **2 already overflows float
        }
        h = make_manager(settings=settings)
        # deterministic clock: record delays instead of sleeping (the
        # wall-clock polling shape was the suite's known ~1-in-8 flake —
        # three jittered backoffs of 0.1-0.15s vs a 2.0s poll budget on
        # a loaded runner). Seed the jitter RNG too, so bounds are exact.
        recorded: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            recorded.append(seconds)
            await asyncio.sleep(0)  # yield: never busy-spin the loop

        sleep_override: Any = fake_sleep  # asyncio.sleep's overloaded shape
        h.manager._sleep = sleep_override
        h.manager._typing_rng = __import__("random").Random(0)
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime
        h.factory.client_kwargs["fail_connect"] = True  # reconnects fail
        runtime.client.drop("socket closed")
        # retries proceed instantly under the fake sleep — poll only for
        # the COUNT of attempts, never wall-clock
        await _poll_until(lambda: len(recorded) >= 3, timeout=2.0)
        # overflow clamps to max (0.1 floor) with x[1.0,1.5) jitter
        self.assertTrue(
            all(0.1 <= d <= 0.15 for d in recorded), f"delays escaped the clamp: {recorded}"
        )
        supervisor = runtime.supervisor
        assert supervisor is not None
        self.assertIsNotNone(supervisor)
        self.assertFalse(supervisor.done())  # alive, still driving retries
        self.assertGreaterEqual(len(h.factory.clients), 4)  # kept retrying
        await h.manager.stop("alpha")
        self.assertEqual(runtime.status, AccountStatus.STOPPED)

    async def test_reconnect_loop_crash_stands_down_failed(self) -> None:
        """Round-3: an unexpected reconnect-loop crash must surface as a
        FAILED account (start() stays usable), not a dead supervisor with
        the account wedged in RECONNECTING."""
        h = make_manager(settings=_fast_reconnect_settings())
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime

        async def _boom(name: str, rt: Any, gen: int | None = None) -> None:
            raise RuntimeError("kaboom")

        h.manager._reconnect_loop = _boom  # type: ignore[method-assign]
        runtime.client.drop("bye")
        await _poll_until(lambda: runtime.status == AccountStatus.FAILED, timeout=1.0)
        assert runtime.last_error is not None
        self.assertIn("kaboom", runtime.last_error)
        self.assertFalse(runtime.lost.is_set())  # no stale wake-ups
        supervisor = runtime.supervisor
        assert supervisor is not None
        self.assertTrue(supervisor.done())
        self.assertIsNone(supervisor.exception())  # clean stand-down
        # automation path: the account can simply be started again
        await h.manager.start("alpha")
        self.assertEqual(runtime.status, AccountStatus.RUNNING)
        await h.manager.stop("alpha")


def _fast_reconnect_settings() -> FakeSettings:
    return FakeSettings(
        reconnect_enabled=True,
        reconnect_initial_delay=0.02,
        reconnect_max_delay=0.1,
        reconnect_multiplier=1.5,
    )


class SendHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_action_sends_and_audits(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        message = await h.manager.send_hook(make_task(content="hello"))
        self.assertIsInstance(message, DiscordMessage)
        client = h.manager.get("alpha").runtime.client
        self.assertEqual(client.sent, [("message", 100, "hello")])
        # INTEGRATION NOTE: outbound audit moved to the scheduler's
        # terminal path (exactly-once with attempts context) — send_hook no
        # longer records 'out' rows itself (double-audit fix).
        outbound = [e for e in h.audit.entries if e.get("direction") == "out"]
        self.assertEqual(outbound, [])
        await h.manager.stop("alpha")

    async def test_dm_action_uses_cached_channel_first(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        client = h.manager.get("alpha").runtime.client
        message = await h.manager.send_hook(
            make_task(action="dm", user_id=777, channel_id=None, content="psst")
        )
        assert message is not None
        self.assertTrue(message.dm)
        self.assertEqual(client.open_dm_calls, [])  # snapshot DM 9001 was cached
        # unified path: DMs send through send_message with the resolved
        # channel id (never re-fetching the user / re-creating the DM)
        self.assertEqual(client.sent[-1], ("message", 9001, "psst"))
        await h.manager.stop("alpha")

    async def test_dm_action_opens_and_records_missing_dm(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime
        await h.manager.send_hook(make_task(action="dm", user_id=888, content="hi"))
        self.assertEqual(runtime.client.open_dm_calls, [888])
        dm_cache = runtime.cache
        assert dm_cache is not None
        self.assertEqual(dm_cache.dm_channel_for(888), 9888)
        await h.manager.send_hook(make_task(action="dm", user_id=888, content="again"))
        self.assertEqual(runtime.client.open_dm_calls, [888])  # cached now
        await h.manager.stop("alpha")

    async def test_target_gone_propagates(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        with self.assertRaises(TargetGone):
            await h.manager.send_hook(make_task(channel_id=424242))
        client = h.manager.get("alpha").runtime.client
        self.assertEqual(client.sent, [])  # nothing leaked through
        await h.manager.stop("alpha")

    async def test_not_running_raises_connection_error(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        with self.assertRaises(ConnectionError) as ctx:
            await h.manager.send_hook(make_task())
        self.assertIn("is not running", str(ctx.exception))

    async def test_unknown_account_raises_account_error(self) -> None:
        h = make_manager()
        with self.assertRaises(AccountError):
            await h.manager.send_hook(make_task(account="ghost"))

    async def test_unsupported_action_raises_scheduler_error(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        with self.assertRaises(SchedulerError) as ctx:
            await h.manager.send_hook(make_task(action="poke"))
        self.assertIn("poke", str(ctx.exception))
        await h.manager.stop("alpha")

    async def test_button_target_gone_heals_stale_channel_cache(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime
        client = runtime.client

        async def vanish(channel_id: int, message_id: int, custom_id: str) -> None:
            raise TargetGone("alpha", f"channel {channel_id} vanished")

        client.click_button = vanish
        with self.assertRaises(TargetGone):
            await h.manager.send_hook(
                make_task(action="button", channel_id=100, message_id=5, content="btn")
            )
        stale_cache = runtime.cache
        assert stale_cache is not None
        with self.assertRaises(TargetGone):
            stale_cache.validate_channel(100)
        await h.manager.stop("alpha")


class DetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_exact_shape_with_limiter(self) -> None:
        limiter = FakeDetailsLimiter()
        limiter.policies_list = [
            {
                "level": "global",
                "owner": None,
                "action": "message",
                "interval": 3.0,
                "jitter": [0.2, 0.8],
            },
            {
                "level": "account",
                "owner": "alpha",
                "action": "message",
                "interval": 5.0,
                "jitter": [0.0, 0.0],
            },
            {
                "level": "global",
                "owner": None,
                "action": "dm",
                "interval": 4.0,
                "jitter": [0.1, 0.3],
            },
        ]
        h = make_manager(limiter=limiter)
        await h.manager.add_account("alpha", plugins=["exporter"])
        h.plugins.states_map["alpha"] = [
            {
                "name": "exporter",
                "enabled": True,
                "settings": {},
                "last_error": None,
                "error_count": 0,
            },
        ]
        await h.manager.start("alpha")
        d = await h.manager.details("alpha")
        self.assertEqual(set(d), DETAILS_KEYS)
        self.assertEqual(d["name"], "alpha")
        self.assertEqual(set(d["status"]), {"connected", "state", "latency_ms", "uptime_s"})
        self.assertTrue(d["status"]["connected"])
        self.assertEqual(d["status"]["state"], "running")
        self.assertEqual(d["status"]["latency_ms"], 42.0)
        self.assertGreaterEqual(d["status"]["uptime_s"], 0.0)
        self.assertEqual(d["identity"], {"name": "AlphaSelf", "discord_id": 555})
        self.assertEqual(
            set(d["runtime"]), {"cached_guilds", "cached_channels", "cached_dms", "cache_source"}
        )
        self.assertEqual(d["runtime"]["cached_guilds"], 1)
        self.assertEqual(d["runtime"]["cached_channels"], 3)  # 100, 101 + dm 9001
        self.assertEqual(d["runtime"]["cached_dms"], 1)
        self.assertEqual(d["runtime"]["cache_source"], "client cache (READY)")
        self.assertEqual(d["plugins"], [{"name": "exporter", "enabled": True}])
        self.assertEqual(
            d["rate_limit"],
            {
                "message": {
                    "interval": 5.0,
                    "jitter": [0.0, 0.0],
                    "source": "global, account:alpha",
                },
                "dm": {"interval": 4.0, "jitter": [0.1, 0.3], "source": "inherited from global"},
            },
        )
        await h.manager.stop("alpha")

    async def test_details_rate_limit_empty_without_limiter(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        d = await h.manager.details("alpha")
        self.assertEqual(d["rate_limit"], {})
        self.assertEqual(d["identity"], {"name": "alpha", "discord_id": None})
        self.assertFalse(d["status"]["connected"])
        self.assertIsNone(d["status"]["latency_ms"])

    async def test_details_falls_back_to_account_plugin_view(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        h.manager.get("alpha").plugins = {
            "exporter": {"enabled": False, "last_error": None, "error_count": 0},
        }
        d = await h.manager.details("alpha")
        self.assertEqual(d["plugins"], [{"name": "exporter", "enabled": False}])

    async def test_details_unknown_account(self) -> None:
        h = make_manager()
        with self.assertRaises(AccountError):
            await h.manager.details("ghost")

    async def test_inbound_messages_are_audited(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        client = h.manager.get("alpha").runtime.client
        client.receive(
            DiscordMessage(
                id=1,
                channel_id=100,
                author_id=42,
                content="x" * 100,
            )
        )
        client.receive(
            DiscordMessage(
                id=2,
                channel_id=9001,
                author_id=777,
                content="dm\nline",
                dm=True,
            )
        )
        await asyncio.sleep(0.05)
        inbound = [e for e in h.audit.entries if e.get("direction") == "in"]
        self.assertEqual(len(inbound), 2)
        first, second = inbound
        self.assertEqual(first["account"], "alpha")
        self.assertEqual(first["action"], "message")
        self.assertEqual(first["channel_id"], 100)
        self.assertEqual(first["content"], "x" * 80)  # 80-char preview
        self.assertEqual(first["note"], "author=42")
        self.assertEqual(second["note"], "author=777 dm")
        self.assertEqual(second["content"], "dm\\nline")  # newline escaped
        await h.manager.stop("alpha")

    async def test_inbound_audit_survives_audit_recorder_failure(self) -> None:
        h = make_manager()

        class ExplodingAudit:
            def record(self, **kwargs: Any) -> None:
                raise RuntimeError("audit backend down")

        manager = h.manager
        manager._audit = ExplodingAudit()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        client = h.manager.get("alpha").runtime.client
        client.receive(DiscordMessage(id=1, channel_id=100, author_id=1, content="x"))
        await asyncio.sleep(0.05)  # must not raise
        await h.manager.stop("alpha")


class CredentialWiringTests(unittest.IsolatedAsyncioTestCase):
    """The new credential-service path: DB-first resolution, the
    MISSING_CREDENTIAL / INVALID_CREDENTIAL classifications, and the
    account-removal cascades."""

    async def test_resolve_db_first_and_status_field(self) -> None:
        import tempfile
        from pathlib import Path

        from core.credential_service import CredentialService
        from database.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(":memory:")
            await db.connect()
            try:
                h = make_manager(
                    token_values={
                        "DISCORD_TOKEN_ALPHA": "env-token",
                    }
                )
                service = CredentialService(db.credentials, key_path=Path(tmp) / "k.key")
                await service.store("alpha", "db-token")
                h.manager._credential_service = service
                await h.manager.add_account("alpha")
                await h.manager.start("alpha")
                # DB credential wins over the legacy env value
                client_zero: Any = h.factory.clients[0]  # factory stamps .token
                self.assertEqual(client_zero.token, "db-token")
                row = (await h.manager.accounts())[0]
                self.assertEqual(row["credential"], "db")
                d = await h.manager.details("alpha")
                self.assertEqual(d["credential"], "db")
                await h.manager.stop("alpha")
            finally:
                await db.close()

    async def test_env_token_ignored_no_fallback(self) -> None:
        """NEGATIVE (spec §34): .env token exists but DB credential absent
        -> MISSING_CREDENTIAL, NOT env fallback. The old .env path is GONE."""
        import tempfile
        from pathlib import Path

        from core.credential_service import CredentialService

        with tempfile.TemporaryDirectory() as tmp:
            # .env HAS a token, but the credential service has NO DB store
            h = make_manager(token_values={"DISCORD_TOKEN_ALPHA": "env-token"})
            service = CredentialService(None, key_path=Path(tmp) / "k.key")
            h.manager._credential_service = service
            await h.manager.add_account("alpha")
            row = (await h.manager.accounts())[0]
            self.assertEqual(row["credential"], "missing")  # NOT "env"
            await h.manager.stop("alpha") if h.manager.get(
                "alpha"
            ).runtime.status.value == "running" else None

    async def test_auth_error_classified_invalid_credential(self) -> None:
        from core.errors import AccountError

        h = make_manager()
        await h.manager.add_account("alpha")
        h.manager._client_factory = RejectingFactory()
        with self.assertRaises(AccountError):
            await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime
        self.assertEqual(runtime.status, AccountStatus.INVALID_CREDENTIAL)
        await h.collector.wait_for(AccountFailed, 1)

    async def test_remove_cascades_credential_and_cooldowns(self) -> None:
        class FakeCooldowns:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            async def delete_account(self, account: str) -> int:
                self.deleted.append(account)
                return 1

        import tempfile
        from pathlib import Path

        from core.credential_service import CredentialService
        from database.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(":memory:")
            await db.connect()
            try:
                h = make_manager()
                cooldowns = FakeCooldowns()
                service = CredentialService(db.credentials, key_path=Path(tmp) / "k.key")
                await service.store("alpha", "tok")
                h.manager._cooldowns = cooldowns
                h.manager._credential_service = service
                await h.manager.add_account("alpha")
                await h.manager.remove_account("alpha")
                self.assertEqual(cooldowns.deleted, ["alpha"])
                self.assertEqual(service.status("alpha"), "missing")
                self.assertIsNone(await db.credentials.load("alpha"))
            finally:
                await db.close()


class _RemovalWindowPlugins:
    """Delegating plugin-manager double: ``deactivate_account`` submits one
    farewell task when the account is already UNREGISTERED — exactly the
    still-active-plugin submit window inside remove_account that used to
    resurrect the removed account's scheduler lane
    (probe_lane_resurrect.py)."""

    def __init__(self, inner: Any, manager: AccountManager, scheduler: TaskScheduler) -> None:
        self._inner = inner
        self._manager = manager
        self._scheduler = scheduler
        self.farewell_tasks: list[Any] = []

    async def deactivate_account(self, account: str) -> None:
        if account not in self._manager._accounts:  # removal window
            with contextlib.suppress(Exception):
                self.farewell_tasks.append(
                    self._scheduler.submit(
                        account, "roll", "message", channel_id=100, content="farewell"
                    )
                )
        await self._inner.deactivate_account(account)  # always None in practice

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)


class RemoveLaneIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Round-3 (probe_lane_resurrect.py): remove_account must deactivate the
    account's plugins BEFORE the scheduler lane cleanup, so a submit fired
    during the removal window (a farewell task from an on_disable hook)
    cannot re-create the removed account's parked lane — a worker that
    previously leaked until the global scheduler stop."""

    async def test_remove_leaves_no_resurrected_lane(self) -> None:
        h = make_manager(token_values={"DISCORD_TOKEN_GHOST": "tok-ghost"})
        scheduler = _real_scheduler(h.bus, h.manager.send_hook)
        farewell = _RemovalWindowPlugins(h.plugins, h.manager, scheduler)
        h.manager._scheduler = scheduler
        h.manager._plugin_manager = farewell
        try:
            await h.manager.add_account("ghost")
            await h.manager.start("ghost")
            first = scheduler.submit("ghost", "roll", "message", channel_id=100, content="one")

            def first_done() -> bool:
                first_row = scheduler.get(first.id)
                return first_row is not None and first_row.status == STATUS_COMPLETED

            await _poll_until(first_done)
            await h.manager.stop("ghost")
            # stop-time deactivation is NOT the removal window: no submit
            self.assertEqual(farewell.farewell_tasks, [])
            self.assertIn("ghost", scheduler._lanes)  # lane persists by design

            await h.manager.remove_account("ghost")
            # a farewell task WAS submitted inside the removal window...
            self.assertEqual(len(farewell.farewell_tasks), 1)
            task = farewell.farewell_tasks[0]
            await _poll_until(lambda: task.status in TERMINAL_STATUSES)
            # ...yet every lane bookkeeping entry for the account is GONE
            # (no parked zombie lane until the global stop)
            self.assertNotIn("ghost", scheduler._lanes)
            self.assertNotIn("ghost", scheduler._lane_events)
            self.assertNotIn("ghost", scheduler._queues)
        finally:
            with contextlib.suppress(AccountError):  # already removed
                await h.manager.stop("ghost")
            await scheduler.stop()


class EmergencyStopIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Per-account estop through the manager paths with a REAL scheduler:
    a slow send in flight is driven terminal, the account stops, and a
    restart of the same account works immediately after."""

    async def test_estop_with_slow_task_in_flight(self) -> None:
        h = make_manager()
        scheduler = _real_scheduler(h.bus, h.manager.send_hook)
        h.manager._scheduler = scheduler
        try:
            await h.manager.add_account("alpha")
            await h.manager.start("alpha")
            client = h.manager.get("alpha").runtime.client
            original_send: Any = client.send_message

            async def slow_send(channel_id: int, content: str) -> DiscordMessage:
                await asyncio.sleep(0.4)  # the "slow task in flight"
                sent: DiscordMessage = await original_send(channel_id, content)
                return sent

            client.send_message = slow_send
            task = scheduler.submit(
                "alpha", "exporter", "message", channel_id=100, content="slow", timeout=10.0
            )

            def is_running() -> bool:
                row = scheduler.get(task.id)
                return row is not None and row.status == STATUS_RUNNING

            await _poll_until(is_running)

            out = h.manager.emergency_stop("alpha")
            self.assertTrue(out["stop_initiated"])
            self.assertEqual(out["cancelled_tasks"], 1)

            runtime = h.manager.get("alpha").runtime

            def is_terminal() -> bool:
                row = scheduler.get(task.id)
                return row is not None and row.status in TERMINAL_STATUSES

            await _poll_until(is_terminal, timeout=3.0)
            cancelled_row = scheduler.get(task.id)
            assert cancelled_row is not None
            self.assertEqual(cancelled_row.status, "cancelled")
            await _poll_until(lambda: runtime.status == AccountStatus.STOPPED, timeout=3.0)
            await h.collector.wait_for(AccountDisconnected, 1)
            self.assertIsNone(runtime.client)

            # restart works right away, and sends flow again (fast client)
            await h.manager.start("alpha")
            self.assertEqual(runtime.status, AccountStatus.RUNNING)
            fresh = scheduler.submit(
                "alpha", "exporter", "message", channel_id=100, content="after"
            )

            def fresh_done() -> bool:
                fresh_row = scheduler.get(fresh.id)
                return fresh_row is not None and fresh_row.status == STATUS_COMPLETED

            await _poll_until(fresh_done)
            await h.manager.stop("alpha")
        finally:
            with contextlib.suppress(AccountError):
                await h.manager.stop("alpha")
            await scheduler.stop()


class RejectingFactory:
    """Builds clients whose connect() fails like a rejected token."""

    def __init__(self) -> None:
        self.clients: list[FakeRejectingClient] = []

    def __call__(self, name: str, token: str) -> FakeRejectingClient:
        client = FakeRejectingClient()
        client.token = token
        self.clients.append(client)
        return client


class FakeRejectingClient:
    """A client whose connect() fails like a rejected token."""

    def __init__(self) -> None:
        self.connected = False
        self.token = ""
        self.user_id: int | None = None
        self.latency_ms: float | None = None

    async def connect(self) -> None:
        from core.errors import AccountError

        raise AccountError("live client connected without a user (bad token?)")

    async def disconnect(self) -> None:
        self.connected = False

    def cached_snapshot(self) -> dict[str, Any]:
        return {"available": False}


class CachePortTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_invalidation_flow(self) -> None:
        from core.account_manager import DiscordCache
        from core.errors import TargetGone as TGone

        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())
        cache.validate_channel(100)
        seen: list[tuple[str, int, str]] = []
        cache.on_invalidate(lambda acct, cid, reason: seen.append((acct, cid, reason)))
        self.assertTrue(cache.invalidate_channel(100, "deleted by mod"))
        with self.assertRaises(TGone):
            cache.validate_channel(100)
        self.assertEqual(seen, [("alpha", 100, "deleted by mod")])
        self.assertFalse(cache.invalidate_channel(555555, "unknown"))
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["channels_total"], 3)
        self.assertEqual(snapshot["dms"], 1)
        self.assertEqual(snapshot["invalidated"], [100])

    async def test_cache_record_dm_resurrects_invalidated(self) -> None:
        from core.account_manager import DiscordCache

        cache = DiscordCache("alpha")
        cache.record_dm(5000, 999)
        self.assertEqual(cache.dm_channel_for(999), 5000)
        cache.invalidate_channel(5000, "closed")
        self.assertIsNone(cache.dm_channel_for(999))
        cache.record_dm(5000, 999)  # re-opened -> usable again
        self.assertEqual(cache.dm_channel_for(999), 5000)

    async def test_describe_channels(self) -> None:
        from core.account_manager import DiscordCache

        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())
        self.assertEqual(cache.describe(100), "#general in Guildy")
        self.assertEqual(cache.describe(9001), "dm with 777")
        self.assertEqual(cache.describe(31337), "channel-31337")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class AttachCogReconcileTests(unittest.IsolatedAsyncioTestCase):
    """roadmap #4 round 2 (CodeRabbit pre-review): the attach flow
    RECONCILES hosted cogs with the assignment set — newly assigned COG
    plugins get their command face without a reconnect, removed plugins
    lose theirs, retained toolboxes rebind to fresh facades."""

    async def test_attach_hosts_unhosts_and_rebinds(self) -> None:
        from types import SimpleNamespace as NS

        from core.plugin_manager.cog_host import PluginCogHost
        from plugins.ping.plugin import PingCog

        h = make_manager()
        await h.manager.add_account("alpha", plugins=["ping"])
        host = PluginCogHost()
        h.plugins.cog_host = host

        # the plugin-manager view attach_account builds: ping (declares
        # COG) attached; "oldie" was hosted on an EARLIER assignment
        # and is gone from the current runtimes
        h.plugins._accounts = {
            "alpha": {"ping": NS(instance=NS(COG=PingCog))},
        }

        async def host_cog(account: str, plugin: str, bot: Any, paced: Any = None) -> Any:
            cog_class = h.plugins._accounts[account][plugin].instance.COG
            return await host.attach(bot, account, plugin, cog_class, paced=paced)

        async def unhost_cog(account: str, plugin: str) -> bool:
            return await host.detach(account, plugin)

        h.plugins.host_cog = host_cog
        h.plugins.unhost_cog = unhost_cog

        added: list[Any] = []
        removed: list[str] = []

        class _FakeBot:
            async def add_cog(self, cog: Any) -> None:
                added.append(type(cog).__name__)

            async def remove_cog(self, name: str) -> None:
                removed.append(name)

        bot = _FakeBot()
        rt = h.manager.get("alpha")
        rt.client = NS(_client=bot)  # type: ignore[attr-defined]

        # the stale cog from the previous assignment set
        class _OldCog:
            def __init__(self, *, services: Any = None) -> None:
                self.services = services

        await host.attach(bot, "alpha", "oldie", _OldCog)
        self.assertEqual(host.hosted_plugins("alpha"), {"oldie"})

        await h.manager._attach_plugins("alpha")

        # hosted: ping's command face landed on the bot immediately
        self.assertEqual(host.hosted_plugins("alpha"), {"ping"})
        self.assertEqual(added, ["_OldCog", "PingCog"])
        self.assertEqual(removed, ["_OldCog"])
        # the fresh facade is bound on the retained-new toolbox
        toolbox = host.services_for("alpha", "ping")
        self.assertIsNotNone(toolbox.paced)
