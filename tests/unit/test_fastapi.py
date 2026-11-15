"""FastAPI core-API tests — routes over a stub ControlAPI."""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from interfaces.api.app import build_app


def _stub_api() -> NS:
    async def _accounts_list():
        return [{"name": "alpha", "status": "running"}]

    async def _accounts_add(payload, surface="api"):
        return {"name": payload["name"], "status": "created"}

    async def _accounts_info(name):
        return {"name": name, "status": "stopped"}

    async def _accounts_remove(name, surface="api"):
        return {"removed": name}

    async def _accounts_start(name):
        return {"started": name}

    async def _accounts_stop(name):
        return {"stopped": name}

    async def _status():
        return {"status": "ok", "accounts": 1}

    async def _estop(surface="api"):
        return {"stopped": True}

    async def _workspaces_list():
        return [{"name": "grind", "member_count": 2}]

    async def _workspaces_create(payload, surface="api"):
        return {"name": payload["name"]}

    async def _workspaces_get(name):
        return {"name": name, "members": ["alpha"]}

    async def _workspaces_delete(name, detach=False, surface="api"):
        return {"deleted": name}

    async def _workspaces_add_account(account, name, surface="api"):
        return {"account": account, "workspace": name}

    async def _workspaces_remove_account(account, surface="api"):
        return {"left": True}

    async def _workspaces_pause(name, surface="api"):
        return {"name": name, "paused": True}

    async def _workspaces_resume(name, surface="api"):
        return {"name": name, "paused": False}

    async def _workspaces_tasks(workspace=None, status=None):
        return [{"id": "t1", "workspace": workspace}]

    async def _plugins_manifests():
        return [{"name": "probe"}]

    async def _accounts_plugins_list(name):
        return ["probe"]

    async def _accounts_plugin_assign(name, plugin, surface="api"):
        return None

    async def _accounts_plugin_unassign(name, plugin, surface="api"):
        return True

    async def _tasks_list(status=None):
        return [{"id": 1}]

    async def _tasks_cancel(task_id):
        return {"cancelled": task_id}

    async def _logs_tail(limit=50, account=None):
        return [{"message": "x"}]

    async def _audit_tail(limit=50, account=None):
        return [{"action": "send"}]

    async def _system_resume():
        return {"resumed": True}

    async def _emergency_stop_all(surface="api"):
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
        self.app = build_app(_stub_api(), token="secret")
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


if __name__ == "__main__":
    unittest.main()
