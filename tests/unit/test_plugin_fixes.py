"""Regression tests for the audited plugin-manager fixes.

Covers, per the audit report:

  (a) help pagination — groups join with blank lines so _paginate_help
      can actually split; page 1 carries the more-help suffix and
      ``help 2`` returns the second disjoint page;
  (b) ArgSpec grammar — an unrecognized kind (e.g. kind="int") fails at
      parse time; a greedy arg with a scalar default parses as a
      ONE-element list, never char-split;
  (c) executor authz — a wired access manager is consulted (and the
      legacy env-owner list ignored); invoker_id=None denies;
  (d) admin gate — fleet-wide mutating verbs (accounts start/stop,
      plugins on/off, prefixrate set) are admin-only; reads stay open
      to any permitted invoker; estop stays ungated for allowed users;
  (e) dispatch flood bound — per-account in-flight dispatch tasks are
      capped, excess events dropped-newest and counted;
  (f) the bound-profile settings layer reaches attach/activate-time
      resolution with defaults <- global <- profile <- account
      precedence, and the manager-side effective view includes global;
  plus: boot dashboard-UI takeover by the live instance (boot
      registrations survive deactivation), and loader sys.modules
      purge on a failed discover.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.credentials import Credentials
from core.event_bus import EventBus, MessageReceived
from core.plugin_base import ArgSpec, CommandSpec, parse_command_args
from core.plugin_manager import PluginManager
from core.plugin_manager.commands import (
    COMMAND_HUB,
    HELP_PAGE_CHARS,
    _paginate_help,
    _render_help_body,
    render_help,
)
from core.plugin_manager.loader import PluginLoader
from core.settings_manager import SettingsManager

LOG = logging.getLogger("test-plugin-fixes")

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PLUGINS = REPO_ROOT / "tests" / "fixtures" / "plugins"
SPY = "spy"
OWNER_ID = 555
CHANNEL = 42
GUILD = 7


# ------------------------------------------------------------- fakes
class FakeServices:
    """PluginServices double: records paced replies."""

    def __init__(self, owner_ids: Optional[list] = None) -> None:
        self.account = "alpha"
        self.plugin = "spy"
        self.sent: List[tuple] = []
        self._owners = owner_ids if owner_ids is not None else [OWNER_ID]

    @property
    def credentials(self) -> Credentials:
        return Credentials.from_values({
            "OWNER_USER_IDS": ",".join(str(o) for o in self._owners),
            "MUDAE_BOT_ID": "999"})

    async def send_message(self, channel_id: int, content: str, *,
                           priority: Any = None) -> None:
        self.sent.append((int(channel_id), content))

    async def send_dm(self, user_id: int, content: str, *,
                      priority: Any = None) -> None:
        self.sent.append((-int(user_id), content))

    async def wait_for_message(self, check, timeout):
        return None

    async def collect_messages(self, check, idle_timeout):
        return []

    def publish(self, event) -> None:
        pass

    def validate_channel(self, channel_id: int):
        return None

    def describe_channel(self, channel_id: int) -> str:
        return f"#chan-{channel_id}"


class RecordingAccess:
    """AccessManager double that RECORDS can_invoke consultations."""

    def __init__(self, allowed: set, admins: Optional[set] = None) -> None:
        self._allowed = set(allowed)
        self._admins = set(admins or ())
        self.calls: List[tuple] = []

    async def can_invoke(self, account: str, user_id: Optional[int]) -> bool:
        self.calls.append((account, user_id))
        return user_id is not None and \
            (user_id in self._allowed or user_id in self._admins)

    async def is_admin_id(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self._admins


class FakeFrameworkApi:
    """ControlAPI double for the framework verbs (calls journal)."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def accounts_list(self):
        self.calls.append(("accounts_list",))
        return [{"name": "alpha", "status": "running", "profiles": []}]

    async def accounts_start(self, name, surface="api"):
        self.calls.append(("accounts_start", name))
        return {"started": name}

    async def accounts_stop(self, name, surface="api"):
        self.calls.append(("accounts_stop", name))
        return {"stopped": name}

    async def plugins_state(self, account):
        self.calls.append(("plugins_state", account))
        return [{"name": "spy", "enabled": True, "error_count": 0}]

    async def plugins_set_enabled(self, account, plugin, enabled):
        self.calls.append(("plugins_set_enabled", account, plugin, enabled))
        return {"enabled": enabled}

    async def emergency_stop_all(self):
        self.calls.append(("emergency_stop_all",))
        return {"cancelled_tasks": 0}


