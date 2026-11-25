"""Unit tests for core.account_manager.services: task-ack sends, waits,
collect idle-timeout semantics, cache-first DMs and passthrough helpers."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from core.account_manager import DiscordCache
from core.account_manager.services import AccountPluginServices, TaskAckRegistry
from core.credentials import Credentials
from core.errors import AccountError, SchedulerError, ServerRateLimit, TargetGone
from core.event_bus import EventBus, MessageReceived, TaskCancelled, TaskCompleted, TaskFailed
from core.priority import Priority
from core.task_scheduler import TaskScheduler
from tests.unit.helpers import FakeClient, FakeScheduler, default_snapshot, make_manager

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from core.cooldowns import CooldownService
    from core.credential_service import CredentialService
    from core.plugin_state import PluginStateService
    from core.task_scheduler import Task

_TEST_LOG = logging.getLogger("tests.account_services")


class _InstantLimiter:
    """Duck-typed limiter for the real-scheduler tests (instant grants)."""

    class _Grant:
        wait_s: float = 0.0
        jitter_s: float = 0.0

    async def acquire(
        self, action: str, account: str | None, plugin: str | None, priority: int
    ) -> _InstantLimiter._Grant:
        return self._Grant()

    def report_server_limit(self, *args: Any, **kwargs: Any) -> None:
        pass


def make_services(
    *,
    bus: EventBus | None = None,
    scheduler: Any = None,
    client: FakeClient | None = None,
    settings_dict: dict[str, Any] | None = None,
    ack_registry: TaskAckRegistry | None = None,
) -> AccountPluginServices:
    """Direct AccountPluginServices construction with sensible defaults."""
    cache = DiscordCache("alpha")
    cache.ingest_snapshot(default_snapshot())
    return AccountPluginServices(
        "alpha",
        "exporter",
        bus=bus or EventBus(),
        scheduler=scheduler or FakeScheduler(),
        credentials=Credentials.from_values({"PRIMARY_BOT_ID": "123"}),
        cache=cache,
        settings_dict=settings_dict if settings_dict is not None else {"k": 1},
        ack_registry=ack_registry,
        client_getter=lambda: client,
    )


class SendAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_resolves_on_task_completed(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)  # submit + future registration happened
        task = scheduler.submitted[0]
        self.assertEqual(
            (task.account, task.plugin, task.action, task.channel_id, task.content),
            ("alpha", "exporter", "message", 100, "hi"),
        )
        self.assertEqual(task.priority, Priority.LOW)
        await bus.publish(
            TaskCompleted(
                account="alpha",
                task_id=task.id,
                duration_s=0.2,
                summary="sent",
            )
        )
        self.assertIsNone(await asyncio.wait_for(pending, 1.0))  # ack-only return

    async def test_send_dm_submits_dm_action(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_dm(777, "psst"))
        await asyncio.sleep(0.02)
        task = scheduler.submitted[0]
        self.assertEqual((task.action, task.user_id, task.content), ("dm", 777, "psst"))
        await bus.publish(
            TaskCompleted(account="alpha", task_id=task.id, duration_s=0.1, summary="dm sent")
        )
        self.assertIsNone(await asyncio.wait_for(pending, 1.0))

    async def test_send_message_raises_on_task_failed(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        task_id = scheduler.submitted[0].id
        await bus.publish(TaskFailed(account="alpha", task_id=task_id, error="channel gone"))
        with self.assertRaises(SchedulerError) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertIn("channel gone", str(ctx.exception))

    async def test_flow_blocked_survives_the_ack_seam(self) -> None:
        """REGRESSION (channel_gone debug round): a task failing with
        TargetGone must re-raise TargetGone at the waiting plugin — NOT an
        opaque SchedulerError — so the flow-abort contract (exporter's
        catch-all) actually fires."""
        from core.errors import TargetGone

        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        task_id = scheduler.submitted[0].id
        await bus.publish(
            TaskFailed(
                account="alpha",
                task_id=task_id,
                error="TargetGone: [] flow blocked: channel 500 vanished",
                error_kind="TargetGone",
            )
        )
        with self.assertRaises(TargetGone) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertEqual(ctx.exception.account, "alpha")
        self.assertIn("channel 500 vanished", ctx.exception.reason)

    async def test_send_message_raises_on_task_cancelled(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        await bus.publish(TaskCancelled(account="alpha", task_id=scheduler.submitted[0].id))
        with self.assertRaises(SchedulerError) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertIn("cancelled", str(ctx.exception))

    async def test_send_message_times_out_without_completion_event(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        services.task_ack_timeout = 0.05  # configurable attribute
        _registry = services._ack_registry if services._ack_registry else None
        with self.assertRaises(SchedulerError) as ctx:
            await services.send_message(100, "hi")
        self.assertIn("did not complete", str(ctx.exception))
        # the abandoned future must not linger in the registry
        self.assertEqual(services._acks.pending(), [])

    async def test_submit_failure_propagates_immediately(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        scheduler.fail_submit = True
        services = make_services(bus=bus, scheduler=scheduler)
        with self.assertRaises(SchedulerError):
            await services.send_message(100, "hi")
        self.assertEqual(services._acks.pending(), [])

    async def test_duplicate_live_wait_for_same_task_rejected(self) -> None:
        """Round-2 audit: overwriting a live waiter's future would orphan it
        (full-timeout hang + a spurious cancel of the shared task)."""
        bus = EventBus()
        registry = TaskAckRegistry(bus)
        first = asyncio.create_task(registry.wait_for_task(9, timeout=1.0))
        await asyncio.sleep(0.02)
        with self.assertRaises(SchedulerError) as ctx:
            await registry.wait_for_task(9, timeout=1.0)
        self.assertIn("already being awaited", str(ctx.exception))
        await bus.publish(TaskCompleted(account="alpha", task_id=9, summary="ok"))
        self.assertEqual(await asyncio.wait_for(first, 1.0), "ok")
        self.assertEqual(registry.pending(), [])

    async def test_rewait_after_timeout_is_allowed(self) -> None:
        """A timed-out wait pops its future: a fresh wait on the same id
        must register cleanly (the guard only rejects LIVE duplicates)."""
        bus = EventBus()
        registry = TaskAckRegistry(bus)

        async def never_finishes() -> None:
            await asyncio.sleep(10)

        # kept referenced for the life of the test: a live in-flight task
        # while the registry times out and re-waits
        parked: list[asyncio.Task[None]] = []
        parked.append(asyncio.create_task(never_finishes()))
        with self.assertRaises(SchedulerError):
            await registry.wait_for_task(4, timeout=0.02)
        self.assertEqual(registry.pending(), [])
        second = asyncio.create_task(registry.wait_for_task(4, timeout=1.0))
        await asyncio.sleep(0.02)
        await bus.publish(TaskCompleted(account="alpha", task_id=4, summary="ok"))
        self.assertEqual(await asyncio.wait_for(second, 1.0), "ok")

    async def test_shared_registry_serves_many_services(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        registry = TaskAckRegistry(bus)
        one = make_services(bus=bus, scheduler=scheduler, ack_registry=registry)
        two = make_services(bus=bus, scheduler=scheduler, ack_registry=registry)
        p1 = asyncio.create_task(one.send_message(100, "a"))
        p2 = asyncio.create_task(two.send_message(101, "b"))
        await asyncio.sleep(0.02)
        await bus.publish(
            TaskCompleted(account="alpha", task_id=scheduler.submitted[0].id, summary="ok")
        )
        await bus.publish(
            TaskCompleted(account="alpha", task_id=scheduler.submitted[1].id, summary="ok")
        )
        await asyncio.wait_for(asyncio.gather(p1, p2), 1.0)
        self.assertEqual(registry.pending(), [])

    async def test_manager_services_for_uses_shared_registry(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        h.plugins.states_map["alpha"] = [
            {
                "name": "exporter",
                "enabled": True,
                "settings": {"k": 9},
                "last_error": None,
                "error_count": 0,
            },
        ]
        services = h.manager.services_for("alpha", "exporter")
        self.assertIs(h.manager.services_for("alpha", "exporter"), services)
        self.assertEqual(services.settings, {"k": 9})  # seeded from states()
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        await h.bus.publish(
            TaskCompleted(account="alpha", task_id=h.scheduler.submitted[0].id, summary="ok")
        )
        self.assertIsNone(await asyncio.wait_for(pending, 1.0))


class LiveSettingsTests(unittest.IsolatedAsyncioTestCase):
    """REGRESSION (isolation finding): services.settings must be the SAME
    live dict the plugin contexts read — a runtime set_setting change has
    to be visible through BOTH views, not a frozen-at-activation copy."""

    async def test_settings_live_view_shared_with_context(self) -> None:
        from tests.unit.test_plugin_manager import make_manager as make_pm

        manager, _bus, _ = make_pm()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", lambda _name: None)
        live = manager.live_settings("alpha", "spy")
        assert live is not None
        self.assertIsInstance(live, dict)
        # a runtime change through the manager mutates the LIVE dict...
        await manager.set_setting("alpha", "spy", "greeting", "changed")
        self.assertEqual(live["greeting"], "changed")
        # ...and states() still renders the new value (display copy)
        states = {s["name"]: s for s in manager.states("alpha")}
        self.assertEqual(states["spy"]["settings"]["greeting"], "changed")


class WaitForMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_match_wins_and_filters_account(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)
        pending = asyncio.create_task(services.wait_for_message(lambda e: e.content == "yes", 1.0))
        await asyncio.sleep(0.02)
        await bus.publish(
            MessageReceived(
                account="alpha", message_id=1, channel_id=100, author_id=42, content="no"
            )
        )
        await bus.publish(
            MessageReceived(
                account="beta", message_id=2, channel_id=100, author_id=42, content="yes"
            )
        )
        await bus.publish(
            MessageReceived(
                account="alpha", message_id=3, channel_id=100, author_id=42, content="yes"
            )
        )
        event = await asyncio.wait_for(pending, 1.0)
        assert event is not None
        self.assertEqual(event.message_id, 3)  # fast return, right account

    async def test_timeout_returns_none(self) -> None:
        services = make_services()
        self.assertIsNone(await services.wait_for_message(lambda e: True, 0.05))

    async def test_nonpositive_timeout_rejected(self) -> None:
        services = make_services()
        # Any-typed: the None entry is a deliberately invalid timeout
        bad_timeouts: tuple[Any, ...] = (0, -1, None)
        for bad in bad_timeouts:
            with self.assertRaises(AccountError):
                await services.wait_for_message(lambda e: True, bad)

    async def test_raising_check_surfaces_as_account_error(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)

        def bad_check(_event: MessageReceived) -> NoReturn:
            raise ValueError("check exploded")

        pending = asyncio.create_task(services.wait_for_message(bad_check, 0.5))
        await asyncio.sleep(0.02)
        await bus.publish(
            MessageReceived(account="alpha", message_id=1, channel_id=100, author_id=1, content="x")
        )
        with self.assertRaises(AccountError) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertIn("check exploded", str(ctx.exception))


class CollectMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_window_resets_on_each_match(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)
        box: list[Any] = []
        pending = asyncio.create_task(services.collect_messages(lambda e: "page" in e.content, 0.6))

        def done(future: asyncio.Future[Any]) -> None:
            box.extend(future.result() if not future.exception() else [])

        pending.add_done_callback(done)
        for index in range(3):
            await asyncio.sleep(0.25)  # under the idle window (2.4x:
            # a stalled loop - 0.2s observed in-gate 2026-11-25 -
            # must not eat the window before the next match)
            await bus.publish(
                MessageReceived(
                    account="alpha",
                    message_id=index,
                    channel_id=9001,
                    author_id=777,
                    content=f"page {index}",
                    dm=True,
                )
            )
        collected = await asyncio.wait_for(pending, 5.0)
        self.assertEqual([e.content for e in collected], ["page 0", "page 1", "page 2"])

    async def test_quiet_sender_ends_collection(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)
        await bus.publish(
            MessageReceived(
                account="alpha", message_id=1, channel_id=100, author_id=1, content="hit"
            )
        )
        # subscribe-late: publish again AFTER the collector starts.
        # The subscription is OBSERVED, not slept past (review round
        # 5): the bus.subscribe seam fires an event when the
        # collector's handler registers, so the second publish
        # provably follows the live subscription - no margin can
        # race.
        subscribed = asyncio.Event()
        real_subscribe = bus.subscribe

        def traced_subscribe(*args: Any, **kwargs: Any) -> Any:
            subscribed.set()
            return real_subscribe(*args, **kwargs)

        bus.subscribe = traced_subscribe
        pending = asyncio.create_task(services.collect_messages(lambda e: e.content == "hit", 0.5))
        await subscribed.wait()
        await bus.publish(
            MessageReceived(
                account="alpha", message_id=2, channel_id=100, author_id=1, content="hit"
            )
        )
        collected = await asyncio.wait_for(pending, 5.0)
        self.assertEqual(len(collected), 1)

    async def test_nonpositive_idle_timeout_rejected(self) -> None:
        services = make_services()
        with self.assertRaises(AccountError):
            await services.collect_messages(lambda e: True, 0)

    async def test_raising_check_surfaces_as_account_error(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)

        def bad_check(_event: MessageReceived) -> NoReturn:
            raise KeyError("missing field")

        pending = asyncio.create_task(services.collect_messages(bad_check, 0.05))
        await asyncio.sleep(0.02)
        await bus.publish(
            MessageReceived(account="alpha", message_id=1, channel_id=100, author_id=1, content="x")
        )
        with self.assertRaises(AccountError) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertIn("missing field", str(ctx.exception))


class OpenDmTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_dm_never_touches_client(self) -> None:
        client = FakeClient()
        services = make_services(client=client)
        self.assertEqual(await services.open_dm(777), 9001)  # from READY snapshot
        self.assertEqual(client.open_dm_calls, [])

    async def test_cache_miss_opens_and_records(self) -> None:
        client = FakeClient()
        services = make_services(client=client)
        self.assertEqual(await services.open_dm(888), 9888)
        self.assertEqual(client.open_dm_calls, [888])
        self.assertEqual(await services.open_dm(888), 9888)  # cached now
        self.assertEqual(client.open_dm_calls, [888])  # opened exactly once

    async def test_no_client_raises_account_error(self) -> None:
        services = make_services(client=None)
        with self.assertRaises(AccountError):
            await services.open_dm(123)


class CooldownStateApiTests(unittest.IsolatedAsyncioTestCase):
    """The pair-scoped cooldown_* / state_* plugin API on the services
    facade: persistence, scoping, and the clear-error degradation when the
    services are not wired."""

    def _services(
        self,
        cooldowns: CooldownService | None = None,
        plugin_state: PluginStateService | None = None,
    ) -> AccountPluginServices:
        cache = DiscordCache("alpha")
        cache.ingest_snapshot(default_snapshot())
        return AccountPluginServices(
            "alpha",
            "exporter",
            bus=EventBus(),
            scheduler=FakeScheduler(),
            credentials=Credentials.from_values({"PRIMARY_BOT_ID": "123"}),
            cache=cache,
            settings_dict={},
            cooldowns=cooldowns,
            plugin_state=plugin_state,
        )

    async def test_cooldown_and_state_over_real_services(self) -> None:
        import tempfile
        from pathlib import Path

        from core.cooldowns import CooldownService
        from core.plugin_state import PluginStateService
        from database.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            try:
                cooldowns = CooldownService(db.cooldowns, time_fn=lambda: 1000.0)
                await cooldowns.restore()
                plugin_state = PluginStateService(db.kv)
                services = self._services(cooldowns, plugin_state)

                self.assertFalse(await services.cooldown_active("claim"))
                row = await services.cooldown_set("claim", 60.0, reason="grinding")
                self.assertAlmostEqual(row["expires_at"], 1060.0)
                self.assertTrue(await services.cooldown_active("claim"))
                self.assertAlmostEqual(await services.cooldown_remaining("claim"), 60.0)
                listed = await services.cooldown_list()
                self.assertEqual([r["cooldown_key"] for r in listed], ["claim"])
                self.assertTrue(await services.cooldown_clear("claim"))
                self.assertFalse(await services.cooldown_active("claim"))

                self.assertIsNone(await services.state_get("counter"))
                self.assertEqual(await services.state_get("counter", 5), 5)
                await services.state_set("counter", 41)
                self.assertEqual(await services.state_get("counter"), 41)
                await services.state_set("counter", 42)
                self.assertEqual(await services.state_get("counter"), 42)
                self.assertTrue(await services.state_delete("counter"))
                self.assertIsNone(await services.state_get("counter"))
            finally:
                await db.close()

    async def test_pair_scoping_isolation(self) -> None:
        from core.plugin_state import PluginStateService

        written: dict[str, Any] = {}

        class KvSpy:
            async def get(self, key: str) -> Any:
                return written.get(key)

            async def set(self, key: str, value: Any) -> None:
                written[key] = value

            async def delete(self, key: str) -> bool:
                return written.pop(key, None) is not None

        kv: Any = KvSpy()  # duck-typed KvRepository double
        plugin_state = PluginStateService(kv)
        alpha = self._services(plugin_state=plugin_state)
        beta = AccountPluginServices(
            "beta",
            "exporter",
            bus=EventBus(),
            scheduler=FakeScheduler(),
            credentials=Credentials.from_values({}),
            cache=DiscordCache("beta"),
            settings_dict={},
            plugin_state=plugin_state,
        )
        await alpha.state_set("k", "alpha-value")
        await beta.state_set("k", "beta-value")
        self.assertEqual(await alpha.state_get("k"), "alpha-value")
        self.assertEqual(await beta.state_get("k"), "beta-value")

    async def test_unwired_services_raise_clear_errors(self) -> None:
        from core.errors import PluginError

        services = self._services()
        with self.assertRaises(PluginError) as ctx:
            await services.cooldown_set("k", 5)
        self.assertIn("cooldown service is not wired", str(ctx.exception))
        with self.assertRaises(PluginError) as ctx:
            await services.state_get("k")
        self.assertIn("plugin-state service is not wired", str(ctx.exception))


class PassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_channel_raises_target_gone(self) -> None:
        services = make_services()
        services.validate_channel(100)  # cached -> fine
        with self.assertRaises(TargetGone):
            services.validate_channel(424242)

    async def test_describe_channel_labels(self) -> None:
        services = make_services()
        self.assertEqual(services.describe_channel(100), "#general in Guildy")
        self.assertEqual(services.describe_channel(9001), "dm with 777")

    async def test_publish_reaches_bus_subscribers(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)
        seen: list[MessageReceived] = []
        bus.subscribe(MessageReceived, seen.append)
        services.publish(
            MessageReceived(account="alpha", message_id=9, channel_id=1, author_id=2, content="x")
        )
        await asyncio.sleep(0.02)  # fire-and-forget dispatch
        self.assertEqual(len(seen), 1)

    async def test_properties(self) -> None:
        settings_dict = {"live": True}
        services = make_services(settings_dict=settings_dict)
        self.assertEqual(services.account, "alpha")
        self.assertEqual(services.plugin, "exporter")
        self.assertEqual(services.credentials.primary_bot_id, 123)
        self.assertIs(services.settings, settings_dict)  # live reference kept
        settings_dict["live"] = False
        self.assertFalse(services.settings["live"])


class TransientTypingAcrossAckSeamTests(unittest.IsolatedAsyncioTestCase):
    """Round-3 (3-lens cross-validated): a terminal task failure caused by a
    ConnectionError must reach the waiting plugin as a TYPED
    ConnectionError — not an opaque SchedulerError the flow code has to
    substring-match English error text against."""

    @staticmethod
    def _scheduler(
        sender: Callable[[Task], Coroutine[Any, Any, None]], **kwargs: Any
    ) -> TaskScheduler:
        scheduler = TaskScheduler(EventBus(), _TEST_LOG, _InstantLimiter(), sender, **kwargs)
        scheduler.start()
        return scheduler

    async def test_real_connection_error_raises_connection_error(self) -> None:
        async def sender(task: Task) -> None:
            raise ConnectionError("discord unreachable")

        scheduler = self._scheduler(sender)
        try:
            bus = scheduler._bus
            registry = TaskAckRegistry(bus, scheduler)
            task = scheduler.submit(
                "alpha", "roll", "message", channel_id=1, content="x", retries=0
            )
            with self.assertRaises(ConnectionError) as ctx:
                await asyncio.wait_for(registry.wait_for_task(task.id, timeout=2.0), 2.5)
            self.assertIs(type(ctx.exception), ConnectionError)
            self.assertNotIsInstance(ctx.exception, SchedulerError)
            self.assertIn("discord unreachable", str(ctx.exception))
        finally:
            await scheduler.stop()

    async def test_timeout_error_mapped_by_kind(self) -> None:
        """Kind-map pin: a TaskFailed carrying error_kind="TimeoutError" is
        reconstructed as TimeoutError (duck-typed schedulers / future
        formats; the live scheduler renders timeouts as 'timed out …')."""
        bus = EventBus()
        registry = TaskAckRegistry(bus)
        future = registry.register(31)
        await bus.publish(
            TaskFailed(
                account="alpha",
                task_id=31,
                error="TimeoutError: gateway hung",
                error_kind="TimeoutError",
            )
        )
        with self.assertRaises(TimeoutError) as ctx:
            await asyncio.wait_for(future, 1.0)
        self.assertIn("gateway hung", str(ctx.exception))

    async def test_server_rate_limit_stays_retried_invisible(self) -> None:
        """The scheduler always retries ServerRateLimit at least once; when
        it finally goes terminal it stays an OPAQUE SchedulerError here
        (unchanged contract — plugins never see transport 429 churn)."""
        attempts = []

        async def sender(task: Task) -> None:
            attempts.append(task.id)
            raise ServerRateLimit(0.01)

        scheduler = self._scheduler(sender, retry_backoff=0.0, max_backoff=0.0)
        try:
            bus = scheduler._bus
            registry = TaskAckRegistry(bus, scheduler)
            task = scheduler.submit(
                "alpha", "roll", "message", channel_id=1, content="x", retries=0
            )
            with self.assertRaises(SchedulerError) as ctx:
                await asyncio.wait_for(registry.wait_for_task(task.id, timeout=2.0), 2.5)
            self.assertIn("server rate limit", str(ctx.exception))
            self.assertNotIsInstance(ctx.exception, ServerRateLimit)
            self.assertEqual(len(attempts), 2)  # retried once, then terminal
        finally:
            await scheduler.stop()

    async def test_plugin_send_sees_typed_connection_error(self) -> None:
        """The plugin-facing path: services.send_message raises the typed
        ConnectionError raised by the real pipeline (here: send_hook's
        not-running gate through the manager harness)."""
        h = make_manager()
        scheduler = TaskScheduler(h.bus, _TEST_LOG, _InstantLimiter(), h.manager.send_hook)
        scheduler.start()
        h.manager._scheduler = scheduler
        try:
            await h.manager.add_account("alpha")  # never started: not running
            services = h.manager.services_for("alpha", "exporter")
            services.task_ack_timeout = 2.0
            with self.assertRaises(ConnectionError) as ctx:
                await asyncio.wait_for(services.send_message(100, "hi"), 2.5)
            self.assertIs(type(ctx.exception), ConnectionError)
            self.assertIn("is not running", str(ctx.exception))
        finally:
            await scheduler.stop()


class CredentialKeyRotationTests(unittest.IsolatedAsyncioTestCase):
    """Investigator-11 gap (a): credentials stored under Fernet master key A
    are unreadable under key B — resolve must fail closed with a
    CredentialsError that names the master key (never plaintext, never a
    raw InvalidToken escaping the service boundary)."""

    async def test_rotated_master_key_fails_closed_with_clear_error(self) -> None:
        from cryptography.fernet import Fernet

        from core.credential_service import CredentialService
        from core.errors import CredentialsError
        from database.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            key_a = Path(tmp) / "a.key"
            key_b = Path(tmp) / "b.key"
            key_b.write_bytes(Fernet.generate_key())  # a DIFFERENT key
            db = Database(":memory:")
            await db.connect()
            try:
                service_a = CredentialService(db.credentials, key_path=key_a)
                await service_a.store("alpha", "tok-under-a")

                service_b = CredentialService(db.credentials, key_path=key_b)
                with self.assertRaises(CredentialsError) as ctx:
                    await service_b.resolve("alpha")
                self.assertIn("master key", str(ctx.exception))
                self.assertIn("alpha", str(ctx.exception))
            finally:
                await db.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class CredentialDeleteGenerationTests(unittest.IsolatedAsyncioTestCase):
    """Review #4: deletion generations are PER-ACCOUNT — deleting one
    account must not trigger another account's in-flight store
    tombstone (the service-wide counter made store('beta') delete its
    own freshly written ciphertext when alpha was deleted mid-write)."""

    def _service(self) -> CredentialService:
        import shutil
        import tempfile
        from pathlib import Path

        from core.credential_service import CredentialService

        tmp = tempfile.mkdtemp(prefix="sm-cred-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.repo = _PausingRepo()
        return CredentialService(self.repo, key_path=Path(tmp) / "k.key")

    async def test_unrelated_delete_does_not_tombstone_inflight_store(self) -> None:
        svc = self._service()
        await svc.store("alpha", "tok-alpha")
        self.repo.pause()
        store_task = asyncio.create_task(svc.store("beta", "tok-beta"))
        await self.repo.wait_paused()  # beta's store parked mid-write
        await svc.delete("alpha")  # UNRELATED account deleted
        self.repo.resume()
        await store_task
        # beta's ciphertext must SURVIVE (the old service-wide generation
        # made store() think beta was deleted and tombstone its row)
        self.assertEqual(await svc.resolve("beta"), "tok-beta")
        self.assertIsNone(await svc.resolve("alpha"))

    async def test_own_account_delete_still_tombstones(self) -> None:
        svc = self._service()
        self.repo.pause()
        store_task = asyncio.create_task(svc.store("alpha", "tok-alpha"))
        await self.repo.wait_paused()
        await svc.delete("alpha")  # the SAME account deleted
        self.repo.resume()
        await store_task
        self.assertIsNone(await svc.resolve("alpha"))


class _PausingRepo:
    """Repo double with a gate inside store(): a test parks an in-flight
    store while a delete commits, then resumes."""

    def __init__(self) -> None:
        self._rows: dict[str, bytes] = {}
        self._gate = asyncio.Event()
        self._gate.set()  # unpaused by default
        self._paused = asyncio.Event()

    def pause(self) -> None:
        self._gate.clear()
        self._paused.clear()

    async def wait_paused(self) -> None:
        await self._paused.wait()

    def resume(self) -> None:
        self._gate.set()

    async def store(self, account: str, ciphertext: bytes) -> None:
        if not self._gate.is_set():
            self._paused.set()  # signal parked WHILE still parked
            await self._gate.wait()  # release
        self._rows[account] = ciphertext

    async def load(self, account: str) -> bytes | None:
        if not self._gate.is_set():
            self._paused.set()
            await self._gate.wait()
        return self._rows.get(account)

    async def delete(self, account: str) -> bool:
        # deliberately NOT gated: the delete commits while a store is
        # parked — that interleaving is exactly what these tests probe
        return self._rows.pop(account, None) is not None

    async def accounts(self) -> list[str]:
        return sorted(self._rows)
