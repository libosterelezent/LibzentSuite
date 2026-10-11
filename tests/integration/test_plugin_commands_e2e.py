"""Per-account command interface over the REAL production stack (Phase 3).

Boots ``core.bootstrap.build`` over the fixture plugins (probe) and a
temporary database (the harness style of
tests/integration/test_account_plugins_e2e.py). Accounts are never
STARTED — no Discord client is needed:

  * registry wiring  — assignment drives commands_for: assign probe →
                        its declared verb appears; unassign → gone;
                        disable → gone (assignment = visibility, enabled
                        also required),
  * executor + DB    — the full execution path against the REAL database
                        (plugin profiles, bindings, per-account setting
                        rows): the declared probeinfo handler runs over
                        the real settings api, a profile-bound setting
                        rides into the effective view, and help lists
                        everything.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.bootstrap import build
from core.priority import Priority
from core.credentials import Credentials

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PLUGINS = REPO_ROOT / "tests" / "fixtures" / "plugins"
PROBE = "probe"

OWNER_ID = 555
GUILD = 7
CHANNEL_OK = 42


class ChannelInfo:
    def __init__(self, channel_id: int, guild_id: int) -> None:
        self.id = channel_id
        self.guild_id = guild_id
        self.dm = False


class E2EServices:
    """PluginServices double good enough for the probe fixture's
    lifecycle (ctx stash) and command replies."""

    def __init__(self, account: str = "alpha") -> None:
        self.account = account
        self.plugin = "probe"
        self.sent: List[tuple] = []
        self.channels = {
            CHANNEL_OK: ChannelInfo(CHANNEL_OK, GUILD),
        }
        self.settings: Dict[str, Any] = {}
        self._state: Dict[str, Any] = {}

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({
            "OWNER_USER_IDS": str(OWNER_ID), "MUDAE_BOT_ID": "999"})

    async def send_message(self, channel_id: int, content: str, *,
                           priority: Priority = Priority.LOW) -> None:
        self.sent.append((int(channel_id), content, int(priority)))

    async def send_dm(self, user_id: int, content: str, *,
                      priority: Priority = Priority.LOW) -> None:
        pass

    async def open_dm(self, user_id: int) -> int:
        return 1

    async def wait_for_message(self, check, timeout):
        return None

    async def collect_messages(self, check, idle_timeout):
        return []

    def publish(self, event) -> None:
        pass

    def validate_channel(self, channel_id: int):
        return self.channels.get(int(channel_id))

    def describe_channel(self, channel_id: int) -> str:
        return f"#chan-{channel_id}"

    async def state_get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    async def state_set(self, key: str, value: Any) -> None:
        self._state[key] = value

    async def state_delete(self, key: str) -> bool:
        return self._state.pop(key, None) is not None

    async def cooldown_remaining(self, key: str) -> float:
        return 0.0

    async def cooldown_set(self, key: str, duration_s: float, **kw):
        return {"cooldown_key": key}

    async def cooldown_active(self, key: str) -> bool:
        return False

    async def cooldown_clear(self, key: str) -> bool:
        return False

    async def cooldown_list(self):
        return []


class CommandE2EBase(unittest.IsolatedAsyncioTestCase):

    async def boot(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-cmds-e2e-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        fw = await build(
            env_file=str(root / "absent.env"),
            config_dir=str(root / "config"),  # no config -> defaults
            db_path=str(root / "state" / "slaughter.db"),
            plugin_dir=str(REAL_PLUGINS),
            quiet=True,
        )
        self.addCleanup(fw.shutdown)
        return fw


class RegistryWiringTests(CommandE2EBase):
    """Assignment/enabled axes drive commands_for through the real stack."""

    PROBE_VERBS = {"probeinfo", "probeset", "probestart"}

    async def test_assign_unassign_disable_drive_commands_for(self):
        fw = await self.boot()
        api = fw.api
        await api.accounts_add({"name": "alpha", "plugins": [PROBE]})

        # assigned -> the declared verbs are collected (bound + categorized)
        names = {s.name for s in fw.plugins.commands_for("alpha")}
        self.assertTrue(self.PROBE_VERBS <= names)
        # probe declares its category explicitly ("Probe")
        self.assertEqual(
            {s.category for s in fw.plugins.commands_for("alpha")}, {"Probe"})

        # disabled -> gone (assignment = visibility; enabled also required)
        await api.plugins_set_enabled("alpha", PROBE, False)
        self.assertEqual(fw.plugins.commands_for("alpha"), [])
        await api.plugins_set_enabled("alpha", PROBE, True)
        self.assertEqual({s.name for s in fw.plugins.commands_for("alpha")},
                         self.PROBE_VERBS)

        # unassigned -> gone
        await api.accounts_plugin_unassign("alpha", PROBE)
        self.assertEqual(fw.plugins.commands_for("alpha"), [])


class ExecutorOverRealDatabaseTests(CommandE2EBase):
    """The full execution path with profiles/bindings in a real DB."""

    async def asyncSetUp(self) -> None:
        self.fw = await self.boot()
        self.api = self.fw.api
        await self.api.accounts_add({"name": "alpha", "plugins": [PROBE]})
        # a profile whose settings shape the plugin's effective view
        await self.api.plugin_profile_create(
            PROBE, "grind-only",
            description="only the grind channel",
            settings={
                "trigger": "spin",
                "channels": {"mode": "allow", "rules": [
                    {"guild_id": str(GUILD),
                     "channel_ids": [str(CHANNEL_OK)]}]},
            })
        await self.api.accounts_plugin_profile("alpha", PROBE, "grind-only")
        # the account is never STARTED: activate with fake services so the
        # executor + handlers run against the REAL database layer
        self.services = E2EServices()
        await self.fw.plugins.activate_account(
            "alpha", lambda plugin: self.services)

    async def _run(self, name: str, args: List[str], *,
                   channel: int = CHANNEL_OK,
                   invoker: Optional[int] = OWNER_ID) -> Optional[str]:
        self.services.sent.clear()
        await self.fw.plugins.execute_self_command(
            "alpha", name, args, channel, guild_id=GUILD, invoker_id=invoker)
        return self.services.sent[-1][1] if self.services.sent else None

    async def test_declared_command_runs_over_the_real_db(self):
        """probeinfo executes through the executor, and its reply flows
        back through the per-plugin services facade."""
        reply = await self._run("probeinfo", [])
        self.assertEqual(reply, "probe ok")

    async def test_profile_settings_reach_the_live_view(self):
        """The bound profile's settings ride into the runtime effective
        view (the merge path the executor's settings api serves)."""
        api = self.fw.plugins._settings_api("alpha", PROBE)
        effective = await api.effective()
        self.assertEqual(effective.get("trigger"), "spin")
        self.assertEqual(await api.binding(), "grind-only")

    async def test_help_and_framework_verbs_over_the_real_stack(self):
        reply = await self._run("help", [])
        for verb in ("probeinfo", "pg", "say", "help"):
            self.assertIn(verb, reply)
        say = await self._run("say", ["integration", "echo"])
        self.assertEqual(say, "integration echo")
        # non-owner: silently ignored (no reply at all)
        self.assertIsNone(await self._run("pg", [], invoker=999))


if __name__ == "__main__":
    unittest.main()
