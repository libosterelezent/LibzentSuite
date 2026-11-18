"""Live-debug toolkit contracts (scripts/live-debugging/live_debug.py).

The toolkit is a LIVE harness a human watches, not a test suite — but
its own invariants are pinned here: the findings schema, the phase
registry's completeness, the anomaly classifier's severity mapping, and
the state/ isolation guarantee. One integration-style test boots the
SMALLEST phase set (boot) in-process against the simulator, bounded in
time, no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_MODULE_PATH = REPO_ROOT / "scripts" / "live-debugging" / "live_debug.py"
_SPEC = importlib.util.spec_from_file_location("live_debug_toolkit", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None  # static path, always present
live_debug = importlib.util.module_from_spec(_SPEC)
# register BEFORE exec_module: dataclasses resolves types through
# sys.modules[cls.__module__] at class-creation time
sys.modules["live_debug_toolkit"] = live_debug
_SPEC.loader.exec_module(live_debug)


class FindingSchemaTests(unittest.TestCase):
    def test_finding_dict_has_exactly_the_schema_keys(self) -> None:
        finding = live_debug.make_finding(
            7,
            "20261117T000000",
            phase="cog-commands",
            area="cog-dispatch",
            severity="minor",
            summary="s",
            evidence=["e1", "e2"],
            expectation="expected behavior",
            actual="observed behavior",
            repro="cog-commands/step",
        )
        self.assertEqual(
            set(finding.as_dict()),
            {
                "id",
                "ts",
                "phase",
                "area",
                "severity",
                "summary",
                "evidence",
                "expectation",
                "actual",
                "repro",
            },
        )
        self.assertEqual(finding.id, "20261117T000000-0007")

    def test_bad_severity_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            live_debug.make_finding(
                1,
                "stamp",
                phase="p",
                area="a",
                severity="catastrophic",
                summary="s",
                evidence=[],
                expectation="e",
                actual="a",
                repro="r",
            )

    def test_evidence_is_capped_and_stringified(self) -> None:
        finding = live_debug.make_finding(
            1,
            "stamp",
            phase="p",
            area="a",
            severity="observation",
            summary="s",
            evidence=[str(i) for i in range(50)],
            expectation="e",
            actual="a",
            repro="r",
        )
        self.assertEqual(len(finding.as_dict()["evidence"]), live_debug.EVIDENCE_MAX)

    def test_findings_writer_refuses_to_truncate_an_existing_file(self) -> None:
        # a run-id collision must be LOUD, never a silent "w"-mode
        # truncation of the earlier run's evidence
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-x.jsonl"
            live_debug.FindingsWriter(path).close()
            with self.assertRaises(FileExistsError):
                live_debug.FindingsWriter(path)

    def test_findings_writer_writes_one_json_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-x.jsonl"
            writer = live_debug.FindingsWriter(path)
            for seq in range(3):
                writer.write(
                    live_debug.make_finding(
                        seq,
                        "stamp",
                        phase="p",
                        area="a",
                        severity="minor",
                        summary=f"s{seq}",
                        evidence=[],
                        expectation="e",
                        actual="a",
                        repro="r",
                    )
                )
            counts = writer.counts()
            writer.close()
            lines = [line for line in path.read_text().splitlines() if line]
            self.assertEqual(len(lines), 3)
            rows = [json.loads(line) for line in lines]
            self.assertTrue(all(set(row) >= {"id", "severity"} for row in rows))
            self.assertEqual(counts["minor"], 3)


class ClassifierTests(unittest.TestCase):
    def test_error_rows_are_major(self) -> None:
        self.assertEqual(live_debug.classify_log_entry("ERROR", "scheduler", "boom"), "major")

    def test_contract_warning_is_minor(self) -> None:
        self.assertEqual(
            live_debug.classify_log_entry("WARNING", "accounts", "stop failed for alpha"), "minor"
        )

    def test_the_estop_announcement_is_an_observation(self) -> None:
        # ERROR-loud BY DESIGN: exact source + exact prefix
        self.assertEqual(
            live_debug.classify_log_entry(
                "ERROR", "accounts", "EMERGENCY STOP gamma: cancelled 0 tasks, disconnecting"
            ),
            "observation",
        )

    def test_an_unrelated_error_mentioning_emergency_stop_is_major(self) -> None:
        # the old substring match let this through as an exit-0 observation
        self.assertEqual(
            live_debug.classify_log_entry(
                "ERROR", "plugins", "restore-before-emergency stop failed: database locked"
            ),
            "major",
        )

    def test_scheduler_cancellation_receipts_are_observations(self) -> None:
        self.assertEqual(
            live_debug.classify_log_entry("WARNING", "scheduler", "task 7 was cancelled"),
            "observation",
        )

    def test_vanished_target_abort_notices_are_observations(self) -> None:
        self.assertEqual(
            live_debug.classify_log_entry(
                "WARNING", "account:alpha", "flow aborted for alpha: channel gone (TargetGone)"
            ),
            "observation",
        )

    def test_ordinary_warning_and_info_are_not_findings(self) -> None:
        self.assertIsNone(
            live_debug.classify_log_entry("WARNING", "accounts", "account alpha stopped")
        )
        self.assertIsNone(live_debug.classify_log_entry("INFO", "plugins", "loaded ping"))

    def test_severity_matching_is_case_insensitive(self) -> None:
        self.assertEqual(live_debug.classify_log_entry("error", "x", "y"), "major")


class PhaseSelectionTests(unittest.TestCase):
    """select_phases: validate first, registry order, no file side effects."""

    def test_typo_is_rejected_before_any_findings_file_opens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyError):
                live_debug.select_phases(["typo"])
            self.assertEqual(list(Path(tmp).iterdir()), [])  # nothing created

    def test_argument_order_never_reorders_the_pass(self) -> None:
        # --phase shutdown --phase accounts must still run accounts first
        chosen = live_debug.select_phases(["shutdown", "accounts"])
        self.assertEqual([phase.name for phase in chosen], ["accounts", "shutdown"])

    def test_a_single_phase_runs_alone(self) -> None:
        self.assertEqual(
            [phase.name for phase in live_debug.select_phases(["rate-limits"])], ["rate-limits"]
        )

    def test_empty_selection_means_every_phase(self) -> None:
        self.assertEqual(
            [phase.name for phase in live_debug.select_phases([])],
            [phase.name for phase in live_debug.PHASES],
        )


class PhaseRegistryTests(unittest.TestCase):
    def test_every_phase_name_resolves(self) -> None:
        for phase in live_debug.PHASES:
            self.assertIs(live_debug.phase_by_name(phase.name), phase)

    def test_unknown_phase_names_the_known_set(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            live_debug.phase_by_name("no-such-phase")
        self.assertIn("boot", str(ctx.exception))

    def test_names_are_unique_and_areas_nonempty(self) -> None:
        names = [phase.name for phase in live_debug.PHASES]
        self.assertEqual(len(names), len(set(names)))
        for phase in live_debug.PHASES:
            self.assertTrue(phase.area.strip(), phase.name)
            self.assertTrue(phase.description.strip(), phase.name)
            self.assertTrue(inspect.iscoroutinefunction(phase.run), phase.name)

    def test_the_feature_areas_are_covered_by_name(self) -> None:
        names = {phase.name for phase in live_debug.PHASES}
        for expected in (
            "boot",
            "accounts",
            "credentials",
            "plugins",
            "profiles",
            "cog-commands",
            "tasks-scheduler",
            "rate-limits",
            "workspaces",
            "presets",
            "chains-signals",
            "observability",
            "simulation",
            "shutdown",
        ):
            self.assertIn(expected, names)


def dir_fingerprint(root: Path) -> list[tuple[str, str]]:
    """RECURSIVE content fingerprint of a directory tree: one sorted
    (relative path, sha256 of contents) tuple per FILE ([] when the
    root is absent). Recursion + digests matter: a nested overwrite
    (state/cache/account.bin) changes no parent entry's stat tuple, so
    the old depth-one metadata snapshot could not see it. Nothing here
    ever deletes or resets anything (AGENTS.md §7)."""
    if not root.is_dir():
        return []
    return sorted(
        (str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*")
        if path.is_file()
    )


def state_snapshot() -> list[tuple[str, str]]:
    """The live state/ directory's fingerprint (see dir_fingerprint)."""
    return dir_fingerprint(REPO_ROOT / "state")


