"""Ported remote-control commands: framework management from Discord.

The RemoteControl mention bridge is retired — its allowlist (status,
accounts, plugins, ratelimit, lane, tasks, estop, simulation) now lives
as framework CommandSpecs (category "Framework") in the SelfCommandHub,
so these tests drive every ported verb through the REAL execution path:
``PluginManager.execute_self_command`` → handler → ControlAPI (faked) →
reply through the paced pipeline (faked services record the send).

The mention itself (@account status → Bot when_mentioned prefix → hub
dispatch) is covered in tests/unit/test_plugin_commands.py
(AdapterLayerTests) and tests/integration/test_full_stack.py exercises
the production bootstrap wiring end to end.
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.credentials import Credentials
from core.event_bus import EventBus
from core.plugin_manager import PluginManager
from core.plugin_manager.commands import COMMAND_HUB, MAX_REPLY_CHARS
from core.settings_manager import SettingsManager

OWNER = 1001
STRANGER = 4242
CHANNEL = 500
GUILD = 7

LOG = logging.getLogger("test-remote-control")


class FakeApi:
    """ControlAPI double: canned replies + a call journal."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def status(self):
        self.calls.append(("status",))
        return {"uptime_s": 12.0, "tasks": {"queued": 0, "completed": 3},
                "accounts": [{"name": "alpha", "status": "running",
                              "latency_ms": 83.0, "profiles": []}],
                "warnings": []}

    async def accounts_list(self):
        self.calls.append(("accounts_list",))
        return [{"name": "alpha", "status": "running", "profiles": ["copier"]}]

    async def accounts_info(self, name):
        self.calls.append(("accounts_info", name))
        return {"name": name, "status": {"state": "running",
                                         "latency_ms": 83.0,
                                         "uptime_s": 45.0},
                "runtime": {"cached_guilds": 2, "cached_channels": 10,
                            "cached_dms": 1}}

    async def accounts_start(self, name, surface="api"):
        self.calls.append(("accounts_start", name, surface))
        return {"started": name}

    async def accounts_stop(self, name, surface="api"):
        self.calls.append(("accounts_stop", name, surface))
        return {"stopped": name}

    async def plugins_state(self, account):
        return [{"name": "soulcopy", "enabled": True, "error_count": 0},
                {"name": "roll", "enabled": False, "error_count": 0}]

    async def plugins_set_enabled(self, account, plugin, enabled):
        self.calls.append(("plugins_set_enabled", account, plugin, enabled))
        return {"account": account, "plugin": plugin, "enabled": enabled}

    async def ratelimit_policies(self):
        return [{"level": "global", "owner": None, "action": "message",
                 "interval": 2.0, "jitter": [0.1, 0.5]}]

    async def ratelimit_warnings(self):
        return [{"level": "global", "code": "below_floor",
                 "message": "interval under the safety floor"}]

    async def ratelimit_lane(self):
        return {"enabled": True, "prefixrate": 1.5, "paused": False,
                "waiting": 0, "grants": 4}

    async def tasks_list(self, status=None, account=None):
        return [{"id": 7, "account": "alpha", "plugin": "soulcopy",
                 "action": "message", "status": "queued"}]

    async def emergency_stop_all(self):
        self.calls.append(("emergency_stop_all",))
        return {"cancelled_tasks": 2}

    async def accounts_emergency_stop(self, name, surface="api"):
        self.calls.append(("accounts_emergency_stop", name, surface))
        return {"cancelled_tasks": 1}

    async def simulation_list(self):
        return [{"name": "soulcopy_basic", "description": "d"}]


class FakeServices:
    """PluginServices double: records the paced replies."""

    def __init__(self) -> None:
        self.account = "alpha"
        self.plugin = "selfcmd"
        self.sent: List[tuple] = []

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({"OWNER_USER_IDS": str(OWNER)})

    async def send_message(self, channel_id: int, content: str, *,
                           priority: Any = None) -> None:
        self.sent.append((int(channel_id), content))


class FakeAccess:
    """AccessManager double for the no-services (estop) path."""

    def __init__(self, *owners: int) -> None:
        self._owners = set(owners)

    async def can_invoke(self, account: str, user_id: Optional[int]) -> bool:
        return user_id in self._owners


