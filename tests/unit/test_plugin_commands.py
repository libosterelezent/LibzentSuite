"""Per-account command interface (Phase 3): registry, executor, handlers.

Covers the contract end to end WITHOUT the Discord library:
  * registry collection — assigned ∧ enabled filtering, the deterministic
    collision rule (manifest sort, first-registered wins + warning),
    category defaults, loader-time validation of bad declarations,
  * execution — replies through the paced pipeline (fake services record
    the send), usage errors (ValueError/SettingsValidationError) rendered
    back as the usage line, crash isolation, owner-gate behavior,
  * framework verbs — pg (hub stats), say, the dynamic categorized help
    (Mudae layout, built from the registry, never hand-written),
  * the roll plugin's six handlers against fakes — scope refusal,
    validation path, profile binding display,
  * the adapter-side layer — the owner gate, invocation parsing, the
    registration rebuild diff over a REAL ext.commands Bot (the pinned
    discord.py-self is a dev dependency; nothing connects).
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

from core.credentials import Credentials
from core.errors import PluginError, SettingsValidationError, TargetGone
from core.event_bus import EventBus, MessageReceived
from core.plugin_base import CommandContext, CommandSpec
from core.plugin_manager import PluginManager
from core.plugin_manager.commands import (
    COMMAND_HUB,
    FRAMEWORK_COMMANDS,
    MAX_REPLY_CHARS,
    SelfCommandHub,
    render_help,
)
from core.plugin_manager.loader import PluginLoader
from core.priority import Priority
from core.settings_manager import SettingsManager

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PLUGINS = REPO_ROOT / "plugins"
OWNER_ID = 555
CHANNEL = 42
GUILD = 7

LOG = logging.getLogger("test-plugin-commands")


# ------------------------------------------------------------- fakes
class FakeChannelInfo:
    def __init__(self, channel_id: int, guild_id: Optional[int] = GUILD,
                 dm: bool = False) -> None:
        self.id = channel_id
        self.guild_id = guild_id
        self.dm = dm


class FakeServices:
    """PluginServices double: records sends, validates known channels."""

    def __init__(self, channels: Optional[Dict[int, FakeChannelInfo]] = None,
                 settings: Optional[Dict[str, Any]] = None) -> None:
        self.account = "alpha"
        self.plugin = "roll"
        self.sent: List[tuple] = []
        self.channels = channels if channels is not None else {
            CHANNEL: FakeChannelInfo(CHANNEL),
            43: FakeChannelInfo(43),  # exists but scope-refused in tests
        }
        self._settings = settings if settings is not None else {}
        self._state: Dict[str, Any] = {}
        self._cooldowns: Dict[str, float] = {}

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({
            "OWNER_USER_IDS": str(OWNER_ID), "MUDAE_BOT_ID": "999"})

    @property
    def settings(self) -> Dict[str, Any]:
        return self._settings

    async def send_message(self, channel_id: int, content: str, *,
                           priority: Priority = Priority.LOW) -> None:
        self.sent.append((int(channel_id), content, int(priority)))

    async def send_dm(self, user_id: int, content: str, *,
                      priority: Priority = Priority.LOW) -> None:
        self.sent.append((-int(user_id), content, int(priority)))

    async def open_dm(self, user_id: int) -> int:
        return 1

    async def add_reaction(self, *a, **k) -> None:  # pragma: no cover
        pass

    async def click_button(self, *a, **k) -> None:  # pragma: no cover
        pass

    async def fetch_user(self, user_id: int):  # pragma: no cover
        return None

    async def cooldown_set(self, key: str, duration_s: float, **kw):
        self._cooldowns[key] = duration_s
        return {"cooldown_key": key}

    async def cooldown_remaining(self, key: str) -> float:
        return self._cooldowns.get(key, 0.0)

    async def cooldown_active(self, key: str) -> bool:
        return self._cooldowns.get(key, 0.0) > 0

    async def cooldown_clear(self, key: str) -> bool:
        return self._cooldowns.pop(key, None) is not None

    async def cooldown_list(self):
        return []

    async def state_get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    async def state_set(self, key: str, value: Any) -> None:
        self._state[key] = value

    async def state_delete(self, key: str) -> bool:
        return self._state.pop(key, None) is not None

    async def wait_for_message(self, check, timeout):
        return None

    async def collect_messages(self, check, idle_timeout):
        return []

    def publish(self, event) -> None:
        pass

    def validate_channel(self, channel_id: int):
        info = self.channels.get(int(channel_id))
        if info is None:
            raise TargetGone("alpha", f"channel {channel_id} vanished")
        return info

    def describe_channel(self, channel_id: int) -> str:
        return f"#chan-{channel_id}"


class FakeSettingsApi:
    """CommandSettingsApi double the roll handlers tests steer."""

    def __init__(self, effective: Optional[Dict[str, Any]] = None,
                 binding: Optional[str] = None,
                 profiles: Optional[List[Dict[str, Any]]] = None) -> None:
        self.effective_view = dict(effective or {})
        self.binding_name = binding
        self.profiles_rows = list(profiles or [])
        self.set_calls: List[tuple] = []
        self.clear_calls: List[str] = []

    def schema(self):
        return ()

    async def effective(self):
        return dict(self.effective_view)

    async def set(self, key: str, value: Any) -> Any:
        self.set_calls.append((key, value))
        if key == "__raise__":
            raise SettingsValidationError(
                "roll.__raise__", "planned validation failure")
        old = self.effective_view.get(key)
        self.effective_view[key] = value
        return value if old is None else value

    async def clear(self, key: str) -> Any:
        self.clear_calls.append(key)
        self.effective_view.pop(key, None)
        return "$wa"

    async def binding(self):
        return self.binding_name

    async def profiles(self):
        return list(self.profiles_rows)


def _plugin_src(body: str) -> str:
    return (
        "from core.plugin_base import PluginBase, CommandSpec\n"
        "from core.settings_manager import Setting\n"
        "\n"
        "class P(PluginBase):\n"
        f"    {body}\n"
    )


#: a minimal valid handler for generated test plugins
_HANDLER = 'handler=lambda ctx, args: "ok"'

#: sentinel distinguishing "no api override" from "install api=None"
_UNSET = object()


# ----------------------------------------------------- registry collection
class RegistryCollectionTests(unittest.IsolatedAsyncioTestCase):
    """commands_for filtering + collisions over temp plugins."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-cmd-")
        self.addCleanup(self.tmp.cleanup)
        self.plugin_dir = Path(self.tmp.name)

    def _manager(self) -> PluginManager:
        bus = EventBus()
        manager = PluginManager(
            bus, LOG, SettingsManager(bus), plugin_dir=str(self.plugin_dir))
        return manager

    def _write(self, name: str, commands: str, settings: str = "()",
               plugin_cls: str = "P") -> None:
        (self.plugin_dir / f"{name}.py").write_text(_plugin_src(
            f'name = "{name}"\n'
            "    version = '1.0.0'\n"
            f"    settings = {settings}\n"
            f"    COMMANDS = ({commands},)\n"
        ))

    async def test_assigned_and_enabled_filtering(self) -> None:
        self._write("alpha", f'CommandSpec("one", "h1", {_HANDLER})')
        self._write("beta", f'CommandSpec("two", "h2", {_HANDLER})')
        manager = self._manager()
        await manager.attach_account("acct", {"alpha"})  # assignment axis
        names = [s.name for s in manager.commands_for("acct")]
        self.assertEqual(names, ["one"])  # beta never attached

        # enabled axis: disabling drops the commands
        await manager.set_enabled("acct", "alpha", False)
        self.assertEqual(manager.commands_for("acct"), [])
        await manager.set_enabled("acct", "alpha", True)
        self.assertEqual([s.name for s in manager.commands_for("acct")],
                         ["one"])

        # unassignment drops them too (re-attach with a smaller set)
        await manager.attach_account("acct", set())
        self.assertEqual(manager.commands_for("acct"), [])
        manager.close()

    async def test_collision_first_registered_wins_in_manifest_order(self):
        self._write("aaa", f'CommandSpec("shared", "from aaa", {_HANDLER})')
        self._write("bbb", f'CommandSpec("shared", "from bbb", {_HANDLER}), '
                            f'CommandSpec("unique", "from bbb", {_HANDLER})')
        manager = self._manager()
        await manager.attach_account("acct", {"aaa", "bbb"})
        specs = {s.name: s for s in manager.commands_for("acct")}
        self.assertEqual(set(specs), {"shared", "unique"})
        # aaa sorts before bbb: aaa's declaration wins the shared name
        self.assertEqual(specs["shared"].help, "from aaa")
        manager.close()

    async def test_category_defaults_to_plugin_name(self) -> None:
        self._write("alpha", f'CommandSpec("one", "h", {_HANDLER})')
        self._write("beta",
                    f'CommandSpec("two", "h", category="Custom", {_HANDLER})')
        manager = self._manager()
        await manager.attach_account("acct", {"alpha", "beta"})
        by_name = {s.name: s for s in manager.commands_for("acct")}
        self.assertEqual(by_name["one"].category, "alpha")  # defaulted
        self.assertEqual(by_name["two"].category, "Custom")  # explicit kept
        manager.close()

    async def test_handlers_bound_to_the_instance(self) -> None:
        source = (
            "from core.plugin_base import PluginBase, CommandSpec, "
            "CommandContext\n"
            "class P(PluginBase):\n"
            "    name = 'alpha'\n"
            "    marker = 'unset'\n"
            "    async def on_enable(self, ctx):\n"
            "        type(self).marker = 'set'\n"
            "    async def _h(self, ctx, args):\n"
            "        return f'bound:{self.marker}'\n"
            "    COMMANDS = (CommandSpec('one', 'h', handler=_h),)\n"
        )
        (self.plugin_dir / "alpha.py").write_text(source)
        manager = self._manager()
        await manager.attach_account("acct", {"alpha"})
        spec = manager.commands_for("acct")[0]
        # bind to a plain instance: the handler must receive `self`
        instance = manager._accounts["acct"]["alpha"].instance
        result = await spec.handler(
            NS(account="acct", plugin="alpha"), [])
        self.assertEqual(result, "bound:unset")
        self.assertIsNotNone(instance)
        manager.close()

    async def test_loader_rejects_bad_declarations(self) -> None:
        cases = {
            "badname": 'CommandSpec("Not-Ok", "h", handler=lambda c, a: "")',
            "dup": ('CommandSpec("x", "h", handler=lambda c, a: ""), '
                    'CommandSpec("x", "h2", handler=lambda c, a: "")'),
            "nohandler": 'CommandSpec("x", "h")',
            "nohelp": 'CommandSpec("x", "  ", handler=lambda c, a: "")',
        }
        for name, commands in cases.items():
            with self.subTest(name=name):
                self._write(name, commands)
                with self.assertRaises(PluginError):
                    PluginLoader(self.plugin_dir).discover()

    async def test_framework_names_are_reserved(self) -> None:
        # the collision check against framework verbs lives in the hub
        self._write("alpha", f'CommandSpec("pg", "my pg", {_HANDLER})')
        manager = self._manager()
        await manager.attach_account("acct", {"alpha"})
        hub = SelfCommandHub()
        manager.command_hub = hub  # route this manager's syncs at the test hub
        hub.bind(manager)
        try:
            names = [entry.spec.name for entry in hub.entries("acct")]
            self.assertIn("pg", names)
            spec = next(e.spec for e in hub.entries("acct")
                        if e.spec.name == "pg")
            self.assertEqual(spec.help, "Gateway latency and uptime")
            self.assertEqual(
                len([e for e in hub.entries("acct") if e.spec.name == "pg"]),
                1, "the plugin's colliding declaration must be dropped")
        finally:
            hub.forget("acct")
            hub.unbind(manager)
            manager.close()


