"""WorkspaceService tests (W3) — the one writer's contracts.

Real repositories over a temp database (the W2 layer), stub seams for
pacing/settings/plugin-data. The load-bearing contracts: membership
persist-first + the A→X racing A→Y lock discipline, generation
fencing on leave/move with task requeue, config-untouched rules,
transition-validated task ops with read-back guards, manual-only
cooldown clears, channel copy-bindings, preset apply with the
stale-preview guard and provenance, and the cross-store deletion
workflow with quarantine on failure.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any

from libzentsuite.core.workspace.service import WorkspaceService
from libzentsuite.database.db import Database

if TYPE_CHECKING:
    from libzentsuite.database.models import PresetTemplateRecord, WorkspaceMemberRecord


class StubSettings:
    """SettingsWriter double: an in-memory map with set/map."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict[str, Any]] = {}

    async def set_setting(self, account: str, plugin: str, key: str, value: Any) -> None:
        self.values.setdefault((account, plugin), {})[key] = value

    async def settings(self, account: str, plugin: str) -> dict[str, Any]:
        return dict(self.values.get((account, plugin), {}))


class StubPluginData:
    def __init__(self) -> None:
        self.dropped: list[str] = []
        self.fail = False

    async def drop_account(self, account: str) -> int:
        if self.fail:
            raise RuntimeError("documents store unreachable")
        self.dropped.append(account)
        return len(self.dropped)


class StubLimiter:
    def __init__(self) -> None:
        self.mapping: dict[str, str | None] = {}

    def set_account_group(self, account: str, group: str | None) -> None:
        self.mapping[account] = group


class WorkspaceServiceTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.settings = StubSettings()
        self.plugin_data = StubPluginData()
        self.limiter = StubLimiter()
        self.events: list[str] = []
        self.service = WorkspaceService(
            self.db.workspaces,
            accounts=self.db.accounts,
            channels_repo=self.db.account_channels,
            presets_repo=self.db.presets,
            provenance=self.db.account_plugins,
            plugin_data=self.plugin_data,
            settings_writer=self.settings,
            limiter=self.limiter,
            publisher=lambda ev: self.events.append(ev.kind),
        )
        await self.db.accounts.add("A")
        await self.db.accounts.add("B")

    async def member(self, account: str) -> WorkspaceMemberRecord | None:
        return await self.db.workspaces.member(account)

    async def pointer(self, account: str) -> str | None:
        record = await self.db.accounts.get(account)
        return record.workspace if record else None


