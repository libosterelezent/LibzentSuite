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
  * the probe fixture plugin's declared handler against fakes — the
    plugin-declared surface end to end (discovery binds it, the executor
    runs it, the reply flows through the fake services),
  * the adapter-side layer — the owner gate, invocation parsing, the
    registration rebuild diff over a REAL ext.commands Bot (the pinned
    discord.py-self is a dev dependency; nothing connects).
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

from core.credentials import Credentials
from core.errors import PluginError, TargetGone
from core.event_bus import EventBus
from core.plugin_base import (
    ArgSpec, CommandSpec, parse_command_args, usage_line,
)
from core.plugin_manager import PluginManager
from core.plugin_manager.commands import (
    COMMAND_HUB, FRAMEWORK_COMMANDS, MAX_REPLY_CHARS, SelfCommandHub, _prefixrate_handler, render_help,
)
from core.plugin_manager.loader import PluginLoader
from core.priority import Priority
from core.settings_manager import SettingsManager

class TestArgSpecParsing(unittest.TestCase):
    """Declarative positional grammar (the rollset-style endpoint)."""

    def _spec(self, **kw):
        return CommandSpec(name="t", help="h", handler=None, **kw)

    def test_positional_choice_int_optional(self):
        spec = self._spec(args=(
            ArgSpec("name"),
            ArgSpec("mode", choices=("fast", "slow")),
            ArgSpec("count", kind=int),
            ArgSpec("note", required=False, default="-"),
        ))
        out = parse_command_args(spec, ["farm", "fast", "3", "hi"])
        self.assertEqual(out, {"name": "farm", "mode": "fast",
                               "count": 3, "note": "hi"})
        out2 = parse_command_args(spec, ["farm", "slow", "10"])
        self.assertEqual(out2["note"], "-")
        self.assertIsInstance(out2["count"], int)

    def test_missing_required_names_the_arg(self):
        spec = self._spec(args=(ArgSpec("a"), ArgSpec("b")))
        with self.assertRaises(ValueError) as cm:
            parse_command_args(spec, ["only-one"])
        self.assertIn("missing <b>", str(cm.exception))

    def test_bad_int_renders_reason(self):
        spec = self._spec(args=(ArgSpec("count", kind=int),))
        with self.assertRaises(ValueError) as cm:
            parse_command_args(spec, ["ten"])
        self.assertIn("<count> must be a valid int", str(cm.exception))

    def test_choice_rejection_lists_allowed(self):
        spec = self._spec(args=(ArgSpec("mode", choices=("a", "b")),))
        with self.assertRaises(ValueError) as cm:
            parse_command_args(spec, ["zzz"])
        self.assertIn("a/b", str(cm.exception))

    def test_greedy_consumes_rest(self):
        spec = self._spec(args=(ArgSpec("names", greedy=True),))
        out = parse_command_args(spec, ["p1", "p2", "p3"])
        self.assertEqual(out["names"], ["p1", "p2", "p3"])
        with self.assertRaises(ValueError):
            parse_command_args(spec, [])

    def test_extras_rejected(self):
        spec = self._spec(args=(ArgSpec("a"),))
        with self.assertRaises(ValueError) as cm:
            parse_command_args(spec, ["one", "two"])
        self.assertIn("unexpected argument", str(cm.exception))

    def test_usage_line_generated(self):
        spec = self._spec(args=(
            ArgSpec("name"), ArgSpec("mode", choices=("a",)),
            ArgSpec("count", kind=int, required=False),
            ArgSpec("rest", greedy=True),
        ))
        self.assertEqual(usage_line(spec), "<name> <mode> [count] <rest...>")




