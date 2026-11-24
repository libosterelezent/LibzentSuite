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

#: ``python3`` as a whole shell word: not part of a path, a version,
#: another name or a ``#``-joined word (``/usr/bin/python3``, ``python3.10``,
#: ``python3.exe``, ``a#python3``). Whatever shell syntax sits next to it does
#: not matter: ``python3<<'PY'`` (a heredoc), ``out=$(python3 x)`` and
#: ``python3;`` all invoke it.
_BARE_PYTHON3 = re.compile(r"(?<![\w./#-])python3(?![\w.])")

#: a ``#`` opens a comment only at the start of a word: at the line start,
#: or right after one of these
_COMMENT_PREFIXES = " \t;&|("


def _command_part(line: str) -> str:
    """The part of a shell line that the shell executes.

    Cut at the first ``#`` that opens a comment. A ``#`` does that only
    outside quotes and at the start of a word: in ``echo '#'; python3 x`` it
    is a quoted character and the second command stays, ``a#b`` is one word,
    and ``echo ok # python3`` loses its tail. Outside single quotes a
    backslash makes the next character literal, so ``\\"`` opens nothing and
    ``\\#`` is an ordinary character.
    """
    quote = ""
    in_word = False
    index = 0
    while index < len(line):
        char = line[index]
        if quote != "'" and char == "\\":
            index += 2
            in_word = True
            continue
        if quote:
            if char == quote:
                quote = ""
            in_word = True
        elif char in "'\"":
            quote = char
            in_word = True
        elif char == "#" and not in_word:
            return line[:index]
        else:
            in_word = char not in _COMMENT_PREFIXES
        index += 1
    return line


def _bare_python3_lines(script: str) -> list[str]:
    """The lines of `script` that invoke a bare ``python3``."""
    return [
        line.strip() for line in script.splitlines() if _BARE_PYTHON3.search(_command_part(line))
    ]


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
        offenders = [
            f"{step['name']}: {line}"
            for step in self.steps
            for line in _bare_python3_lines(str(step.get("run", "")))
        ]
        self.assertEqual(offenders, [])

    def test_a_hash_inside_quotes_is_not_a_comment(self) -> None:
        # A plain split at the first `#` saw `echo '` here and let the
        # command through
        self.assertEqual(
            _bare_python3_lines("echo '#'; python3 -m pytest"),
            ["echo '#'; python3 -m pytest"],
        )
        self.assertEqual(
            _bare_python3_lines('echo "#"; python3 x.py'),
            ['echo "#"; python3 x.py'],
        )
        # a real comment is skipped, with or without a space before the hash
        self.assertEqual(_bare_python3_lines("echo ok # python3 -m pytest"), [])
        self.assertEqual(_bare_python3_lines("echo ok #python3"), [])
        # a hash that does not start a word is part of the word
        self.assertEqual(_bare_python3_lines("echo a#python3"), [])

    def test_an_escaped_quote_opens_nothing(self) -> None:
        # Outside single quotes a backslash makes the next character
        # literal. The scanner used to open a string at the escaped quote
        # and then took the hash for a comment, missing the command.
        self.assertEqual(
            _bare_python3_lines('echo \\" " # "; python3 -m pytest'),
            ['echo \\" " # "; python3 -m pytest'],
        )
        self.assertEqual(
            _bare_python3_lines("echo \\' ' # '; python3 x.py"),
            ["echo \\' ' # '; python3 x.py"],
        )
        # an escaped hash is a literal character, not a comment
        self.assertEqual(
            _bare_python3_lines("echo \\# ; python3 x.py"), ["echo \\# ; python3 x.py"]
        )
        # an escaped quote INSIDE a string does not end it, so the hash
        # after the string is a real comment
        self.assertEqual(_bare_python3_lines('echo "a\\"b" # python3 x.py'), [])
        # inside single quotes a backslash is literal and escapes nothing
        self.assertEqual(_bare_python3_lines("echo '\\' # python3 x.py"), [])

    def test_the_bare_python3_probe_catches_every_shell_form(self) -> None:
        # The probe guards the workflow, and the workflow holds no offender
        # for it to catch, so nothing else would notice a probe that had gone
        # blind. Pin what it must catch and what it must spare.
        invokes = (
            "python3 -m pytest",
            "  python3 x.py",
            "python3<<'PY'",
            "python3 < script.py",
            "uv run ruff check && python3 x.py",
            "uv run ruff check || python3 x.py",
            "python3 a.py; python3 b.py",
            "out=$(python3 x.py)",
            "out=`python3 x.py`",
            "out=$(python3 x.py | tail -1)",
            "! python3 x.py",
            "{ python3 x.py; }",
            "( cd sub && python3 x.py )",
            "if true; then python3 x.py; fi",
        )
        spares = (
            "/usr/bin/python3 x.py",
            "python3.10 x.py",
            "python3.exe x.py",
            "uv run --python 3.13 python -m pytest",
            "pyenv install 3.13.0",
            "echo a#python3",
        )
        for line in invokes:
            with self.subTest(line=line):
                self.assertRegex(line, _BARE_PYTHON3)
        for line in spares:
            with self.subTest(line=line):
                self.assertNotRegex(line, _BARE_PYTHON3)