# ----------------------------------------------------------- help rendering
class HelpRenderingTests(unittest.TestCase):

    ROLL_SPECS = [
        CommandSpec(name="rollinfo", help="Show effective roll settings",
                    category="roll"),
        CommandSpec(name="rolllist", help="List profiles", category="roll"),
        CommandSpec(name="say", help="Repeats the user's message.",
                    category="Utilities", usage="<text>"),
        CommandSpec(name="help", help="Shows this message"),
    ]

    @staticmethod
    def _col(specs) -> str:
        """The rendered name column for those specs' width."""
        width = max(max((len(s.name) for s in specs), default=0) + 2, 10)
        return width

    def test_mudae_layout(self) -> None:
        specs = self.ROLL_SPECS
        text = render_help(specs)
        width = self._col(specs)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Roll:")           # plugin category, titled
        self.assertEqual(lines[1], f"  {'rollinfo'.ljust(width)}"
                                   "Show effective roll settings")
        self.assertIn("Utilities:", text)
        self.assertTrue(text.index("Roll:") < text.index("Utilities:")
                        < text.index("No Category:"))
        self.assertIn(f"  {'help'.ljust(width)}Shows this message", text)
        self.assertIn(f"  {'say'.ljust(width)}Repeats the user's message.",
                      text)
        # inside a category, commands are name-sorted
        roll_block = text.split("Roll:")[1].split("Utilities:")[0]
        self.assertLess(roll_block.index("rollinfo"),
                        roll_block.index("rolllist"))

    def test_per_command_help_and_usage(self) -> None:
        specs = [
            CommandSpec(name="rollset", help="Set an override",
                        usage="<key> <value>", category="roll"),
            CommandSpec(name="say", help="Repeats the user's message.",
                        category="Utilities", usage="<text>"),
        ]
        text = render_help(specs)
        width = self._col(specs)
        self.assertIn(f"  {'rollset'.ljust(width)}Set an override", text)
        # usage surfaces through `help <command>` (executor-level below)
        framework_text = render_help(list(FRAMEWORK_COMMANDS))
        fwidth = self._col(list(FRAMEWORK_COMMANDS))
        self.assertIn(f"  {'pg'.ljust(fwidth)}Gateway latency and uptime",
                      framework_text)

    def test_long_names_widen_the_column(self) -> None:
        text = render_help([
            CommandSpec(name="rollremove", help="x", category="roll"),
            CommandSpec(name="pg", help="y", category="Utilities"),
        ])
        # width = len("rollremove") + 2 = 12 > 10 floor
        self.assertIn(f"  {'pg'.ljust(12)}y", text)

    def test_empty_registry(self) -> None:
        self.assertEqual(render_help([]), "no commands registered")


