"""Per-account command interface over the REAL production stack (Phase 3).

Boots ``core.bootstrap.build`` over the REAL plugins (roll + soulcopy)
and a temporary database (the harness style of
tests/integration/test_account_plugins_e2e.py). Accounts are never
STARTED — no Discord client is needed:

  * registry wiring  — assignment drives commands_for: assign roll →
                       the six roll verbs appear; unassign → gone;
                       disable → gone (assignment = visibility, enabled
                       also required),
  * executor + DB    — the full execution path against the REAL database
                       (profiles, bindings, per-account setting rows):
                       a bound profile's channel scope REFUSES an
                       out-of-scope rollstart, permits an in-scope one
                       (the loop runs against fake services), rollset
                       validates + persists through the same path as the
                       dashboard's set-setting surface, rollinfo shows
                       the profile binding, help lists everything.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.bootstrap import build
from core.errors import TargetGone
from core.priority import Priority
from core.credentials import Credentials

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PLUGINS = REPO_ROOT / "plugins"
ROLL = "roll"

OWNER_ID = 555
GUILD = 7
CHANNEL_OK = 42          # inside the profile's allow-scope
CHANNEL_BLOCKED = 43     # exists, outside the allow-scope

LOG_SCHEDULED = []


class ChannelInfo:
    def __init__(self, channel_id: int, guild_id: int) -> None:
        self.id = channel_id
        self.guild_id = guild_id
        self.dm = False


class E2EServices:
    """PluginServices double good enough for the REAL roll plugin's
    lifecycle (state_get), roll loop (cooldown/wait) and command replies."""

    def __init__(self, account: str = "alpha") -> None:
        self.account = account
        self.plugin = "roll"
        self.sent: List[tuple] = []
        self.channels = {
            CHANNEL_OK: ChannelInfo(CHANNEL_OK, GUILD),
            CHANNEL_BLOCKED: ChannelInfo(CHANNEL_BLOCKED, GUILD),
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
        info = self.channels.get(int(channel_id))
        if info is None:
            raise TargetGone(self.account, f"channel {channel_id} vanished")
        return info

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

    ROLL_VERBS = {"rollinfo", "rolllist", "rollremove", "rollset",
                  "rollstart", "rollstop"}

    async def test_assign_unassign_disable_drive_commands_for(self):
        fw = await self.boot()
        api = fw.api
        await api.accounts_add({"name": "alpha", "plugins": [ROLL]})

        # assigned -> the six verbs are collected (bound + categorized)
        names = {s.name for s in fw.plugins.commands_for("alpha")}
        self.assertTrue(self.ROLL_VERBS <= names)
        self.assertTrue({s.category for s in fw.plugins.commands_for("alpha")}
                        <= {ROLL})  # category defaults to the plugin name

        # disabled -> gone (assignment = visibility; enabled also required)
        await api.plugins_set_enabled("alpha", ROLL, False)
        self.assertEqual(fw.plugins.commands_for("alpha"), [])
        await api.plugins_set_enabled("alpha", ROLL, True)
        self.assertEqual({s.name for s in fw.plugins.commands_for("alpha")},
                         self.ROLL_VERBS)

        # unassigned -> gone
        await api.accounts_plugin_unassign("alpha", ROLL)
        self.assertEqual(fw.plugins.commands_for("alpha"), [])


class ExecutorOverRealDatabaseTests(CommandE2EBase):
    """The full execution path with profiles/bindings in a real DB."""

    async def asyncSetUp(self) -> None:
        self.fw = await self.boot()
        self.api = self.fw.api
        await self.api.accounts_add({"name": "alpha", "plugins": [ROLL]})
        # a profile whose allow-scope permits ONLY CHANNEL_OK in GUILD
        await self.api.plugin_profile_create(
            ROLL, "grind-only",
            description="only the grind channel",
            settings={
                "command": "$wg",
                "channels": {"mode": "allow", "rules": [
                    {"guild_id": str(GUILD),
                     "channel_ids": [str(CHANNEL_OK)]}]},
            })
        await self.api.accounts_plugin_profile("alpha", ROLL, "grind-only")
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

    async def test_rollstart_outside_scope_is_refused_with_reason(self):
        reply = await self._run("rollstart", [str(CHANNEL_BLOCKED)])
        self.assertIsNotNone(reply)
        self.assertIn(f"refused: channel {CHANNEL_BLOCKED}", reply)
        self.assertIn("outside the roll channel scope", reply)

    async def test_rollstart_inside_scope_starts_the_loop(self):
        reply = await self._run("rollstart", [])
        self.assertIn("rolling started", reply)
        # the SCOPE verdict came from the merged effective settings (the
        # bound profile's channels); the command the loop will actually
        # send is the RUNTIME live view's value — Phase-1 profiles shape
        # the effective view, the settings-manager resolution (defaults
        # <- global <- account overrides) still feeds the plugin runtime
        self.assertIn("($wa)", reply)
        instance = self.fw.plugins._accounts["alpha"][ROLL].instance
        self.assertTrue(instance._rolling.get("alpha"))
        await self._run("rollstop", [])

    async def test_rollinfo_shows_binding_and_profile_settings(self):
        reply = await self._run("rollinfo", [])
        self.assertIn("profile: grind-only", reply)
        self.assertIn("command = '$wg'", reply)          # from the profile
        self.assertIn("scope: allow-list, 1 channel(s)", reply)

    async def test_rolllist_lists_profiles(self):
        reply = await self._run("rolllist", [])
        self.assertIn("grind-only", reply)
        self.assertIn("only the grind channel", reply)

    async def test_rollset_validates_and_persists_the_real_row(self):
        # invalid value -> the usage error names the valid commands
        bad = await self._run("rollset", ["command", "nope"])
        self.assertIn("usage: rollset <key> <value>", bad)
        self.assertIn("is not a Mudae roulette command", bad)
        # valid value -> persisted through the same path as the dashboard
        good = await self._run("rollset", ["command", "$wg"])
        self.assertIn("set roll.command = '$wg'", good)
        self.assertEqual(
            await self.fw.database.plugins.setting("alpha", ROLL, "command"),
            "$wg",
        )
        # and the account override BEATS the profile (highest layer)
        await self._run("rollset", ["command", "$ha"])
        info = await self._run("rollinfo", [])
        self.assertIn("command = '$ha'", info)
        # clearing the override falls back to the profile's value
        cleared = await self._run("rollremove", ["command"])
        self.assertIn("cleared roll.command override", cleared)
        self.assertEqual(
            await self.fw.database.plugins.setting("alpha", ROLL, "command"),
            None,
        )
        info2 = await self._run("rollinfo", [])
        self.assertIn("command = '$wg'", info2)  # profile layer again

    async def test_help_and_framework_verbs_over_the_real_stack(self):
        reply = await self._run("help", [])
        for verb in ("rollinfo", "rolllist", "rollremove", "rollset",
                     "rollstart", "rollstop", "pg", "say", "help"):
            self.assertIn(verb, reply)
        say = await self._run("say", ["integration", "echo"])
        self.assertEqual(say, "integration echo")
        # non-owner: silently ignored (no reply at all)
        self.assertIsNone(await self._run("pg", [], invoker=999))


if __name__ == "__main__":
    unittest.main()
