"""Unit tests for core.account_manager.services: task-ack sends, waits,
collect idle-timeout semantics, cache-first DMs and passthrough helpers."""
from __future__ import annotations

import asyncio
import unittest

from core.account_manager import DiscordCache
from core.account_manager.services import AccountPluginServices, TaskAckRegistry
from core.credentials import Credentials
from core.errors import AccountError, SchedulerError, TargetGone
from core.event_bus import EventBus, MessageReceived, TaskCancelled, TaskCompleted, TaskFailed
from core.priority import Priority

from tests.unit.helpers import FakeClient, FakeScheduler, default_snapshot, make_manager


def make_services(*, bus=None, scheduler=None, client=None, settings_dict=None,
                  ack_registry=None):
    """Direct AccountPluginServices construction with sensible defaults."""
    cache = DiscordCache("alpha")
    cache.ingest_snapshot(default_snapshot())
    return AccountPluginServices(
        "alpha", "soulcopy",
        bus=bus or EventBus(),
        scheduler=scheduler or FakeScheduler(),
        credentials=Credentials.from_values({"MUDAE_BOT_ID": "123"}),
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
            ("alpha", "soulcopy", "message", 100, "hi"),
        )
        self.assertEqual(task.priority, Priority.LOW)
        await bus.publish(TaskCompleted(
            account="alpha", task_id=task.id, duration_s=0.2, summary="sent",
        ))
        self.assertIsNone(await asyncio.wait_for(pending, 1.0))  # ack-only return

    async def test_send_dm_submits_dm_action(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_dm(777, "psst"))
        await asyncio.sleep(0.02)
        task = scheduler.submitted[0]
        self.assertEqual(
            (task.action, task.user_id, task.content), ("dm", 777, "psst")
        )
        await bus.publish(TaskCompleted(account="alpha", task_id=task.id,
                                        duration_s=0.1, summary="dm sent"))
        self.assertIsNone(await asyncio.wait_for(pending, 1.0))

    async def test_send_message_raises_on_task_failed(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        task_id = scheduler.submitted[0].id
        await bus.publish(TaskFailed(account="alpha", task_id=task_id,
                                     error="channel gone"))
        with self.assertRaises(SchedulerError) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertIn("channel gone", str(ctx.exception))

    async def test_flow_blocked_survives_the_ack_seam(self) -> None:
        """REGRESSION (channel_gone debug round): a task failing with
        TargetGone must re-raise TargetGone at the waiting plugin — NOT an
        opaque SchedulerError — so the flow-abort contract (soulcopy's
        catch-all) actually fires."""
        from core.errors import TargetGone

        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        task_id = scheduler.submitted[0].id
        await bus.publish(TaskFailed(
            account="alpha", task_id=task_id,
            error="TargetGone: [] flow blocked: channel 500 vanished",
            error_kind="TargetGone",
        ))
        with self.assertRaises(TargetGone) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertEqual(ctx.exception.account, "alpha")
        self.assertIn("channel 500 vanished", ctx.exception.reason)

    async def test_send_message_raises_on_task_cancelled(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        await bus.publish(TaskCancelled(
            account="alpha", task_id=scheduler.submitted[0].id
        ))
        with self.assertRaises(SchedulerError) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertIn("cancelled", str(ctx.exception))

    async def test_send_message_times_out_without_completion_event(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        services = make_services(bus=bus, scheduler=scheduler)
        services.task_ack_timeout = 0.05  # configurable attribute
        registry = services._ack_registry if services._ack_registry else None
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

    async def test_shared_registry_serves_many_services(self) -> None:
        bus, scheduler = EventBus(), FakeScheduler()
        registry = TaskAckRegistry(bus)
        one = make_services(bus=bus, scheduler=scheduler, ack_registry=registry)
        two = make_services(bus=bus, scheduler=scheduler, ack_registry=registry)
        p1 = asyncio.create_task(one.send_message(100, "a"))
        p2 = asyncio.create_task(two.send_message(101, "b"))
        await asyncio.sleep(0.02)
        await bus.publish(TaskCompleted(
            account="alpha", task_id=scheduler.submitted[0].id, summary="ok"))
        await bus.publish(TaskCompleted(
            account="alpha", task_id=scheduler.submitted[1].id, summary="ok"))
        await asyncio.wait_for(asyncio.gather(p1, p2), 1.0)
        self.assertEqual(registry.pending(), [])

    async def test_manager_services_for_uses_shared_registry(self) -> None:
        h = make_manager()
        h.manager.add_account("alpha")
        h.plugins.states_map["alpha"] = [
            {"name": "soulcopy", "enabled": True, "settings": {"k": 9},
             "last_error": None, "error_count": 0},
        ]
        services = h.manager.services_for("alpha", "soulcopy")
        self.assertIs(h.manager.services_for("alpha", "soulcopy"), services)
        self.assertEqual(services.settings, {"k": 9})  # seeded from states()
        pending = asyncio.create_task(services.send_message(100, "hi"))
        await asyncio.sleep(0.02)
        await h.bus.publish(TaskCompleted(
            account="alpha", task_id=h.scheduler.submitted[0].id, summary="ok"))
        self.assertIsNone(await asyncio.wait_for(pending, 1.0))


class WaitForMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_match_wins_and_filters_account(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)
        pending = asyncio.create_task(
            services.wait_for_message(lambda e: e.content == "yes", 1.0)
        )
        await asyncio.sleep(0.02)
        await bus.publish(MessageReceived(account="alpha", message_id=1,
                                          channel_id=100, author_id=42, content="no"))
        await bus.publish(MessageReceived(account="beta", message_id=2,
                                          channel_id=100, author_id=42, content="yes"))
        await bus.publish(MessageReceived(account="alpha", message_id=3,
                                          channel_id=100, author_id=42, content="yes"))
        event = await asyncio.wait_for(pending, 1.0)
        self.assertEqual(event.message_id, 3)  # fast return, right account

    async def test_timeout_returns_none(self) -> None:
        services = make_services()
        self.assertIsNone(await services.wait_for_message(lambda e: True, 0.05))

    async def test_nonpositive_timeout_rejected(self) -> None:
        services = make_services()
        for bad in (0, -1, None):
            with self.assertRaises(AccountError):
                await services.wait_for_message(lambda e: True, bad)

    async def test_raising_check_surfaces_as_account_error(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)

        def bad_check(_event):
            raise ValueError("check exploded")

        pending = asyncio.create_task(services.wait_for_message(bad_check, 0.5))
        await asyncio.sleep(0.02)
        await bus.publish(MessageReceived(account="alpha", message_id=1,
                                          channel_id=100, author_id=1, content="x"))
        with self.assertRaises(AccountError) as ctx:
            await asyncio.wait_for(pending, 1.0)
        self.assertIn("check exploded", str(ctx.exception))


class CollectMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_window_resets_on_each_match(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)
        box: list = []
        pending = asyncio.create_task(
            services.collect_messages(lambda e: "page" in e.content, 0.15)
        )

        def done(future):
            box.extend(future.result() if not future.exception() else [])

        pending.add_done_callback(done)
        for index in range(3):
            await asyncio.sleep(0.07)  # under the idle window
            await bus.publish(MessageReceived(
                account="alpha", message_id=index, channel_id=9001,
                author_id=777, content=f"page {index}", dm=True,
            ))
        collected = await asyncio.wait_for(pending, 2.0)
        self.assertEqual([e.content for e in collected],
                         ["page 0", "page 1", "page 2"])

    async def test_quiet_sender_ends_collection(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)
        await bus.publish(MessageReceived(account="alpha", message_id=1,
                                          channel_id=100, author_id=1, content="hit"))
        # subscribe-late: publish again AFTER the collector starts
        pending = asyncio.create_task(
            services.collect_messages(lambda e: e.content == "hit", 0.05)
        )
        await asyncio.sleep(0.02)
        await bus.publish(MessageReceived(account="alpha", message_id=2,
                                          channel_id=100, author_id=1, content="hit"))
        collected = await asyncio.wait_for(pending, 1.0)
        self.assertEqual(len(collected), 1)

    async def test_nonpositive_idle_timeout_rejected(self) -> None:
        services = make_services()
        with self.assertRaises(AccountError):
            await services.collect_messages(lambda e: True, 0)

    async def test_raising_check_surfaces_as_account_error(self) -> None:
        bus = EventBus()
        services = make_services(bus=bus)

        def bad_check(_event):
            raise KeyError("missing field")

        pending = asyncio.create_task(
            services.collect_messages(bad_check, 0.05)
        )
        await asyncio.sleep(0.02)
        await bus.publish(MessageReceived(account="alpha", message_id=1,
                                          channel_id=100, author_id=1, content="x"))
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
        self.assertEqual(client.open_dm_calls, [888])        # opened exactly once

    async def test_no_client_raises_account_error(self) -> None:
        services = make_services(client=None)
        with self.assertRaises(AccountError):
            await services.open_dm(123)


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
        seen = []
        bus.subscribe(MessageReceived, seen.append)
        services.publish(MessageReceived(account="alpha", message_id=9,
                                         channel_id=1, author_id=2, content="x"))
        await asyncio.sleep(0.02)  # fire-and-forget dispatch
        self.assertEqual(len(seen), 1)

    async def test_properties(self) -> None:
        settings_dict = {"live": True}
        services = make_services(settings_dict=settings_dict)
        self.assertEqual(services.account, "alpha")
        self.assertEqual(services.plugin, "soulcopy")
        self.assertEqual(services.credentials.mudae_bot_id, 123)
        self.assertIs(services.settings, settings_dict)  # live reference kept
        settings_dict["live"] = False
        self.assertFalse(services.settings["live"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