class MembershipTests(WorkspaceServiceTestBase):
    async def test_join_writes_pointer_and_projection_together(self) -> None:
        result = await self.service.join("A", "alpha")
        self.assertTrue(result["generation"] >= 1)
        self.assertEqual(await self.pointer("A"), "alpha")  # THE pointer
        member = await self.member("A")
        assert member is not None
        self.assertEqual(member.workspace, "alpha")  # the projection agrees
        self.assertEqual(member.mode, "managed")
        self.assertEqual(self.limiter.mapping.get("A"), "alpha")  # pacing registered

    async def test_join_is_idempotent(self) -> None:
        first = await self.service.join("A", "alpha")
        again = await self.service.join("A", "alpha")
        self.assertFalse(again["moved"])
        self.assertEqual(first["workspace"], again["workspace"])

    async def test_move_fences_old_workspace_and_requeues_tasks(self) -> None:
        await self.service.join("A", "alpha")
        task = await self.service.create_task("alpha", "grind")
        await self.service.assign_task(task, "A")
        alpha_state = await self.db.workspaces.workspace_state("alpha")
        assert alpha_state is not None
        old_gen = alpha_state.generation

        await self.service.join("A", "beta")

        moved = await self.member("A")
        assert moved is not None
        self.assertEqual(moved.workspace, "beta")
        beta = await self.db.workspaces.workspace_state("beta")
        alpha = await self.db.workspaces.workspace_state("alpha")
        assert alpha is not None and beta is not None
        self.assertEqual(alpha.generation, old_gen + 1)  # old workspace fenced
        tasks = await self.service.tasks(workspace="alpha")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "queued")  # stale work requeued
        self.assertIsNone(tasks[0]["account"])
        self.assertEqual(self.limiter.mapping.get("A"), "beta")

    async def test_leave_keeps_account_owned_config(self) -> None:
        await self.service.join("A", "alpha")
        await self.settings.set_setting("A", "cleaner", "trigger", "$x")
        await self.service.assign_channel("A", "42", "500")

        left = await self.service.leave("A")

        self.assertTrue(left["left"])
        self.assertIsNone(await self.pointer("A"))
        self.assertIsNone(await self.member("A"))
        self.assertIsNone(self.limiter.mapping.get("A"))
        # rules 3-7: settings, channel assignments survive the leave
        self.assertEqual((await self.settings.settings("A", "cleaner"))["trigger"], "$x")
        self.assertEqual(len(await self.service.account_channels("A")), 1)

    async def test_concurrent_moves_always_agree(self) -> None:
        """The A→X racing A→Y discipline: serialized under the lock,
        the pointer and projection can never disagree."""
        await self.service.join("A", "alpha")
        await asyncio.gather(
            self.service.join("A", "beta"),
            self.service.join("A", "gamma"),
        )
        pointer = await self.pointer("A")
        member = await self.member("A")
        assert member is not None
        self.assertEqual(member.workspace, pointer)  # NEVER divergent
        self.assertIn(pointer, {"beta", "gamma"})
        self.assertEqual(self.limiter.mapping.get("A"), pointer)  # pacing agrees

    async def test_join_unknown_account_refused(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.join("ghost", "alpha")
        self.assertIsNone(await self.member("ghost"))


class WorkspaceStateTests(WorkspaceServiceTestBase):
    async def test_pause_resume_bump_generation(self) -> None:
        await self.service.create_workspace("alpha")
        paused = await self.service.pause("alpha")
        self.assertTrue(paused["paused"])
        resumed = await self.service.resume("alpha")
        self.assertFalse(resumed["paused"])
        self.assertEqual(resumed["generation"], 3)  # pause + resume each bumped
        self.assertEqual(await self.service.bump_generation("alpha"), 4)

    async def test_delete_refuses_members_then_detaches(self) -> None:
        await self.service.join("A", "alpha")
        with self.assertRaises(ValueError) as ctx:
            await self.service.delete_workspace("alpha")
        self.assertIn("detach_members", str(ctx.exception))
        result = await self.service.delete_workspace("alpha", detach_members=True)
        self.assertEqual(result["detached"], ["A"])
        self.assertIsNone(await self.pointer("A"))


class TaskTests(WorkspaceServiceTestBase):
    async def test_lifecycle_through_validators(self) -> None:
        await self.service.join("A", "alpha")
        task = await self.service.create_task("alpha", "grind")
        assigned = await self.service.assign_task(task, "A")
        self.assertEqual(assigned["status"], "assigned")
        running = await self.service.report_task_status(task, "running")
        self.assertEqual(running["status"], "running")
        self.assertIsNotNone(running["started_at"])
        done = await self.service.report_task_status(task, "completed")
        self.assertEqual(done["status"], "completed")
        self.assertIsNotNone(done["stopped_at"])

    async def test_failed_retry_needs_explicit_allowance(self) -> None:
        await self.service.join("A", "alpha")
        task = await self.service.create_task("alpha", "grind")
        await self.service.assign_task(task, "A")
        await self.service.report_task_status(task, "running")
        await self.service.report_task_status(task, "failed", error="boom")
        with self.assertRaises(ValueError) as ctx:
            await self.service.report_task_status(task, "queued")
        self.assertIn("retry allowance", str(ctx.exception))
        requeued = await self.service.report_task_status(task, "queued", retry_allowed=True)
        self.assertEqual(requeued["status"], "queued")

    async def test_assignment_carries_live_generation(self) -> None:
        await self.service.join("A", "alpha")
        task = await self.service.create_task("alpha", "grind")
        await self.service.bump_generation("alpha")  # fence moves
        assigned = await self.service.assign_task(task, "A")
        live = await self.db.workspaces.workspace_state("alpha")
        assert live is not None
        self.assertEqual(assigned["generation"], live.generation)


class CooldownTests(WorkspaceServiceTestBase):
    async def test_manual_clear_and_observed_rides(self) -> None:
        manual = await self.service.start_cooldown("member", "A", "operator hold", 60.0)
        observed = await self.service.start_cooldown(
            "member", "A", "limit seen", 60.0, source="observed"
        )
        cleared = await self.service.clear_cooldown(manual["id"])
        self.assertIsNotNone(cleared["cleared_at"])
        with self.assertRaises(ValueError) as ctx:
            await self.service.clear_cooldown(observed["id"])
        self.assertIn("only manual", str(ctx.exception))

    async def test_active_filter_and_bad_scope(self) -> None:
        await self.service.start_cooldown("member", "A", "hold", 60.0)
        active = await self.service.cooldowns(scope_type="member", active_only=True)
        self.assertEqual(len(active), 1)
        with self.assertRaises(ValueError):
            await self.service.start_cooldown("galaxy", "X", "bad", 60.0)


class ChannelTests(WorkspaceServiceTestBase):
    async def test_bindings_initialize_by_copy_then_diverge(self) -> None:
        await self.service.assign_channel("A", "42", "500")
        await self.service.assign_channel("A", "42", "501")
        count = await self.service.bind_plugin_channels_from_assignments("A", "cleaner")
        self.assertEqual(count, 2)
        # later account changes do NOT propagate into bindings (rule 2)
        await self.service.assign_channel("A", "42", "502")
        bindings = await self.service.plugin_bindings("A", "cleaner")
        self.assertEqual(sorted(b["channel_id"] for b in bindings), ["500", "501"])


class PresetTests(WorkspaceServiceTestBase):
    async def _preset(self) -> PresetTemplateRecord:
        template = await self.db.presets.create(
            "global", "", "cleaner", "standard", {"trigger": "$clean", "auto_clean": True}
        )
        await self.db.account_plugins.assign_plugin("A", "cleaner")
        await self.settings.set_setting("A", "cleaner", "unrelated", "keep")
        return template

    async def test_apply_writes_preset_keys_only_with_provenance(self) -> None:
        template = await self._preset()
        preview = await self.service.preset_preview("A", "cleaner", template.preset_id)
        self.assertEqual(preview["diff"], {"trigger": [None, "$clean"], "auto_clean": [None, True]})

        result = await self.service.preset_apply(
            "A", "cleaner", template.preset_id, preview_revision=preview["revision"]
        )

        self.assertTrue(result["applied"])
        final = await self.settings.settings("A", "cleaner")
        self.assertEqual(
            final, {"trigger": "$clean", "auto_clean": True, "unrelated": "keep"}
        )  # keys-only, rest kept
        row = await self.db.connection.fetch_one(
            "SELECT source_preset_id, source_version FROM account_plugins"
            " WHERE account='A' AND plugin='cleaner'"
        )
        assert row is not None
        self.assertEqual(row["source_preset_id"], str(template.preset_id))
        self.assertEqual(row["source_version"], 1)

    async def test_stale_preview_conflicts_never_overwrites(self) -> None:
        template = await self._preset()
        preview = await self.service.preset_preview("A", "cleaner", template.preset_id)
        # an intervening account edit between preview and apply
        await self.settings.set_setting("A", "cleaner", "trigger", "$user")
        result = await self.service.preset_apply(
            "A", "cleaner", template.preset_id, preview_revision=preview["revision"]
        )
        self.assertFalse(result["applied"])
        self.assertIn("stale preview", result["conflict"])
        # the user's edit survived the refused apply
        self.assertEqual((await self.settings.settings("A", "cleaner"))["trigger"], "$user")

    async def test_detach_keeps_values(self) -> None:
        template = await self._preset()
        preview = await self.service.preset_preview("A", "cleaner", template.preset_id)
        await self.service.preset_apply(
            "A", "cleaner", template.preset_id, preview_revision=preview["revision"]
        )
        await self.service.preset_detach("A", "cleaner")
        row = await self.db.connection.fetch_one(
            "SELECT source_preset_id FROM account_plugins WHERE account='A' AND plugin='cleaner'"
        )
        assert row is not None
        self.assertIsNone(row["source_preset_id"])
        self.assertEqual(  # values stay
            (await self.settings.settings("A", "cleaner"))["trigger"], "$clean"
        )


class ForgetAccountTests(WorkspaceServiceTestBase):
    async def test_deletion_workflow_spans_both_stores(self) -> None:
        await self.service.join("A", "alpha")
        await self.service.assign_channel("A", "42", "500")
        task = await self.service.create_task("alpha", "grind")
        await self.service.assign_task(task, "A")

        result = await self.service.forget_account("A")

        self.assertEqual(result["state"], "complete")
        self.assertIsNone(await self.pointer("A"))
        self.assertEqual(await self.service.account_channels("A"), [])
        cancelled = await self.service.tasks(workspace="alpha")
        self.assertEqual(cancelled[0]["status"], "cancelled")
        self.assertEqual(self.plugin_data.dropped, ["A"])  # the separate store
        job = await self.db.workspaces.deletion_job("A")
        assert job is not None
        self.assertEqual((job.state, job.completed_at is not None), ("complete", True))
        # idempotent re-run: another complete, no error
        again = await self.service.forget_account("A")
        self.assertEqual(again["state"], "complete")

    async def test_deletion_failure_quarantines_never_abandons(self) -> None:
        await self.service.join("A", "alpha")
        self.plugin_data.fail = True
        result = await self.service.forget_account("A")
        self.assertEqual(result["state"], "quarantined")
        self.assertIn("unreachable", result["error"])
        job = await self.db.workspaces.deletion_job("A")
        assert job is not None
        self.assertEqual(job.state, "quarantined")
        self.assertIn("unreachable", job.last_error)
        self.assertIsNone(job.completed_at)  # observable incompletion


if __name__ == "__main__":
    unittest.main()