class FakeBindingRepo:
    """account_plugins double: fixed {plugin: profile} per account."""

    def __init__(self, bindings: Dict[str, Dict[str, Optional[str]]]) -> None:
        self._bindings = bindings

    async def bindings(self, account: str) -> Dict[str, Optional[str]]:
        return dict(self._bindings.get(account, {}))


class FakeProfileRepo:
    """plugin_profiles double: (plugin, profile) -> record-ish.

    Stored values may be bare settings dicts or ``{"settings": ...}``
    rows — both normalize to a record with a ``settings`` attribute."""

    def __init__(self, records: Dict[tuple, Any]) -> None:
        self._records = records

    async def get(self, plugin: str, profile: str) -> Any:
        row = self._records.get((plugin, profile))
        if row is None:
            return None
        settings = row.get("settings", row) if isinstance(row, dict) else row
        return type("Rec", (), {"settings": settings})()

    async def list(self, plugin: str):
        return []


class FakeDatabase:
    """The shape _ManagerSettingsApi._repos reaches through _repo._db.

    Mirrors the real Database: ``plugin_profiles`` is the profile
    RECORD store (get), ``account_plugins`` the BINDINGS repo."""

    def __init__(self, bindings=None, profiles=None) -> None:
        self.plugin_profiles = FakeProfileRepo(profiles or {})
        self.account_plugins = FakeBindingRepo(bindings or {})


class FakePluginRepo:
    """PluginRepository double (enabled-state + settings rows + _db)."""

    def __init__(self, database: Any = None) -> None:
        self.states: Dict[tuple, bool] = {}
        self.settings_rows: Dict[tuple, Any] = {}
        self._db = database

    async def state(self, account, plugin):
        return self.states.get((account, plugin))

    async def set_state(self, account, plugin, enabled):
        self.states[(account, plugin)] = bool(enabled)

    async def set_setting(self, account, plugin, key, value):
        self.settings_rows[(account, plugin, key)] = value

    async def all_account_settings(self):
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for (acct, plug, key), value in self.settings_rows.items():
            out.setdefault(acct, {}).setdefault(plug, {})[key] = value
        return out


def _plugin_src(body: str) -> str:
    return (
        "from core.plugin_base import PluginBase, CommandSpec\n"
        "\n"
        "class P(PluginBase):\n"
        + "\n".join(f"    {line}" for line in body.splitlines())
        + "\n"
    )


def _make_manager(plugin_dir=None, repo=None, global_layer=None,
                  **kw) -> PluginManager:
    bus = EventBus()
    settings = SettingsManager(bus, LOG, global_layer=global_layer,
                               plugin_repo=repo)
    return PluginManager(
        bus, LOG, settings, plugin_repo=repo,
        plugin_dir=str(plugin_dir if plugin_dir is not None else REAL_PLUGINS),
        **kw
    )