# ------------------------------------------------------------- execution
class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    """PluginManager.execute_self_command over the REAL roll plugin."""

    def setUp(self) -> None:
        bus = EventBus()
        self.services = FakeServices()
        self.manager = PluginManager(
            bus, LOG, SettingsManager(bus), plugin_dir=str(REAL_PLUGINS))
        # framework handlers (help/pg) read the process hub — bind THIS
        # test's manager so the view matches (documented last-wins)
        COMMAND_HUB.bind(self.manager)

    def tearDown(self) -> None:
        self.manager.close()

    async def _boot(self, account: str = "alpha",
                    allowed: Optional[set] = None) -> None:
        await self.manager.attach_account(
            account, {"roll"} if allowed is None else allowed)
        await self.manager.activate_account(
            account, lambda plugin: self.services)

    async def _run(self, name: str, args: List[str], *,
                   account: str = "alpha", channel: int = CHANNEL,
                   invoker: Optional[int] = OWNER_ID,
                   api: Any = _UNSET,  # None installs NO settings api
                   services: Optional[FakeServices] = None) -> Optional[str]:
        services = services or self.services
        original = self.manager._settings_api

        def _api(acct, plugin):
            if api is _UNSET:
                return original(acct, plugin)
            return api  # None => handlers must degrade gracefully
        self.manager._settings_api = _api
        try:
            await self.manager.execute_self_command(
                account, name, args, channel, guild_id=GUILD,
                invoker_id=invoker)
        finally:
            self.manager._settings_api = original
        if not services.sent:
            return None
        return services.sent[-1][1]

    # ------------------------------------------------------------ basics
    async def test_reply_flows_through_paced_pipeline(self) -> None:
        await self._boot()
        reply = await self._run("say", ["hello", "world"])
        self.assertEqual(reply, "hello world")
        channel, content, priority = self.services.sent[-1]
        self.assertEqual(channel, CHANNEL)
        self.assertEqual(priority, int(Priority.NORMAL))  # interactive

    async def test_unknown_command_is_silent(self) -> None:
        await self._boot()
        self.assertIsNone(await self._run("nosuch", []))

    async def test_owner_gate_ignores_non_owners(self) -> None:
        await self._boot()
        self.assertIsNone(await self._run("pg", [], invoker=999))
        self.assertEqual(self.services.sent, [])  # no reply at all

    async def test_usage_errors_render_the_usage_line(self) -> None:
        await self._boot()
        reply = await self._run("say", [])
        self.assertIn("usage: say <text>", reply)

    async def test_crashing_handler_is_contained(self) -> None:
        await self._boot()
        # swap ONE declared handler for a crasher (collection reads the
        # class's COMMANDS tuple, so the swap must live there)
        runtime = self.manager._accounts["alpha"]["roll"]
        cls = type(runtime.instance)

        async def boom(ctx, args):
            raise RuntimeError("planned crash")

        crashing = CommandSpec(name="rollstop", help="h", handler=boom)
        original = cls.COMMANDS
        cls.COMMANDS = tuple(
            crashing if spec.name == "rollstop" else spec
            for spec in original)
        try:
            reply = await self._run("rollstop", [])
        finally:
            cls.COMMANDS = original
        self.assertIn("error: rollstop failed internally", reply)

    async def test_reply_truncation(self) -> None:
        await self._boot()
        reply = await self._run("say", ["x" * 5000])
        self.assertLessEqual(len(reply), MAX_REPLY_CHARS)
        self.assertIn("… (truncated)", reply)

    async def test_undeliverable_reply_does_not_raise(self) -> None:
        await self._boot()
        async def failing_send(channel_id, content, *, priority=Priority.LOW):
            raise ConnectionError("planned")
        self.services.send_message = failing_send
        await self._run("say", ["hi"])  # must not raise

    # ------------------------------------------------------- framework verbs
    async def test_pg_reports_latency_and_uptime(self) -> None:
        await self._boot()
        provider = lambda: {"latency_ms": 123.4, "uptime_s": 3725}
        COMMAND_HUB._stats["alpha"] = provider
        try:
            reply = await self._run("pg", [])
        finally:
            COMMAND_HUB._stats.pop("alpha", None)
        self.assertIn("alpha:", reply)
        self.assertIn("123ms", reply)
        self.assertIn("1h02m", reply)

    async def test_pg_without_stats(self) -> None:
        await self._boot()
        reply = await self._run("pg", [])
        self.assertIn("gateway -", reply)

    async def test_help_lists_framework_and_plugin_categories(self) -> None:
        await self._boot()
        reply = await self._run("help", [])
        self.assertIn("Roll:", reply)
        for verb in ("rollinfo", "rolllist", "rollremove", "rollset",
                     "rollstart", "rollstop"):
            self.assertIn(verb, reply)
        self.assertIn("Utilities:", reply)
        self.assertIn("Gateway latency and uptime", reply)
        self.assertIn("Repeats the user's message.", reply)
        self.assertIn("No Category:", reply)
        self.assertIn("Shows this message", reply)

    async def test_help_for_one_command_shows_usage(self) -> None:
        await self._boot()
        reply = await self._run("help", ["rollset"])
        self.assertIn("rollset — Set one per-account roll setting override",
                      reply)
        self.assertIn("usage: rollset <key> <value>", reply)
        unknown = await self._run("help", ["zzz"])
        self.assertIn("usage: help [command]", unknown)
        self.assertIn("no command 'zzz'", unknown)

    async def test_help_is_dynamic_only_registered_verbs(self) -> None:
        # roll unassigned: its verbs must not appear
        await self._boot(allowed=set())
        reply = await self._run("help", [])
        self.assertNotIn("rollstart", reply)
        self.assertIn("pg", reply)  # framework verbs remain

    # ------------------------------------------------------------ roll verbs
    async def test_rollset_validation_error_lists_valid_commands(self) -> None:
        await self._boot()
        reply = await self._run("rollset", ["command", "bogus"])
        self.assertIn("usage: rollset <key> <value>", reply)
        self.assertIn("is not a Mudae roulette command", reply)
        self.assertIn("$wa", reply)  # the valid list rides along

    async def test_rollset_unknown_key_is_a_usage_error(self) -> None:
        await self._boot()
        reply = await self._run("rollset", ["nope", "1"])
        self.assertIn("usage: rollset <key> <value>", reply)
        self.assertIn("unknown setting", reply)

    async def test_rollset_missing_args_is_a_usage_error(self) -> None:
        await self._boot()
        reply = await self._run("rollset", ["onlykey"])
        self.assertIn("rollset <key> <value>", reply)

    async def test_rollset_persists_and_reports(self) -> None:
        await self._boot()
        api = FakeSettingsApi(effective={"command": "$wa"})
        reply = await self._run("rollset", ["command", "$wg"], api=api)
        self.assertIn("set roll.command = '$wg'", reply)
        self.assertIn("(was '$wa')", reply)
        self.assertEqual(api.set_calls, [("command", "$wg")])

    async def test_rollremove(self) -> None:
        await self._boot()
        api = FakeSettingsApi(effective={"command": "$wg"})
        reply = await self._run("rollremove", ["command"], api=api)
        self.assertIn("cleared roll.command override", reply)
        self.assertEqual(api.clear_calls, ["command"])

    async def test_rollinfo_includes_profile_binding_and_scope(self) -> None:
        await self._boot()
        api = FakeSettingsApi(
            effective={"command": "$wa", "userstack": True,
                       "channels": {"mode": "allow",
                                    "rules": [{"guild_id": "7",
                                               "channel_ids": ["42"]}]}},
            binding="grind-only")
        reply = await self._run("rollinfo", [], api=api)
        self.assertIn("profile: grind-only", reply)
        self.assertIn("command = '$wa'", reply)
        self.assertIn("userstack = True", reply)
        self.assertIn("scope: allow-list, 1 channel(s) in 1 guild(s)", reply)

    async def test_rollinfo_falls_back_without_profile_storage(self) -> None:
        await self._boot()
        self.services._settings = {"command": "$ha"}
        reply = await self._run("rollinfo", [], api=None)
        self.assertIn("live view", reply)
        self.assertIn("command = '$ha'", reply)

    async def test_rolllist(self) -> None:
        await self._boot()
        api = FakeSettingsApi(
            profiles=[{"name": "grind-only", "description": "fast"}])
        reply = await self._run("rolllist", [], api=api)
        self.assertIn("roll profiles:", reply)
        self.assertIn("grind-only — fast", reply)
        empty = await self._run(
            "rolllist", [], api=FakeSettingsApi(profiles=[]))
        self.assertIn("no roll profiles", empty)

    async def test_rollstart_scope_refusal_explains_why(self) -> None:
        await self._boot()
        api = FakeSettingsApi(effective={
            "channels": {"mode": "allow",
                         "rules": [{"guild_id": "7",
                                    "channel_ids": ["42"]}]}})
        reply = await self._run("rollstart", ["43"], api=api)
        self.assertIn("refused: channel 43", reply)
        self.assertIn("outside the roll channel scope", reply)
        self.assertIn("allow-list", reply)

    async def test_rollstart_bad_channel_argument(self) -> None:
        await self._boot()
        reply = await self._run("rollstart", ["notanumber"])
        self.assertIn("channel id must be a number", reply)

    async def test_rollstart_unusable_channel(self) -> None:
        await self._boot()
        reply = await self._run("rollstart", ["777"])  # unknown channel
        self.assertIn("unusable", reply)

    async def test_rollstart_starts_the_loop_and_rollstop_stops(self) -> None:
        await self._boot()
        reply = await self._run("rollstart", [])
        self.assertIn("rolling started", reply)
        instance = self.manager._accounts["alpha"]["roll"].instance
        self.assertTrue(instance._rolling.get("alpha"))
        already = await self._run("rollstart", [])
        self.assertIn("already rolling", already)
        stopped = await self._run("rollstop", [])
        self.assertIn("rolling stopped", stopped)
        self.assertFalse(instance._rolling.get("alpha"))
        again = await self._run("rollstop", [])
        self.assertIn("not rolling", again)
        # let any stray loop task settle before teardown
        await asyncio.sleep(0)

    async def test_rollstart_refuses_when_plugin_never_activated(self) -> None:
        # attached + enabled, but the live plugin context is missing
        await self.manager.attach_account("alpha", {"roll"})
        await self.manager.activate_account(
            "alpha", lambda plugin: self.services)
        instance = self.manager._accounts["alpha"]["roll"].instance
        instance._ctx = None  # simulate the missing-ctx corner
        reply = await self._run("rollstart", [])
        self.assertIn("roll is not active", reply)

    async def test_rollstart_inside_scope_starts(self) -> None:
        await self._boot()
        api = FakeSettingsApi(effective={
            "channels": {"mode": "allow",
                         "rules": [{"guild_id": "7",
                                    "channel_ids": ["42"]}]}})
        reply = await self._run("rollstart", [], api=api)  # channel 42 allowed
        self.assertIn("rolling started", reply)
        await self._run("rollstop", [])


