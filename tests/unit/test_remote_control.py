"""RemoteControl tests: mention-prefix management from Discord (fakes only)."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from core.credentials import Credentials
from core.event_bus import EventBus, MessageReceived
from core.priority import Priority
from core.remote_control import RemoteControl

OWNER = 1001
ALPHA_ID = 2001
STRANGER = 4242
CHANNEL = 500


class FakeScheduler:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, account, plugin, action, *, channel_id=None, user_id=None,
               content=None, priority=Priority.NORMAL, name=None, timeout=None,
               retries=0):
        self.submitted.append({
            "account": account, "plugin": plugin, "action": action,
            "channel_id": channel_id, "content": content, "priority": int(priority),
        })
        return SimpleNamespace(id=len(self.submitted))


class FakeAccounts:
    def __init__(self) -> None:
        self._accounts = {"alpha": SimpleNamespace(
            runtime=SimpleNamespace(user_id=ALPHA_ID))}

    def get(self, name):
        return self._accounts.get(name)


class RaisingAccounts(FakeAccounts):
    def get(self, name):
        from core.errors import AccountError
        raise AccountError(f"unknown account {name!r}")


class FakeApi:
    def __init__(self) -> None:
        self.calls: list = []

    async def status(self):
        self.calls.append("status")
        return {"uptime_s": 12.0, "tasks": {"queued": 0, "completed": 3},
                "accounts": [{"name": "alpha", "status": "running",
                              "latency_ms": 83.0, "profiles": []}],
                "warnings": []}

    async def accounts_list(self):
        return [{"name": "alpha", "status": "running", "profiles": ["copier"]}]

    async def plugins_state(self, account):
        return [{"name": "soulcopy", "enabled": True, "error_count": 0},
                {"name": "roll", "enabled": False, "error_count": 0}]

    async def plugins_set_enabled(self, account, plugin, enabled):
        self.last_set_enabled = (account, plugin, enabled)
        return {"account": account, "plugin": plugin, "enabled": enabled}

    async def ratelimit_policies(self):
        return [{"level": "global", "owner": None, "action": "message",
                 "interval": 2.0, "jitter": [0.1, 0.5]}]

    async def ratelimit_warnings(self):
        return []

    async def ratelimit_lane(self):
        return {"enabled": True, "prefixrate": 1.5, "paused": False,
                "waiting": 0, "grants": 4}

    async def tasks_list(self, status=None, account=None):
        return [{"id": 7, "account": "alpha", "plugin": "soulcopy",
                 "action": "message", "status": "queued"}]

    async def emergency_stop_all(self):
        self.calls.append("estop")
        return {"cancelled_tasks": 2}

    async def accounts_emergency_stop(self, name):
        return {"cancelled_tasks": 1}

    async def simulation_list(self):
        return [{"name": "soulcopy_basic", "description": "d"}]


def make_bridge(bus=None, cooldown=0.0, owner=OWNER):
    bus = bus or EventBus()
    scheduler = FakeScheduler()
    accounts = FakeAccounts()
    api = FakeApi()
    credentials = Credentials.from_values({"OWNER_USER_IDS": str(owner)})
    bridge = RemoteControl(
        bus, scheduler, accounts, lambda: api, credentials,
        reply_cooldown_s=cooldown,
    ).start()
    return bridge, bus, scheduler, api


def mention_message(content, author=OWNER, account="alpha", channel=CHANNEL):
    return MessageReceived(
        account=account, message_id=hash(content) % 10**6, channel_id=channel,
        author_id=author, content=content, dm=False,
    )


class TestRemoteControl(unittest.IsolatedAsyncioTestCase):
    async def test_owner_mention_status_gets_pipelined_reply(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> status"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(len(scheduler.submitted), 1)
        reply = scheduler.submitted[0]
        self.assertEqual(reply["plugin"], "control")
        self.assertEqual(reply["channel_id"], CHANNEL)
        self.assertEqual(reply["priority"], int(Priority.NORMAL))
        self.assertIn("uptime", reply["content"])

    async def test_bang_mention_form_accepted(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message(f"<@!{ALPHA_ID}> lane"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(len(scheduler.submitted), 1)
        self.assertIn("send lane", scheduler.submitted[0]["content"])

    async def test_non_owner_ignored_completely(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> estop", author=STRANGER))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(scheduler.submitted, [])  # not even an error reply

    async def test_foreign_mention_ignored(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message("<@999999> status"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(scheduler.submitted, [])

    async def test_unknown_command_replies_with_hint(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> frobnicate"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertIn("unknown command", scheduler.submitted[0]["content"])

    async def test_estop_global(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> estop"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertIn("GLOBAL EMERGENCY STOP", scheduler.submitted[0]["content"])

    async def test_plugin_toggle_and_usage_errors(self) -> None:
        bridge, bus, scheduler, api = make_bridge()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> plugins alpha roll on"))
        await asyncio.sleep(0.05)
        self.assertEqual(getattr(api, "last_set_enabled", None),
                         ("alpha", "roll", True))
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> plugins"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertIn("usage:", scheduler.submitted[-1]["content"])

    async def test_reply_cooldown_self_throttles(self) -> None:
        bridge, bus, scheduler, _ = make_bridge(cooldown=5.0)
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> status"))
        await asyncio.sleep(0.02)
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> status"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(len(scheduler.submitted), 1)  # second one held

    async def test_long_reply_truncated(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> accounts list"))
        await asyncio.sleep(0.05)
        bridge.stop()
        for entry in scheduler.submitted:
            self.assertLessEqual(len(entry["content"]), 1900)

    async def test_broken_api_replies_with_error(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()

        class ExplodingApi(FakeApi):
            async def status(self):
                raise RuntimeError("boom")

        bridge._api_provider = lambda: ExplodingApi()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> status"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertIn("error: RuntimeError: boom", scheduler.submitted[0]["content"])

    async def test_help_lists_commands(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}>"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertIn("commands:", scheduler.submitted[0]["content"])

    async def test_mention_for_removed_account_ignored(self) -> None:
        bus = EventBus()
        scheduler = FakeScheduler()
        api = FakeApi()
        credentials = Credentials.from_values({"OWNER_USER_IDS": str(OWNER)})
        bridge = RemoteControl(
            bus, scheduler, RaisingAccounts(), lambda: api, credentials,
        ).start()
        bus.publish_nowait(
            mention_message(f"<@{ALPHA_ID}> status", account="ghost"))
        await bus.drain(timeout=2)  # F5-c: dispatch completes on drain
        bridge.stop()
        self.assertEqual(scheduler.submitted, [])

    async def test_partial_mention_id_not_matched(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bus.publish_nowait(mention_message("<@200> status"))
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(scheduler.submitted, [])

    async def test_malformed_owner_ids_rejected_quietly(self) -> None:
        bus = EventBus()
        scheduler = FakeScheduler()
        api = FakeApi()
        credentials = Credentials.from_values({"OWNER_USER_IDS": "abc,def"})
        bridge = RemoteControl(
            bus, scheduler, FakeAccounts(), lambda: api, credentials,
        ).start()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> status"))
        await bus.drain(timeout=2)
        bridge.stop()
        self.assertEqual(scheduler.submitted, [])

    # --------------------------------------------- round-2 audit coverage
    async def test_string_author_id_fail_closed(self) -> None:
        """A producer echoing author_id as a STRING must not pass the gate
        ('1001' is not in {1001}) — the owner check is int-membership."""
        bridge, bus, scheduler, api = make_bridge()
        bus.publish_nowait(
            mention_message(f"<@{ALPHA_ID}> status", author=str(OWNER)))
        await bus.drain(timeout=2)
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(scheduler.submitted, [])
        self.assertEqual(api.calls, [])

    async def test_two_mentions_in_one_message_is_one_command(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        await asyncio.wait_for(bus.publish_nowait(
            mention_message(f"<@{ALPHA_ID}> status <@{ALPHA_ID}> estop")), 2)
        await asyncio.sleep(0.05)
        bridge.stop()
        # parsed as 'status' + junk args: one reply, no crash, no estop run
        self.assertEqual(len(scheduler.submitted), 1)
        self.assertIn("takes no arguments", scheduler.submitted[0]["content"])

    async def test_self_mention_when_account_is_owner_runs_once(self) -> None:
        bridge, bus, scheduler, _ = make_bridge(owner=ALPHA_ID)
        await asyncio.wait_for(bus.publish_nowait(
            mention_message(f"<@{ALPHA_ID}> status", author=ALPHA_ID)), 2)
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(len(scheduler.submitted), 1)

    async def test_flood_executes_every_command_replies_bounded(self) -> None:
        """Command flood inside the cooldown window: EXECUTION is never
        throttled; the reply stream is bounded by the cooldown."""
        bridge, bus, scheduler, api = make_bridge(cooldown=0.3)
        for _ in range(6):
            await asyncio.wait_for(bus.publish_nowait(
                mention_message(f"<@{ALPHA_ID}> status")), 2)
            await asyncio.sleep(0.01)  # faster than the window
        await asyncio.sleep(0.2)
        bridge.stop()
        self.assertEqual(len(api.calls), 6)
        self.assertLessEqual(len(scheduler.submitted), 6)

    async def test_safety_command_runs_inside_cooldown_window(self) -> None:
        bridge, bus, scheduler, api = make_bridge(cooldown=60.0)
        await asyncio.wait_for(bus.publish_nowait(
            mention_message(f"<@{ALPHA_ID}> status")), 2)
        await asyncio.sleep(0.05)
        await asyncio.wait_for(bus.publish_nowait(
            mention_message(f"<@{ALPHA_ID}> estop")), 2)
        await asyncio.sleep(0.05)
        bridge.stop()
        self.assertEqual(api.calls, ["status", "estop"])  # estop RAN

    async def test_undeliverable_reply_is_contained(self) -> None:
        class DeadScheduler(FakeScheduler):
            def submit(self, *a, **kw):
                raise RuntimeError("scheduler stopped (estop)")
        bus = EventBus()
        scheduler = DeadScheduler()
        api = FakeApi()
        credentials = Credentials.from_values({"OWNER_USER_IDS": str(OWNER)})
        bridge = RemoteControl(
            bus, scheduler, FakeAccounts(), lambda: api, credentials,
        ).start()
        bus.publish_nowait(mention_message(f"<@{ALPHA_ID}> status"))
        await bus.drain(timeout=2)
        await asyncio.sleep(0.05)
        bridge.stop()

    async def test_start_is_idempotent_no_double_subscription(self) -> None:
        bridge, bus, scheduler, _ = make_bridge()
        bridge.start()  # second start must not duplicate the handler
        registered = sum(
            1 for handlers in bus._subs.values() for h in handlers
            if getattr(h, "__name__", "") == "_on_message")
        bridge.stop()
        self.assertEqual(registered, 1)


if __name__ == "__main__":
    unittest.main()