class DirFingerprintTests(unittest.TestCase):
    """The isolation witness itself: recursion + content digests."""

    def test_nested_overwrite_changes_the_fingerprint(self) -> None:
        # the old depth-one metadata snapshot could not see this: the
        # parent directory's mtime does not change when a nested file's
        # CONTENTS change
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cache").mkdir()
            target = root / "cache" / "account.bin"
            target.write_bytes(b"v1")
            before = dir_fingerprint(root)
            self.assertEqual(before, [("cache/account.bin", hashlib.sha256(b"v1").hexdigest())])
            target.write_bytes(b"v2")  # same length, different content
            self.assertNotEqual(dir_fingerprint(root), before)
            target.write_bytes(b"v1")  # identical content restores it
            self.assertEqual(dir_fingerprint(root), before)

    def test_added_and_removed_files_change_the_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = dir_fingerprint(root)
            nested = root / "sub"
            nested.mkdir()
            (nested / "deep.bin").write_bytes(b"payload")
            with_deep = dir_fingerprint(root)
            self.assertNotEqual(with_deep, before)
            (nested / "deep.bin").unlink()
            self.assertEqual(dir_fingerprint(root), before)


class BootPhaseIntegration(unittest.IsolatedAsyncioTestCase):
    """The smallest phase set, in-process, time-bounded, offline — and
    the observable state/ isolation guarantee (source-text greps proved
    nothing; the directory itself is the witness)."""

    async def test_boot_phase_passes_against_the_simulator(self) -> None:
        findings_rows: list[dict[str, Any]] = []
        state_before = state_snapshot()
        with tempfile.TemporaryDirectory(prefix="live-debug-test-") as tmp:
            # generous bound (the scheduler-flake lesson): it only
            # matters on failure — a real boot takes ~1.5s, but one
            # gate run's environmental load pushed 90s once and never
            # reproduced in ANY context after (isolated x6, full tree,
            # exact gate rerun). 300s still fails inside the gate's
            # own 590s timeout with evidence when a boot truly hangs.
            summary = await asyncio.wait_for(
                live_debug.run_pass(
                    ["boot"], findings_dir=Path(tmp), console=live_debug.Console(quiet=True)
                ),
                timeout=300.0,
            )
            runs = sorted(Path(tmp).glob("run-*.jsonl"))
            self.assertEqual(len(runs), 1)
            summaries = sorted(Path(tmp).glob("run-*.summary.json"))
            self.assertEqual(len(summaries), 1)
            # the SAME collision-resistant id backs both files
            self.assertEqual(
                runs[0].name[: -len(".jsonl")],
                summaries[0].name[: -len(".summary.json")],
            )
            findings_rows = [json.loads(line) for line in runs[0].read_text().splitlines() if line]
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(len(summary["phases"]), 1)
        self.assertEqual(summary["phases"][0]["name"], "boot")
        self.assertGreaterEqual(summary["steps_passed"], 3)
        self.assertEqual(summary["phases"][0]["steps_failed"], 0)
        self.assertEqual(findings_rows, [])  # findings file stays anomalies-only
        # ISOLATION (observable): a full real-stack boot left state/ —
        # the live databases and credentials key — bit-for-bit untouched
        self.assertEqual(state_snapshot(), state_before)

    async def test_two_rapid_runs_never_share_a_findings_file(self) -> None:
        # two boots inside the same wall-clock second used to collide on
        # one filename; the uuid-suffixed run id keeps them distinct
        with tempfile.TemporaryDirectory(prefix="live-debug-test-") as tmp:
            for _ in range(2):
                await asyncio.wait_for(
                    live_debug.run_pass(
                        ["boot"], findings_dir=Path(tmp), console=live_debug.Console(quiet=True)
                    ),
                    timeout=90.0,
                )
            self.assertEqual(len(list(Path(tmp).glob("run-*.jsonl"))), 2)
            self.assertEqual(len(list(Path(tmp).glob("run-*.summary.json"))), 2)

    async def test_failed_boot_records_an_unsuccessful_summary(self) -> None:
        async def broken_boot(self: Any) -> None:
            raise RuntimeError("simulated boot failure")

        with (
            tempfile.TemporaryDirectory(prefix="live-debug-test-") as tmp,
            mock.patch.object(live_debug.Harness, "boot", broken_boot),
        ):
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(
                    live_debug.run_pass(
                        ["boot"], findings_dir=Path(tmp), console=live_debug.Console(quiet=True)
                    ),
                    timeout=90.0,
                )
            # reads happen while the tempdir lives (assertRaises already
            # consumed the propagated boot failure above)
            rows = [
                json.loads(line)
                for line in next(Path(tmp).glob("run-*.jsonl")).read_text().splitlines()
                if line
            ]
            summary = json.loads(next(Path(tmp).glob("run-*.summary.json")).read_text())
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["exit_code"], 1)
        self.assertTrue(
            any(row["severity"] == "critical" and "boot failed" in row["summary"] for row in rows)
        )


if __name__ == "__main__":
    unittest.main()
