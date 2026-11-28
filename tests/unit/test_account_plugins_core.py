"""Direct account→plugin capability model (core runtime + ControlAPI).

Pins the refactor invariant: the ``account_plugins`` table (via
db.account_plugins or its duck-typed doubles) is the ONLY thing deciding
which plugins an account runs.

  * AccountManager._allowed_plugins returns a CONCRETE set from the
    in-memory assignment mirror — NEVER None. Zero assignments attach
    ZERO plugins; the old "no profile = unrestricted" default is gone.
  * load() bulk-restores via all_assignments() (one read, no per-account
    queries) BEFORE any attach.
  * ControlAPI accounts_plugin_* validate against the DISCOVERED plugin
    set, re-attach immediately, and audit mgmt:account.plugin_*.
  * ProfileManager stays functional but DECOUPLED: profile grants never
    move attachment.
  * Assignment and enabled-state are independent axes.

Repository-level persistence itself is covered by
tests/unit/test_account_plugins.py (Agent 1); this file is the runtime.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

from libzentsuite.core.account_manager import AccountManager
from libzentsuite.core.api import ControlAPI
from libzentsuite.core.credentials import Credentials
from libzentsuite.core.errors import AccountError
from libzentsuite.core.event_bus import EventBus
from libzentsuite.core.plugin_manager import PluginManager
from libzentsuite.core.settings_manager import SettingsManager
from libzentsuite.database.db import Database
from tests.unit.helpers import (
    FakeAudit,
    FakeClientFactory,
    FakePluginManager,
    FakeScheduler,
    FakeSettings,
    make_manager,
)

LOG = logging.getLogger("tests.account_plugins_core")

#: the two production plugins the assertions name (the fake plugin manager
#: accepts ANY set — these names are just stable fixtures)
ROLL, EXPORTER = "roll", "exporter"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"


# --------------------------------------------------------------- attachment
class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    """What the plugin manager receives for a given assignment set."""

    async def test_single_plugin_assignment_attaches_exactly_it(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha", plugins=[ROLL])
        self.assertEqual(h.plugins.attached[-1], ("alpha", {ROLL}))
        self.assertEqual(await h.manager.assigned_plugins("alpha"), [ROLL])

    async def test_multiple_plugins_attach_the_union(self) -> None:
        h = make_manager()
        await h.manager.add_account(
            "alpha", plugins=[EXPORTER, ROLL, EXPORTER]
        )  # dupe is stored once
        self.assertEqual(h.plugins.attached[-1], ("alpha", {EXPORTER, ROLL}))
        self.assertEqual(
            await h.account_plugins.plugins_of("alpha"), [EXPORTER, ROLL]
        )  # sorted + deduped on disk

    async def test_zero_plugins_attaches_empty_set_not_all(self) -> None:
        """THE invariant: no assignments -> NOTHING attached (never the
        discovered-everything 'unrestricted' default of the profile era)."""
        h = make_manager()
        out = await h.manager.add_account("alpha")
        self.assertEqual(h.plugins.attached[-1], ("alpha", set()))
        self.assertIsNotNone(h.plugins.attached[-1][1])  # a set, not None
        self.assertEqual(out["assigned_plugins"], [])
        self.assertEqual(h.account_plugins.rows.get("alpha"), [])

    async def test_assign_and_unassign_re_attach_immediately(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        base = len(h.plugins.attached)

        await h.manager.assign_plugin("alpha", ROLL)
        self.assertEqual(h.plugins.attached[-1], ("alpha", {ROLL}))

        await h.manager.assign_plugin("alpha", EXPORTER)
        self.assertEqual(h.plugins.attached[-1], ("alpha", {EXPORTER, ROLL}))

        await h.manager.unassign_plugin("alpha", ROLL)
        self.assertEqual(h.plugins.attached[-1], ("alpha", {EXPORTER}))
        self.assertEqual(len(h.plugins.attached), base + 3)  # every mutation

    async def test_set_account_plugins_replaces_whole_set(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha", plugins=[ROLL])
        stored = await h.manager.set_account_plugins("alpha", [EXPORTER, "echo"])
        self.assertEqual(stored, ["echo", EXPORTER])
        self.assertEqual(h.plugins.attached[-1], ("alpha", {"echo", EXPORTER}))
        self.assertEqual(await h.account_plugins.plugins_of("alpha"), ["echo", EXPORTER])

    async def test_assignment_mirrors_into_status_poll(self) -> None:
        """The 1 Hz dashboard poll serves fresh assigned_plugins."""
        h = make_manager()
        await h.manager.add_account("alpha", plugins=[ROLL])
        rows = await h.manager.accounts()
        self.assertEqual(rows[0]["assigned_plugins"], [ROLL])

        await h.manager.assign_plugin("alpha", EXPORTER)
        rows = await h.manager.accounts()
        self.assertEqual(rows[0]["assigned_plugins"], [EXPORTER, ROLL])


# -------------------------------------------------------------- persistence
class PersistenceSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_assign_is_idempotent(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha", plugins=[ROLL])
        await h.manager.assign_plugin("alpha", ROLL)  # already assigned
        self.assertEqual(await h.account_plugins.plugins_of("alpha"), [ROLL])
        self.assertEqual(h.plugins.attached[-1], ("alpha", {ROLL}))

    async def test_unassign_missing_reports_existed_false(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha", plugins=[ROLL])
        existed = await h.manager.unassign_plugin("alpha", EXPORTER)
        self.assertFalse(existed)  # not an error
        existed = await h.manager.unassign_plugin("alpha", ROLL)
        self.assertTrue(existed)
        self.assertEqual(await h.account_plugins.plugins_of("alpha"), [])
        self.assertEqual(h.plugins.attached[-1], ("alpha", set()))

    async def test_unknown_account_raises_account_error(self) -> None:
        h = make_manager()
        with self.assertRaises(AccountError):
            await h.manager.assign_plugin("ghost", ROLL)
        with self.assertRaises(AccountError):
            await h.manager.unassign_plugin("ghost", ROLL)
        with self.assertRaises(AccountError):
            await h.manager.set_account_plugins("ghost", [ROLL])
        with self.assertRaises(AccountError):
            await h.manager.assigned_plugins("ghost")

    async def test_remove_account_clears_assignments(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha", plugins=[EXPORTER, ROLL])
        await h.manager.remove_account("alpha")
        self.assertEqual(await h.account_plugins.plugins_of("alpha"), [])
        self.assertNotIn("alpha", await h.account_plugins.all_assignments())
        # a re-created same-named account inherits NOTHING
        out = await h.manager.add_account("alpha")
        self.assertEqual(out["assigned_plugins"], [])
        self.assertEqual(h.plugins.attached[-1], ("alpha", set()))


# ----------------------------------------------------------------- restart
class _CountingAccountPlugins:
    """Delegating double that counts reads (bulk-read proof)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.plugins_of_calls: int = 0
        self.all_assignments_calls: int = 0

    async def assign_plugin(self, account: str, plugin: str) -> Any:
        return await self._inner.assign_plugin(account, plugin)

    async def unassign_plugin(self, account: str, plugin: str) -> Any:
        return await self._inner.unassign_plugin(account, plugin)

    async def plugins_of(self, account: str) -> Any:
        self.plugins_of_calls += 1
        return await self._inner.plugins_of(account)

    async def set_plugins(self, account: str, plugins: Iterable[str]) -> Any:
        return await self._inner.set_plugins(account, plugins)

    async def all_assignments(self) -> Any:
        self.all_assignments_calls += 1
        return await self._inner.all_assignments()


