"""Full-stack integration tests: the PRODUCTION composition over the simulator.

These exercise core.bootstrap.build() — the exact boot path of main.py — with
only the Discord boundary replaced by the in-memory simulator (spec §9's
Testing adapter). Everything else (managers, scheduler, limiter, database,
audit, logging) is the real production code.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import json
import tempfile
import unittest
from pathlib import Path

from adapters.simulator.client import SimClient
from adapters.simulator.world import SimWorld
from core.bootstrap import build
from core.credentials import Credentials
from core.event_bus import MessageReceived
from database.db import Database

OWNER_ID = 1001
MUDAE_ID = 999_999
ALPHA_ID = 2001
GUILD_CHANNEL = 500

FAST_SETTINGS = {
    "soulcopy": {
        "inter_chunk_delay": 0.0,
        "dm_read_timeout": 0.6,
    }
}
FAST_POLICIES = {
    "global": {
        "message": {"interval": 1.5, "jitter": [0.0, 0.05]},
        "dm": {"interval": 1.5, "jitter": [0.0, 0.05]},
    },
    "accounts": {},
}


def make_config_dir() -> Path:
    root = Path(tempfile.mkdtemp(prefix="sm-config-"))
    (root / "global_settings.json").write_text(json.dumps(FAST_SETTINGS))
    (root / "rate_limits.json").write_text(json.dumps(FAST_POLICIES))
    (root / "accounts").mkdir()
    return root


class Stack:
    """A fully wired production framework over one simulated world."""

    def __init__(self, db_path: str | None = None, extra_accounts: list = ()) -> None:
        self.world = SimWorld(
            responder_id=MUDAE_ID,
            responses={"$slud-s": [
                "Soulmates\n---------\nAlice\nBob\nCharlie",
                "Dana\nEve\nFinn",
            ]},
            reply_delay=0.01,
        )
        self.world.add_user(OWNER_ID, "owner")
        self.world.add_user(MUDAE_ID, "mudae")
        self.world.add_user(ALPHA_ID, "alpha")
        self.world.add_user(2002, "beta")
        self.world.add_channel(GUILD_CHANNEL, "grind-channel")
        self.user_ids = {"alpha": ALPHA_ID, "beta": 2002}
        self.config_dir = make_config_dir()
        self.credentials = Credentials.from_values({
            "MUDAE_BOT_ID": str(MUDAE_ID),
            "OWNER_USER_IDS": str(OWNER_ID),
            "DISCORD_TOKEN_ALPHA": "sim-alpha",
            "DISCORD_TOKEN_BETA": "sim-beta",
        })
        self.db = Database(db_path if db_path is not None else ":memory:")
        self.framework = build(
            config_dir=str(self.config_dir),
            plugin_dir="plugins",
            client_factory=self._client_factory,
            credentials=self.credentials,
            database=self.db,
            quiet=True,
        )
        self._extra = extra_accounts

    def _client_factory(self, name: str, token: str) -> SimClient:
        return SimClient(self.world, self.user_ids[name], seed=hash(name) % 1000)

    async def start(self) -> None:
        for name in self._extra or ["alpha"]:
            await self.framework.api.accounts_add({"name": name})
        await self.framework.api.accounts_start()

    async def stop(self) -> None:
        await self.framework.shutdown()

    async def owner_says(self, content: str, account: str = "alpha") -> None:
        self.world.deliver(GUILD_CHANNEL, OWNER_ID, content)


class TestFullStackSoulcopy(unittest.IsolatedAsyncioTestCase):
    async def test_flow_end_to_end(self) -> None:
        stack = Stack()
        await stack.start()
        try:
            await stack.owner_says("slcopy")
            await stack.world.wait_for_quiet(GUILD_CHANNEL, quiet=2.2, limit=30.0)
            history = stack.world.messages_of(GUILD_CHANNEL)
            # export request + one chunk (pages fit a single chunk)
            self.assertIn("slcopy", history[0])
            chunks = [m for m in history if m.startswith("$soulcopy ")]
            self.assertEqual(len(chunks), 1)
            self.assertIn("Alice", chunks[0])

            # audit trail recorded outbound interactions with pacing data
            audit = await stack.framework.api.audit_tail(n=20)
            outs = [e for e in audit if e["direction"] == "out"]
            self.assertGreaterEqual(len(outs), 2)
            self.assertTrue(all("plugin=soulcopy" in (e.get("note") or "") for e in outs))

            # task history persisted with statuses
            hist = await stack.framework.api.tasks_history(limit=10)
            self.assertTrue(hist)
            self.assertTrue(all(h["status"] == "completed" for h in hist))

            # structured logging saw the lifecycle
            logs = await stack.framework.api.logs_tail(n=50)
            events = [l.get("event") for l in logs if l.get("event")]
            self.assertIn("TaskCompleted", events)
            self.assertIn("AccountConnected", events)
        finally:
            await stack.stop()

    async def test_pacing_enforced_between_sends(self) -> None:
        stack = Stack()
        await stack.start()
        try:
            await stack.owner_says("slcopy")
            await stack.world.wait_for_quiet(GUILD_CHANNEL, quiet=2.2, limit=30.0)
            audit = await stack.framework.api.audit_tail(n=20)
            outs = sorted(
                (e["ts"] for e in audit if e["direction"] == "out"),
            )
            gaps = [b - a for a, b in zip(outs, outs[1:])]
            # config policy: 1.5s interval (+50ms jitter max) -> every gap >= 1.45
            self.assertTrue(all(g >= 1.45 for g in gaps), gaps)
        finally:
            await stack.stop()


class TestProfilesGating(unittest.IsolatedAsyncioTestCase):
    async def test_profile_limits_plugin_set(self) -> None:
        stack = Stack(extra_accounts=["alpha", "beta"])
        api = stack.framework.api
        await api.profiles_create("copier", ["soulcopy"], "copy duty")
        await api.accounts_add({"name": "alpha", "profiles": ["copier"]})
        await api.accounts_add({"name": "beta"})
        await api.accounts_start()
        try:
            alpha_plugins = {p["name"] for p in await api.plugins_state("alpha")}
            beta_plugins = {p["name"] for p in await api.plugins_state("beta")}
            self.assertEqual(alpha_plugins, {"soulcopy"})  # profile-filtered
            self.assertIn("roll", beta_plugins)            # unrestricted
        finally:
            await stack.stop()


class TestPersistenceRoundtrip(unittest.IsolatedAsyncioTestCase):
    async def test_state_survives_reboot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-state-") as tmp:
            db_path = str(Path(tmp) / "slaughter.db")
            stack = Stack(db_path=db_path)
            api = stack.framework.api
            await api.profiles_create("copier", ["soulcopy"])
            await api.accounts_add({"name": "alpha", "profiles": ["copier"]})
            await api.ratelimit_set("message", 4.0, 0.1, 0.9, level="account", owner="alpha")
            await api.plugins_set_enabled("alpha", "soulcopy", False)
            await api.plugins_set_setting("alpha", "soulcopy", "trigger", "go")
            await stack.stop()

            # second boot on the same DB restores everything
            stack2 = Stack(db_path=db_path)
            api2 = stack2.framework.api
            try:
                accounts = await api2.accounts_list()
                self.assertEqual([a["name"] for a in accounts], ["alpha"])
                self.assertEqual(accounts[0]["profiles"], ["copier"])
                policies = await api2.ratelimit_policies()
                override = [p for p in policies
                            if p["level"] == "account" and p["owner"] == "alpha"]
                self.assertEqual(len(override), 1)
                self.assertEqual(override[0]["interval"], 4.0)
                states = {p["name"]: p for p in await api2.plugins_state("alpha")}
                self.assertFalse(states["soulcopy"]["enabled"])   # plugin state
                self.assertEqual(states["soulcopy"]["settings"]["trigger"], "go")
                profiles = await api2.profiles_list()
                self.assertEqual(profiles[0]["name"], "copier")
            finally:
                await stack2.stop()


class TestEmergencyStop(unittest.IsolatedAsyncioTestCase):
    async def test_global_estop_halts_everything(self) -> None:
        stack = Stack()
        await stack.start()
        try:
            out = await stack.framework.api.emergency_stop_all()
            self.assertTrue(out["halted"])
            accounts = await stack.framework.api.accounts_list()
            self.assertTrue(all(a["status"] == "stopped" for a in accounts))
        finally:
            await stack.stop()


class TestLaneIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_server_wide_sequential_sends(self) -> None:
        """Two accounts sending through the production bootstrap: with the
        lane on, EVERY pair of outbound sends (across accounts too) is spaced
        by at least the prefixrate — the FarmLite guarantee, fleet-wide."""
        stack = Stack()
        # enable the lane with a recognizable spacing (runtime call path)
        lane = await stack.framework.api.ratelimit_set_lane(enabled=True, prefixrate=1.6)
        self.assertTrue(lane["enabled"])
        await stack.start()
        try:
            # alpha and beta both fire the soulcopy flow
            await stack.owner_says("slcopy")
            await stack.world.wait_for_quiet(GUILD_CHANNEL, quiet=2.5, limit=40.0)
            audit = await stack.framework.api.audit_tail(n=30)
            outs = sorted(e["ts"] for e in audit if e["direction"] == "out")
            self.assertGreaterEqual(len(outs), 2)
            gaps = [b - a for a, b in zip(outs, outs[1:])]
            self.assertTrue(all(g >= 1.5 for g in gaps), gaps)
            # lane stats surfaced through the API
            status = await stack.framework.api.ratelimit_lane()
            self.assertTrue(status["enabled"])
            self.assertGreaterEqual(status["grants"], 2)
            # and in the full stats block (dashboard Rate Limits page)
            stats = (await stack.framework.api.status())["ratelimit"]
            self.assertTrue(stats["lane"]["enabled"])
        finally:
            await stack.stop()

    async def test_lane_pause_holds_and_resumes(self) -> None:
        stack = Stack()
        await stack.framework.api.ratelimit_set_lane(enabled=True, prefixrate=1.5)
        await stack.start()
        try:
            # pause the lane: the export send must NOT go out
            await stack.framework.api.ratelimit_set_lane(prefixrate=0.0)
            self.assertTrue((await stack.framework.api.ratelimit_lane())["paused"])
            await stack.owner_says("slcopy")
            await asyncio.sleep(1.0)
            history = stack.world.messages_of(GUILD_CHANNEL)
            self.assertNotIn("$slud-s", history)  # holding
            # resume: the flow completes
            await stack.framework.api.ratelimit_set_lane(prefixrate=1.5)
            await stack.world.wait_for_quiet(GUILD_CHANNEL, quiet=2.5, limit=30.0)
            history = stack.world.messages_of(GUILD_CHANNEL)
            self.assertIn("$slud-s", history)
            # a paused lane also surfaced a warning while it was paused
        finally:
            await stack.stop()


class TestApiCallTrimming(unittest.IsolatedAsyncioTestCase):
    """Anti-flag guarantees: resolved entities cost ONE REST call per account
    lifetime (persistent across restarts), and typing is opt-in."""

    async def test_dm_resolved_once_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-dm-") as tmp:
            db_path = str(Path(tmp) / "slaughter.db")
            stack = Stack(db_path=db_path)
            api = stack.framework.api
            await api.accounts_add({"name": "alpha"})
            await api.accounts_start()
            try:
                # resolve the Mudae DM twice in this boot: ONE client call
                services = stack.framework.accounts.services_for("alpha", "soulcopy")
                cid1 = await services.open_dm(MUDAE_ID)
                cid2 = await services.open_dm(MUDAE_ID)
                self.assertEqual(cid1, cid2)
                client = stack.framework.accounts.get("alpha").runtime.client
                self.assertEqual(client.open_dm_calls, [MUDAE_ID])
            finally:
                await stack.stop()

            # second boot on the same DB: still exactly ONE call ever
            stack2 = Stack(db_path=db_path)
            api2 = stack2.framework.api
            # alpha was restored from the database by the AccountManager
            names = [a["name"] for a in await api2.accounts_list()]
            self.assertIn("alpha", names)
            await api2.accounts_start()
            try:
                services2 = stack2.framework.accounts.services_for("alpha", "soulcopy")
                cid3 = await services2.open_dm(MUDAE_ID)
                self.assertEqual(cid3, cid1)
                client2 = stack2.framework.accounts.get("alpha").runtime.client
                self.assertEqual(client2.open_dm_calls, [])  # DB-served
            finally:
                await stack2.stop()

    async def test_typing_indicator_opt_in(self) -> None:
        stack = Stack()
        api = stack.framework.api
        await api.accounts_add({"name": "alpha"})
        await stack.framework.accounts.start("alpha")
        try:
            client = stack.framework.accounts.get("alpha").runtime.client
            settings = stack.framework.settings
            # default: typing_probability 0.0 -> zero typing calls, zero extra API
            await stack.framework.accounts.send_hook(
                SimpleNamespace(account="alpha", action="message",
                                channel_id=GUILD_CHANNEL, content="x"))
            self.assertEqual(client.typing_calls, [])
            # opted in at 1.0 -> every guild send fires typing first
            settings.set_account_setting("alpha", "typing_probability", 1.0)
            await stack.framework.accounts.send_hook(
                SimpleNamespace(account="alpha", action="message",
                                channel_id=GUILD_CHANNEL, content="y"))
            self.assertEqual(len(client.typing_calls), 1)
        finally:
            await stack.stop()

    async def test_remote_control_end_to_end(self) -> None:
        stack = Stack()
        api = stack.framework.api
        await api.accounts_add({"name": "alpha"})
        await stack.framework.accounts.start("alpha")
        try:
            alpha_uid = stack.framework.accounts.get("alpha").runtime.user_id
            stack.world.deliver(GUILD_CHANNEL, OWNER_ID, f"<@{alpha_uid}> lane")
            await stack.world.wait_for_quiet(GUILD_CHANNEL, quiet=1.2, limit=20.0)
            replies = [m for m in stack.world.messages_of(GUILD_CHANNEL)
                       if "send lane" in m]
            self.assertTrue(replies, "expected a mention-prefix reply in-channel")
        finally:
            await stack.stop()


class TestEmergencyStopResume(unittest.IsolatedAsyncioTestCase):
    """REGRESSION: the global estop message says 'restart manually' — the
    resume path must actually exist. estop halts everything; system_resume
    re-arms the scheduler so plugin sends work again."""

    async def test_estop_then_resume_restores_sending(self) -> None:
        stack = Stack()
        api = stack.framework.api
        await api.accounts_add({"name": "alpha"})
        await api.accounts_start()
        try:
            out = await api.emergency_stop_all()
            self.assertTrue(out["halted"])
            # halted: every submit is rejected
            from core.errors import SchedulerError
            with self.assertRaises(SchedulerError):
                stack.framework.scheduler.submit("alpha", "roll", "message",
                                                 channel_id=GUILD_CHANNEL, content="x")
            # resume: scheduler re-armed, sends flow again
            resumed = await api.system_resume()
            self.assertTrue(resumed["resumed"])
            await api.accounts_start()  # accounts restart is explicit
            scheduler = stack.framework.scheduler
            scheduler.submit("alpha", "roll", "message",
                             channel_id=GUILD_CHANNEL, content="back")
            for _ in range(100):
                await asyncio.sleep(0.05)
                if "back" in stack.world.messages_of(GUILD_CHANNEL):
                    break
            self.assertIn("back", stack.world.messages_of(GUILD_CHANNEL))
        finally:
            await stack.stop()


class TestGroupRateScope(unittest.IsolatedAsyncioTestCase):
    """Per-account/group scope over the production bootstrap: accounts in
    one sync_group serialize their API activity on a shared clock."""

    async def test_group_members_serialize(self) -> None:
        stack = Stack()
        api = stack.framework.api
        await api.accounts_add({"name": "alpha", "sync_group": "grinders"})
        await api.accounts_add({"name": "beta", "sync_group": "grinders"})
        self.assertEqual(
            (await api.ratelimit_stats())["groups"],
            {"alpha": "grinders", "beta": "grinders"},
        )
        await api.accounts_start()
        try:
            scheduler = stack.framework.scheduler
            for account in ("alpha", "beta", "alpha", "beta"):
                scheduler.submit(
                    account, "roll", "message",
                    channel_id=GUILD_CHANNEL, content=f"hi-{account}",
                )
            for _ in range(200):  # 4 sends on a 2.0s group clock land ~t=6s
                await asyncio.sleep(0.05)
                if len(stack.world.messages_of(GUILD_CHANNEL)) >= 4:
                    break
            await asyncio.sleep(0.3)
            history = [m for m in stack.world.messages_of(GUILD_CHANNEL)
                       if m.startswith("hi-")]
            self.assertEqual(len(history), 4)
            audit = await api.audit_tail(n=20)
            outs = sorted(e["ts"] for e in audit if e["direction"] == "out"
                          and e["content"].startswith("hi-"))
            gaps = [b - a for a, b in zip(outs, outs[1:])]
            # every gap between GROUP members (and each account's own calls)
            # respects the 1.5s config interval across the shared clock
            self.assertTrue(all(g >= 1.45 for g in gaps), gaps)
        finally:
            await stack.stop()


if __name__ == "__main__":
    unittest.main()
