"""Workspace persistence-layer tests (W2) — repos + drop_account.

Every new repository against a real (temp-file) database with the
full migration applied; the preset immutability, channel-dependency
conflicts, member projection and deletion-job workflow are the
load-bearing contracts.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database.db import Database
from database.models import WorkspaceMemberRecord, WorkspaceTaskRecord


class WorkspaceRepoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)

    async def test_ensure_and_set_state(self) -> None:
        repo = self.db.workspaces
        created = await repo.ensure_workspace("alpha")
        self.assertEqual(created.generation, 1)
        again = await repo.ensure_workspace("alpha")
        self.assertEqual(again.created_at, created.created_at)  # get-or-create
        paused = await repo.set_workspace("alpha", paused=True)
        self.assertTrue(paused.paused)
        self.assertEqual(paused.generation, 2)  # pause bumps the fence
        unpaused = await repo.set_workspace("alpha", paused=False, bump_generation=False)
        self.assertFalse(unpaused.paused)
        self.assertEqual(unpaused.generation, 2)
        bumped = await repo.bump_generation("alpha")
        self.assertEqual(bumped.generation, 3)

    async def test_member_projection_upsert_and_version(self) -> None:
        repo = self.db.workspaces
        await repo.ensure_workspace("alpha")
        await repo.upsert_member(
            WorkspaceMemberRecord(account="A", workspace="alpha", mode="managed")
        )
        member = await repo.member("A")
        assert member is not None
        self.assertEqual((member.workspace, member.mode), ("alpha", "managed"))
        await repo.upsert_member(
            WorkspaceMemberRecord(account="A", workspace="alpha", mode="independent")
        )
        moved = await repo.member("A")
        assert moved is not None
        self.assertEqual(moved.mode, "independent")
        self.assertEqual(moved.version, 2)  # optimistic-write guard
        await repo.delete_member("A")
        self.assertIsNone(await repo.member("A"))

    async def test_task_roundtrip_and_update_guard(self) -> None:
        repo = self.db.workspaces
        record = WorkspaceTaskRecord(id="t1", workspace="alpha", task_id="grind", account="A")
        await repo.insert_task(record)
        fetched = await repo.task("t1")
        assert fetched is not None
        self.assertEqual(fetched.status, "queued")
        await repo.update_task("t1", status="assigned", account="A", generation=2)
        updated = await repo.task("t1")
        assert updated is not None
        self.assertEqual(updated.status, "assigned")
        self.assertEqual(updated.generation, 2)
        self.assertEqual(updated.version, 2)  # version bumps on update
        with self.assertRaises(ValueError) as ctx:
            await repo.update_task("t1", nonsense="x")
        self.assertIn("nonsense", str(ctx.exception))

    async def test_deletion_job_workflow_states(self) -> None:
        repo = self.db.workspaces
        job = await repo.create_deletion_job("A")
        assert job is not None
        self.assertEqual(job.state, "pending")
        # idempotent creation
        again = await repo.create_deletion_job("A")
        assert again is not None
        self.assertEqual(again.created_at, job.created_at)
        await repo.update_deletion_job("A", state="primary_done")
        await repo.update_deletion_job("A", state="complete")
        done = await repo.deletion_job("A")
        assert done is not None
        self.assertEqual(done.state, "complete")
        self.assertIsNotNone(done.completed_at)
        self.assertEqual(done.attempts, 2)
        pending = await repo.deletion_jobs(state="pending")
        self.assertEqual(pending, [])


class PresetsRepoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)

    async def test_identity_and_immutable_versions(self) -> None:
        repo = self.db.presets
        v1 = await repo.create("global", "", "cleaner", "standard", {"k": 1}, schema_version=3)
        self.assertEqual(v1.version, 1)
        self.assertEqual(v1.schema_version, 3)
        new_version = await repo.publish_version(v1.preset_id, {"k": 2}, schema_version=4)
        self.assertEqual(new_version, 2)
        latest = await repo.latest_version("global", "", "cleaner", "standard")
        assert latest is not None
        self.assertEqual(latest.version, 2)
        self.assertEqual(latest.settings, {"k": 2})
        # the OLD version is immutable and still readable (provenance)
        frozen = await repo.version(v1.preset_id, 1)
        assert frozen is not None
        self.assertEqual(frozen.settings, {"k": 1})
        self.assertEqual(frozen.schema_version, 3)

    async def test_identity_scoped_uniqueness(self) -> None:
        repo = self.db.presets
        await repo.create("global", "", "cleaner", "standard", {})
        with self.assertRaises(ValueError):
            await repo.create("global", "", "cleaner", "standard", {})
        # same name under another owner is a DIFFERENT identity
        account_copy = await repo.create("account", "A", "cleaner", "standard", {})
        global_copy = await repo.latest_version("global", "", "cleaner", "standard")
        assert global_copy is not None
        self.assertNotEqual(
            account_copy.preset_id,
            global_copy.preset_id,
        )

    async def test_archive_hides_from_default_list(self) -> None:
        repo = self.db.presets
        preset = await repo.create("workspace", "alpha", "cleaner", "std", {})
        self.assertEqual(len(await repo.list()), 1)
        self.assertTrue(await repo.archive(preset.preset_id))
        self.assertEqual(await repo.list(), [])
        self.assertEqual(len(await repo.list(include_archived=True)), 1)
        # archived identity still resolvable by exact version
        frozen = await repo.version(preset.preset_id, 1)
        assert frozen is not None
        self.assertTrue(frozen.archived)


class AccountChannelsRepoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)

    async def test_registry_observe_never_touches_selections(self) -> None:
        repo = self.db.account_channels
        await repo.observe("A", "42", "500", name="grind", validity="available")
        await repo.assign_channel("A", "42", "500")
        # a later observation changes validity metadata ONLY — the
        # assignment stays exactly as the user chose it (§5.2 rule)
        await repo.observe("A", "42", "500", validity="stale", validation_reason="cache miss")
        assignments = await repo.assignments("A")
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0].validity, "stale")
        channel = await repo.channel("A", "42", "500")
        assert channel is not None
        self.assertEqual(channel.validity, "stale")
        self.assertEqual(channel.validation_reason, "cache miss")

    async def test_delete_refused_while_referenced(self) -> None:
        repo = self.db.account_channels
        await repo.upsert_channel("A", "42", "500")
        conflict = await repo.delete_channel("A", "42", "500")
        self.assertIsNone(conflict)  # nothing references it: deletes
        await repo.upsert_channel("A", "42", "500")
        await repo.bind_plugin_channel("A", "cleaner", "500", guild_id="42")
        conflict = await repo.delete_channel("A", "42", "500")
        assert conflict is not None
        self.assertIn("cleaner", conflict)
        await repo.unbind_plugin_channel("A", "cleaner", "500")
        self.assertIsNone(await repo.delete_channel("A", "42", "500"))

    async def test_drop_account_clears_everything(self) -> None:
        repo = self.db.account_channels
        await repo.upsert_channel("A", "42", "500")
        await repo.assign_channel("A", "42", "500")
        await repo.bind_plugin_channel("A", "cleaner", "500", guild_id="42")
        await repo.drop_account("A")
        self.assertEqual(await repo.channels("A"), [])
        self.assertEqual(await repo.assignments("A"), [])
        self.assertEqual(await repo.plugin_bindings("A", "cleaner"), [])
        await repo.drop_account("A")  # idempotent


class PluginDataDropAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_drop_account_is_the_cross_store_boundary(self) -> None:
        from database.plugin_data import PluginDataStore

        store = PluginDataStore(Path(self._tmp.name) / "data.db")
        await store.connect()
        try:
            await store.put("alpha", "cleaner", "42", "500", {"claimed": 1})
            await store.put("alpha", "cleaner", "42", "501", {"claimed": 2})
            await store.put("bravo", "cleaner", "42", "500", {"claimed": 3})
            removed = await store.drop_account("alpha")
            self.assertEqual(removed, 2)
            self.assertIsNone(await store.get("alpha", "cleaner", guild_id="42", channel_id="500"))
            # OTHER accounts untouched — the boundary is per-account
            kept = await store.get("bravo", "cleaner", guild_id="42", channel_id="500")
            assert kept is not None
            self.assertEqual(kept.get("data", {}).get("claimed"), 3)
            self.assertEqual(await store.drop_account("alpha"), 0)  # idempotent
            # B017 suppression: the refusal path is intentionally pinned
            # broadly — any failure beats silently dropping an empty name
            with self.assertRaises(Exception):  # noqa: B017
                await store.drop_account("")  # refuses an empty name
        finally:
            await store.close()


if __name__ == "__main__":
    unittest.main()
