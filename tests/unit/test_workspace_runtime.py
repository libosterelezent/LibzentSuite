"""ChainRuntime tests (W4) — the orchestrator's contracts.

Real repositories + the real WorkspaceService + the real policy over
a temp database; stub invoker/gate-reader seams. These are the W4
exit criteria: A acts → B follows; a missing plugin is a VISIBLE
skip; a denied gate skips with the reason; failures isolate per step;
routing is deterministic; snapshots survive edits; restart
reconciliation never blindly replays.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from core.workspace.runtime import ChainRuntime, TerminalTrigger
from core.workspace.service import WorkspaceService
from database.db import Database


class RuntimeTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.service = WorkspaceService(self.db.workspaces)
        await self.service.load()
        # two members: A (senior) and B — first_eligible picks A
        for name in ("A", "B"):
            await self.db.accounts.add(name)
            await self.service.join(name, "alpha")
        # the stub seams
        self.invocations: list[tuple[str, str, dict]] = []
        self.fail_handlers: set[str] = set()
        self.gate_overrides: dict[tuple[str, str], dict] = {}
        self.runtime = ChainRuntime(
            self.db.workspaces,
            service=self.service,
            signal_reader=self.db.signals,
            invoker=self._invoke,
            gate_reader=NS(plugin_gate_facts=self._gate_facts),
        )

    async def _invoke(self, account: str, plugin: str, handler: str, payload: dict):
        self.invocations.append((account, plugin, dict(payload)))
        if handler in self.fail_handlers:
            raise RuntimeError(f"handler {handler} exploded")

    async def _gate_facts(self, account: str, plugin: str, gate: dict):
        return self.gate_overrides.get((account, plugin), {})

    async def _chain(self, **step_overrides) -> int:
        step = {
            "position": 1,
            "plugin": "cleaner",
            "on": {"plugin": "grinder", "task_action": "message",
                   "event": "task_completed"},
        }
        step.update(step_overrides)
        row = await self.runtime.create_chain(
            "alpha", {"name": "claim-cleanup", "steps": [step]})
        return int(row["id"])

    async def _trigger_grinder(self) -> list[str]:
        return await self.runtime.on_terminal(TerminalTrigger(
            account="A", plugin="grinder", action="message",
            kind="task_completed"))


class MechanicalTriggerTests(RuntimeTestBase):
    async def test_terminal_fires_and_first_eligible_routes_to_senior(self) -> None:
        await self._chain()
        runs = await self._trigger_grinder()
        self.assertEqual(len(runs), 1)
        # A joined first — deterministic seniority pick
        self.assertEqual(len(self.invocations), 1)
        account, plugin, payload = self.invocations[0]
        self.assertEqual((account, plugin), ("A", "cleaner"))
        self.assertEqual(payload["trigger"]["plugin"], "grinder")
        steps = await self.runtime.run_steps(runs[0])
        self.assertEqual(steps[0]["outcome"], "done")

    async def test_run_completes(self) -> None:
        await self._chain()
        runs = await self._trigger_grinder()
        run = await self.db.workspaces.chain_run(runs[0])
        self.assertEqual(run["status"], "completed")

    async def test_non_matching_terminal_does_not_fire(self) -> None:
        await self._chain()
        runs = await self.runtime.on_terminal(TerminalTrigger(
            account="A", plugin="OTHER", action="message",
            kind="task_completed"))
        self.assertEqual(runs, [])
        self.assertEqual(self.invocations, [])


class SignalTriggerTests(RuntimeTestBase):
    async def test_signal_fires_matching_chain(self) -> None:
        from core.workspace.signals import SignalOutbox

        outbox = SignalOutbox(self.db.signals)
        signal_id = await outbox.accept("A", "grinder", "claim_done")
        await self._chain(
            on={"signal": "grinder:claim_done"}, plugin="cleaner")
        runs = await self.runtime.on_signal(signal_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.invocations[0][1], "cleaner")

    async def test_foreign_namespace_never_fires(self) -> None:
        from core.workspace.signals import SignalOutbox

        outbox = SignalOutbox(self.db.signals)
        signal_id = await outbox.accept("A", "grinder", "claim_done")
        await self._chain(on={"signal": "cleaner:claim_done"})
        self.assertEqual(await self.runtime.on_signal(signal_id), [])


class VisibleSkipTests(RuntimeTestBase):
    async def test_plugin_not_assigned_is_a_visible_skip(self) -> None:
        await self._chain()
        self.gate_overrides[("A", "cleaner")] = {"plugin_assigned": False}
        self.gate_overrides[("B", "cleaner")] = {"plugin_assigned": False}
        runs = await self._trigger_grinder()
        steps = await self.runtime.run_steps(runs[0])
        self.assertEqual(steps[0]["outcome"], "skipped")
        self.assertIn("not assigned", steps[0]["error"])  # the REASON
        self.assertEqual(self.invocations, [])  # nothing ran

    async def test_gate_denial_skips_with_the_setting_named(self) -> None:
        await self._chain(gate={"setting": "auto_clean", "equals": True})
        self.gate_overrides[("A", "cleaner")] = {"gate_passed": False}
        self.gate_overrides[("B", "cleaner")] = {"gate_passed": False}
        runs = await self._trigger_grinder()
        steps = await self.runtime.run_steps(runs[0])
        self.assertEqual(steps[0]["outcome"], "skipped")
        self.assertIn("chain gate", steps[0]["error"])

    async def test_channel_unusable_skips(self) -> None:
        await self._chain()
        self.gate_overrides[("A", "cleaner")] = {
            "channel_binding_usable": False,
            "channel_binding_reason": "channel deleted"}
        self.gate_overrides[("B", "cleaner")] = {
            "channel_binding_usable": False}
        runs = await self._trigger_grinder()
        steps = await self.runtime.run_steps(runs[0])
        self.assertEqual(steps[0]["outcome"], "skipped")
        self.assertIn("channel deleted", steps[0]["error"])

    async def test_no_eligible_member_skips(self) -> None:
        await self._chain(routing="specific_account", target="ghost")
        runs = await self._trigger_grinder()
        steps = await self.runtime.run_steps(runs[0])
        self.assertEqual(steps[0]["outcome"], "skipped")
        self.assertIn("ghost", steps[0]["error"])
        self.assertIn("not in this workspace", steps[0]["error"])

    async def test_workspace_cooldown_blocks_dispatch_visibly(self) -> None:
        await self._chain()
        await self.service.start_cooldown(
            "workspace", "alpha", "observed limit", 300.0, source="observed")
        runs = await self._trigger_grinder()
        steps = await self.runtime.run_steps(runs[0])
        self.assertEqual(steps[0]["outcome"], "skipped")
        self.assertIn("cooldown", steps[0]["error"])


class FailureSemanticsTests(RuntimeTestBase):
    async def test_handler_failure_is_partial_under_continue(self) -> None:
        await self._chain()
        self.fail_handlers.add("on_cleaner_trigger")
        runs = await self._trigger_grinder()
        run = await self.db.workspaces.chain_run(runs[0])
        self.assertEqual(run["status"], "partial")  # FAILED recorded, run ended
        steps = await self.runtime.run_steps(runs[0])
        self.assertEqual(steps[0]["outcome"], "failed")
        self.assertIn("exploded", steps[0]["error"])

    async def test_abort_fails_the_run(self) -> None:
        await self._chain()
        row = await self.db.workspaces.chain_by_name("alpha", "claim-cleanup")
        definition = {
            "name": "claim-cleanup", "on_skip": "abort",
            "steps": [{
                "position": 1, "plugin": "cleaner",
                "on": {"plugin": "grinder", "task_action": "message",
                       "event": "task_completed"}}],
        }
        await self.runtime.update_chain(int(row["id"]), definition)
        self.fail_handlers.add("on_cleaner_trigger")
        runs = await self._trigger_grinder()
        run = await self.db.workspaces.chain_run(runs[0])
        self.assertEqual(run["status"], "failed")


class SnapshotTests(RuntimeTestBase):
    async def test_edits_affect_future_runs_only(self) -> None:
        chain_id = await self._chain()
        # run 1 on the original definition
        first = await self._trigger_grinder()
        # edit: swap the plugin
        await self.runtime.update_chain(chain_id, {
            "name": "claim-cleanup",
            "steps": [{
                "position": 1, "plugin": "scrubber",
                "on": {"plugin": "grinder", "task_action": "message",
                       "event": "task_completed"}}],
        })
        row = await self.db.workspaces.chain(chain_id)
        self.assertEqual(row["revision"], 2)  # the revision bumped
        second = await self._trigger_grinder()
        # run 1 executed cleaner; run 2 (post-edit) executes scrubber
        self.assertEqual(self.invocations[0][1], "cleaner")
        self.assertEqual(self.invocations[1][1], "scrubber")


class MultiStepTests(RuntimeTestBase):
    async def test_sequential_steps_and_partial_on_skip(self) -> None:
        row = await self.runtime.create_chain("alpha", {
            "name": "two-step",
            "steps": [
                {"position": 1, "plugin": "grinder",
                 "on": {"plugin": "grinder", "task_action": "message",
                        "event": "task_completed"}},
                {"position": 2, "plugin": "cleaner",
                 "on": {"signal": "grinder:done"}},
            ],
        })
        del row
        runs = await self._trigger_grinder()
        # step 1 ran; step 2's trigger never fired (no signal emitted)
        # — it STILL EXECUTED because a run executes ALL steps once
        # started (the chain is the unit, not per-step retriggering)
        self.assertEqual(len(self.invocations), 2)
        run = await self.db.workspaces.chain_run(runs[0])
        self.assertEqual(run["status"], "completed")


class ReconcileTests(RuntimeTestBase):
    async def test_interrupted_run_fails_never_replays(self) -> None:
        await self._chain()
        # simulate a crash mid-run: a live run + an unfinished step row
        await self.db.workspaces.insert_chain_run(
            "run-stuck", 1, "alpha", "{}", triggered_by="test")
        execution_id = await self.db.workspaces.insert_step_execution(
            "run-stuck", 1, "A")
        repaired = await self.runtime.reconcile_interrupted_runs()
        self.assertEqual(repaired, 1)
        steps = await self.runtime.run_steps("run-stuck")
        self.assertEqual(steps[0]["outcome"], "failed")
        self.assertIn("restart", steps[0]["error"])
        run = await self.db.workspaces.chain_run("run-stuck")
        self.assertEqual(run["status"], "failed")


if __name__ == "__main__":
    unittest.main()
