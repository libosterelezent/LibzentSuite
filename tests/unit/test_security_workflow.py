"""Offline regression checks for the Gitleaks v3 workflow upgrade.

These check the repository's action wiring and lock, not the third-party
scanner implementation. PyYAML is supplied by the locked Bandit dev dependency.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = ".github/workflows/security.yml"
_ACTION = "gitleaks/gitleaks-action@v3.0.0"


class GitleaksWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        # BaseLoader preserves GitHub's `on` key and scalar text rather than
        # applying YAML 1.1 boolean coercion to workflow keys.
        self.workflow = yaml.load(
            (_ROOT / _WORKFLOW).read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        self.lock = yaml.load(
            (_ROOT / ".github/workflows/actions.lock").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.job = self.workflow["jobs"]["secret-scan"]
        self.steps = self.job["steps"]
        scans = [
            step
            for step in self.steps
            if step.get("uses", "").startswith("gitleaks/gitleaks-action@")
        ]
        self.assertEqual(len(scans), 1, "Exactly one Gitleaks action must run")
        self.scan = scans[0]

    def test_scanner_uses_the_upgraded_release(self) -> None:
        self.assertEqual(self.scan["uses"], _ACTION)

    def test_workflow_and_lock_agree_on_gitleaks_release(self) -> None:
        locked_actions = self.lock["workflows"][_WORKFLOW]
        self.assertEqual(
            [action for action in locked_actions if action.startswith("gitleaks/")],
            [self.scan["uses"]],
        )
        self.assertIn(self.scan["uses"], self.lock["dependencies"])

    def test_lock_resolves_release_to_expected_upstream_identity(self) -> None:
        self.assertEqual(
            self.lock["dependencies"][_ACTION],
            {
                "ref": "v3.0.0",
                "commit": "sha1-e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e",
                "owner_id": "90395851",
                "repo_id": "242854909",
            },
        )

    def test_lock_contains_no_stale_gitleaks_dependency(self) -> None:
        self.assertEqual(
            [key for key in self.lock["dependencies"] if key.startswith("gitleaks/")],
            [_ACTION],
        )

    def test_upgraded_scanner_receives_full_history_and_token(self) -> None:
        checkouts = [
            step for step in self.steps if step.get("uses", "").startswith("actions/checkout@")
        ]
        self.assertEqual(len(checkouts), 1)
        checkout = checkouts[0]
        self.assertLess(self.steps.index(checkout), self.steps.index(self.scan))
        self.assertEqual(checkout["with"]["fetch-depth"], "0")
        self.assertEqual(self.scan["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")

    def test_scan_failures_remain_blocking(self) -> None:
        for scope in (self.job, self.scan):
            with self.subTest(scope=scope.get("name")):
                self.assertIn(scope.get("continue-on-error", "false"), ("false",))
                self.assertNotIn("if", scope, "Secret scanning must remain unconditional")


if __name__ == "__main__":
    unittest.main()