# ------------------------------------------------------------- hub behavior
class HubTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.hub = SelfCommandHub()
        self.addCleanup(lambda: self.hub.unbind(None))

    async def test_refresh_notifies_subscriber_with_plugin_specs(self) -> None:
        bus = EventBus()
        manager = PluginManager(
            bus, LOG, SettingsManager(bus), plugin_dir=str(REAL_PLUGINS))
        self.addCleanup(manager.close)
        self.hub.bind(manager)
        manager.command_hub = self.hub  # this manager's syncs → the test hub
        await manager.attach_account("alpha", {"roll"})
        delivered: List[List[str]] = []
        self.hub.subscribe("alpha",
                           lambda specs: delivered.append(
                               [s.name for s in specs]))
        self.assertIn("rollstart", delivered[-1])  # framework + roll verbs
        self.assertIn("pg", delivered[-1])
        # a disable re-publishes through the same wire
        await manager.set_enabled("alpha", "roll", False)
        self.assertNotIn("rollstart", delivered[-1])
        self.hub.forget("alpha")

    async def test_invoke_routes_to_bound_manager(self) -> None:
        calls: List[tuple] = []

        class FakeManager:
            async def execute_self_command(self, *args, **kw):
                calls.append((args, kw))

        self.hub.bind(FakeManager())
        await self.hub.invoke("alpha", "pg", [], 42, invoker_id=1, raw="")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "alpha")

    async def test_invoke_without_manager_is_silent(self) -> None:
        await self.hub.invoke("alpha", "pg", [], 42)  # must not raise

    async def test_invoke_contains_a_broken_executor(self) -> None:
        class BrokenManager:
            async def execute_self_command(self, *a, **k):
                raise RuntimeError("planned")
        self.hub.bind(BrokenManager())
        await self.hub.invoke("alpha", "pg", [], 42)  # must not raise

    async def test_runtime_stats_provider(self) -> None:
        self.assertEqual(self.hub.runtime_stats("x"), {})
        self.hub.subscribe("x", lambda specs: None,
                           stats_provider=lambda: {"latency_ms": 5.0})
        self.assertEqual(self.hub.runtime_stats("x"), {"latency_ms": 5.0})
        self.hub.unsubscribe("x")
        self.assertEqual(self.hub.runtime_stats("x"), {})