# --------------------------------------------------- (a) help pagination
class HelpPaginationTests(unittest.TestCase):

    @staticmethod
    def _specs(count: int = 120, categories: int = 3):
        per = count // categories
        specs = []
        for c in range(categories):
            for i in range(per):
                specs.append(CommandSpec(
                    name=f"verb{c:02d}{i:03d}",
                    help="lorem ipsum dolor sit amet consectetur",
                    category=f"cat{c:02d}",
                ))
        return specs

    def test_pages_respect_the_budget_and_split(self) -> None:
        specs = self._specs()
        self.assertEqual(len(specs), 120)
        pages = _paginate_help(_render_help_body(specs))
        self.assertGreater(len(pages), 1, "120 specs must split into pages")
        for page in pages:
            self.assertLessEqual(len(page), HELP_PAGE_CHARS)
        # pages are disjoint on command names
        def names(page):
            return {line.split()[0] for line in page.splitlines()
                    if line.startswith("    ")}
        seen: set = set()
        for page in pages:
            fresh = names(page)
            self.assertFalse(seen & fresh, "pages must be disjoint")
            seen |= fresh
        self.assertEqual(len(seen), 120)

    def test_render_help_is_page_one_plus_suffix(self) -> None:
        text = render_help(self._specs())
        self.assertTrue(text.endswith("(help 2 for more…)"),
                        f"page 1 must end with the more-help suffix: {text[-60:]!r}")
        # single small registry: no suffix
        small = render_help([CommandSpec(name="pg", help="h",
                                         category="Utilities")])
        self.assertFalse(small.endswith("(help 2 for more…)"))


