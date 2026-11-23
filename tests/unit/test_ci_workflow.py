"""The CI workflow's contracts that nothing online enforces.

``.github/workflows/tests.yml`` is read as data. What is pinned here:

* the Linux check names. master's ruleset requires four contexts BY NAME
  (``unittest (3.10)`` .. ``unittest (3.13)``): a renamed leg never reports,
  and every pull request to master then waits on a check that does not
  exist.
* the Windows legs. The suite is green on both platforms, so CI runs it on
  both; every leg is named uniquely.
* ``defaults.run.shell: bash``. Every step is bash, and a Windows runner
  defaults to pwsh.
* one step per name, and no bare ``python3``. A duplicated step ran pyright
  twice per leg, and ``python3`` is whichever interpreter the runner has.

Set ``LZ_CHECK_RULESET=1`` to compare the names against the LIVE ruleset
instead of the literals. That needs ``gh`` and the network, so it is never
part of a routine run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
RULESET_BRANCH = "master"

#: The interpreters the project supports (the CI matrix and the trove
#: classifiers in pyproject.toml agree on these).
SUPPORTED_PYTHONS = ("3.10", "3.11", "3.12", "3.13")

#: A mirror of master's ruleset (``required_status_checks``). Re-verify it
#: against the live one with ``LZ_CHECK_RULESET=1`` whenever either side
#: changes.
RULESET_LINUX_CONTEXTS = tuple(f"unittest ({v})" for v in SUPPORTED_PYTHONS)

_BARE_PYTHON3 = re.compile(r"(?:^|[\s|;&(])python3(?=\s|$)")


def _load_job() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["unittest"]
    legs = job["strategy"]["matrix"]["include"]
    return job, legs


def _leg_name(job: dict[str, Any], leg: dict[str, Any]) -> str:
    out: str = job["name"]
    for key in ("python-version", "label"):
        out = out.replace("${{ matrix." + key + " }}", str(leg[key]))
    return out


class TestWorkflowMatrix(unittest.TestCase):
    """Legs and the check names a ruleset depends on."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.job, cls.legs = _load_job()
        cls.names = [_leg_name(cls.job, leg) for leg in cls.legs]

    def test_every_placeholder_in_the_job_name_is_consumed(self) -> None:
        for name in self.names:
            self.assertNotIn("${{", name)

    def test_leg_names_are_unique(self) -> None:
        self.assertEqual(sorted(self.names), sorted(set(self.names)))

    def test_linux_legs_are_exactly_the_required_contexts(self) -> None:
        linux = [_leg_name(self.job, leg) for leg in self.legs if leg["os"].startswith("ubuntu")]
        self.assertEqual(
            sorted(linux),
            sorted(RULESET_LINUX_CONTEXTS),
            "master's ruleset requires these four contexts by name: a renamed "
            "leg never reports and every PR to master then blocks",
        )

    def test_every_supported_python_runs_on_linux(self) -> None:
        on_linux = {leg["python-version"] for leg in self.legs if leg["os"].startswith("ubuntu")}
        self.assertEqual(on_linux, set(SUPPORTED_PYTHONS))

    def test_windows_runs_both_ends_of_the_range(self) -> None:
        on_windows = {leg["python-version"] for leg in self.legs if leg["os"].startswith("windows")}
        # INCLUDE, not equal: more Windows legs are welcome
        self.assertIn(min(SUPPORTED_PYTHONS), on_windows)
        self.assertIn(max(SUPPORTED_PYTHONS), on_windows)

    def test_linux_legs_match_the_live_ruleset(self) -> None:
        """Opt-in: ``LZ_CHECK_RULESET=1`` (needs ``gh`` and the network)."""
        if os.environ.get("LZ_CHECK_RULESET") != "1":
            self.skipTest("set LZ_CHECK_RULESET=1 to compare against the live ruleset")
        slug = subprocess.run(
            ["git", "remote", "get-url", "origin"],  # a fixed query, no shell
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", slug)
        assert match is not None, slug
        rules = subprocess.run(
            ["gh", "api", f"repos/{match.group(1)}/rules/branches/{RULESET_BRANCH}"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout

        required = next(
            rule["parameters"]["required_status_checks"]
            for rule in json.loads(rules)
            if rule["type"] == "required_status_checks"
        )
        linux = [_leg_name(self.job, leg) for leg in self.legs if leg["os"].startswith("ubuntu")]
        self.assertEqual(sorted(linux), sorted(c["context"] for c in required))


class TestWorkflowSteps(unittest.TestCase):
    """How the job's steps run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.job, _ = _load_job()
        cls.steps = cls.job["steps"]

    def test_every_step_runs_under_bash(self) -> None:
        # A Windows runner defaults to pwsh: $(...), && and [ ] all fail there
        self.assertEqual(self.job["defaults"]["run"]["shell"], "bash")
        overrides = [s["name"] for s in self.steps if s.get("shell") not in (None, "bash")]
        self.assertEqual(overrides, [])

    def test_step_names_are_unique(self) -> None:
        # a duplicated block ran pyright twice per leg (a `uses:` step is
        # identified by its action — it carries no name)
        names = [s.get("name") or s["uses"] for s in self.steps]
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_no_step_invokes_a_bare_python3(self) -> None:
        # `python3` is whichever interpreter the runner has on PATH, not
        # necessarily the project's
        offenders = []
        for step in self.steps:
            script = str(step.get("run", ""))
            for line in script.splitlines():
                code = line.split("#", 1)[0]
                if _BARE_PYTHON3.search(code):
                    offenders.append(f"{step['name']}: {line.strip()}")
        self.assertEqual(offenders, [])
