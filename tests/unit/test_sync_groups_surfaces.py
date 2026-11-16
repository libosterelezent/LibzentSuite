"""ControlAPI / CLI / dashboard surface tests for sync groups.

The CLI and dashboard must only ever talk to ControlAPI (parity by
construction); these tests pin that boundary with duck-typed fakes, the
same conventions as test_cli_commands / test_dashboard_http.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from core.api import ControlAPI
from core.errors import SyncGroupError
from core.event_bus import EventBus
from core.rate_limit_manager import RateLimitManager
from core.sync_group_manager import SyncGroupManager

from database.db import Database

from interfaces.cli.commands import UsageError, run_command
from interfaces.dashboard.server import DashboardServer

LOG = logging.getLogger("test.sync_groups_surfaces")

ACCOUNT_ROWS = [
    {"name": "alpha", "status": "running", "enabled": True,
     "sync_group": None},
    {"name": "beta", "status": "stopped", "enabled": True,
     "sync_group": None},
]


class _StubAccount:
    """record-shaped stub for the ControlAPI memory-refresh check."""

    def __init__(self) -> None:
        self.record = SimpleNamespace(sync_group=None)


class _StubAccountsManager:
    """Duck-typed AccountManager: accounts() + the _accounts memory map."""

    def __init__(self) -> None:
        self._accounts = {row["name"]: _StubAccount() for row in ACCOUNT_ROWS}

    async def accounts(self, *, refresh_records: bool = False):
        return [dict(row) for row in ACCOUNT_ROWS]


class ControlApiGroupsTestBase(unittest.IsolatedAsyncioTestCase):
    """Real SyncGroupManager + real repos; stub account manager."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-api-groups-")
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self) -> None:
        self.db = Database(Path(self.tmp.name) / "api.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.limiter = RateLimitManager(EventBus(), LOG, None)
        self.sync_groups = SyncGroupManager(
            self.db.sync_groups, LOG, accounts_repo=self.db.accounts,
            limiter=self.limiter,
        )
        await self.sync_groups.load()
        await self.db.accounts.add("alpha")
        await self.db.accounts.add("beta")
        self.accounts_mgr = _StubAccountsManager()
        self.api = ControlAPI(
            accounts=self.accounts_mgr,
            plugins=SimpleNamespace(),
                        settings=SimpleNamespace(),
            limiter=self.limiter,
            scheduler=SimpleNamespace(),
            logs=SimpleNamespace(),
            sync_groups=self.sync_groups,
        )


class ControlApiGroupsTests(ControlApiGroupsTestBase):
    async def test_full_crud_and_membership_flow(self) -> None:
        created = await self.api.groups_create(
            {"name": "grinders", "description": "the grind"}, surface="cli")
        self.assertEqual(created["name"], "grinders")
        listed = await self.api.groups_list()
        self.assertEqual(listed[0]["member_count"], 0)
        got = await self.api.groups_get("grinders")
        self.assertEqual(got["id"], created["id"])

        out = await self.api.groups_add_account("grinders", "alpha",
                                                surface="dashboard")
        self.assertEqual(out, {"account": "alpha", "group": "grinders"})
        # the memory record of the account manager follows (F3 pattern)
        self.assertEqual(
            self.accounts_mgr._accounts["alpha"].record.sync_group,
            "grinders")
        self.assertEqual(
            (await self.db.accounts.get("alpha")).sync_group, "grinders")
        self.assertEqual(self.limiter.account_groups(), {"alpha": "grinders"})

        moved = await self.api.groups_set_account("alpha", created["id"])
        self.assertEqual(moved["group"], "grinders")
        cleared = await self.api.groups_clear_account("alpha")
        self.assertIsNone(cleared["group"])
        self.assertEqual(self.limiter.account_groups(), {})

        removed = await self.api.groups_remove_account("grinders", "alpha")
        self.assertFalse(removed["existed"])  # already cleared

    async def test_snapshot_shape_and_member_enrichment(self) -> None:
        await self.api.groups_create({"name": "grinders"})
        await self.api.groups_add_account("grinders", "alpha")
        await self.api.groups_add_account("grinders", "beta")
        snapshot = await self.api.groups_snapshot("grinders")
        self.assertEqual(snapshot["member_count"], 2)
        self.assertEqual(snapshot["pacing_key"], "group:grinders")
        self.assertEqual(
            snapshot["members"],
            [{"name": "alpha", "status": "running", "enabled": True},
             {"name": "beta", "status": "stopped", "enabled": True}],
        )
        self.assertEqual(snapshot["runtime_members"], ["alpha", "beta"])

    async def test_update_and_delete_validation(self) -> None:
        await self.api.groups_create({"name": "one"})
        with self.assertRaises(ValueError):  # empty payload
            await self.api.groups_update("one", {})
        with self.assertRaises(ValueError):  # unknown key (typo trap)
            await self.api.groups_create({"name": "x", "nope": 1})
        with self.assertRaises(ValueError):  # strict booleans only
            await self.api.groups_create({"name": "x", "enabled": "yes"})

        await self.api.groups_add_account("one", "alpha")
        with self.assertRaises(SyncGroupError):  # members must detach first
            await self.api.groups_delete("one")
        out = await self.api.groups_delete("one", detach_members=True)
        self.assertEqual(out["detached"], ["alpha"])
        self.assertEqual(await self.api.groups_list(), [])

    async def test_rename_and_detach_refresh_account_records(self) -> None:
        """Regression: rename / delete+detach used to leave the account
        manager's memory record naming the OLD (or deleted) group — the
        1 Hz dashboard poll serves record.sync_group, so account cards
        showed a stale/dangling chip until restart."""
        await self.api.groups_create({"name": "alpha-grp"})
        await self.api.groups_add_account("alpha-grp", "alpha")
        await self.api.groups_add_account("alpha-grp", "beta")

        renamed = await self.api.groups_update(
            "alpha-grp", {"name": "beta-grp"})
        self.assertEqual(renamed["name"], "beta-grp")
        for name in ("alpha", "beta"):
            self.assertEqual(
                self.accounts_mgr._accounts[name].record.sync_group,
                "beta-grp", f"{name}'s memory record must follow the rename")
            self.assertEqual(
                (await self.db.accounts.get(name)).sync_group, "beta-grp")
        self.assertEqual(
            self.limiter.account_groups(),
            {"alpha": "beta-grp", "beta": "beta-grp"})

        out = await self.api.groups_delete("beta-grp", detach_members=True)
        self.assertEqual(sorted(out["detached"]), ["alpha", "beta"])
        for name in ("alpha", "beta"):
            self.assertIsNone(
                self.accounts_mgr._accounts[name].record.sync_group,
                f"{name}'s memory record must clear on detach-delete")
        self.assertEqual(self.limiter.account_groups(), {})
        # a non-rename update must not disturb the propagated value
        await self.api.groups_create({"name": "gamma-grp"})
        await self.api.groups_add_account("gamma-grp", "alpha")
        await self.api.groups_update("gamma-grp", {"enabled": False})
        self.assertEqual(
            self.accounts_mgr._accounts["alpha"].record.sync_group,
            "gamma-grp")

    async def test_unwired_api_fails_loudly(self) -> None:
        api = ControlAPI(
            accounts=_StubAccountsManager(),
            plugins=SimpleNamespace(),             settings=SimpleNamespace(), limiter=SimpleNamespace(),
            scheduler=SimpleNamespace(), logs=SimpleNamespace(),
        )
        self.assertEqual(await api.groups_list(), [])  # reads degrade
        for call in (
            lambda: api.groups_get("x"),
            lambda: api.groups_create({"name": "x"}),
            lambda: api.groups_add_account("x", "y"),
        ):
            with self.assertRaises(RuntimeError):
                await call()


# ---------------------------------------------------------------- CLI
class FakeCliApi:
    """Duck-typed ControlAPI recording every groups_* call."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Tuple, dict]] = []

    def _record(self, name, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    async def groups_list(self):
        self._record("groups_list")
        return [
            {"id": 1, "name": "grinders", "description": "the grind",
             "enabled": True, "member_count": 2,
             "created_at": 1.0, "updated_at": 2.0},
            {"id": 2, "name": "idle", "description": "",
             "enabled": False, "member_count": 0,
             "created_at": 1.0, "updated_at": 2.0},
        ]

    async def groups_snapshot(self, group):
        self._record("groups_snapshot", group)
        return {
            "id": 1, "name": "grinders", "description": "the grind",
            "enabled": True, "created_at": 1.0, "updated_at": 2.0,
            "member_count": 1, "pacing_key": "group:grinders",
            "runtime_members": ["alpha"],
            "members": [{"name": "alpha", "status": "running",
                         "enabled": True}],
        }

    async def groups_create(self, payload, surface="api"):
        self._record("groups_create", payload, surface=surface)
        return {"id": 3, "name": payload["name"],
                "description": payload.get("description", ""),
                "enabled": True}

    async def groups_update(self, group, payload, surface="api"):
        self._record("groups_update", group, payload, surface=surface)
        return {"id": 1, "name": payload.get("name", group),
                "enabled": payload.get("enabled", True)}

    async def groups_delete(self, group, detach_members=False, surface="api"):
        self._record("groups_delete", group,
                     detach_members=detach_members, surface=surface)
        return {"deleted": group, "existed": True, "detached": ["alpha"]}

    async def groups_add_account(self, group, account, surface="api"):
        self._record("groups_add_account", group, account, surface=surface)
        return {"account": account, "group": group}

    async def groups_remove_account(self, group, account, surface="api"):
        self._record("groups_remove_account", group, account, surface=surface)
        return {"account": account, "group": group, "existed": True}

    async def groups_set_account(self, account, group, surface="api"):
        self._record("groups_set_account", account, group, surface=surface)
        return {"account": account, "group": group, "previous": None}

    async def groups_clear_account(self, account, surface="api"):
        self._record("groups_clear_account", account, surface=surface)
        return {"account": account, "group": None, "previous": "grinders"}


class CliGroupsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.api = FakeCliApi()

    async def test_list_renders(self) -> None:
        out = await run_command(self.api, "groups list")
        self.assertIn("grinders", out)
        self.assertIn("2 member(s)", out)
        self.assertIn("[disabled]", out)

    async def test_show_renders_snapshot(self) -> None:
        out = await run_command(self.api, "groups show grinders")
        self.assertIn("group:grinders", out)
        self.assertIn("alpha", out)

    async def test_create_joins_description(self) -> None:
        out = await run_command(self.api, "groups create raiders farm all day")
        self.assertIn("raiders", out)
        name, args, kwargs = self.api.calls[-1]
        self.assertEqual(name, "groups_create")
        self.assertEqual(args[0],
                         {"name": "raiders", "description": "farm all day"})
        self.assertEqual(kwargs.get("surface"), "cli")

    async def test_update_fields(self) -> None:
        await run_command(self.api, "groups update grinders enabled off")
        name, args, kwargs = self.api.calls[-1]
        self.assertEqual(name, "groups_update")
        self.assertEqual(args[1], {"enabled": False})
        await run_command(self.api, "groups update grinders name raiders")
        self.assertEqual(self.api.calls[-1][1][1], {"name": "raiders"})
        with self.assertRaises(UsageError):  # unknown field
            await run_command(self.api, "groups update grinders bogus x")

    async def test_delete_flag(self) -> None:
        await run_command(self.api, "groups delete grinders")
        name, args, kwargs = self.api.calls[-1]
        self.assertEqual(name, "groups_delete")
        self.assertFalse(kwargs["detach_members"])
        await run_command(self.api,
                          "groups delete grinders --detach-members")
        self.assertTrue(self.api.calls[-1][2]["detach_members"])

    async def test_membership_commands(self) -> None:
        for line, expected in (
            ("groups add-member grinders alpha", "groups_add_account"),
            ("groups remove-member grinders alpha", "groups_remove_account"),
            ("groups set-account alpha grinders", "groups_set_account"),
            ("groups clear-account alpha", "groups_clear_account"),
        ):
            out = await run_command(self.api, line)
            self.assertTrue(out.strip())
            self.assertEqual(self.api.calls[-1][0], expected)

    async def test_usage_errors(self) -> None:
        for line in ("groups", "groups bogus", "groups create",
                     "groups show", "groups add-member grinders",
                     "groups update grinders enabled maybe"):
            with self.assertRaises(UsageError):
                await run_command(self.api, line)

    async def test_help_lists_the_group(self) -> None:
        from interfaces.cli.commands import help_text

        self.assertIn("groups", help_text())


# ------------------------------------------------------------ dashboard
class FakeDashApi(FakeCliApi):
    """FakeCliApi + the fields the dashboard server touches."""

    async def status(self):
        return {"accounts": [], "ratelimit": {}, "tasks": {}, "warnings": [],
                "uptime_s": 1.0}


class DashboardGroupsRoutesTests(unittest.IsolatedAsyncioTestCase):
    """The /api/groups/* routes reach ControlAPI and render its payload."""

    async def asyncSetUp(self) -> None:
        self.fake = FakeDashApi()
        self.server = DashboardServer(self.fake, token=None, port=0)
        self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    async def _get(self, path: str) -> Tuple[int, Dict[str, Any]]:
        import json
        import urllib.error
        import urllib.request

        def _fetch():
            try:
                with urllib.request.urlopen(self.base + path, timeout=5) as resp:
                    return resp.status, json.load(resp)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)

        return await asyncio.to_thread(_fetch)

    async def _post(self, path: str, body: Dict[str, Any]
                    ) -> Tuple[int, Dict[str, Any]]:
        import json as _json
        import urllib.error
        import urllib.request

        def _fetch():
            data = _json.dumps(body).encode()
            request = urllib.request.Request(
                self.base + path, data=data,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=5) as resp:
                    return resp.status, _json.load(resp)
            except urllib.error.HTTPError as exc:
                return exc.code, _json.load(exc)

        return await asyncio.to_thread(_fetch)

    async def test_list_and_info_routes(self) -> None:
        status, payload = await self._get("/api/groups")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"][0]["name"], "grinders")
        status, payload = await self._get("/api/groups/info?group=grinders")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["pacing_key"], "group:grinders")
        self.assertEqual(self.fake.calls[-1], ("groups_snapshot",
                                               ("grinders",), {}))

    async def test_create_route_validates(self) -> None:
        status, payload = await self._post(
            "/api/groups/create", {"name": "x", "description": "d",
                                   "enabled": False})
        self.assertEqual(status, 200)
        name, args, kwargs = self.fake.calls[-1]
        self.assertEqual(name, "groups_create")
        self.assertEqual(args[0],
                         {"name": "x", "description": "d", "enabled": False})
        self.assertEqual(kwargs.get("surface"), "dashboard")
        status, payload = await self._post(
            "/api/groups/create", {"name": "x", "enabled": "oui"})
        self.assertEqual(status, 400)
        status, payload = await self._post("/api/groups/create", {})
        self.assertEqual(status, 400)

    async def test_update_route_requires_one_field(self) -> None:
        status, _ = await self._post(
            "/api/groups/update", {"group": "g", "name": "h"})
        self.assertEqual(status, 200)
        status, payload = await self._post(
            "/api/groups/update", {"group": "g"})
        self.assertEqual(status, 400)
        self.assertIn("at least one", payload["error"])

    async def test_delete_route_parses_detach_flag(self) -> None:
        status, _ = await self._post(
            "/api/groups/delete", {"group": "g", "detach_members": True})
        self.assertEqual(status, 200)
        self.assertTrue(self.fake.calls[-1][2]["detach_members"])
        status, _ = await self._post("/api/groups/delete", {"group": "g"})
        self.assertFalse(self.fake.calls[-1][2]["detach_members"])

    async def test_membership_routes(self) -> None:
        for path, body, expected in (
            ("/api/groups/add-member", {"group": "g", "account": "a"},
             "groups_add_account"),
            ("/api/groups/remove-member", {"group": "g", "account": "a"},
             "groups_remove_account"),
            ("/api/groups/set-account", {"account": "a", "group": "g"},
             "groups_set_account"),
            ("/api/groups/clear-account", {"account": "a"},
             "groups_clear_account"),
        ):
            status, payload = await self._post(path, body)
            self.assertEqual(status, 200, path)
            self.assertTrue(payload["ok"])
            self.assertEqual(self.fake.calls[-1][0], expected)

    async def test_domain_errors_render_as_400(self) -> None:
        async def boom(group, **kwargs):
            raise SyncGroupError("unknown sync group 'nope'")

        self.fake.groups_snapshot = boom
        status, payload = await self._get("/api/groups/info?group=nope")
        self.assertEqual(status, 400)
        self.assertIn("unknown sync group", payload["error"])


if __name__ == "__main__":
    unittest.main()