REPO_ROOT = Path(__file__).resolve().parents[2]
#: fixture plugins (the framework bundles none; probe carries the
#: declared-command surface these suites exercise)
REAL_PLUGINS = REPO_ROOT / "tests" / "fixtures" / "plugins"
PROBE = "probe"
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
        self.plugin = "probe"
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

    def test_plugin_grouped_layout(self) -> None:
        """Operator-specified format: PLUGIN (uppercase) header, 4-space
        indented commands, aligned descriptions, 25-char truncation."""
        specs = self.ROLL_SPECS
        text = render_help(specs)
        width = self._col(specs)
        lines = text.splitlines()
        self.assertEqual(lines[0], "ROLL")            # plugin, UPPERCASE
        self.assertEqual(lines[1], f"    {'rollinfo'.ljust(width)}"
                                   "Show effective roll sett…")
        self.assertIn("UTILITIES", text)
        self.assertTrue(text.index("ROLL") < text.index("UTILITIES")
                        < text.index("No Category"))
        # 25-char truncation with ellipsis on EVERY long description
        for line in lines:
            if line.startswith("    ") and line.strip():
                self.assertLessEqual(
                    len(line.strip()[len(line.strip().split()[0]):].strip()),
                    25, f"description over 25 chars: {line!r}")
        # inside a group, commands are name-sorted
        roll_block = text.split("ROLL")[1].split("UTILITIES")[0]
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
        self.assertIn(f"    {'rollset'.ljust(width)}Set an override", text)
        # usage surfaces through `help <command>` (executor-level below)
        framework_text = render_help(list(FRAMEWORK_COMMANDS))
        fwidth = self._col(list(FRAMEWORK_COMMANDS))
        # 25-char truncation applies to framework verbs too
        self.assertIn(f"    {'pg'.ljust(fwidth)}"
                      "Gateway latency and upti…", framework_text)

    def test_long_names_widen_the_column(self) -> None:
        text = render_help([
            CommandSpec(name="rollremove", help="x", category="roll"),
            CommandSpec(name="pg", help="y", category="Utilities"),
        ])
        # width = len("rollremove") + 2 = 12 > 10 floor
        self.assertIn(f"    {'pg'.ljust(12)}y", text)

    def test_empty_registry(self) -> None:
        self.assertEqual(render_help([]), "no commands registered")


