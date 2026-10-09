"""ControlAPI surface tests for plugin profiles (style of
test_sync_groups_surfaces.py): real Database + real repositories,
duck-typed account/plugin managers and audit recorder.

Pins: profile CRUD validation (unknown plugin / unknown setting key /
bad value type / channels scope), binding-requires-assignment, the
effective-settings merge order (defaults < profile < account overrides)
with channels passthrough, accounts_channels offline->[] and
wired->snapshot, in-memory record refresh, and audit rows.
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from core.api import ControlAPI
from core.errors import AccountError, SettingsValidationError
from core.plugin_scopes import validate as validate_scope
from core.settings_manager import Setting

from database.db import Database

LOG = logging.getLogger("test.plugin_profiles_surfaces")

#: a roll-like schema: every Setting type + bounds the validation paths hit
SCHEMA = (
    Setting("trigger", "str", "roll", "trigger word"),
    Setting("command", "str", "$wa", "roulette command"),
    Setting("roll_interval", "float", 2.0, "seconds between rolls",
            min_value=1.5, max_value=120.0),
    Setting("userstack", "bool", True, "send $us 20"),
)

SCOPE = {"mode": "allow",
         "rules": [{"guild_id": "111", "channel_ids": ["aaa", "bbb"]}]}


class _StubAudit:
    """AuditRecorder double: records every _audit_mgmt write."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def record(self, **kwargs) -> None:
        self.rows.append(kwargs)

    def actions(self) -> List[str]:
        return [row.get("action") for row in self.rows]


class _StubClient:
    """Duck-typed client wrapper with a channels_snapshot."""

    def __init__(self, snapshot: List[Dict[str, Any]]) -> None:
        self._snapshot = snapshot

    def channels_snapshot(self) -> List[Dict[str, Any]]:
        return self._snapshot


class _StubAccount:
    """Account-shaped stub: assigned plugins, binding mirror, runtime."""

    def __init__(self, name: str, client: Any = None) -> None:
        self.record = SimpleNamespace(name=name, sync_group=None)
        self.assigned_plugins: List[str] = []
        self.plugin_profiles: Dict[str, Optional[str]] = {}
        self.runtime = SimpleNamespace(client=client)


class _StubAccountsManager:
    """Duck-typed AccountManager: the surfaces ControlAPI touches."""

    def __init__(self) -> None:
        self._accounts: Dict[str, _StubAccount] = {}

    def add(self, name: str, client: Any = None) -> _StubAccount:
        account = _StubAccount(name, client)
        self._accounts[name] = account
        return account

    def get(self, name: str) -> _StubAccount:
        try:
            return self._accounts[name]
        except KeyError:
            raise AccountError(f"unknown account {name!r}") from None

    async def assigned_plugins(self, name: str) -> List[str]:
        return sorted(self.get(name).assigned_plugins)

    async def channels_snapshot(self, name: str) -> List[Dict[str, Any]]:
        return self.get(name).runtime.client.channels_snapshot()


class _StubPluginManager:
    """Duck-typed PluginManager: names() + _schema_for()."""

    def __init__(self, names: List[str]) -> None:
        self._names = list(names)

    def names(self) -> List[str]:
        return list(self._names)

    def _schema_for(self, plugin: str) -> tuple:
        if plugin not in self._names:
            raise ValueError(f"unknown plugin {plugin!r}")
        return SCHEMA


SNAPSHOT = [
    {"guild_id": "111", "guild_name": "Alpha Guild",
     "channels": [{"id": "aaa", "name": "general", "type": "text"}]},
]