class HelpPaginationExecutorTests(unittest.IsolatedAsyncioTestCase):
    """``help`` / ``help 2`` through the REAL executor over 120 verbs."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-fix-help-")
        self.addCleanup(self.tmp.cleanup)
        plugin_dir = Path(self.tmp.name)
        commands = ",\n".join(
            f'CommandSpec("verb{c:02d}{i:03d}", "lorem ipsum dolor sit amet '
            f'consectetur", category="cat{c:02d}", '
            f"handler=lambda ctx, args: 'ok')"
            for c in range(3) for i in range(40)
        )
        (plugin_dir / "big.py").write_text(_plugin_src(
            "name = 'big'\n"
            "version = '1.0.0'\n"
            f"COMMANDS = ({commands},)\n"
        ))
        self.services = FakeServices()
        self.manager = _make_manager(plugin_dir=plugin_dir)
        COMMAND_HUB.bind(self.manager)

    def tearDown(self) -> None:
        self.manager.close()

    async def _run(self, name: str, args: List[str]) -> Optional[str]:
        await self.manager.execute_self_command(
            "alpha", name, args, CHANNEL, guild_id=GUILD, invoker_id=OWNER_ID)
        return self.services.sent[-1][1] if self.services.sent else None

    async def asyncSetUp(self) -> None:
        await self.manager.attach_account("alpha", {"big"})
        await self.manager.activate_account(
            "alpha", lambda plugin: self.services)

    async def test_help_pages_through_the_executor(self) -> None:
        page1 = await self._run("help", [])
        self.assertIsNotNone(page1)
        self.assertTrue(page1.endswith("(help 2 for more…)"))
        page2 = await self._run("help", ["2"])
        self.assertIsNotNone(page2)
        self.assertIn("(help 3 for more…)", page2)
        self.assertNotIn("(help 3 for more…)", page1)
        # disjoint command surfaces
        n1 = {ln.split()[0] for ln in page1.splitlines()
              if ln.startswith("    ")}
        n2 = {ln.split()[0] for ln in page2.splitlines()
              if ln.startswith("    ")}
        self.assertTrue(n1 and n2)
        self.assertFalse(n1 & n2)
        # out-of-range page is a usage error
        reply = await self._run("help", ["99"])
        self.assertIn("usage: help [command]", reply)


# ------------------------------------------------------- (b) ArgSpec grammar
class ArgSpecGrammarTests(unittest.TestCase):

    def _spec(self, **kw):
        return CommandSpec(name="t", help="h", handler=None, **kw)

    def test_unrecognized_string_kind_raises_at_parse_time(self) -> None:
        spec = self._spec(args=(ArgSpec("count", kind="int"),))
        with self.assertRaises(ValueError) as cm:
            parse_command_args(spec, ["5"])
        self.assertIn("unsupported kind", str(cm.exception))
        self.assertIn("'int'", str(cm.exception))

    def test_greedy_scalar_default_is_not_char_split(self) -> None:
        spec = self._spec(args=(
            ArgSpec("names", greedy=True, required=False,
                    default="alpha beta"),))
        out = parse_command_args(spec, [])
        self.assertEqual(out["names"], ["alpha beta"])

    def test_greedy_list_default_expands_element_wise(self) -> None:
        spec = self._spec(args=(
            ArgSpec("names", greedy=True, required=False,
                    default=["a", "b"]),))
        self.assertEqual(parse_command_args(spec, [])["names"], ["a", "b"])

    def test_greedy_none_default_is_empty_list(self) -> None:
        spec = self._spec(args=(
            ArgSpec("names", greedy=True, required=False),))
        self.assertEqual(parse_command_args(spec, [])["names"], [])

    def test_greedy_scalar_int_default_becomes_one_element_list(self) -> None:
        spec = self._spec(args=(
            ArgSpec("n", greedy=True, required=False, default=7),))
        self.assertEqual(parse_command_args(spec, [])["n"], [7])


# ------------------------------------------------------- (c) executor authz
class ExecutorAuthzTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.services = FakeServices(owner_ids=[777])  # env-owner ONLY 777
        self.manager = _make_manager()
        COMMAND_HUB.bind(self.manager)

    def tearDown(self) -> None:
        self.manager.close()

    async def _boot(self) -> None:
        await self.manager.attach_account("alpha", {SPY})
        await self.manager.activate_account(
            "alpha", lambda plugin: self.services)

    async def _run(self, invoker: Optional[int]) -> Optional[str]:
        self.services.sent.clear()
        await self.manager.execute_self_command(
            "alpha", "say", ["hi"], CHANNEL, guild_id=GUILD,
            invoker_id=invoker)
        return self.services.sent[-1][1] if self.services.sent else None

    async def test_wired_access_manager_wins_over_env_owners(self) -> None:
        """777 is in the facade's env OWNER_USER_IDS but NOT allowed by
        the wired access manager -> DENIED; 888 is allowed by the access
        manager but not an env owner -> runs. can_invoke is consulted
        (recorded) for both."""
        await self._boot()
        access = RecordingAccess(allowed={888})
        self.manager._access_manager = access
        self.assertIsNone(await self._run(777))  # env owner, access denies
        self.assertEqual(await self._run(888), "hi")  # access allows
        self.assertIn(("alpha", 777), access.calls)
        self.assertIn(("alpha", 888), access.calls)

    async def test_hub_access_backs_up_a_missing_manager_attribute(self) -> None:
        """No _access_manager on the manager: the hub's bootstrap
        access manager is consulted even though services exist (the
        regression skipped it whenever a services object was present)."""
        await self._boot()
        access = RecordingAccess(allowed={OWNER_ID})
        saved = COMMAND_HUB.access
        COMMAND_HUB.access = access
        try:
            self.assertEqual(await self._run(OWNER_ID), "hi")
            self.assertIn(("alpha", OWNER_ID), access.calls)
        finally:
            COMMAND_HUB.access = saved

    async def test_unknown_invoker_denies_even_for_env_owners(self) -> None:
        await self._boot()
        self.assertIsNone(await self._run(None))  # fail-closed, no reply

    async def test_legacy_env_owner_fallback_without_any_access(self) -> None:
        """No access manager anywhere (direct-construction stack): the
        facade's owner list is the last resort — and None still denies."""
        await self._boot()
        self.assertEqual(await self._run(777), "hi")
        self.assertIsNone(await self._run(778))
        self.assertIsNone(await self._run(None))


