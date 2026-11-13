"""AccountPluginData tests (PHASE 10, B3 LOCKED).

The separate-database document store, the services facade's scoping,
and the REST surface — pinned to the locked decisions: scope lattice
account->plugin->guild->channel with '0' sentinels, STANDALONE copies
(no inheritance), optimistic writes on version, snowflake-STRING ids,
plugins reach the store ONLY through the (account, plugin) facade.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from core.account_manager.services import AccountPluginServices
from core.api import ControlAPI
from core.errors import PluginError
from database.plugin_data import PLUGIN_GLOBAL, PluginDataStore, VersionConflict

TOKEN = "MTMxNzg1MDk2MTY4Mjg5NDkxOA.GmW8Wg.qG4vmMjd"


class PluginDataStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "plugin-data.db")
        self.store = PluginDataStore(self.path)
        await self.store.connect()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tmp.cleanup()

    async def test_schema_self_initializes_and_reopens(self):
        """Separate flow: no migrator — connect() creates, a SECOND
        store on the same file reopens with the data intact."""
        doc = await self.store.put("bravo", "game_roll", "111", "222",
                                   {"count": 3})
        self.assertEqual(doc["version"], 1)
        await self.store.close()
        again = PluginDataStore(self.path)
        await again.connect()
        try:
            kept = await again.get("bravo", "game_roll",
                                   guild_id="111", channel_id="222")
            self.assertEqual(kept["data"], {"count": 3})
            self.assertEqual(kept["version"], 1)
        finally:
            await again.close()

    async def test_scope_lattice_and_sentinels(self):
        """Four dimensions: plugin-global (0/0), guild-level (g/0),
        channel (g/c) — each its OWN document."""
        await self.store.put("a", "p", PLUGIN_GLOBAL, PLUGIN_GLOBAL, {"s": "global"})
        await self.store.put("a", "p", "111", PLUGIN_GLOBAL, {"s": "guild"})
        await self.store.put("a", "p", "111", "222", {"s": "channel"})
        self.assertEqual((await self.store.get("a", "p"))["data"]["s"], "global")
        guild_doc = await self.store.get("a", "p", guild_id="111")
        self.assertEqual(guild_doc["data"]["s"], "guild")
        channel_doc = await self.store.get("a", "p", guild_id="111",
                                           channel_id="222")
        self.assertEqual(channel_doc["data"]["s"], "channel")

    async def test_standalone_copies_no_inheritance(self):
        """A missing channel document is ABSENT — never the guild's or
        the plugin-global document (locked: no inheritance)."""
        await self.store.put("a", "p", "111", PLUGIN_GLOBAL, {"guild": True})
        self.assertIsNone(await self.store.get(
            "a", "p", guild_id="111", channel_id="999"))
        await self.store.put("a", "p", PLUGIN_GLOBAL, PLUGIN_GLOBAL,
                             {"global": True})
        self.assertIsNone(await self.store.get(
            "a", "p", guild_id="888", channel_id="999"))

    async def test_optimistic_write_guard(self):
        first = await self.store.put("a", "p", "1", "1", {"v": 1})
        # correct expected version -> bump
        second = await self.store.put("a", "p", "1", "1", {"v": 2},
                                      expected_version=first["version"])
        self.assertEqual(second["version"], 2)
        # stale writer -> refused, data intact
        with self.assertRaises(VersionConflict):
            await self.store.put("a", "p", "1", "1", {"v": "clobber"},
                                 expected_version=1)
        kept = await self.store.get("a", "p", guild_id="1", channel_id="1")
        self.assertEqual(kept["data"], {"v": 2})
        self.assertEqual(kept["version"], 2)
        # None = the simple last-writer-wins path (the dashboard's)
        third = await self.store.put("a", "p", "1", "1", {"v": 3})
        self.assertEqual(third["version"], 3)

    async def test_put_over_deleted_with_stale_version_refused(self):
        doc = await self.store.put("a", "p", "1", "1", {"v": 1})
        await self.store.delete("a", "p", guild_id="1", channel_id="1")
        with self.assertRaises(VersionConflict):
            await self.store.put("a", "p", "1", "1", {"v": 2},
                                 expected_version=doc["version"])

    async def test_snowflake_ids_must_be_digit_strings(self):
        for bad in (123, "abc", "12 3", None, "", "channel-general"):
            with self.subTest(bad=bad):
                with self.assertRaises(PluginError):
                    await self.store.put("a", "p", "1", bad, {})
        # the REAL transport hazard: a 19-digit id as an int (JS rounds)
        with self.assertRaises(PluginError):
            await self.store.put("a", "p", 1553823440471986326, "0", {})
        # digit strings of any length are exact
        ok = await self.store.put("a", "p", "1553823440471986326", "0", {})
        self.assertEqual(ok["version"], 1)

    async def test_document_must_be_a_json_object(self):
        with self.assertRaises(PluginError):
            await self.store.put("a", "p", "1", "1", [1, 2, 3])
        with self.assertRaises(PluginError):
            await self.store.put("a", "p", "1", "1", object())

    async def test_list_filters_and_isolation(self):
        await self.store.put("a", "p1", "0", "0", {})
        await self.store.put("a", "p1", "111", "222", {})
        await self.store.put("a", "p2", "0", "0", {})
        await self.store.put("b", "p1", "0", "0", {})   # other account
        self.assertEqual(
            [n["plugin"] for n in await self.store.list("a")],
            ["p1", "p1", "p2"])
        self.assertEqual(
            len(await self.store.list("a", "p1")), 2)
        self.assertEqual(
            len(await self.store.list("a", "p1", guild_id="111")), 1)
        self.assertEqual(await self.store.list("b"), [
            {**n, "plugin": "p1"} for n in await self.store.list("b")])
        # store-level cross-account read is possible (the API layer
        # validates account); the FACADE is what scopes plugins
        self.assertEqual((await self.store.list("b"))[0]["account"], "b")

    async def test_delete_reports_existence(self):
        await self.store.put("a", "p", "0", "0", {})
        self.assertTrue(await self.store.delete("a", "p"))
        self.assertFalse(await self.store.delete("a", "p"))
        self.assertIsNone(await self.store.get("a", "p"))

    async def test_wal_mode_active(self):
        cur = await self.store._conn.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
        self.assertEqual(mode.lower(), "wal")


class FacadeScopeTests(unittest.IsolatedAsyncioTestCase):
    """The plugin-facing API: (account, plugin) scoping is STRUCTURAL —
    the facade never accepts an account/plugin argument at all."""

    def _facade(self, account, plugin, store):
        return AccountPluginServices(
            account, plugin, bus=_Bus(), scheduler=None, credentials=None,
            cache=_Cache(), plugin_data=store)

    async def test_facade_scopes_reads_and_writes(self):
        async with _store() as store:
            bravo = self._facade("bravo", "roll", store)
            alpha = self._facade("alpha", "roll", store)
            other = self._facade("bravo", "grind", store)
            await bravo.data_put("111", "222", {"rolls": 5})
            self.assertEqual(
                (await bravo.data_get("111", "222"))["data"], {"rolls": 5})
            # a different ACCOUNT sees nothing at that scope
            self.assertIsNone(await alpha.data_get("111", "222"))
            # a different PLUGIN on the same account sees nothing
            self.assertIsNone(await other.data_get("111", "222"))
            self.assertEqual(await other.data_list(), [])
            self.assertEqual(len(await bravo.data_list()), 1)

    async def test_facade_version_guard_passes_through(self):
        async with _store() as store:
            facade = self._facade("bravo", "roll", store)
            doc = await facade.data_put("0", "0", {"n": 1})
            with self.assertRaises(VersionConflict):
                await facade.data_put("0", "0", {"n": 2},
                                      expected_version=doc["version"] - 1)
            bumped = await facade.data_put("0", "0", {"n": 2},
                                           expected_version=doc["version"])
            self.assertEqual(bumped["version"], 2)

    async def test_unwired_store_raises_honest_error(self):
        facade = self._facade("bravo", "roll", None)
        with self.assertRaises(PluginError) as ctx:
            await facade.data_get("0", "0")
        self.assertIn("not wired", str(ctx.exception))


class _Bus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, *a, **k):
        return None


class _Cache:
    pass


def _store():
    class _Ctx:
        async def __aenter__(self):
            self.tmp = tempfile.TemporaryDirectory()
            s = PluginDataStore(str(Path(self.tmp.name) / "pd.db"))
            await s.connect()
            self.store = s
            return s

        async def __aexit__(self, *exc):
            await self.store.close()
            self.tmp.cleanup()
    return _Ctx()


class FacadeScopeTests(unittest.IsolatedAsyncioTestCase):
    """The plugin-facing API: (account, plugin) scoping is STRUCTURAL —
    the facade never accepts an account/plugin argument at all."""

    def _facade(self, account, plugin, store):
        return AccountPluginServices(
            account, plugin, bus=_Bus(), scheduler=None, credentials=None,
            cache=_Cache(), plugin_data=store)

    async def test_facade_scopes_reads_and_writes(self):
        async with _store() as store:
            bravo = self._facade("bravo", "roll", store)
            alpha = self._facade("alpha", "roll", store)
            other = self._facade("bravo", "grind", store)
            await bravo.data_put("111", "222", {"rolls": 5})
            self.assertEqual(
                (await bravo.data_get("111", "222"))["data"], {"rolls": 5})
            # a different ACCOUNT sees nothing at that scope
            self.assertIsNone(await alpha.data_get("111", "222"))
            # a different PLUGIN on the same account sees nothing
            self.assertIsNone(await other.data_get("111", "222"))
            self.assertEqual(await other.data_list(), [])
            self.assertEqual(len(await bravo.data_list()), 1)

    async def test_facade_version_guard_passes_through(self):
        async with _store() as store:
            facade = self._facade("bravo", "roll", store)
            doc = await facade.data_put("0", "0", {"n": 1})
            with self.assertRaises(VersionConflict):
                await facade.data_put("0", "0", {"n": 2},
                                      expected_version=doc["version"] - 1)
            bumped = await facade.data_put("0", "0", {"n": 2},
                                           expected_version=doc["version"])
            self.assertEqual(bumped["version"], 2)

    async def test_unwired_store_raises_honest_error(self):
        facade = self._facade("bravo", "roll", None)
        with self.assertRaises(PluginError) as ctx:
            await facade.data_get("0", "0")
        self.assertIn("not wired", str(ctx.exception))


class _Bus:
    def subscribe(self, *a, **k):
        return lambda: None

    def publish(self, *a, **k):
        return None


class _Cache:
    pass


def _store():
    class _Ctx:
        async def __aenter__(self):
            self.tmp = tempfile.TemporaryDirectory()
            s = PluginDataStore(str(Path(self.tmp.name) / "pd.db"))
            await s.connect()
            self.store = s
            return s

        async def __aexit__(self, *exc):
            await self.store.close()
            self.tmp.cleanup()
    return _Ctx()


class ScopeValidationTests(unittest.TestCase):
    """ControlAPI._data_scope: the transport rule at the API boundary."""

    def test_digit_strings_and_sentinels_pass(self):
        self.assertEqual(ControlAPI._data_scope("0", "0"), ["0", "0"])
        self.assertEqual(
            ControlAPI._data_scope("1553823440471986326", "222"),
            ["1553823440471986326", "222"])

    def test_everything_else_refused(self):
        for gid, cid in ((123, "0"), ("0", 456), ("abc", "0"),
                         ("12 3", "0"), (None, "0"), ("", "0")):
            with self.subTest(gid=gid, cid=cid):
                with self.assertRaises(ValueError) as ctx:
                    ControlAPI._data_scope(gid, cid)
                self.assertIn("digit string", str(ctx.exception))


class RestContractTests(unittest.IsolatedAsyncioTestCase):
    """The /api/accounts/data surface against a real DashboardServer."""

    async def test_routes_exist_and_shape_validate(self):
        from interfaces.dashboard.server import DashboardServer
        import urllib.error
        import urllib.request

        class _Api:
            async def accounts_data_list(self, account, *, plugin=None,
                                         guild_id=None, channel_id=None):
                assert account == "bravo"
                return [{"account": "bravo", "plugin": "roll",
                         "guild_id": "0", "channel_id": "0",
                         "data": {"count": 2}, "version": 1,
                         "updated_at": 100.0}]

            async def accounts_data_get(self, account, plugin, *,
                                        guild_id="0", channel_id="0"):
                return {"account": account, "plugin": plugin,
                        "guild_id": guild_id, "channel_id": channel_id,
                        "data": {}, "version": 1, "updated_at": 100.0}

            async def accounts_data_put(self, account, plugin, guild_id,
                                        channel_id, data, *,
                                        expected_version=None):
                # the real ControlAPI validates shape + digit-string scopes
                # before touching the store; mirror it here so the route's
                # 400 mapping is pinned (the store's own guard is covered
                # by PluginDataStoreTests)
                from core.api import ControlAPI
                if not isinstance(plugin, str) or not plugin.strip():
                    raise ValueError("field 'plugin' must be a non-empty string")
                if not isinstance(data, dict):
                    raise ValueError(
                        f"field 'data' must be a JSON object, got {type(data).__name__}")
                ControlAPI._data_scope(guild_id, channel_id)
                return {"account": account, "plugin": plugin,
                        "guild_id": guild_id, "channel_id": channel_id,
                        "data": data, "version": (expected_version or 0) + 1,
                        "updated_at": 101.0}

            async def accounts_data_delete(self, account, plugin, *,
                                           guild_id="0", channel_id="0"):
                return True

        server = DashboardServer(_Api(), token="t", port=0)
        server.start()          # rides THIS loop: requests run off-loop
        try:
            await asyncio.sleep(0.3)

            def call(path, method="GET", body=None):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.bound_port}{path}",
                    method=method,
                    headers={"X-Dashboard-Token": "t",
                             "Content-Type": "application/json"},
                    data=json.dumps(body).encode() if body is not None else None)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.loads(r.read().decode())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read().decode())

            async def get(path, method="GET", body=None):
                return await asyncio.to_thread(call, path, method, body)

            status, body = await get("/api/accounts/data?account=bravo")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"][0]["plugin"], "roll")

            status, body = await get(
                "/api/accounts/data/doc?account=bravo&plugin=roll"
                "&guild_id=111&channel_id=222")
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["guild_id"], "111")

            status, body = await get("/api/accounts/data/put", "POST", {
                "account": "bravo", "plugin": "roll",
                "guild_id": "111", "channel_id": "222",
                "data": {"rolls": 9}, "expected_version": 4})
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["version"], 5)

            # non-object data -> honest 400
            status, body = await get("/api/accounts/data/put", "POST", {
                "account": "bravo", "plugin": "roll",
                "guild_id": "111", "channel_id": "222",
                "data": [1, 2]})
            self.assertEqual(status, 400)

            # int snowflake -> honest 400 (the transport rule; the route's
            # require_str catches the type before the API's digit check)
            status, body = await get("/api/accounts/data/put", "POST", {
                "account": "bravo", "plugin": "roll",
                "guild_id": 1553823440471986326, "channel_id": "0",
                "data": {}})
            self.assertEqual(status, 400)
            self.assertIn("guild_id", body["error"])

            status, body = await get("/api/accounts/data/delete", "POST", {
                "account": "bravo", "plugin": "roll",
                "guild_id": "111", "channel_id": "222"})
            self.assertEqual(status, 200)
            self.assertTrue(body["data"]["deleted"])
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