# ------------------------------------------------------------- execution
class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    """PluginManager.execute_self_command over the REAL fixture plugin."""

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
            account, {PROBE} if allowed is None else allowed)
        await self.manager.activate_account(
            account, lambda plugin: self.services)

    async def _run(self, name: str, args: List[str], *,
                   account: str = "alpha", channel: int = CHANNEL,
                   invoker: Optional[int] = OWNER_ID) -> Optional[str]:
        await self.manager.execute_self_command(
            account, name, args, channel, guild_id=GUILD, invoker_id=invoker)
        if not self.services.sent:
            return None
        return self.services.sent[-1][1]

    # ------------------------------------------------------------ basics
    async def test_reply_flows_through_paced_pipeline(self) -> None:
        await self._boot()
        reply = await self._run("say", ["hello", "world"])
        self.assertEqual(reply, "hello world")
        channel, content, priority = self.services.sent[-1]
        self.assertEqual(channel, CHANNEL)
        self.assertEqual(priority, int(Priority.NORMAL))  # interactive

    async def test_probeinfo_declared_command_runs_end_to_end(self) -> None:
        """The fixture plugin's declared handler: discovery collects it,
        the executor binds it to the per-account instance and its reply
        flows through the paced pipeline."""
        await self._boot()
        reply = await self._run("probeinfo", [])
        self.assertEqual(reply, "probe ok")
        channel, _content, priority = self.services.sent[-1]
        self.assertEqual(channel, CHANNEL)
        self.assertEqual(priority, int(Priority.NORMAL))

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
        runtime = self.manager._accounts["alpha"][PROBE]
        cls = type(runtime.instance)

        async def boom(ctx, args):
            raise RuntimeError("planned crash")

        crashing = CommandSpec(name="probeinfo", help="h", handler=boom)
        original = cls.COMMANDS
        cls.COMMANDS = tuple(
            crashing if spec.name == "probeinfo" else spec
            for spec in original)
        try:
            reply = await self._run("probeinfo", [])
        finally:
            cls.COMMANDS = original
        self.assertIn("error: probeinfo failed internally", reply)

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
        self.assertIn("PROBE", reply)           # plugin group, UPPERCASE
        self.assertIn("probeinfo", reply)       # its declared verb
        self.assertIn("probeset", reply)        # ArgSpec grammar demo verb
        self.assertIn("probestart", reply)      # greedy grammar demo verb
        self.assertIn("UTILITIES", reply)
        self.assertIn("Gateway latency and upti", reply)  # truncated @25
        self.assertIn("Repeats the user's messa", reply)  # truncated @25
        self.assertIn("No Category", reply)
        self.assertIn("Shows this message", reply)

    async def test_help_for_one_command_shows_usage(self) -> None:
        await self._boot()
        reply = await self._run("help", ["say"])
        self.assertIn("say — Repeats the user's message.", reply)
        self.assertIn("usage: say <text>", reply)
        unknown = await self._run("help", ["zzz"])
        self.assertIn("usage: help [command]", unknown)
        self.assertIn("no command 'zzz'", unknown)

    async def test_help_is_dynamic_only_registered_verbs(self) -> None:
        # probe unassigned: its verb must not appear
        await self._boot(allowed=set())
        reply = await self._run("help", [])
        self.assertNotIn("probeinfo", reply)
        self.assertIn("pg", reply)  # framework verbs remain

    # NOTE: the roll plugin's verb-specific executor tests (rollset
    # validation/persistence, rollstart scope refusals, rollinfo/rolllist
    # profile display) were removed with the plugin — those behaviors had
    # no fixture equivalent. The executor's usage-error, containment,
    # pacing and owner-gate paths stay covered above, and the
    # settings-through-commands path survives in
    # tests/integration/test_plugin_commands_e2e.py over the real DB.


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
        await manager.attach_account("alpha", {PROBE})
        delivered: List[List[str]] = []
        self.hub.subscribe("alpha",
                           lambda specs: delivered.append(
                               [s.name for s in specs]))
        self.assertIn("probeinfo", delivered[-1])  # framework + fixture verb
        self.assertIn("pg", delivered[-1])
        # a disable re-publishes through the same wire
        await manager.set_enabled("alpha", PROBE, False)
        self.assertNotIn("probeinfo", delivered[-1])
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
            command_prefix="sm!", user_bot=True,
            owner_ids=set(client._owner_ids), help_command=None)
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

    def test_invoker_gate_any_owner_may_command(self) -> None:
        """user_bot dispatch: the gate is the TYPER, not the account —
        any OWNER_USER_IDS owner can command this account, strangers
        cannot, and unknown/absent invokers fail closed. (Regression:
        self_bot=True made the library drop other owners' commands
        before this gate could ever run.)"""
        client = self._client(owner_ids=frozenset({1, 2}))
        client.user_id = 1  # the account itself is one of the owners
        self.assertTrue(client.allowed_invoker(1))   # the account itself
        self.assertTrue(client.allowed_invoker(2))   # a DIFFERENT owner
        self.assertFalse(client.allowed_invoker(999))  # a stranger
        self.assertFalse(client.allowed_invoker(None))  # unknown invoker
        closed = self._client(owner_ids=frozenset())
        self.assertFalse(closed.allowed_invoker(1))

    def test_parse_invocation(self) -> None:
        parse = self._client()._parse_invocation
        # AUDIT FIX polarity: re-tokenize on the content FOLLOWING the
        # parsed prefix (ctx.prefix) and match the invocation token by
        # EQUALITY — the old endswith rule mis-anchored on the mention
        # token when a numeric command name was a suffix of the account's
        # own user id (argument parsing shifted by one token).
        self.assertEqual(parse("sm!rollstart 123", "rollstart", prefix="sm!"),
                         (["123"], "123"))
        self.assertEqual(parse("sm!say   hello  world", "say", prefix="sm!"),
                         (["hello", "world"], "hello world"))
        self.assertEqual(parse("<@555> pg", "pg"), ([], ""))
        self.assertEqual(parse("SM!RollStart 9 8", "rollstart", prefix="SM!"),
                         (["9", "8"], "9 8"))
        self.assertEqual(parse("", "pg"), ([], ""))
        # mention prefixes strip exactly (the library's ctx.prefix form)
        self.assertEqual(parse("<@555> rollstart 7", "rollstart",
                               prefix="<@555>"), (["7"], "7"))
        # the boundary bug: "9" is a suffix of the mention's user id —
        # the OLD endswith match consumed the mention token and shifted
        # the args; equality (with or without the prefix stripped) must
        # find no invocation for a different command word
        self.assertEqual(
            parse("<@11123456789> actualcmd x", "9",
                  prefix="<@11123456789>"), ([], ""))
        self.assertEqual(
            parse("<@11123456789> actualcmd x", "9"), ([], ""))
        # a genuinely numeric command still parses when actually invoked
        self.assertEqual(
            parse("<@11123456789> 9 roll", "9", prefix="<@11123456789>"),
            (["roll"], "roll"))

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
            # an OWNER typer (the account itself or any other owner)
            ctx = NS(message=NS(content="sm!pg 1"),
                     channel=NS(id=42, guild=NS(id=GUILD)),
                     author=NS(id=OWNER_ID))
            await client._dispatch_self_command(ctx, "pg")
            self.assertEqual(invoked, [("alpha", "pg")])
            # a stranger typer: silently ignored before the hub
            await client._dispatch_self_command(
                NS(message=NS(content="sm!pg"), channel=ctx.channel,
                   author=NS(id=999)), "pg")
            self.assertEqual(len(invoked), 1)  # only the owner's invocation
            # author-less context: fail closed
            await client._dispatch_self_command(
                NS(message=NS(content="sm!pg"), channel=ctx.channel), "pg")
            self.assertEqual(len(invoked), 1)
        finally:
            commands_mod.COMMAND_HUB.invoke = original

    async def test_mention_prefix_mode_resolves_to_when_mentioned(self) -> None:
        """prefix_mode=mention (the admin-panel default): the Bot prefix
        is the library's when_mentioned — the retired RemoteControl's
        mention surface now rides THIS path (one command layer)."""
        from discord.ext import commands as ext_commands
        client = self._client()
        client._prefix_mode = "mention"
        client._command_prefix = "@me"  # the SELF_COMMAND_PREFIX env default
        self.assertIs(client._resolve_prefix(ext_commands),
                      ext_commands.when_mentioned)

    async def test_mention_invocation_routes_to_the_hub(self) -> None:
        """`@account status` (mention prefix): the Bot dispatch reaches
        _dispatch_self_command, the invocation parses out of the mention
        text and lands on the hub as the ported Framework verb."""
        client = self._client()
        self._bot(client)
        client.set_plugin_commands(FRAMEWORK_COMMANDS)
        self.assertIn("status", client._client.all_commands)
        invoked: List[tuple] = []
        original = COMMAND_HUB.invoke

        async def fake_invoke(account, name, args, channel_id, **kw):
            invoked.append((account, name, args, channel_id, kw.get("invoker_id")))

        import core.plugin_manager.commands as commands_mod
        commands_mod.COMMAND_HUB.invoke = fake_invoke
        try:
            ctx = NS(message=NS(content=f"<@{OWNER_ID}> status"),
                     channel=NS(id=42, guild=NS(id=GUILD)),
                     author=NS(id=OWNER_ID))
            await client._dispatch_self_command(ctx, "status")
            self.assertEqual(invoked, [("alpha", "status", [], 42, OWNER_ID)])
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