# ---------------------------------------------------------- (d) admin gate
class AdminGateTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-fix-admin-")
        self.addCleanup(self.tmp.cleanup)
        self.services = FakeServices()
        self.manager = _make_manager(plugin_dir=Path(self.tmp.name))
        COMMAND_HUB.bind(self.manager)
        self.api = FakeFrameworkApi()
        self._saved_provider = COMMAND_HUB.api_provider
        COMMAND_HUB.api_provider = lambda: self.api
        # USER is permitted on the account but NOT an admin; ADMIN is both
        self.access = RecordingAccess(allowed={111}, admins={222})
        self._saved_access = COMMAND_HUB.access
        COMMAND_HUB.access = self.access

    def tearDown(self) -> None:
        COMMAND_HUB.access = self._saved_access
        COMMAND_HUB.api_provider = self._saved_provider
        self.manager.close()

    async def asyncSetUp(self) -> None:
        await self.manager.attach_account("alpha", set())
        await self.manager.activate_account(
            "alpha", lambda plugin: self.services)

    async def _run(self, name: str, args: List[str], *,
                   invoker: int) -> Optional[str]:
        self.services.sent.clear()
        await self.manager.execute_self_command(
            "alpha", name, args, CHANNEL, guild_id=GUILD, invoker_id=invoker)
        return self.services.sent[-1][1] if self.services.sent else None

    async def test_mutating_verbs_denied_for_non_admin_allowed_user(self) -> None:
        for name, args in (
            ("accounts", ["start", "alpha"]),
            ("accounts", ["stop", "alpha"]),
            ("plugins", ["alpha", "spy", "off"]),
        ):
            with self.subTest(verb=f"{name} {args}"):
                reply = await self._run(name, args, invoker=111)
                self.assertEqual(reply, "error: admins only")
        self.assertEqual(self.api.calls, [])  # nothing reached the API

    async def test_mutating_verbs_allowed_for_admin(self) -> None:
        self.assertEqual(
            await self._run("accounts", ["start", "alpha"], invoker=222),
            "started alpha")
        self.assertEqual(
            await self._run("accounts", ["stop", "alpha"], invoker=222),
            "stopped alpha")
        self.assertEqual(
            await self._run("plugins", ["alpha", "spy", "on"], invoker=222),
            "spy enabled on alpha")
        self.assertIn(("accounts_start", "alpha"), self.api.calls)
        self.assertIn(("plugins_set_enabled", "alpha", "spy", True),
                      self.api.calls)

    async def test_read_verbs_stay_open_to_permitted_non_admins(self) -> None:
        reply = await self._run("accounts", ["list"], invoker=111)
        self.assertIn("alpha: running", reply)
        reply = await self._run("plugins", ["alpha"], invoker=111)
        self.assertIn("+ spy", reply)
        self.assertIn(("accounts_list",), self.api.calls)
        self.assertIn(("plugins_state", "alpha"), self.api.calls)

    async def test_estop_stays_ungated_for_allowed_users(self) -> None:
        """SAFETY channel: estop must remain invokable by any permitted
        user (its docstring pins 'always invokable')."""
        reply = await self._run("estop", [], invoker=111)
        self.assertIn("GLOBAL EMERGENCY STOP", reply)
        self.assertIn(("emergency_stop_all",), self.api.calls)