class PortedCommandTests(unittest.IsolatedAsyncioTestCase):
    """Every ported verb through execute_self_command (the one path)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-rc-")
        self.addCleanup(self.tmp.cleanup)
        bus = EventBus()
        self.api = FakeApi()
        self.services = FakeServices()
        self.manager = PluginManager(
            bus, LOG, SettingsManager(bus), plugin_dir=self.tmp.name)
        # framework handlers read the process hub: bind THIS test's
        # manager + API bridge, restore the previous state on teardown
        COMMAND_HUB.bind(self.manager)
        self._saved_provider = COMMAND_HUB.api_provider
        COMMAND_HUB.api_provider = lambda: self.api

    def tearDown(self) -> None:
        COMMAND_HUB.api_provider = self._saved_provider
        self.manager.close()

    async def asyncSetUp(self) -> None:
        await self.manager.attach_account("alpha", set())
        await self.manager.activate_account(
            "alpha", lambda plugin: self.services)

    async def _run(self, name: str, args: List[str], *,
                   invoker: Optional[int] = OWNER) -> Optional[str]:
        await self.manager.execute_self_command(
            "alpha", name, args, CHANNEL, guild_id=GUILD, invoker_id=invoker)
        if not self.services.sent:
            return None
        return self.services.sent[-1][1]

    # ------------------------------------------------------------- status
    async def test_status_overview(self) -> None:
        reply = await self._run("status", [])
        self.assertIn("uptime 12s", reply)
        self.assertIn("queued=0", reply)
        self.assertIn("completed=3", reply)
        self.assertIn("· alpha: running (lat 83ms)", reply)
        channel = self.services.sent[-1][0]
        self.assertEqual(channel, CHANNEL)

    async def test_status_rejects_arguments(self) -> None:
        self.assertEqual(await self._run("status", ["x"]),
                         "'status' takes no arguments")

    # ----------------------------------------------------------- accounts
    async def test_accounts_list_default_and_explicit(self) -> None:
        self.assertIn("· alpha: running profiles=['copier']",
                      await self._run("accounts", []))
        self.assertIn("· alpha: running profiles=['copier']",
                      await self._run("accounts", ["list"]))

    async def test_accounts_info(self) -> None:
        reply = await self._run("accounts", ["info", "alpha"])
        self.assertIn("alpha — running", reply)
        self.assertIn("latency 83.0ms · uptime 45s", reply)
        self.assertIn("cached: 2g 10c 1d (client cache)", reply)

    async def test_accounts_start_and_stop(self) -> None:
        self.assertEqual(await self._run("accounts", ["start", "alpha"]),
                         "started alpha")
        self.assertEqual(await self._run("accounts", ["stop", "alpha"]),
                         "stopped alpha")

    async def test_accounts_usage_error(self) -> None:
        self.assertEqual(await self._run("accounts", ["frobnicate"]),
                         "usage: accounts list|info <n>|start <n>|stop <n>")
        self.assertEqual(await self._run("accounts", ["info"]),
                         "usage: accounts list|info <n>|start <n>|stop <n>")

    # ------------------------------------------------------------ plugins
    async def test_plugins_listing(self) -> None:
        reply = await self._run("plugins", ["alpha"])
        self.assertIn("+ soulcopy", reply)
        self.assertIn("- roll", reply)

    async def test_plugins_toggle_and_usage(self) -> None:
        reply = await self._run("plugins", ["alpha", "roll", "on"])
        self.assertEqual(reply, "roll enabled on alpha")
        self.assertIn(("plugins_set_enabled", "alpha", "roll", True),
                      self.api.calls)
        self.assertEqual(await self._run("plugins", []),
                         "usage: plugins <account> [<plugin> on|off]")
        self.assertEqual(await self._run("plugins", ["alpha", "roll"]),
                         "usage: plugins <account> <plugin> on|off")

    # ---------------------------------------------------------- ratelimit
    async def test_ratelimit_show_default_and_explicit(self) -> None:
        expected = "· global message: 2.0s +0.1..0.5s"
        self.assertIn(expected, await self._run("ratelimit", []))
        self.assertIn(expected, await self._run("ratelimit", ["show"]))

    async def test_ratelimit_warnings(self) -> None:
        reply = await self._run("ratelimit", ["warnings"])
        self.assertIn("[global] below_floor: interval under the safety floor",
                      reply)

    async def test_ratelimit_bad_choice_is_usage_error(self) -> None:
        reply = await self._run("ratelimit", ["explode"])
        self.assertIn("usage: ratelimit show|warnings", reply)
        self.assertIn("must be one of: show/warnings", reply)

    # ---------------------------------------------------------------- lane
    async def test_lane_state(self) -> None:
        self.assertIn("send lane: on (active) · spacing 1.5s · "
                      "waiting 0 · grants 4", await self._run("lane", []))
        self.assertIn("send lane", await self._run("lane", ["show"]))

    async def test_lane_disabled(self) -> None:
        async def off():  # local override of the canned reply
            return {"enabled": False}
        self.api.ratelimit_lane = off
        self.assertEqual(await self._run("lane", []),
                         "send lane: off (per-account pacing only)")

    # --------------------------------------------------------------- tasks
    async def test_tasks_listing(self) -> None:
        reply = await self._run("tasks", [])
        self.assertIn("· #7 alpha/soulcopy message [queued]", reply)

    async def test_tasks_capped_at_ten_with_overflow_note(self) -> None:
        rows = [{"id": i, "account": "alpha", "plugin": "p",
                 "action": "message", "status": "queued"} for i in range(12)]

        async def many(status=None, account=None):
            return rows
        self.api.tasks_list = many
        reply = await self._run("tasks", [])
        self.assertIn("(+2 more)", reply)
        self.assertNotIn("· #11 ", reply)  # only the first ten render

    # --------------------------------------------------------------- estop
    async def test_estop_global(self) -> None:
        reply = await self._run("estop", [])
        self.assertIn("GLOBAL EMERGENCY STOP — 2 task(s) cancelled", reply)
        self.assertIn(("emergency_stop_all",), self.api.calls)

    async def test_estop_account_scoped_uses_remote_surface(self) -> None:
        reply = await self._run("estop", ["beta"])
        self.assertEqual(reply,
                         "emergency stop beta: 1 task(s) cancelled")
        self.assertIn(("accounts_emergency_stop", "beta", "remote"),
                      self.api.calls)

    async def test_estop_extra_arguments_usage(self) -> None:
        reply = await self._run("estop", ["a", "b"])
        self.assertIn("usage: estop [account]", reply)

    async def test_estop_runs_without_services(self) -> None:
        """SAFETY: estop must run even when the account's plugins are
        deactivated (no paced pipeline → no facade credentials either;
        the gate falls back to the hub's bootstrap-registered
        AccessManager). The reply is logged, not sent, but the API call
        ALWAYS happens."""
        manager = self.manager
        await manager.deactivate_account("alpha")  # drops the services gate
        saved_access = COMMAND_HUB.access
        COMMAND_HUB.access = FakeAccess(OWNER)
        try:
            await manager.execute_self_command(
                "alpha", "estop", [], CHANNEL, invoker_id=OWNER)
            self.assertIn(("emergency_stop_all",), self.api.calls)
            self.assertEqual(self.services.sent, [])  # reply logged, not sent
        finally:
            COMMAND_HUB.access = saved_access

    # ---------------------------------------------------------- simulation
    async def test_simulation_list_bare_and_explicit(self) -> None:
        expected = "· soulcopy_basic: d"
        self.assertIn(expected, await self._run("simulation", []))
        self.assertIn(expected, await self._run("simulation", ["list"]))

    async def test_simulation_rejects_other_subcommands(self) -> None:
        reply = await self._run("simulation", ["run", "x"])
        self.assertIn("usage: simulation list", reply)

    async def test_simulation_empty(self) -> None:
        async def empty():
            return []
        self.api.simulation_list = empty
        self.assertEqual(await self._run("simulation", ["list"]),
                         "no scenarios available")

    # ---------------------------------------------------------- the gates
    async def test_non_owner_ignored_completely(self) -> None:
        self.assertIsNone(await self._run("estop", [], invoker=STRANGER))
        self.assertEqual(self.services.sent, [])  # not even an error reply
        self.assertEqual(self.api.calls, [])

    async def test_domain_error_renders_bare_error(self) -> None:
        from core.errors import AccountError

        async def boom(name):
            raise AccountError(f"unknown account {name!r}")
        self.api.accounts_info = boom
        reply = await self._run("accounts", ["info", "ghost"])
        self.assertEqual(reply, "error: unknown account 'ghost'")

    async def test_internal_error_is_masked(self) -> None:
        async def boom():
            raise RuntimeError("boom (paths, sql)")
        self.api.status = boom
        reply = await self._run("status", [])
        self.assertIn("error: status failed internally (see logs)", reply)
        self.assertNotIn("boom", reply)

    async def test_no_api_provider_answers_honestly(self) -> None:
        COMMAND_HUB.api_provider = None
        reply = await self._run("status", [])
        self.assertEqual(reply, "error: framework management unavailable "
                                "on this stack")

    # ----------------------------------------------------------- plumbing
    async def test_long_reply_truncated_at_1900(self) -> None:
        huge = "x" * 5000

        async def one_huge_row():
            return [{"name": huge, "status": "running", "profiles": []}]
        self.api.accounts_list = one_huge_row
        reply = await self._run("accounts", ["list"])
        self.assertLessEqual(len(reply), MAX_REPLY_CHARS)
        self.assertIn("… (truncated)", reply)

    async def test_help_lists_the_framework_category(self) -> None:
        reply = await self._run("help", [])
        self.assertIn("FRAMEWORK", reply)
        for verb in ("status", "accounts", "plugins", "ratelimit", "lane",
                     "tasks", "estop", "simulation"):
            self.assertIn(verb, reply)
        # per-command help shows the ported grammar
        detail = await self._run("help", ["estop"])
        self.assertIn("usage: estop [account]", detail)
        self.assertIn("category: Framework", detail)

    async def test_framework_names_are_reserved_for_the_verbs(self) -> None:
        entries = {e.spec.name: e for e in COMMAND_HUB.entries("alpha")}
        for verb in ("status", "accounts", "plugins", "ratelimit", "lane",
                     "tasks", "estop", "simulation"):
            self.assertIn(verb, entries)
            self.assertEqual(entries[verb].plugin, "selfcmd")


if __name__ == "__main__":
    unittest.main()
