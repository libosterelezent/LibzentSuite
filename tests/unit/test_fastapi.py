"""FastAPI core-API tests — routes over a stub ControlAPI."""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from typing import Any

from fastapi.testclient import TestClient

from interfaces.api.app import build_app


def _stub_api() -> NS:
    async def _accounts_list() -> list[dict[str, Any]]:
        return [{"name": "alpha", "status": "running"}]

    async def _accounts_add(payload: dict[str, Any], surface: str = "api") -> dict[str, Any]:
        return {"name": payload["name"], "status": "created"}

    async def _accounts_info(name: str) -> dict[str, Any]:
        return {"name": name, "status": "stopped"}

    async def _accounts_remove(name: str, surface: str = "api") -> dict[str, Any]:
        return {"removed": name}

    async def _accounts_start(name: str) -> dict[str, Any]:
        return {"started": name}

    async def _accounts_stop(name: str) -> dict[str, Any]:
        return {"stopped": name}

    async def _status() -> dict[str, Any]:
        return {"status": "ok", "accounts": 1}

    async def _estop(surface: str = "api") -> dict[str, Any]:
        return {"stopped": True}

    async def _workspaces_list() -> list[dict[str, Any]]:
        return [{"name": "grind", "member_count": 2}]

    async def _workspaces_create(payload: dict[str, Any], surface: str = "api") -> dict[str, Any]:
        return {"name": payload["name"]}

    async def _workspaces_get(name: str) -> dict[str, Any]:
        return {"name": name, "members": ["alpha"]}

    async def _workspaces_delete(
        name: str, detach: bool = False, surface: str = "api"
    ) -> dict[str, Any]:
        return {"deleted": name}

    async def _workspaces_add_account(
        account: str, name: str, surface: str = "api"
    ) -> dict[str, Any]:
        return {"account": account, "workspace": name}

    async def _workspaces_remove_account(account: str, surface: str = "api") -> dict[str, Any]:
        return {"left": True}

    async def _workspaces_pause(name: str, surface: str = "api") -> dict[str, Any]:
        return {"name": name, "paused": True}

    async def _workspaces_resume(name: str, surface: str = "api") -> dict[str, Any]:
        return {"name": name, "paused": False}

    async def _workspaces_tasks(
        workspace: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        return [{"id": "t1", "workspace": workspace}]

    async def _account_plugin_profiles(name: str, plugin: str) -> list[dict[str, Any]]:
        return [{"name": "main", "settings": {"x": 1}}]

    async def _account_plugin_profile_create(
        name: str,
        plugin: str,
        profile: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
        schema_version: int = 1,
        surface: str = "api",
    ) -> dict[str, Any]:
        return {"account": name, "plugin": plugin, "name": profile, "settings": settings or {}}

    async def _account_plugin_profile_get(
        name: str,
        plugin: str,
        profile: str,
    ) -> dict[str, Any] | None:
        return {"name": profile, "settings": {"x": 1}} if profile == "main" else None

    async def _account_plugin_profile_delete(
        name: str,
        plugin: str,
        profile: str,
        surface: str = "api",
    ) -> dict[str, Any]:
        return {"existed": profile == "main"}

    async def _plugins_manifests() -> list[dict[str, Any]]:
        return [{"name": "probe"}]

    async def _accounts_plugins_list(name: str) -> list[str]:
        return ["probe"]

    async def _accounts_plugin_assign(name: str, plugin: str, surface: str = "api") -> None:
        return None

    async def _accounts_plugin_unassign(name: str, plugin: str, surface: str = "api") -> bool:
        return True

    async def _tasks_list(status: str | None = None) -> list[dict[str, Any]]:
        return [{"id": 1}]

    async def _tasks_cancel(task_id: int) -> dict[str, Any]:
        return {"cancelled": task_id}

    async def _logs_tail(limit: int = 50, account: str | None = None) -> list[dict[str, Any]]:
        return [{"message": "x"}]

    async def _audit_tail(limit: int = 50, account: str | None = None) -> list[dict[str, Any]]:
        return [{"action": "send"}]

    async def _system_resume() -> dict[str, Any]:
        return {"resumed": True}

    async def _emergency_stop_all(surface: str = "api") -> dict[str, Any]:
        return {"stopped": True}

    return NS(
        accounts_list=_accounts_list,
        accounts_add=_accounts_add,
        accounts_info=_accounts_info,
        accounts_remove=_accounts_remove,
        accounts_start=_accounts_start,
        accounts_stop=_accounts_stop,
        status=_status,
        emergency_stop_all=_estop,
        workspaces_list=_workspaces_list,
        workspaces_create=_workspaces_create,
        workspaces_get=_workspaces_get,
        workspaces_delete=_workspaces_delete,
        workspaces_add_account=_workspaces_add_account,
        workspaces_remove_account=_workspaces_remove_account,
        workspaces_pause=_workspaces_pause,
        workspaces_resume=_workspaces_resume,
        workspaces_tasks=_workspaces_tasks,
        plugins_manifests=_plugins_manifests,
        account_plugin_profiles=_account_plugin_profiles,
        account_plugin_profile_create=_account_plugin_profile_create,
        account_plugin_profile_get=_account_plugin_profile_get,
        account_plugin_profile_delete=_account_plugin_profile_delete,
        accounts_plugins_list=_accounts_plugins_list,
        accounts_plugin_assign=_accounts_plugin_assign,
        accounts_plugin_unassign=_accounts_plugin_unassign,
        tasks_list=_tasks_list,
        tasks_cancel=_tasks_cancel,
        logs_tail=_logs_tail,
        audit_tail=_audit_tail,
        system_resume=_system_resume,
    )


class FastApiTests(unittest.TestCase):
    def setUp(self) -> None:
        # duck-typed ControlAPI stub: the routes only await these methods
        self.app = build_app(_stub_api(), token="secret")  # type: ignore[arg-type]
        self.client = TestClient(self.app)
        self.headers = {"X-API-Token": "secret"}

    def test_unauthorized_rejected(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_status(self) -> None:
        response = self.client.get("/api/status", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_accounts_crud(self) -> None:
        listed = self.client.get("/api/accounts", headers=self.headers)
        self.assertEqual(listed.json()[0]["name"], "alpha")
        created = self.client.post("/api/accounts", json={"name": "beta"}, headers=self.headers)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["name"], "beta")
        started = self.client.post("/api/accounts/alpha/start", headers=self.headers)
        self.assertEqual(started.json()["started"], "alpha")

    def test_workspaces_surface(self) -> None:
        listed = self.client.get("/api/workspaces", headers=self.headers)
        self.assertEqual(listed.json()[0]["name"], "grind")
        created = self.client.post("/api/workspaces", json={"name": "new"}, headers=self.headers)
        self.assertEqual(created.json()["name"], "new")
        member = self.client.post("/api/workspaces/grind/members/alpha", headers=self.headers)
        self.assertEqual(member.json()["workspace"], "grind")
        paused = self.client.post("/api/workspaces/grind/pause", headers=self.headers)
        self.assertTrue(paused.json()["paused"])

    def test_plugins_tasks_logs(self) -> None:
        self.assertEqual(
            self.client.get("/api/plugins", headers=self.headers).json()[0]["name"],
            "probe",
        )
        self.assertEqual(self.client.get("/api/tasks", headers=self.headers).json()[0]["id"], 1)
        self.assertEqual(len(self.client.get("/api/logs", headers=self.headers).json()), 1)

    def test_profile_routes(self) -> None:
        r = self.client.get("/api/accounts/probe/plugins/ping/profiles", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()[0]["name"], "main")
        r = self.client.post(
            "/api/accounts/probe/plugins/ping/profiles/secondary",
            json={"description": "d", "settings": {"y": 2}},
            headers=self.headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "secondary")
        r = self.client.get("/api/accounts/probe/plugins/ping/profiles/main", headers=self.headers)
        self.assertEqual(r.json()["settings"], {"x": 1})
        r = self.client.get("/api/accounts/probe/plugins/ping/profiles/ghost", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json())
        r = self.client.delete(
            "/api/accounts/probe/plugins/ping/profiles/main", headers=self.headers
        )
        self.assertTrue(r.json()["existed"])


if __name__ == "__main__":
    unittest.main()