# ----------------------------------------------------- (e) dispatch flood
class DispatchBoundTests(unittest.IsolatedAsyncioTestCase):

    async def test_flood_bounded_inflight_and_dropped_counted(self) -> None:
        manager = _make_manager(dispatch_inflight_limit=5)
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices())
        inst = manager._accounts["alpha"][SPY].instance
        gate = asyncio.Event()
        handled: List[Any] = []

        async def slow(ctx, event):
            handled.append(event)
            await gate.wait()  # park every dispatch "forever"

        inst.handle_event = slow
        for i in range(100):
            manager._bus.publish_nowait(MessageReceived(
                account="alpha", message_id=i, channel_id=5, author_id=9,
                content="flood",
            ))
        await asyncio.sleep(0.2)  # let the bus dispatcher drain its queue
        stats = manager.dispatch_stats("alpha")
        # INVARIANTS (scheduler-independent): the live-task set is bounded
        # (running cap 5 + backlog net 4x cap = 20), every event is either
        # admitted (parked at the gate) or counted as dropped, and the
        # drop counter survives until the account detaches.
        self.assertLessEqual(stats["in_flight"], 20)
        self.assertGreaterEqual(stats["dropped"], 80)
        self.assertEqual(stats["dropped"], 100 - stats["in_flight"])
        self.assertEqual(len(handled), stats["in_flight"])
        gate.set()
        await asyncio.sleep(0.1)
        self.assertEqual(manager.dispatch_stats("alpha")["in_flight"], 0)
        self.assertEqual(
            manager.dispatch_stats("alpha")["dropped"], stats["dropped"])
        manager.close()

    async def test_sustained_flood_caps_running_handlers(self) -> None:
        """Events arriving OVER TIME (realistic traffic spacing — each
        publish gives the loop a tick) park exactly `limit` handlers and
        drop everything after: the tier-1 running bound biting alone."""
        manager = _make_manager(dispatch_inflight_limit=5)
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices())
        inst = manager._accounts["alpha"][SPY].instance
        gate = asyncio.Event()
        handled: List[Any] = []

        async def slow(ctx, event):
            handled.append(event)
            await gate.wait()

        inst.handle_event = slow
        for i in range(60):
            manager._bus.publish_nowait(MessageReceived(
                account="alpha", message_id=i, channel_id=5, author_id=9,
                content="flood",
            ))
            await asyncio.sleep(0.001)  # real tick: dispatch tasks start & park
        await asyncio.sleep(0.1)
        stats = manager.dispatch_stats("alpha")
        self.assertLessEqual(stats["running"], 5)
        self.assertGreater(stats["dropped"], 0)
        self.assertEqual(len(handled), stats["in_flight"])
        gate.set()
        manager.close()

    async def test_normal_traffic_never_drops(self) -> None:
        """A zero-gap burst of events with INSTANT handlers is normal
        traffic (a busy channel replaying history): the bus dispatcher
        yields only every SLICE_EVENTS, so none of these tasks may have
        started when the later admission checks run — the two-tier bound
        must not misread that scheduling artifact as a flood."""
        manager = _make_manager(dispatch_inflight_limit=5)
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices())
        inst = manager._accounts["alpha"][SPY].instance
        seen: List[Any] = []

        async def fast(ctx, event):
            seen.append(event)

        inst.handle_event = fast
        for i in range(20):
            manager._bus.publish_nowait(MessageReceived(
                account="alpha", message_id=i, channel_id=5, author_id=9,
                content="ok",
            ))
        await asyncio.sleep(0.2)
        self.assertEqual(len(seen), 20)
        self.assertEqual(manager.dispatch_stats("alpha")["dropped"], 0)
        manager.close()


