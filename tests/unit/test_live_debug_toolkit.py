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
import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

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

    def test_benign_warning_is_observation(self) -> None:
        self.assertEqual(
            live_debug.classify_log_entry("WARNING", "ratelimit", "server rate limit honored"),
            "observation",
        )

    def test_ordinary_warning_and_info_are_not_findings(self) -> None:
        self.assertIsNone(
            live_debug.classify_log_entry("WARNING", "accounts", "account alpha stopped")
        )
        self.assertIsNone(live_debug.classify_log_entry("INFO", "plugins", "loaded ping"))

    def test_severity_matching_is_case_insensitive(self) -> None:
        self.assertEqual(live_debug.classify_log_entry("error", "x", "y"), "major")


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


class IsolationTests(unittest.TestCase):
    """The toolkit must never touch the live state/ directory."""

    def setUp(self) -> None:
        self.source = inspect.getsource(live_debug)

    def test_no_production_state_path_in_source(self) -> None:
        self.assertNotIn("state/libzent.db", self.source)
        self.assertNotIn("state/credentials.key", self.source)

    def test_workdir_is_a_disposable_tempdir(self) -> None:
        self.assertIn("libzent-live-debug-", self.source)  # mkdtemp prefix
        self.assertIn("tempfile.mkdtemp", self.source)

    def test_default_findings_dir_is_inside_the_toolkit(self) -> None:
        self.assertEqual(
            live_debug.DEFAULT_FINDINGS_DIR,
            REPO_ROOT / "scripts" / "live-debugging" / "findings",
        )
        self.assertNotIn("state", live_debug.DEFAULT_FINDINGS_DIR.parts)

    def test_boot_targets_are_all_disposable(self) -> None:
        # the staged database + config live under the temp workdir; the
        # only REPO paths the harness touches read-only are the plugin
        # sources and the sim fixtures
        self.assertTrue(str(live_debug.SCENARIO_FIXTURES).startswith(str(REPO_ROOT)))


class BootPhaseIntegration(unittest.IsolatedAsyncioTestCase):
    """The smallest phase set, in-process, time-bounded, offline."""

    async def test_boot_phase_passes_against_the_simulator(self) -> None:
        findings_rows: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="live-debug-test-") as tmp:
            summary = await asyncio.wait_for(
                live_debug.run_pass(
                    ["boot"], findings_dir=Path(tmp), console=live_debug.Console(quiet=True)
                ),
                timeout=90.0,
            )
            runs = sorted(Path(tmp).glob("run-*.jsonl"))
            self.assertEqual(len(runs), 1)
            findings_rows = [json.loads(line) for line in runs[0].read_text().splitlines() if line]
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(len(summary["phases"]), 1)
        self.assertEqual(summary["phases"][0]["name"], "boot")
        self.assertGreaterEqual(summary["steps_passed"], 3)
        self.assertEqual(summary["phases"][0]["steps_failed"], 0)
        self.assertEqual(findings_rows, [])  # findings file stays anomalies-only


if __name__ == "__main__":
    unittest.main()
