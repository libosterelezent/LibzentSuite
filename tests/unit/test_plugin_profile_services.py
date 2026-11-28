"""P3 — the plugin-facing profile lifecycle (services facade).

The isolation property under test is STRUCTURAL: the facade's profile
methods carry no account/plugin parameters at all — every operation is
bound to the facade's own (account, plugin) by construction, so a
plugin cannot even express a cross-scope access. The active-profile
pointer (migration v14) follows the same rule.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from libzentsuite.core.account_manager.services import AccountPluginServices
from libzentsuite.core.errors import PluginError
from libzentsuite.database.db import Database

#: the facade types bus/cache as REQUIRED, but these tests exercise the
#: deliberately unwired construction — bind None through Any (runtime
#: accepts it; only the type signature says EventBus/DiscordCache).
_UNWIRED: Any = None


class _Facade:
    """A minimal real services facade over a real profile store."""

    def __init__(self, db: Database, account: str, plugin: str) -> None:
        self.svc = AccountPluginServices(
            account,
            plugin,
            bus=_UNWIRED,
            scheduler=None,
            credentials=None,
            cache=_UNWIRED,
            profile_store=db.account_plugin_profiles,
        )


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "s.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()


class FacadeCrud(_Base):
    async def test_create_get_list_update_delete_through_the_facade(self) -> None:
        fac = _Facade(self.db, "alpha", "exporter").svc
        made = await fac.profile_create("main", description="primary", settings={"x": 1})
        self.assertEqual(made["name"], "main")
        got = await fac.profile_get("main")
        assert got is not None
        self.assertEqual(got["settings"], {"x": 1})
        self.assertEqual([r["name"] for r in await fac.profile_list()], ["main"])
        upd = await fac.profile_update("main", settings={"x": 2})
        assert upd is not None
        self.assertEqual(upd["settings"], {"x": 2})
        self.assertTrue(await fac.profile_delete("main"))
        self.assertIsNone(await fac.profile_get("main"))

    async def test_duplicate_create_raises(self) -> None:
        fac = _Facade(self.db, "alpha", "exporter").svc
        await fac.profile_create("main")
        with self.assertRaises(PluginError):
            await fac.profile_create("main")

    async def test_get_missing_returns_none(self) -> None:
        fac = _Facade(self.db, "alpha", "exporter").svc
        self.assertIsNone(await fac.profile_get("ghost"))


class StructuralIsolation(_Base):
    async def test_same_name_other_scope_is_invisible_and_untouchable(self) -> None:
        export_a = _Facade(self.db, "alpha", "exporter").svc
        probe_a = _Facade(self.db, "alpha", "probe").svc
        export_b = _Facade(self.db, "beta", "exporter").svc
        await export_a.profile_create("main", settings={"who": "A"})
        await probe_a.profile_create("main", settings={"who": "probe"})
        await export_b.profile_create("main", settings={"who": "B"})

        # the OTHER scopes' rows are invisible through this facade
        got = await export_a.profile_get("main")
        assert got is not None
        self.assertEqual(got["settings"]["who"], "A")
        self.assertEqual([r["name"] for r in await export_a.profile_list()], ["main"])

        # mutating "main" through this facade cannot touch the others
        await export_a.profile_update("main", settings={"who": "A2"})
        await export_a.profile_delete("main")
        b_row = await self.db.account_plugin_profiles.get("beta", "exporter", "main")
        p_row = await self.db.account_plugin_profiles.get("alpha", "probe", "main")
        assert b_row is not None and p_row is not None
        self.assertEqual(b_row.settings["who"], "B")
        self.assertEqual(p_row.settings["who"], "probe")

    async def test_facade_methods_have_no_scope_parameters(self) -> None:
        """The escape hatch must not exist: no account/plugin params."""
        import inspect

        for name in (
            "profile_create",
            "profile_get",
            "profile_update",
            "profile_delete",
            "profile_list",
            "profile_set_active",
            "profile_active",
        ):
            sig = inspect.signature(getattr(AccountPluginServices, name))
            self.assertNotIn("account", sig.parameters, name)
            self.assertNotIn("plugin", sig.parameters, name)


class ActiveProfile(_Base):
    async def test_pointer_roundtrip_and_persistence(self) -> None:
        fac = _Facade(self.db, "alpha", "exporter").svc
        await fac.profile_create("main")
        await fac.profile_create("alt")
        self.assertIsNone(await fac.profile_active())
        await fac.profile_set_active("main")
        self.assertEqual(await fac.profile_active(), "main")
        await fac.profile_set_active("alt")
        self.assertEqual(await fac.profile_active(), "alt")

    async def test_setting_active_to_missing_profile_fails_closed(self) -> None:
        fac = _Facade(self.db, "alpha", "exporter").svc
        with self.assertRaises(PluginError):
            await fac.profile_set_active("ghost")

    async def test_deleting_the_active_profile_clears_the_pointer(self) -> None:
        fac = _Facade(self.db, "alpha", "exporter").svc
        await fac.profile_create("main")
        await fac.profile_set_active("main")
        await fac.profile_delete("main")
        self.assertIsNone(await fac.profile_active())

    async def test_pointer_is_scoped_and_survives_restart(self) -> None:
        fac = _Facade(self.db, "alpha", "exporter").svc
        other = _Facade(self.db, "alpha", "probe").svc
        await fac.profile_create("main")
        await other.profile_create("main")
        await fac.profile_set_active("main")
        self.assertIsNone(await other.profile_active())  # per (account, plugin)
        await self.db.close()
        self.db = Database(Path(self._tmp.name) / "s.db")
        await self.db.connect()
        fac2 = AccountPluginServices(
            "alpha",
            "exporter",
            bus=_UNWIRED,
            scheduler=None,
            credentials=None,
            cache=_UNWIRED,
            profile_store=self.db.account_plugin_profiles,
        )
        self.assertEqual(await fac2.profile_active(), "main")

    async def test_account_deletion_cascades_the_pointer(self) -> None:
        await self.db.accounts.add("alpha")
        await _Facade(self.db, "alpha", "exporter").svc.profile_create("main")
        await _Facade(self.db, "alpha", "exporter").svc.profile_set_active("main")
        await self.db.accounts.delete("alpha")
        self.assertIsNone(await self.db.account_plugin_profiles.get_active("alpha", "exporter"))


class Unwired(_Base):
    async def test_unwired_store_fails_honest(self) -> None:
        fac = AccountPluginServices(
            "alpha",
            "exporter",
            bus=_UNWIRED,
            scheduler=None,
            credentials=None,
            cache=_UNWIRED,
        )
        with self.assertRaises(RuntimeError):
            await fac.profile_list()


if __name__ == "__main__":
    unittest.main()