# ------------------------------------------- (f) profile layer at attach
class ProfileSettingsLayerTests(unittest.IsolatedAsyncioTestCase):

    def _manager(self, bindings, profiles, global_layer, repo):
        repo._db = FakeDatabase(bindings, profiles)
        return _make_manager(repo=repo, global_layer=global_layer)

    async def test_attach_resolves_defaults_global_profile_account(self):
        repo = FakePluginRepo()
        manager = self._manager(
            bindings={"alpha": {SPY: "night"}},
            profiles={(SPY, "night"): {"count": 7}},
            global_layer={"spy": {"greeting": "global-hello", "count": 5}},
            repo=repo,
        )
        # account override wins over profile+global for greeting
        await manager._settings.load()
        await manager._settings.set_plugin_setting(
            "alpha", SPY, "greeting", "account-hello",
            manager._schema_for(SPY))
        await manager.attach_account("alpha", {SPY})
        settings = manager._accounts["alpha"][SPY].settings
        self.assertEqual(settings["greeting"], "account-hello")  # account
        self.assertEqual(settings["count"], 7)                   # profile
        self.assertEqual(settings["enabled_flag"], True)         # default
        await manager.activate_account("alpha", FakeServices())
        self.assertEqual(manager._accounts["alpha"][SPY].settings["count"], 7)
        manager.close()

    async def test_activate_resolves_the_same_precedence(self):
        """A profile bound AFTER attach still reaches the live settings
        at the next activation (activate re-resolves)."""
        repo = FakePluginRepo()
        bindings: Dict[str, Dict[str, Optional[str]]] = {"alpha": {}}
        profiles = {(SPY, "night"): {"count": 9}}
        manager = self._manager(
            bindings=bindings, profiles=profiles,
            global_layer={"spy": {"count": 5}}, repo=repo)
        await manager._settings.load()
        await manager.attach_account("alpha", {SPY})
        self.assertEqual(manager._accounts["alpha"][SPY].settings["count"], 5)
        bindings["alpha"][SPY] = "night"  # bind out-of-band
        await manager.activate_account("alpha", FakeServices())
        self.assertEqual(manager._accounts["alpha"][SPY].settings["count"], 9)
        manager.close()

    async def test_manager_effective_view_includes_the_global_layer(self):
        repo = FakePluginRepo()
        manager = self._manager(
            bindings={"alpha": {}}, profiles={},
            global_layer={"spy": {"count": 4}}, repo=repo)
        await manager._settings.load()
        await manager.attach_account("alpha", {SPY})
        await manager.activate_account("alpha", FakeServices())
        api = manager._settings_api("alpha", SPY)
        effective = await api.effective()
        self.assertEqual(effective["count"], 4)  # global visible in the view
        self.assertIsNone(effective.get("channels"))
        manager.close()

    async def test_clear_setting_unmasks_to_the_profile_layer(self):
        repo = FakePluginRepo()
        manager = self._manager(
            bindings={"alpha": {SPY: "night"}},
            profiles={(SPY, "night"): {"count": 8}},
            global_layer={"spy": {"count": 5}}, repo=repo)
        await manager._settings.load()
        await manager.attach_account("alpha", {SPY})
        await manager.activate_account("alpha", FakeServices())
        await manager.set_setting("alpha", SPY, "count", 10)
        self.assertEqual(
            manager._accounts["alpha"][SPY].settings["count"], 10)
        effective = await manager.clear_setting("alpha", SPY, "count")
        self.assertEqual(effective, 8)  # profile layer unmasked, not global
        self.assertEqual(
            manager._accounts["alpha"][SPY].settings["count"], 8)
        manager.close()


