"""P8 — workspace presets REFERENCE account-owned profiles.

The owner-locked decisions (docs/design/data-management-adoption.md):
a workspace preset is a reusable orchestration DEFINITION that
references (account, plugin, profile) tuples — never copies profile
configuration. A workspace EXECUTION is one running/recoverable
instance of a definition: it carries the runtime state (workflow
progress, dependency checkpoints, coordination signals, participant
status) and persists checkpoints so an interrupted run resumes without
duplicating completed operations.

The properties under test:
1. Reference, not copy: applying a preset validates every referenced
   profile EXISTS on its account and ACTIVATES it — the profile body
   is never duplicated into the preset or the execution.
2. Membership: a preset may only reference member accounts.
3. Drift detection: a renamed/deleted profile is DETECTED at apply
   time — never silently substituted.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.workspace.service import WorkspaceService
from database.db import Database


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "w.db")
        await self.db.connect()
        self.svc = WorkspaceService(
            self.db.workspaces,
            preset_defs=self.db.workspace_presets,
            profiles=self.db.account_plugin_profiles,
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()


class ReferencesNotCopies(_Base):
    async def test_apply_activates_referenced_profiles_without_copying(self) -> None:
        # a workspace with two member accounts, each holding a profile
        await self.svc.create_workspace("farm")
        await self.svc.join("alpha", "farm")
        await self.svc.join("beta", "farm")
        await self.db.account_plugin_profiles.create(
            "alpha", "exporter", "main", settings={"chunk": 1800}
        )
        await self.db.account_plugin_profiles.create(
            "beta", "exporter", "secondary", settings={"chunk": 900}
        )
        preset = await self.svc.preset_define(
            "farm",
            "Mudae Farming",
            participants=[
                {"account": "alpha", "plugin": "exporter", "profile": "main"},
                {"account": "beta", "plugin": "exporter", "profile": "secondary"},
            ],
        )
        await self.svc.preset_apply_references(preset["preset_id"])
        # every referenced profile is now ACTIVE on its account — the
        # BODY was never copied anywhere
        self.assertEqual(
            await self.db.account_plugin_profiles.get_active("alpha", "exporter"), "main"
        )
        self.assertEqual(
            await self.db.account_plugin_profiles.get_active("beta", "exporter"),
            "secondary",
        )
        instances = await self.db.account_plugin_profiles.list_for("alpha", "exporter")
        self.assertEqual(len(instances), 1)  # no copy was created

    async def test_apply_detects_a_missing_profile_fails_closed(self) -> None:
        await self.svc.create_workspace("farm")
        await self.svc.join("alpha", "farm")
        preset = await self.svc.preset_define(
            "farm",
            "drifted",
            participants=[
                {"account": "alpha", "plugin": "exporter", "profile": "renamed-away"},
            ],
        )
        with self.assertRaises(Exception) as ctx:
            await self.svc.preset_apply_references(preset["preset_id"])
        self.assertIn("renamed-away", str(ctx.exception))

    async def test_membership_enforced_on_non_member(self) -> None:
        await self.svc.create_workspace("farm")
        await self.svc.join("alpha", "farm")  # beta is NOT a member
        await self.db.account_plugin_profiles.create("beta", "exporter", "main")
        preset = await self.svc.preset_define(
            "farm",
            "smuggle",
            participants=[
                {"account": "beta", "plugin": "exporter", "profile": "main"},
            ],
        )
        with self.assertRaises(Exception) as ctx:
            await self.svc.preset_apply_references(preset["preset_id"])
        self.assertIn("member", str(ctx.exception))

    async def test_execution_checkpoints_resume_without_replaying(self) -> None:
        """An execution instance carries workflow state; a resumed run
        skips completed participants."""
        await self.svc.create_workspace("farm")
        await self.svc.join("alpha", "farm")
        await self.svc.join("beta", "farm")
        for account, profile in (("alpha", "main"), ("beta", "alt")):
            await self.db.account_plugin_profiles.create(account, "exporter", profile)
        preset = await self.svc.preset_define(
            "farm",
            "ordered",
            participants=[
                {"account": "alpha", "plugin": "exporter", "profile": "main"},
                {"account": "beta", "plugin": "exporter", "profile": "alt"},
            ],
            order=["alpha", "beta"],
        )
        run = await self.svc.execution_start(preset["preset_id"])
        # alpha completes; the run is interrupted before beta
        await self.svc.execution_checkpoint(run["execution_id"], "alpha", "done")
        resumed = await self.svc.execution_resume(run["execution_id"])
        self.assertEqual(resumed["completed"], ["alpha"])  # not replayed
        self.assertEqual(resumed["pending"], ["beta"])  # where it resumes


if __name__ == "__main__":
    unittest.main()