class ProfileSurfaceTestBase(unittest.IsolatedAsyncioTestCase):
    """Real Database + stub managers, wired exactly like the bootstrap."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-api-profiles-")
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self) -> None:
        self.db = Database(Path(self.tmp.name) / "api.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        await self.db.accounts.add("alpha")
        await self.db.account_plugins.assign_plugin("alpha", "roll")
        self.accounts_mgr = _StubAccountsManager()
        self.alpha = self.accounts_mgr.add("alpha", _StubClient(SNAPSHOT))
        self.alpha.assigned_plugins = ["roll"]
        self.audit = _StubAudit()
        self.api = ControlAPI(
            accounts=self.accounts_mgr,
            plugins=_StubPluginManager(["roll", "snipe"]),
            profiles=SimpleNamespace(),
            settings=SimpleNamespace(),
            limiter=SimpleNamespace(),
            scheduler=SimpleNamespace(),
            logs=SimpleNamespace(),
            audit=self.audit,
            database=self.db,
        )

    async def _assign(self, account: str, plugin: str) -> None:
        """Test-side assignment: repo row + stub-manager mirror."""
        await self.db.account_plugins.assign_plugin(account, plugin)
        stub = self.accounts_mgr.get(account)
        if plugin not in stub.assigned_plugins:
            stub.assigned_plugins = sorted([*stub.assigned_plugins, plugin])


# --------------------------------------------------------------- CRUD
class ProfileCrudTests(ProfileSurfaceTestBase):
    async def test_create_and_list_roundtrip(self) -> None:
        created = await self.api.plugin_profile_create(
            "roll", "grind", "fast", {"trigger": "go", "command": "$m"},
            surface="cli")
        self.assertEqual(created["plugin"], "roll")
        self.assertEqual(created["name"], "grind")
        self.assertEqual(created["description"], "fast")
        self.assertEqual(created["settings"],
                         {"trigger": "go", "command": "$m"})
        listed = await self.api.plugin_profiles_list()
        self.assertEqual([row["name"] for row in listed], ["grind"])
        self.assertEqual(listed[0]["settings"]["trigger"], "go")

    async def test_list_filters_by_plugin(self) -> None:
        await self.api.plugin_profile_create("roll", "a")
        await self.api.plugin_profile_create("roll", "b")
        rows = await self.api.plugin_profiles_list(plugin="roll")
        self.assertEqual([r["name"] for r in rows], ["a", "b"])
        self.assertEqual(await self.api.plugin_profiles_list(plugin="snipe"),
                         [])

    async def test_create_unknown_plugin_lists_known(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.api.plugin_profile_create("rll", "grind")
        self.assertIn("known plugins", str(ctx.exception))
        self.assertIn("roll", str(ctx.exception))

    async def test_create_unknown_settings_key_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.api.plugin_profile_create(
                "roll", "grind", settings={"triger": "go"})
        self.assertIn("triger", str(ctx.exception))
        self.assertIn("known", str(ctx.exception))

    async def test_create_bad_value_type_rejected(self) -> None:
        with self.assertRaises(SettingsValidationError):
            await self.api.plugin_profile_create(
                "roll", "grind", settings={"roll_interval": "fast"})
        with self.assertRaises(SettingsValidationError):  # below min bound
            await self.api.plugin_profile_create(
                "roll", "grind", settings={"roll_interval": 0.5})

    async def test_create_settings_validated_same_as_set_setting(self) -> None:
        # the set-setting path COERCES numeric strings; so does the profile
        created = await self.api.plugin_profile_create(
            "roll", "grind", settings={"roll_interval": "3"})
        self.assertEqual(created["settings"]["roll_interval"], 3.0)

    async def test_create_channels_scope_stored_normalized(self) -> None:
        created = await self.api.plugin_profile_create(
            "roll", "grind", settings={"channels": {"mode": "allow"}})
        self.assertEqual(created["settings"]["channels"],
                         {"mode": "allow", "rules": []})

    async def test_create_bad_channels_scope_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.api.plugin_profile_create(
                "roll", "grind",
                settings={"channels": {"mode": "whitelist"}})
        self.assertIn("mode", str(ctx.exception))

    async def test_create_duplicate_rejected(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        with self.assertRaises(Exception):
            await self.api.plugin_profile_create("roll", "grind")

    async def test_update_description_settings_and_rename(self) -> None:
        await self.api.plugin_profile_create("roll", "grind", "old")
        updated = await self.api.plugin_profile_update(
            "roll", "grind", description="new",
            settings={"trigger": "hi"}, surface="dashboard")
        self.assertEqual(updated["description"], "new")
        self.assertEqual(updated["settings"]["trigger"], "hi")
        renamed = await self.api.plugin_profile_update(
            "roll", "grind", name_new="farm")
        self.assertEqual(renamed["name"], "farm")

    async def test_update_requires_at_least_one_field(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        with self.assertRaises(ValueError) as ctx:
            await self.api.plugin_profile_update("roll", "grind")
        self.assertIn("at least one", str(ctx.exception))

    async def test_update_unknown_profile_lists_known(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        with self.assertRaises(ValueError) as ctx:
            await self.api.plugin_profile_update("roll", "ghost",
                                                 description="d")
        self.assertIn("known", str(ctx.exception))

    async def test_rename_refuses_duplicate(self) -> None:
        await self.api.plugin_profile_create("roll", "a")
        await self.api.plugin_profile_create("roll", "b")
        with self.assertRaises(ValueError):  # surfaced as 400, not 500
            await self.api.plugin_profile_update("roll", "a", name_new="b")

    async def test_delete_and_idempotent_redelete(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        out = await self.api.plugin_profile_delete("roll", "grind",
                                                   surface="cli")
        self.assertEqual(out, {"plugin": "roll", "profile": "grind",
                               "existed": True})
        out = await self.api.plugin_profile_delete("roll", "grind")
        self.assertFalse(out["existed"])

    async def test_audit_rows_written(self) -> None:
        await self.api.plugin_profile_create("roll", "grind", surface="cli")
        await self.api.plugin_profile_update("roll", "grind",
                                             description="d",
                                             surface="dashboard")
        await self.api.plugin_profile_delete("roll", "grind", surface="cli")
        self.assertEqual(
            self.audit.actions(),
            ["mgmt:plugin_profile.created", "mgmt:plugin_profile.updated",
             "mgmt:plugin_profile.deleted"])
        note = self.audit.rows[0]["note"]
        self.assertIn("surface=cli", note)
        self.assertIn("plugin=roll", note)


# ------------------------------------------------------------ bindings
class BindingTests(ProfileSurfaceTestBase):
    async def test_bind_and_read_back(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        out = await self.api.accounts_plugin_profile(
            "alpha", "roll", "grind", surface="dashboard")
        self.assertEqual(out, {"account": "alpha", "plugin": "roll",
                               "profile": "grind"})
        # memory record follows (status-poll mirror, F3 pattern)
        self.assertEqual(self.alpha.plugin_profiles, {"roll": "grind"})
        # every ASSIGNED plugin appears, unbound ones as None
        await self._assign("alpha", "snipe")
        self.assertEqual(
            await self.api.accounts_plugin_profiles("alpha"),
            {"roll": "grind", "snipe": None})

    async def test_clear_binding_with_none(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        await self.api.accounts_plugin_profile("alpha", "roll", "grind")
        out = await self.api.accounts_plugin_profile("alpha", "roll", None)
        self.assertIsNone(out["profile"])
        self.assertEqual(self.alpha.plugin_profiles, {"roll": None})
        self.assertEqual(
            await self.api.accounts_plugin_profiles("alpha"), {"roll": None})

    async def test_binding_requires_assignment(self) -> None:
        await self.api.plugin_profile_create("snipe", "quiet")
        with self.assertRaises(ValueError) as ctx:
            await self.api.accounts_plugin_profile("alpha", "snipe", "quiet")
        self.assertIn("not assigned", str(ctx.exception))

    async def test_binding_unknown_plugin_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.accounts_plugin_profile("alpha", "rll", "x")

    async def test_binding_unknown_account_rejected(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        with self.assertRaises(AccountError):
            await self.api.accounts_plugin_profile("ghost", "roll", "grind")

    async def test_binding_unknown_profile_lists_known(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        with self.assertRaises(ValueError) as ctx:
            await self.api.accounts_plugin_profile("alpha", "roll", "ghost")
        self.assertIn("known", str(ctx.exception))
        self.assertIn("grind", str(ctx.exception))

    async def test_binding_wrong_plugin_profile_rejected(self) -> None:
        await self.api.plugin_profile_create("snipe", "quiet")
        with self.assertRaises(ValueError):
            # a snipe profile can never bind to roll
            await self.api.accounts_plugin_profile("alpha", "roll", "quiet")

    async def test_binding_audit_row_written(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        await self.api.accounts_plugin_profile("alpha", "roll", "grind",
                                               surface="dashboard")
        row = self.audit.rows[-1]
        self.assertEqual(row["action"], "mgmt:account.plugin_profile")
        self.assertEqual(row["account"], "alpha")
        self.assertIn("surface=dashboard", row["note"])
        self.assertIn("profile=grind", row["note"])

    async def test_delete_cascades_binding_and_memory(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        await self.api.accounts_plugin_profile("alpha", "roll", "grind")
        await self.api.plugin_profile_delete("roll", "grind")
        # DB cascade + memory refresh: the binding reads back as None
        self.assertEqual(
            await self.api.accounts_plugin_profiles("alpha"), {"roll": None})
        self.assertEqual(self.alpha.plugin_profiles, {"roll": None})

    async def test_rename_refreshes_memory_binding(self) -> None:
        await self.api.plugin_profile_create("roll", "grind")
        await self.api.accounts_plugin_profile("alpha", "roll", "grind")
        await self.api.plugin_profile_update("roll", "grind", name_new="farm")
        self.assertEqual(self.alpha.plugin_profiles, {"roll": "farm"})
        self.assertEqual(
            await self.api.accounts_plugin_profiles("alpha"),
            {"roll": "farm"})


# --------------------------------------------------- effective settings
class EffectiveSettingsTests(ProfileSurfaceTestBase):
    async def test_merge_order_defaults_profile_account(self) -> None:
        await self.api.plugin_profile_create(
            "roll", "grind",
            settings={"trigger": "go", "roll_interval": 5.0,
                      "channels": SCOPE})
        await self.api.accounts_plugin_profile("alpha", "roll", "grind")
        # the account override wins over BOTH default and profile
        await self.db.plugins.set_setting("alpha", "roll", "trigger", "mine")

        effective = await self.api.accounts_effective_settings("alpha",
                                                               "roll")
        self.assertEqual(effective["trigger"], "mine")     # account layer
        self.assertEqual(effective["roll_interval"], 5.0)  # profile layer
        self.assertEqual(effective["command"], "$wa")      # schema default
        self.assertEqual(effective["userstack"], True)     # schema default
        self.assertEqual(effective["channels"], validate_scope(SCOPE))

    async def test_no_profile_gives_defaults_and_null_scope(self) -> None:
        effective = await self.api.accounts_effective_settings("alpha",
                                                               "roll")
        self.assertEqual(effective["trigger"], "roll")
        self.assertEqual(effective["command"], "$wa")
        self.assertIsNone(effective["channels"])

    async def test_account_override_alone_beats_default(self) -> None:
        await self.db.plugins.set_setting("alpha", "roll", "command", "$m")
        effective = await self.api.accounts_effective_settings("alpha",
                                                               "roll")
        self.assertEqual(effective["command"], "$m")

    async def test_requires_assignment(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.api.accounts_effective_settings("alpha", "snipe")
        self.assertIn("not assigned", str(ctx.exception))

    async def test_unknown_account_rejected(self) -> None:
        with self.assertRaises(AccountError):
            await self.api.accounts_effective_settings("ghost", "roll")

    async def test_unknown_plugin_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.accounts_effective_settings("alpha", "rll")


# --------------------------------------------------------------- channels
class ChannelsTests(ProfileSurfaceTestBase):
    async def test_wired_client_returns_snapshot(self) -> None:
        self.assertEqual(
            await self.api.accounts_channels("alpha"), SNAPSHOT)

    async def test_offline_account_returns_empty(self) -> None:
        self.accounts_mgr.add("beta")  # no client at all
        self.assertEqual(await self.api.accounts_channels("beta"), [])

    async def test_client_without_snapshot_method_returns_empty(self) -> None:
        self.accounts_mgr.add("gamma", SimpleNamespace())  # duck-typed double
        self.assertEqual(await self.api.accounts_channels("gamma"), [])

    async def test_unknown_account_rejected(self) -> None:
        with self.assertRaises(AccountError):
            await self.api.accounts_channels("ghost")


# -------------------------------------------------------------- unwired
class UnwiredTests(unittest.IsolatedAsyncioTestCase):
    """Stacks built without a database: reads degrade, mutations fail."""

    async def test_reads_degrade(self) -> None:
        api = ControlAPI(
            accounts=_StubAccountsManager(),
            plugins=_StubPluginManager(["roll"]),
            profiles=SimpleNamespace(), settings=SimpleNamespace(),
            limiter=SimpleNamespace(), scheduler=SimpleNamespace(),
            logs=SimpleNamespace(),
        )
        self.assertEqual(await api.plugin_profiles_list(), [])

    async def test_mutations_fail_loudly(self) -> None:
        api = ControlAPI(
            accounts=_StubAccountsManager(),
            plugins=_StubPluginManager(["roll"]),
            profiles=SimpleNamespace(), settings=SimpleNamespace(),
            limiter=SimpleNamespace(), scheduler=SimpleNamespace(),
            logs=SimpleNamespace(),
        )
        for call in (
            lambda: api.plugin_profile_create("roll", "g"),
            lambda: api.plugin_profile_update("roll", "g", description="d"),
            lambda: api.plugin_profile_delete("roll", "g"),
            lambda: api.accounts_plugin_profile("alpha", "roll", "g"),
        ):
            with self.assertRaises(RuntimeError):
                await call()


if __name__ == "__main__":
    unittest.main()
