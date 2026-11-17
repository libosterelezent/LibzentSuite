"""ControlAPI workspace-surface tests (W3) — the workspaces_* layer.

Real WorkspaceService + real repositories + the real limiter; a stub
account manager. Pins the §14 mutation contracts: membership moves
refresh the in-memory record (F3), pacing follows membership, and
every surface verb reaches the one writer.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

from core.api import ControlAPI
from core.event_bus import EventBus
from core.rate_limit_manager import RateLimitManager
from core.workspace.service import WorkspaceService
from database.db import Database

LOG_NS = NS(logger=NS(warning=lambda *a, **k: None))

ACCOUNT_ROWS: list[dict[str, Any]] = [
    {"name": "alpha", "status": "running", "enabled": True, "workspace": None},
    {"name": "beta", "status": "stopped", "enabled": True, "workspace": None},
]


class _StubAccount:
    def __init__(self) -> None:
        self.record = NS(workspace=None)


class _StubAccountsManager:
    def __init__(self) -> None:
        self._accounts = {row["name"]: _StubAccount() for row in ACCOUNT_ROWS}

    async def accounts(self, *, refresh_records: bool = False) -> list[dict[str, Any]]:
        return [dict(row) for row in ACCOUNT_ROWS]

    def refresh_workspace(self, name: str, workspace: str | None) -> None:
        self._accounts[name].record.workspace = workspace or None


class ControlApiWorkspacesTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="ws-api-")
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self) -> None:
        self.db = Database(Path(self.tmp.name) / "api.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.limiter = RateLimitManager(EventBus(), LOG_NS, None)
        self.service = WorkspaceService(
            self.db.workspaces,
            accounts=self.db.accounts,
            channels_repo=self.db.account_channels,
            presets_repo=self.db.presets,
            provenance=self.db.account_plugins,
            limiter=self.limiter,
        )
        await self.service.load()
        await self.db.accounts.add("alpha")
        await self.db.accounts.add("beta")
        self.accounts_mgr = _StubAccountsManager()
        # The NS()/stub collaborators are inert duck-typed stand-ins; the
        # facade types them nominally, so they ride the dynamic seam.
        collaborators: dict[str, Any] = {
            "accounts": self.accounts_mgr,
            "plugins": NS(),
            "settings": NS(),
            "limiter": self.limiter,
            "scheduler": NS(),
            "logs": NS(),
            "workspaces": self.service,
        }
        self.api = ControlAPI(**collaborators)


class ControlApiWorkspacesTests(ControlApiWorkspacesTestBase):
    async def test_full_membership_flow(self) -> None:
        created = await self.api.workspaces_create(
            {"name": "grinders", "description": "the grind"}, surface="cli"
        )
        self.assertEqual(created["name"], "grinders")
        listed = await self.api.workspaces_list()
        self.assertEqual(listed[0]["member_count"], 0)
        got = await self.api.workspaces_get("grinders")
        self.assertEqual(got["name"], "grinders")

        out = await self.api.workspaces_add_account("alpha", "grinders")
        self.assertEqual(out["account"], "alpha")
        # the memory record follows (F3 pattern)
        self.assertEqual(self.accounts_mgr._accounts["alpha"].record.workspace, "grinders")
        # the pointer + pacing followed
        alpha = await self.db.accounts.get("alpha")
        assert alpha is not None  # narrowing: added in asyncSetUp
        self.assertEqual(alpha.workspace, "grinders")
        self.assertEqual(self.limiter.account_groups(), {"alpha": "grinders"})

        left = await self.api.workspaces_remove_account("alpha")
        self.assertTrue(left["left"])
        self.assertEqual(self.limiter.account_groups(), {})
        self.assertIsNone(self.accounts_mgr._accounts["alpha"].record.workspace)

    async def test_set_account_none_removes(self) -> None:
        await self.api.workspaces_create({"name": "grinders"})
        await self.api.workspaces_add_account("alpha", "grinders")
        out = await self.api.workspaces_set_account("alpha", None)
        self.assertTrue(out["left"])

    async def test_pause_resume_fence(self) -> None:
        await self.api.workspaces_create({"name": "grinders"})
        paused = await self.api.workspaces_pause("grinders")
        self.assertTrue(paused["paused"])
        resumed = await self.api.workspaces_resume("grinders")
        self.assertFalse(resumed["paused"])
        self.assertEqual(resumed["generation"], 3)

    async def test_delete_refuses_then_detaches(self) -> None:
        await self.api.workspaces_create({"name": "grinders"})
        await self.api.workspaces_add_account("alpha", "grinders")
        with self.assertRaises(ValueError):
            await self.api.workspaces_delete("grinders")
        out = await self.api.workspaces_delete("grinders", detach_members=True, surface="cli")
        self.assertEqual(out["detached"], ["alpha"])
        detached = await self.db.accounts.get("alpha")
        assert detached is not None  # narrowing: added in asyncSetUp
        self.assertIsNone(detached.workspace)

    async def test_tasks_surface(self) -> None:
        await self.api.workspaces_create({"name": "grinders"})
        await self.api.workspaces_add_account("alpha", "grinders")
        row_id = (
            await self.api.workspaces_op("create_task", workspace="grinders", task_id="grind")
        )["id"]
        assigned = await self.api.workspaces_op("assign_task", id=row_id, account="alpha")
        self.assertEqual(assigned["status"], "assigned")
        rows = await self.api.workspaces_tasks(workspace="grinders")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account"], "alpha")

    async def test_cooldown_surface(self) -> None:
        await self.api.workspaces_create({"name": "grinders"})
        rec = await self.api.workspaces_op(
            "cooldown", scope_type="workspace", scope_id="grinders", reason="hold", duration_s=60.0
        )
        self.assertIsNotNone(rec["id"])
        cleared = await self.api.workspaces_op("cooldown_clear", id=rec["id"])
        self.assertIsNotNone(cleared["cleared_at"])
        active = await self.api.workspaces_cooldowns(active_only=True)
        self.assertEqual(active, [])

    async def test_unwired_api_is_honest(self) -> None:
        inert: dict[str, Any] = {
            "accounts": NS(),
            "plugins": NS(),
            "settings": NS(),
            "limiter": NS(),
            "scheduler": NS(),
            "logs": NS(),
        }
        api = ControlAPI(**inert)
        self.assertEqual(await api.workspaces_list(), [])  # reads degrade
        with self.assertRaises(RuntimeError):  # writes fail loudly
            await api.workspaces_create({"name": "x"})


if __name__ == "__main__":
    unittest.main()