# ---------------------------------------------- boot dashboard-UI lifecycle
class BootDashboardUITests(unittest.IsolatedAsyncioTestCase):

    _SRC = "\n".join([
        "name = 'uifix'",
        "version = '1.0.0'",
        "def __init__(self):",
        "    self.marker = 'throwaway'",
        "def initialize_dashboard_ui(self, ui):",
        "    button = ui.Button(component_id='btn').set_label('Go')",
        "    button.set_action(lambda ctx, payload: 'clicked:' + self.marker)",
        "    page = ui.Page(component_id='page').set_title('UI Fix')",
        "    page.add_section(ui.Section(component_id='sec')",
        "                     .add_component(button))",
        "    self.attach_dashboard_ui(ui.Layout().add_page(page))",
    ])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-fix-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.plugin_dir = Path(self.tmp.name)
        (self.plugin_dir / "uifix.py").write_text(_plugin_src(self._SRC))
        from core.dashboard_ui import ActionContext, DashboardUIRegistry
        self.registry = DashboardUIRegistry()
        self.ActionContext = ActionContext

    def _manager(self) -> PluginManager:
        bus = EventBus()
        return PluginManager(
            bus, LOG, SettingsManager(bus, LOG),
            plugin_dir=str(self.plugin_dir), ui_registry=self.registry)

    async def _click(self) -> str:
        result = await self.registry.dispatch_action(
            "uifix", "btn", {},
            self.ActionContext(plugin="uifix", component_id="btn",
                               account="alpha"))
        return result.message

    async def test_live_instance_takes_over_the_boot_ui(self) -> None:
        manager = self._manager()
        manager.discover()
        self.assertEqual(manager.init_boot_dashboard_uis(), 1)
        self.assertIn("uifix", self.registry.list_plugins())
        self.assertEqual(await self._click(), "clicked:throwaway")

        # activation REPLACES the throwaway registration with the LIVE
        # instance's (registry.attach re-attaches validated layouts)
        await manager.attach_account("alpha", {"uifix"})
        live = manager._accounts["alpha"]["uifix"].instance
        live.marker = "live"
        await manager.activate_account("alpha", FakeServices())
        self.assertEqual(await self._click(), "clicked:live")
        manager.close()

    async def test_boot_registration_survives_deactivation(self) -> None:
        """The last account going down must not tear down a BOOT-owned
        plugin UI (dashboard pages exist with no account connected)."""
        manager = self._manager()
        manager.discover()
        manager.init_boot_dashboard_uis()
        await manager.attach_account("alpha", {"uifix"})
        await manager.deactivate_account("alpha")
        self.assertIn("uifix", self.registry.list_plugins())
        self.assertIsNotNone(self.registry.get_layout("uifix"))
        manager.close()

    async def test_taken_over_ui_detaches_with_its_account(self) -> None:
        """Once a LIVE instance owns the registration it is
        account-owned again: the last deactivation detaches it."""
        manager = self._manager()
        manager.discover()
        manager.init_boot_dashboard_uis()
        await manager.attach_account("alpha", {"uifix"})
        await manager.activate_account("alpha", FakeServices())
        self.assertNotIn("uifix", manager._boot_ui_plugins)
        await manager.deactivate_account("alpha")
        self.assertNotIn("uifix", self.registry.list_plugins())
        manager.close()


# ------------------------------------------------------ loader purge (9)
class LoaderFailurePurgeTests(unittest.TestCase):

    def test_failed_discover_purges_its_synthetic_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good.py").write_text(_plugin_src(
                "name = 'good'\nversion = '1.0.0'\n"))
            bad = root / "bad"
            bad.mkdir()
            (bad / "__init__.py").write_text(
                "raise RuntimeError('package import-time death')\n")
            loader = PluginLoader(root)
            before = {m for m in sys.modules
                      if m.startswith("_mudae_plugroot")}
            with self.assertRaises(Exception):
                loader.discover()
            # the synthetic parent AND every module this discover pulled
            # in are gone (OTHER loaders' live modules are not ours to
            # judge — scope the assertion to the delta)
            delta = {m for m in sys.modules
                     if m.startswith("_mudae_plugroot")} - before
            self.assertEqual(delta, set(), f"leaked modules: {delta}")
            # and the loader is retry-able: healing the tree works
            (bad / "__init__.py").write_text(
                "from core.plugin_base import PluginBase\n"
                "class BadPlugin(PluginBase):\n"
                "    name = 'bad'\n"
            )
            records = loader.discover()
            self.assertEqual(sorted(records), ["bad", "good"])


# --------------------------------------------- set_enabled active clearing
class SetEnabledActiveClearTests(unittest.IsolatedAsyncioTestCase):

    async def test_disable_clears_active_before_the_hook(self) -> None:
        manager = _make_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", FakeServices())
        runtime = manager._accounts["alpha"][SPY]
        self.assertTrue(runtime.active)
        observed: List[bool] = []

        async def spy_disable(ctx):
            observed.append(runtime.active)  # must already be False

        runtime.instance.on_disable = spy_disable
        await manager.set_enabled("alpha", SPY, False)
        self.assertFalse(runtime.active)
        self.assertEqual(observed, [False])  # cleared BEFORE on_disable
        manager.close()


if __name__ == "__main__":
    unittest.main()