class TestPrefixRateCommand(unittest.IsolatedAsyncioTestCase):
    """prefixrate: show/set send pacing (per-account message interval).

    The SET branch is admin-gated (fleet-wide mutation): these tests
    wire a permissive admin access on the hub and invoke with a known
    invoker id, mirroring how the production path runs."""

    def setUp(self) -> None:
        from core.plugin_manager.commands import COMMAND_HUB

        class AdminAccess:
            async def is_admin_id(self, user_id):
                return user_id == 1

            async def can_invoke(self, account, user_id):
                return user_id == 1

        self._saved_access = COMMAND_HUB.access
        COMMAND_HUB.access = AdminAccess()
        self.addCleanup(setattr, COMMAND_HUB, "access", self._saved_access)

    async def test_show_without_args(self) -> None:
        from core.plugin_manager.commands import COMMAND_HUB, PacingBackend
        from core.rate_limit_manager import RateLimitManager
        from core.event_bus import EventBus
        import logging
        lim = RateLimitManager(EventBus(), logging.getLogger("t"), None)
        lim.set_policy_values("message", 2.6)
        COMMAND_HUB.pacing = PacingBackend(lim)
        reply = await _prefixrate_handler(_ctx("alpha"), [])
        self.assertIn("2.60s", reply)
        self.assertIn("alpha", reply)

    async def test_set_then_show(self) -> None:
        from core.plugin_manager.commands import COMMAND_HUB, PacingBackend
        from core.rate_limit_manager import RateLimitManager
        from core.event_bus import EventBus
        import logging
        lim = RateLimitManager(EventBus(), logging.getLogger("t"), None)
        # NO stored global policy: the learned default (2.6) is the
        # fallback baseline the command reports; the set then stores the
        # ONLY policy (account level), replacing the default
        COMMAND_HUB.pacing = PacingBackend(lim)
        reply = await _prefixrate_handler(_ctx("alpha"), ["1.5"])
        self.assertIn("2.60s → 1.50s", reply)
        self.assertIn("⚠", reply)          # below the safe rate warning
        reply2 = await _prefixrate_handler(_ctx("alpha"), [])
        self.assertIn("1.50s", reply2)

    async def test_set_denied_for_non_admin(self) -> None:
        """The mutation branch is admin-only: a non-admin invoker is
        rejected before the limiter is touched (the executor's _ported
        wrapper renders the ValueError as ``error: admins only``)."""
        from core.plugin_manager.commands import COMMAND_HUB, PacingBackend
        from core.rate_limit_manager import RateLimitManager
        from core.event_bus import EventBus
        import logging
        lim = RateLimitManager(EventBus(), logging.getLogger("t"), None)
        lim.set_policy_values("message", 2.6)
        COMMAND_HUB.pacing = PacingBackend(lim)
        with self.assertRaises(ValueError) as cm:
            await _prefixrate_handler(_ctx("alpha", invoker_id=999),
                                      ["1.5"])
        self.assertIn("admins only", str(cm.exception))
        self.assertIn("2.60s", await _prefixrate_handler(_ctx("alpha"), []))

    async def test_rejects_bad_values(self) -> None:
        for bad in (["abc"], ["-1"], ["0"]):
            with self.assertRaises(ValueError):
                await _prefixrate_handler(_ctx("alpha"), bad)

    async def test_no_backend_answers_honestly(self) -> None:
        from core.plugin_manager.commands import COMMAND_HUB
        saved = COMMAND_HUB.pacing
        COMMAND_HUB.pacing = None
        try:
            with self.assertRaises(ValueError) as cm:
                await _prefixrate_handler(_ctx("alpha"), [])
            self.assertIn("unavailable", str(cm.exception))
        finally:
            COMMAND_HUB.pacing = saved


def _ctx(account="alpha", invoker_id=1):
    from core.plugin_base import CommandContext
    import logging
    return CommandContext(
        account=account, plugin="selfcmd", services=None,
        logger=logging.getLogger("t"), channel_id=1, guild_id=1,
        invoker_id=invoker_id)