# ---------------------------------------------------------- adapter layer
class AdapterLayerTests(unittest.IsolatedAsyncioTestCase):
    """Owner gate + parsing + registration diff over a REAL ext Bot."""

    def _client(self, owner_ids=frozenset({OWNER_ID})):
        from adapters.discord.client import RealDiscordClient
        client = RealDiscordClient("tok", command_prefix="sm!",
                                   owner_ids=owner_ids)
        client.account = "alpha"
        client.user_id = OWNER_ID
        return client

    def _bot(self, client):
        from discord.ext import commands as ext_commands
        client._client = ext_commands.Bot(
            command_prefix="sm!", self_bot=True, help_command=None)
        return client._client

    def test_owner_gate(self) -> None:
        client = self._client()
        self.assertTrue(client.self_command_allowed())
        client.user_id = 999
        self.assertFalse(client.self_command_allowed())
        client.user_id = None
        self.assertFalse(client.self_command_allowed())
        # no owners configured: fail closed
        closed = self._client(owner_ids=frozenset())
        self.assertFalse(closed.self_command_allowed())

    def test_parse_invocation(self) -> None:
        parse = self._client()._parse_invocation
        self.assertEqual(parse("sm!rollstart 123", "rollstart"),
                         (["123"], "123"))
        self.assertEqual(parse("sm!say   hello  world", "say"),
                         (["hello", "world"], "hello world"))
        self.assertEqual(parse("<@555> pg", "pg"), ([], ""))
        self.assertEqual(parse("SM!RollStart 9 8", "rollstart"),
                         (["9", "8"], "9 8"))
        self.assertEqual(parse("", "pg"), ([], ""))

    async def test_registration_rebuild_diff(self) -> None:
        client = self._client()
        bot = self._bot(client)
        client.set_plugin_commands([
            CommandSpec(name="pg", help="h", category="Utilities"),
            CommandSpec(name="rollstart", help="h", category="roll"),
        ])
        self.assertEqual(sorted(bot.all_commands), ["pg", "rollstart"])
        # shrink: stale names removed, fresh ones added — in one call
        client.set_plugin_commands([
            CommandSpec(name="rollstop", help="h", category="roll"),
        ])
        self.assertEqual(sorted(bot.all_commands), ["rollstop"])
        # stored set without a Bot is harmless
        client._client = None
        client.set_plugin_commands([])
        self.assertEqual(client._self_command_specs, {})

    async def test_dispatch_gates_non_owner_before_the_hub(self) -> None:
        client = self._client()
        self._bot(client)
        invoked: List[tuple] = []
        original = COMMAND_HUB.invoke

        async def fake_invoke(account, name, args, channel_id, **kw):
            invoked.append((account, name))

        import core.plugin_manager.commands as commands_mod
        commands_mod.COMMAND_HUB.invoke = fake_invoke
        try:
            ctx = NS(message=NS(content="sm!pg 1"),
                     channel=NS(id=42, guild=NS(id=GUILD)))
            await client._dispatch_self_command(ctx, "pg")
            self.assertEqual(invoked, [("alpha", "pg")])
            client.user_id = 999  # not an owner anymore
            await client._dispatch_self_command(ctx, "pg")
            self.assertEqual(len(invoked), 1)  # silently ignored
        finally:
            commands_mod.COMMAND_HUB.invoke = original

    async def test_dispatch_contained_when_hub_raises(self) -> None:
        client = self._client()
        self._bot(client)
        import core.plugin_manager.commands as commands_mod
        original = commands_mod.COMMAND_HUB.invoke

        async def boom(*a, **k):
            raise RuntimeError("planned")

        commands_mod.COMMAND_HUB.invoke = boom
        try:
            ctx = NS(message=NS(content="sm!pg"),
                     channel=NS(id=42, guild=None))
            await client._dispatch_self_command(ctx, "pg")  # no raise
        finally:
            commands_mod.COMMAND_HUB.invoke = original

    async def test_stats_provider_shape(self) -> None:
        client = self._client()
        self.assertIsNone(client._self_command_stats()["uptime_s"])
        import time as _time
        client._connected_since = _time.monotonic() - 10
        stats = client._self_command_stats()
        self.assertGreaterEqual(stats["uptime_s"], 9)


if __name__ == "__main__":
    unittest.main()
