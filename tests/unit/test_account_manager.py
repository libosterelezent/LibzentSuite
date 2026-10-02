"""Unit tests for core.account_manager: registry, lifecycle, supervisor,
send hook, details page shape and inbound audit."""
from __future__ import annotations

import asyncio
import unittest

from adapters.base import DiscordMessage
from core.account_manager import AccountStatus
from core.errors import AccountError, CredentialsError, ProfileError, SchedulerError, TargetGone
from core.event_bus import AccountConnected, AccountDisconnected, AccountFailed

from tests.unit.helpers import (
    FakeDetailsLimiter,
    default_snapshot,
    make_manager,
    make_task,
)

STATUS_KEYS = {"id", "name", "status", "user_id", "enabled", "sync_group",
               "profiles", "latency_ms", "uptime_s", "last_error",
               "credential", "plugins"}
DETAILS_KEYS = {"name", "status", "identity", "runtime", "plugins",
                "profiles", "rate_limit", "credential"}


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_account_happy_path(self) -> None:
        h = make_manager()
        out = await h.manager.add_account("alpha", sync_group="g1")
        self.assertEqual(out["name"], "alpha")
        self.assertEqual(out["status"], "created")
        self.assertEqual(set(out), STATUS_KEYS)
        self.assertIsNotNone(await h.repo.get("alpha"))          # persisted
        self.assertEqual(h.manager.names(), ["alpha"])     # memory
        self.assertEqual(h.plugins.attached, [("alpha", None)])  # unrestricted

    async def test_add_account_invalid_name(self) -> None:
        h = make_manager()
        for bad in ("", "ALPHA", "a b", "-x", "x" * 33, 5, None):
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

    async def test_add_with_profiles_assigns_and_gates(self) -> None:
        h = make_manager()
        await h.add_profile("basic", "soulcopy")
        out = await h.manager.add_account("alpha", profiles=["basic"])
        self.assertEqual(out["profiles"], ["basic"])
        self.assertEqual(h.plugins.attached[-1], ("alpha", {"soulcopy"}))

    async def test_add_with_unknown_profile_rejected_before_persist(self) -> None:
        h = make_manager()
        with self.assertRaises(ProfileError):
            await h.manager.add_account("alpha", profiles=["nope"])
        self.assertEqual(h.manager.names(), [])
        self.assertEqual(len(h.repo.records), 0)

    async def test_get_unknown_and_names_sorted(self) -> None:
        h = make_manager()
        await h.manager.add_account("beta")
        await h.manager.add_account("alpha")
        self.assertEqual(h.manager.names(), ["alpha", "beta"])
        self.assertEqual(h.manager.get("beta").record.name, "beta")
        with self.assertRaises(AccountError):
            h.manager.get("ghost")
        with self.assertRaises(AccountError):  # unhashable dashboard payload
            h.manager.get({"name": "alpha"})

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
            {"name": "soulcopy", "enabled": True, "settings": {"x": 1},
             "last_error": None, "error_count": 0},
            {"name": "roll", "enabled": False, "settings": {},
             "last_error": "boom", "error_count": 2},
        ]
        rows = await h.manager.accounts()
        self.assertEqual(rows[0]["plugins"]["soulcopy"]["enabled"], True)
        self.assertEqual(rows[0]["plugins"]["roll"]["error_count"], 2)
        self.assertEqual(rows[0]["plugins"]["roll"]["last_error"], "boom")

    async def test_plugin_states_failure_keeps_last_known(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        h.plugins.states_map["alpha"] = [
            {"name": "soulcopy", "enabled": True, "settings": {},
             "last_error": None, "error_count": 0},
        ]
        await h.manager.accounts()
        h.plugins.fail_states = True
        rows = await h.manager.accounts()
        self.assertIn("soulcopy", rows[0]["plugins"])  # last known kept

    async def test_set_enabled_persists(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        out = await h.manager.set_enabled("alpha", False)
        self.assertFalse(out["enabled"])
        self.assertFalse((await h.repo.get("alpha")).enabled)
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
            {"name": "soulcopy", "enabled": True, "settings": {"k": 1},
             "last_error": None, "error_count": 0},
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
        services = factory("soulcopy")
        self.assertEqual((services.account, services.plugin), ("alpha", "soulcopy"))
        # cache ingested from the client's READY snapshot
        cache = h.manager.get("alpha").runtime.cache
        cache.validate_channel(100)  # would raise TargetGone if not ingested
        self.assertEqual(cache.dms(), 1)
        # supervisor running (reconnect enabled by default settings)
        self.assertIsNotNone(h.manager.get("alpha").runtime.supervisor)
        await h.manager.stop("alpha")

    async def test_start_resolves_token_by_convention(self) -> None:
        h = make_manager(token_values={"DISCORD_TOKEN_ALPHA": "tok-alpha"})
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        self.assertEqual(h.factory.clients[0].token, "tok-alpha")
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
        self.assertEqual(h.manager.get("alpha").runtime.status,
                         AccountStatus.STOPPED)

    async def test_stop_never_started_account(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        out = await h.manager.stop("alpha")
        self.assertEqual(out["status"], "stopped")
        await self._drain()
        self.assertEqual(h.collector.of(AccountDisconnected), [])  # never live

    async def test_start_all_skips_disabled_and_collects_failures(self) -> None:
        # only alpha and gamma have stored credentials; beta does not
        h = make_manager(token_values={
            "DISCORD_TOKEN_ALPHA": "tok-alpha",
            "DISCORD_TOKEN_GAMMA": "tok-gamma",
        })
        await h.manager.add_account("alpha")
        await h.manager.add_account("beta")
        await h.manager.add_account("gamma")
        await h.manager.set_enabled("gamma", False)
        out = await h.manager.start_all()
        self.assertEqual(out["started"], ["alpha"])
        self.assertEqual(out["skipped"], ["gamma"])
        self.assertIn("beta", out["failed"])
        self.assertEqual(h.manager.get("beta").runtime.status,
                         AccountStatus.MISSING_CREDENTIAL)
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
            self.assertEqual(h.manager.get(name).runtime.status,
                             AccountStatus.STOPPED)

    async def test_emergency_stop_cancels_tasks_and_stops(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        for _ in range(3):
            h.scheduler.submit("alpha", "soulcopy", "message",
                               channel_id=100, content="x")
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
        cache.validate_channel(100)  # re-ingested on reconnect
        await h.manager.stop("alpha")
        self.assertEqual(len(h.factory.clients), 2)  # no extra reconnects

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
        self.assertFalse(runtime.lost.is_set())  # no stale wake-ups
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
        self.assertEqual(h.manager.get("alpha").runtime.status,
                         AccountStatus.RUNNING)
        await h.manager.stop("alpha")


def _fast_reconnect_settings():
    from tests.unit.helpers import FakeSettings

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
        self.assertEqual(runtime.cache.dm_channel_for(888), 9888)
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


class DetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_exact_shape_with_limiter(self) -> None:
        limiter = FakeDetailsLimiter()
        limiter.policies_list = [
            {"level": "global", "owner": None, "action": "message",
             "interval": 3.0, "jitter": [0.2, 0.8]},
            {"level": "account", "owner": "alpha", "action": "message",
             "interval": 5.0, "jitter": [0.0, 0.0]},
            {"level": "global", "owner": None, "action": "dm",
             "interval": 4.0, "jitter": [0.1, 0.3]},
        ]
        h = make_manager(limiter=limiter)
        await h.add_profile("basic", "soulcopy")
        await h.manager.add_account("alpha", profiles=["basic"])
        h.plugins.states_map["alpha"] = [
            {"name": "soulcopy", "enabled": True, "settings": {},
             "last_error": None, "error_count": 0},
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
        self.assertEqual(set(d["runtime"]),
                         {"cached_guilds", "cached_channels", "cached_dms", "cache_source"})
        self.assertEqual(d["runtime"]["cached_guilds"], 1)
        self.assertEqual(d["runtime"]["cached_channels"], 3)  # 100, 101 + dm 9001
        self.assertEqual(d["runtime"]["cached_dms"], 1)
        self.assertEqual(d["runtime"]["cache_source"], "client cache (READY)")
        self.assertEqual(d["plugins"], [{"name": "soulcopy", "enabled": True}])
        self.assertEqual(d["profiles"], ["basic"])
        self.assertEqual(d["rate_limit"], {
            "message": {"interval": 5.0, "jitter": [0.0, 0.0],
                        "source": "global, account:alpha"},
            "dm": {"interval": 4.0, "jitter": [0.1, 0.3],
                   "source": "inherited from global"},
        })
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
            "soulcopy": {"enabled": False, "last_error": None, "error_count": 0},
        }
        d = await h.manager.details("alpha")
        self.assertEqual(d["plugins"], [{"name": "soulcopy", "enabled": False}])

    async def test_details_unknown_account(self) -> None:
        h = make_manager()
        with self.assertRaises(AccountError):
            await h.manager.details("ghost")

    async def test_details_reflects_profile_assigned_after_add(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.add_profile("basic", "soulcopy")
        await h.profile_manager.assign("alpha", "basic")
        d = await h.manager.details("alpha")  # record refreshed from the repo
        self.assertEqual(d["profiles"], ["basic"])


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_messages_are_audited(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        client = h.manager.get("alpha").runtime.client
        client.receive(DiscordMessage(
            id=1, channel_id=100, author_id=42, content="x" * 100,
        ))
        client.receive(DiscordMessage(
            id=2, channel_id=9001, author_id=777, content="dm\nline", dm=True,
        ))
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
            def record(self, **kwargs):
                raise RuntimeError("audit backend down")

        manager = h.manager
        manager._audit = ExplodingAudit()  # type: ignore[attr-defined]
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
                h = make_manager(token_values={
                    "DISCORD_TOKEN_ALPHA": "env-token",
                })
                service = CredentialService(
                    db.credentials, key_path=Path(tmp) / "k.key"
                )
                await service.store("alpha", "db-token")
                h.manager._credential_service = service  # type: ignore[attr-defined]
                await h.manager.add_account("alpha")
                await h.manager.start("alpha")
                # DB credential wins over the legacy env value
                self.assertEqual(h.factory.clients[0].token, "db-token")
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
            h.manager._credential_service = service  # type: ignore[attr-defined]
            await h.manager.add_account("alpha")
            row = (await h.manager.accounts())[0]
            self.assertEqual(row["credential"], "missing")  # NOT "env"
            await h.manager.stop("alpha") if h.manager.get("alpha").runtime.status.value == "running" else None

    async def test_auth_error_classified_invalid_credential(self) -> None:
        from core.errors import AccountError

        h = make_manager()
        await h.manager.add_account("alpha")
        h.manager._client_factory = RejectingFactory()  # type: ignore[attr-defined]
        with self.assertRaises(AccountError):
            await h.manager.start("alpha")
        runtime = h.manager.get("alpha").runtime
        self.assertEqual(runtime.status, AccountStatus.INVALID_CREDENTIAL)
        await h.collector.wait_for(AccountFailed, 1)

    async def test_remove_cascades_credential_and_cooldowns(self) -> None:
        class FakeCooldowns:
            def __init__(self) -> None:
                self.deleted: list = []

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
                service = CredentialService(
                    db.credentials, key_path=Path(tmp) / "k.key"
                )
                await service.store("alpha", "tok")
                h.manager._cooldowns = cooldowns  # type: ignore[attr-defined]
                h.manager._credential_service = service  # type: ignore[attr-defined]
                await h.manager.add_account("alpha")
                await h.manager.remove_account("alpha")
                self.assertEqual(cooldowns.deleted, ["alpha"])
                self.assertEqual(service.status("alpha"), "missing")
                self.assertIsNone(await db.credentials.load("alpha"))
            finally:
                await db.close()


class RejectingFactory:
    """Builds clients whose connect() fails like a rejected token."""

    def __init__(self) -> None:
        self.clients: list = []

    def __call__(self, name: str, token: str):
        client = FakeRejectingClient()
        client.token = token
        self.clients.append(client)
        return client


class FakeRejectingClient:
    """A client whose connect() fails like a rejected token."""

    def __init__(self) -> None:
        self.connected = False
        self.user_id = None
        self.latency_ms = None

    async def connect(self) -> None:
        from core.errors import AccountError

        raise AccountError("live client connected without a user (bad token?)")

    async def disconnect(self) -> None:
        self.connected = False

    def cached_snapshot(self):
        return {"available": False}


class CachePortTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_invalidation_flow(self) -> None:
        from core.account_manager import DiscordCache
        from core.errors import TargetGone as TGone

        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())
        cache.validate_channel(100)
        seen = []
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