def _manager_over(
    db: Database, *, plugins: Any = None, account_plugins: Any = None
) -> tuple[AccountManager, Any]:
    """AccountManager over a REAL database with fake collaborators.

    Restart tests need real repo roundtrips (accounts + account_plugins
    through one Database); the collaborators stay duck-typed doubles.
    """
    bus = EventBus()
    plugins = plugins if plugins is not None else FakePluginManager()
    settings_view: Any = FakeSettings()  # duck-typed SettingsManager double
    manager = AccountManager(
        bus,
        LOG,
        db.accounts,
        plugins,
        FakeScheduler(),
        settings_view,
        FakeAudit(),
        FakeClientFactory(),
        Credentials.from_values({"DISCORD_TOKEN_ALPHA": "tok-alpha"}),
        account_plugins=account_plugins if account_plugins is not None else db.account_plugins,
    )
    return manager, plugins


class RestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_restores_the_same_set(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-core-plugins-") as tmp:
            db = Database(Path(tmp) / "restart.db")
            await db.connect()
            try:
                first, _plugins1 = _manager_over(db)
                await first.add_account("alpha", plugins=[EXPORTER, ROLL])
                await first.add_account("beta")  # zero plugins

                # "restart": a fresh manager over the SAME database
                second, plugins2 = _manager_over(db)
                await second.load()
                attached = dict(plugins2.attached)
                self.assertEqual(attached["alpha"], {EXPORTER, ROLL})
                self.assertEqual(attached["beta"], set())
                self.assertIsNotNone(attached["beta"])  # set(), never None
                self.assertEqual(
                    (await second.accounts())[0]["assigned_plugins"],
                    [EXPORTER, ROLL],
                )
            finally:
                await db.close()

    async def test_load_uses_one_bulk_read_no_per_account_queries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-core-plugins-") as tmp:
            db = Database(Path(tmp) / "bulk.db")
            await db.connect()
            try:
                counting = _CountingAccountPlugins(db.account_plugins)
                first, _ = _manager_over(db, account_plugins=counting)
                for name in ("alpha", "beta", "gamma"):
                    await first.add_account(name, plugins=[ROLL])

                second, _ = _manager_over(db, account_plugins=counting)
                counting.plugins_of_calls = 0
                counting.all_assignments_calls = 0
                await second.load()
                self.assertEqual(counting.all_assignments_calls, 1)
                self.assertEqual(counting.plugins_of_calls, 0)
                self.assertEqual(len(second.names()), 3)
            finally:
                await db.close()


# ---------------------------------------------------------- profile bindings
class BindingMirrorTests(unittest.IsolatedAsyncioTestCase):
    """The plugin_profiles in-memory mirror: boot restore, status poll,
    and the assignment mutations that implicitly rewrite it."""

    async def test_bindings_restore_at_boot_and_mirror_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-core-bindings-") as tmp:
            db = Database(Path(tmp) / "bindings.db")
            await db.connect()
            try:
                first, _ = _manager_over(db)
                await first.add_account("alpha", plugins=[EXPORTER, ROLL])
                await db.plugin_profiles.create(ROLL, "grind")
                await db.account_plugins.set_profile("alpha", ROLL, "grind")

                second, _ = _manager_over(db)
                await second.load()
                # memory-first read: {plugin: profile or None} for every
                # ASSIGNED plugin
                self.assertEqual(
                    await second.plugin_profiles("alpha"), {ROLL: "grind", EXPORTER: None}
                )
                rows = await second.accounts()
                self.assertEqual(rows[0]["plugin_profiles"], {ROLL: "grind", EXPORTER: None})
            finally:
                await db.close()

    async def test_unassign_clears_the_binding_mirror(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha", plugins=[EXPORTER, ROLL])
        await h.account_plugins.set_profile("alpha", ROLL, "grind")
        await h.manager._refresh_assignment("alpha")
        self.assertEqual(h.manager.get("alpha").plugin_profiles, {ROLL: "grind", EXPORTER: None})
        await h.manager.unassign_plugin("alpha", ROLL)  # row (and its
        # profile column) is gone — the mirror must not keep the name
        self.assertEqual(h.manager.get("alpha").plugin_profiles, {EXPORTER: None})
        self.assertEqual(await h.manager.plugin_profiles("alpha"), {EXPORTER: None})


# --------------------------------------------------------------- ControlAPI
class _LogsStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


class _LimiterStub:
    def stats(self) -> dict[str, Any]:
        return {}

    def warnings(self) -> list[Any]:
        return []


class ApiStack:
    """A real ControlAPI over the make_manager() fake wiring."""

    def __init__(self) -> None:
        self.h = make_manager()
        self.h.plugins.known_names = [EXPORTER, ROLL, "echo"]
        plugins_mgr: Any = self.h.plugins  # duck-typed PluginManager
        settings_mgr: Any = self.h.settings  # duck-typed SettingsManager
        limiter: Any = _LimiterStub()  # duck-typed RateLimitManager
        scheduler: Any = self.h.scheduler  # duck-typed TaskScheduler
        logs: Any = _LogsStub()  # duck-typed LogService
        audit: Any = self.h.audit  # duck-typed AuditRecorder
        self.api = ControlAPI(
            accounts=self.h.manager,
            plugins=plugins_mgr,
            settings=settings_mgr,
            limiter=limiter,
            scheduler=scheduler,
            logs=logs,
            audit=audit,
            credentials=self.h.credentials,
        )


def _actions(audit: FakeAudit) -> list[Any]:
    return [e.get("action") for e in audit.entries]


class ControlAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stack = ApiStack()

    async def test_list_assign_set_roundtrip_shapes(self) -> None:
        api = self.stack.api
        await api.accounts_add({"name": "alpha", "plugins": [ROLL]})
        self.assertEqual(await api.accounts_plugins_list("alpha"), [ROLL])

        out = await api.accounts_plugin_assign("alpha", EXPORTER, surface="cli")
        self.assertEqual(out, {"account": "alpha", "plugin": EXPORTER})
        self.assertEqual(await api.accounts_plugins_list("alpha"), [EXPORTER, ROLL])
        self.assertEqual(self.stack.h.plugins.attached[-1], ("alpha", {EXPORTER, ROLL}))

        out = await api.accounts_plugins_set("alpha", ["echo", EXPORTER])
        self.assertEqual(out, {"account": "alpha", "plugins": ["echo", EXPORTER]})
        self.assertEqual(self.stack.h.plugins.attached[-1], ("alpha", {"echo", EXPORTER}))

        out = await api.accounts_plugin_unassign("alpha", "echo")
        self.assertEqual(out, {"account": "alpha", "plugin": "echo", "existed": True})
        out = await api.accounts_plugin_unassign("alpha", ROLL)  # not assigned
        self.assertEqual(out["existed"], False)
        self.assertEqual(await api.accounts_plugins_list("alpha"), [EXPORTER])

    async def test_assign_rejects_unknown_plugin_listing_known(self) -> None:
        api = self.stack.api
        await api.accounts_add({"name": "alpha"})
        with self.assertRaises(ValueError) as ctx:
            await api.accounts_plugin_assign("alpha", "rol")  # typo
        self.assertIn("rol", str(ctx.exception))
        self.assertIn(ROLL, str(ctx.exception))  # the known list is shown
        self.assertEqual(await api.accounts_plugins_list("alpha"), [])

    async def test_set_is_all_or_nothing_on_unknown_names(self) -> None:
        api = self.stack.api
        await api.accounts_add({"name": "alpha", "plugins": [ROLL]})
        with self.assertRaises(ValueError):
            await api.accounts_plugins_set("alpha", [EXPORTER, "nope"])
        # nothing was written — the pre-existing set survives whole
        self.assertEqual(await api.accounts_plugins_list("alpha"), [ROLL])
        self.assertEqual(self.stack.h.plugins.attached[-1], ("alpha", {ROLL}))

    async def test_unknown_account_raises_account_error(self) -> None:
        api = self.stack.api
        for call in (
            lambda: api.accounts_plugin_assign("ghost", ROLL),
            lambda: api.accounts_plugin_unassign("ghost", ROLL),
            lambda: api.accounts_plugins_set("ghost", [ROLL]),
            lambda: api.accounts_plugins_list("ghost"),
        ):
            with self.assertRaises(AccountError):
                await call()

    async def test_accounts_add_rejects_profiles_key(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.stack.api.accounts_add({"name": "alpha", "profiles": ["basic"]})
        self.assertIn("unknown account payload keys", str(ctx.exception))
        self.assertIn("profiles", str(ctx.exception))
        self.assertEqual(self.stack.h.manager.names(), [])

    async def test_accounts_add_rejects_unknown_plugin(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.stack.api.accounts_add({"name": "alpha", "plugins": [ROLL, "typo"]})
        self.assertIn("typo", str(ctx.exception))
        self.assertEqual(self.stack.h.manager.names(), [])
        self.assertEqual(self.stack.h.account_plugins.rows, {})

    async def test_mutations_are_audited(self) -> None:
        api = self.stack.api
        await api.accounts_add({"name": "alpha", "plugins": [ROLL]})
        await api.accounts_plugin_assign("alpha", EXPORTER)
        await api.accounts_plugin_unassign("alpha", EXPORTER)
        await api.accounts_plugins_set("alpha", ["echo"])
        actions = _actions(self.stack.h.audit)
        for expected in (
            "mgmt:account.plugin_assigned",
            "mgmt:account.plugin_unassigned",
            "mgmt:account.plugin_set",
        ):
            self.assertIn(expected, actions)

    async def test_status_poll_serves_fresh_assignments(self) -> None:
        api = self.stack.api
        await api.accounts_add({"name": "alpha"})
        await api.accounts_plugin_assign("alpha", ROLL)
        rows = await api.accounts_list()
        self.assertEqual(rows[0]["assigned_plugins"], [ROLL])


# ------------------------------------------------ enabled vs assigned (real)
class _PluginRepoFake:
    """Dict-backed PluginRepository (persisted enabled state)."""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], bool] = {}

    async def state(self, account: str, plugin: str) -> bool | None:
        return self.states.get((account, plugin))

    async def set_state(self, account: str, plugin: str, enabled: bool) -> None:
        self.states[(account, plugin)] = bool(enabled)

    async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> None:
        pass

    async def all_account_settings(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {}

    async def upsert_metadata(
        self, name: str, version: str, description: str, manifest: dict[str, Any]
    ) -> None:
        pass


class AssignmentVsEnabledTests(unittest.IsolatedAsyncioTestCase):
    """Assignment and enabled-state are INDEPENDENT axes (real PluginManager
    + real account_plugins table + fixture plugins spy/echo)."""

    async def test_enabled_toggles_and_assignments_never_touch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-core-plugins-") as tmp:
            db = Database(Path(tmp) / "enabled.db")
            await db.connect()
            try:
                bus = EventBus()
                plugin_repo = _PluginRepoFake()
                plugins = PluginManager(
                    bus,
                    LOG,
                    SettingsManager(bus, LOG),
                    plugin_repo=plugin_repo,
                    plugin_dir=str(FIXTURES),
                )
                plugins.discover()
                self.assertIn("spy", plugins.names())

                manager, _ = _manager_over(db, plugins=plugins)
                await manager.add_account("alpha", plugins=["spy"])
                names = [s["name"] for s in plugins.states("alpha")]
                self.assertEqual(names, ["spy"])  # attached exactly spy

                # disable spy: enabled-state flips, assignment rows do NOT
                await plugins.set_enabled("alpha", "spy", False)
                states = {s["name"]: s["enabled"] for s in plugins.states("alpha")}
                self.assertEqual(states, {"spy": False})
                self.assertEqual(await db.account_plugins.plugins_of("alpha"), ["spy"])

                # assign echo: re-attach reads PERSISTED state — spy stays
                # disabled (assignment did not reset enabled) and echo
                # arrives enabled
                await manager.assign_plugin("alpha", "echo")
                states = {s["name"]: s["enabled"] for s in plugins.states("alpha")}
                self.assertEqual(states, {"spy": False, "echo": True})
                self.assertEqual(await db.account_plugins.plugins_of("alpha"), ["echo", "spy"])

                # unassign spy: only echo survives, echo's state untouched
                await manager.unassign_plugin("alpha", "spy")
                states = {s["name"]: s["enabled"] for s in plugins.states("alpha")}
                self.assertEqual(states, {"echo": True})
            finally:
                await db.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
