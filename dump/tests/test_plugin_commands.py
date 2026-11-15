"""Per-account command interface (Phase 3): registry, executor, handlers.

Covers the contract end to end WITHOUT the Discord library:
  * registry collection — assigned ∧ enabled filtering, the deterministic
    collision rule (manifest sort, first-registered wins + warning),
    category defaults, loader-time validation of bad declarations,
  * execution — replies through the paced pipeline (fake services record
    the send), usage errors (ValueError/SettingsValidationError) rendered
    back as the usage line, crash isolation, owner-gate behavior,
  * framework verbs — pg (hub stats), say, the dynamic categorized help
    (the_bot layout, built from the registry, never hand-written),
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
from typing import Any

from core.credentials import Credentials
from core.errors import PluginError, TargetGone
from core.event_bus import EventBus
from core.plugin_base import (
    parse_command_args,
    usage_line,
)
from core.plugin_manager import PluginManager
from core.plugin_manager.commands import COMMAND_HUB, SelfCommandHub

LOG = logging.getLogger("test.selfcmd")
REAL_PLUGINS = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
from core.plugin_manager.loader import PluginLoader
from core.priority import Priority
from core.settings_manager import SettingsManager

PROBE = "probe"
OWNER_ID = 12345
GUILD = 42
CHANNEL = 500




class FakeChannelInfo:
    def __init__(self, channel_id: int, guild_id: int | None = GUILD,
                 dm: bool = False) -> None:
        self.id = channel_id
        self.guild_id = guild_id
        self.dm = dm


class FakeServices:
    """PluginServices double: records sends, validates known channels."""

    def __init__(self, channels: dict[int, FakeChannelInfo] | None = None,
                 settings: dict[str, Any] | None = None) -> None:
        self.account = "alpha"
        self.plugin = "probe"
        self.sent: list[tuple] = []
        self.channels = channels if channels is not None else {
            CHANNEL: FakeChannelInfo(CHANNEL),
            43: FakeChannelInfo(43),  # exists but scope-refused in tests
        }
        self._settings = settings if settings is not None else {}
        self._state: dict[str, Any] = {}
        self._cooldowns: dict[str, float] = {}

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({
            "OWNER_USER_IDS": str(OWNER_ID), "PRIMARY_BOT_ID": "999"})

    @property
    def settings(self) -> dict[str, Any]:
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
                    allowed: set | None = None) -> None:
        await self.manager.attach_account(
            account, {PROBE} if allowed is None else allowed)
        await self.manager.activate_account(
            account, lambda plugin: self.services)

    async def _run(self, name: str, args: list[str], *,
                   account: str = "alpha", channel: int = CHANNEL,
                   invoker: int | None = OWNER_ID) -> str | None:
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

    async def boom(ctx, args):
            raise RuntimeError("planned crash")

class HubTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.hub = SelfCommandHub()
        self.addCleanup(lambda: self.hub.unbind(None))

    async def execute_self_command(self, *args, **kw):
                calls.append((args, kw))

class TestPrefixRateCommand(unittest.IsolatedAsyncioTestCase):
    """prefixrate: show/set send pacing (per-account message interval).

    The SET branch is admin-gated (fleet-wide mutation): these tests
    wire a permissive admin access on the hub and invoke with a known
    invoker id, mirroring how the production path runs."""

    def setUp(self) -> None:
        from core.plugin_manager.commands import COMMAND_HUB, SelfCommandHub

LOG = logging.getLogger("test.selfcmd")
REAL_PLUGINS = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"

def _ctx(account="alpha", invoker_id=1):
    import logging

    from core.plugin_base import CommandContext
    return CommandContext(
        account=account, plugin="selfcmd", services=None,
        logger=logging.getLogger("t"), channel_id=1, guild_id=1,
        invoker_id=invoker_id)
