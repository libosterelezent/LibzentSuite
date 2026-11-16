"""Account-owned plugin profiles — the two-level model's P1/P2 contract
(docs/design/data-management-adoption.md).

The ownership invariant under test: a profile instance belongs to
exactly one (account, plugin, name). Identical names across accounts
and across plugins are DIFFERENT profiles; every read/write/delete
carries the full scope; account deletion cascades.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.errors import DatabaseError
from database.db import Database


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "p.db")
        await self.db.connect()
        self.repo = self.db.account_plugin_profiles

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()


class CrudRoundtrip(_Base):
    async def test_create_get_roundtrip_preserves_payload_verbatim(self) -> None:
        record = await self.repo.create(
            "alpha",
            "exporter",
            "main",
            description="primary",
            settings={"channels": ["123"], "chunk": 1800},
            schema_version=3,
        )
        self.assertEqual(record.account, "alpha")
        self.assertEqual(record.plugin, "exporter")
        self.assertEqual(record.name, "main")
        fetched = await self.repo.get("alpha", "exporter", "main")
        assert fetched is not None
        self.assertEqual(fetched.settings, {"channels": ["123"], "chunk": 1800})
        self.assertEqual(fetched.schema_version, 3)
        self.assertEqual(fetched.description, "primary")

    async def test_duplicate_name_fails_clean(self) -> None:
        await self.repo.create("alpha", "exporter", "main")
        with self.assertRaises(DatabaseError) as ctx:
            await self.repo.create("alpha", "exporter", "main")
        self.assertIn("already exists", str(ctx.exception))

    async def test_update_touches_only_named_fields_and_stamps(self) -> None:
        record = await self.repo.create("alpha", "exporter", "main", settings={"a": 1})
        updated = await self.repo.update("alpha", "exporter", "main", settings={"a": 2, "b": 3})
        assert updated is not None
        self.assertEqual(updated.settings, {"a": 2, "b": 3})
        self.assertEqual(updated.description, record.description)  # untouched
        self.assertGreaterEqual(updated.updated_at, record.updated_at)

    async def test_delete_and_exists(self) -> None:
        await self.repo.create("alpha", "exporter", "main")
        self.assertTrue(await self.repo.exists("alpha", "exporter", "main"))
        self.assertTrue(await self.repo.delete("alpha", "exporter", "main"))
        self.assertFalse(await self.repo.exists("alpha", "exporter", "main"))
        self.assertFalse(await self.repo.delete("alpha", "exporter", "main"))
        self.assertIsNone(await self.repo.get("alpha", "exporter", "main"))

    async def test_list_scoped_to_account_and_plugin(self) -> None:
        for name in ("main", "secondary"):
            await self.repo.create("alpha", "exporter", name)
        await self.repo.create("alpha", "probe", "main")  # other plugin
        await self.repo.create("beta", "exporter", "main")  # other account
        names = sorted(r.name for r in await self.repo.list_for("alpha", "exporter"))
        self.assertEqual(names, ["main", "secondary"])


class OwnershipIsolation(_Base):
    """Two accounts, two plugins, colliding names — no leak in any direction."""

    async def test_same_name_different_accounts_are_independent(self) -> None:
        a = await self.repo.create("alpha", "exporter", "main", settings={"who": "A"})
        b = await self.repo.create("beta", "exporter", "main", settings={"who": "B"})
        self.assertNotEqual(a.id, b.id)
        got_a = await self.repo.get("alpha", "exporter", "main")
        got_b = await self.repo.get("beta", "exporter", "main")
        assert got_a is not None and got_b is not None
        self.assertEqual(got_a.settings["who"], "A")
        self.assertEqual(got_b.settings["who"], "B")

    async def test_scoped_update_cannot_touch_the_other_account(self) -> None:
        await self.repo.create("alpha", "exporter", "main", settings={"who": "A"})
        await self.repo.create("beta", "exporter", "main", settings={"who": "B"})
        # alpha-scoped update of the SAME (plugin, name) leaves beta alone
        updated = await self.repo.update("alpha", "exporter", "main", settings={"who": "A2"})
        assert updated is not None
        got_b = await self.repo.get("beta", "exporter", "main")
        assert got_b is not None
        self.assertEqual(got_b.settings["who"], "B")
        # a scope that does not exist updates nothing (None, not a create)
        self.assertIsNone(await self.repo.update("gamma", "exporter", "main", settings={}))

    async def test_scoped_delete_cannot_touch_the_other_account(self) -> None:
        await self.repo.create("alpha", "exporter", "main")
        await self.repo.create("beta", "exporter", "main")
        self.assertTrue(await self.repo.delete("alpha", "exporter", "main"))
        self.assertTrue(await self.repo.exists("beta", "exporter", "main"))
        self.assertFalse(await self.repo.exists("alpha", "exporter", "main"))

    async def test_same_name_different_plugins_are_independent(self) -> None:
        await self.repo.create("alpha", "exporter", "main", settings={"p": "exporter"})
        await self.repo.create("alpha", "probe", "main", settings={"p": "probe"})
        e = await self.repo.get("alpha", "exporter", "main")
        p = await self.repo.get("alpha", "probe", "main")
        assert e is not None and p is not None
        self.assertEqual(e.settings["p"], "exporter")
        self.assertEqual(p.settings["p"], "probe")

    async def test_concurrent_identical_creates_one_winner(self) -> None:
        results = await asyncio.gather(
            self.repo.create("alpha", "exporter", "main"),
            self.repo.create("alpha", "exporter", "main"),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        losers = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)


class Lifecycle(_Base):
    async def test_persistence_survives_restart(self) -> None:
        await self.repo.create("alpha", "exporter", "main", settings={"k": "v"})
        await self.db.close()
        self.db = Database(Path(self._tmp.name) / "p.db")
        await self.db.connect()
        self.repo = self.db.account_plugin_profiles
        fetched = await self.repo.get("alpha", "exporter", "main")
        assert fetched is not None
        self.assertEqual(fetched.settings, {"k": "v"})

    async def test_account_deletion_cascades_profiles(self) -> None:
        await self.repo.create("alpha", "exporter", "main")
        await self.repo.create("beta", "exporter", "main")
        await self.db.accounts.add("alpha")
        await self.db.accounts.add("beta")
        await self.db.accounts.delete("alpha")
        self.assertFalse(await self.repo.exists("alpha", "exporter", "main"))
        self.assertTrue(await self.repo.exists("beta", "exporter", "main"))

    async def test_migration_version_is_current(self) -> None:
        from database.schema import latest_version

        # 12 = account-owned profiles, 13 = cooldowns' profile dimension
        self.assertEqual(latest_version(), 13)


if __name__ == "__main__":
    unittest.main()


class FacadeSurface(unittest.IsolatedAsyncioTestCase):
    """ControlAPI over the store: fail-closed validation + pass-through."""

    async def asyncSetUp(self) -> None:
        from core.api import ControlAPI

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "f.db")
        await self.db.connect()
        await self.db.accounts.add("alpha")
        self.api = ControlAPI(
            accounts=type("M", (), {"get": staticmethod(lambda n: None)})(),
            plugins=object(),
            settings=object(),
            limiter=object(),
            scheduler=object(),
            logs=object(),
            database=self.db,
        )
        # the facade's known-sets: one account, one plugin
        self.api._validate_account_known = lambda a: (
            None if a == "alpha" else (_ for _ in ()).throw(ValueError("unknown account"))
        )
        self.api._validate_plugins_known = lambda ps: (
            None if ps == ["exporter"] else (_ for _ in ()).throw(ValueError("unknown plugin"))
        )
        self.api._audit_mgmt = lambda *a, **k: None

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_facades_crud_and_isolation(self) -> None:
        made = await self.api.account_plugin_profile_create(
            "alpha", "exporter", "main", settings={"x": 1}
        )
        self.assertEqual(made["settings"], {"x": 1})
        got = await self.api.account_plugin_profile_get("alpha", "exporter", "main")
        assert got is not None
        self.assertEqual(got["schema_version"], 1)
        names = [r["name"] for r in await self.api.account_plugin_profiles("alpha", "exporter")]
        self.assertEqual(names, ["main"])
        upd = await self.api.account_plugin_profile_update(
            "alpha", "exporter", "main", settings={"x": 2}
        )
        self.assertEqual(upd["settings"], {"x": 2})
        gone = await self.api.account_plugin_profile_delete("alpha", "exporter", "main")
        self.assertTrue(gone["existed"])

    async def test_unknown_account_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.account_plugin_profile_create("ghost", "exporter", "main")

    async def test_unknown_plugin_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.account_plugin_profile_create("alpha", "ghostplug", "main")

    async def test_bad_name_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            await self.api.account_plugin_profile_create("alpha", "exporter", "  ")

    async def test_update_missing_profile_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError):
            await self.api.account_plugin_profile_update("alpha", "exporter", "nope", settings={})
