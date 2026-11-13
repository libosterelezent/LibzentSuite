---
name: verification-before-completion
description: Fresh-evidence gate for every claimed result — run commands, capture exit codes, verify process and job status. Use before reporting tests green, CI fixed, work committed, or any task complete. A summary line is not a finished process.
---

## When this activates

Before stating ANY completion claim: "tests pass", "CI is green", "committed",
"the hang is fixed", "the migration applies cleanly". Run it as the last step
of every task, and again after any change made afterwards.

## Ordered steps

1. **List the claims.** Every sentence that asserts an outcome is a claim.
2. **Produce fresh evidence per claim**, from the current state of the tree:
   the exact command, its exit code, and when it ran. Stale evidence (an
   earlier run, a different branch, a since-edited file) does not count.
3. **Classify every check** honestly: passed / failed / skipped / not run /
   in progress / could not run (say why — missing tool, missing credentials,
   unsupported platform).
4. **Verify beyond the summary line.** A test summary proves tests finished,
   not that the process exited; a green step proves the command exited, not
   that the workflow completed. Check the process exit code and, for CI, the
   actual run conclusion in the GitHub API/Actions UI.
5. **Re-verify after any post-hoc change.** Edited a file, re-ran a formatter,
   committed? The prior evidence is void; re-run what the change touches.
6. **Report with the labels.** A table of claims → evidence → status, with
   failures and non-runs visible. Never imply a check succeeded.

## Repo-informed specifics

- The full suite is `python -m unittest discover -s tests -t .` (~7 min on
  CI/Linux; slower on Windows). CI's job is `unittest (3.11)` in
  `.github/workflows/tests.yml`; its pyflakes step gates the test step.
- Local Windows caveats, verified 2026-10-24/25: the `benchmark` tests need
  the Unix `resource` module; symlink tests need privilege; a few timing
  tests are load-sensitive. These fail on Windows even when CI is green —
  report them as platform-specific, with CI as the deciding evidence.
- Interpreting CI without the `gh` CLI: use the REST API
  (`api.github.com/repos/libosterelezent/LibzentSuite/actions/...`) with the
  stored git credentials; do not guess run status from pushes alone.

## Worked example (the discipline, not a template)

2026-10-24, run 40121167928: the log ended with `Ran 2350 tests … FAILED`
yet the job stayed `in_progress` — a post-summary hang (leaked non-daemon
aiosqlite threads). Step 4's "verify the process, not the summary" is what
separates "tests finished" from "the run completed".

## Common mistakes

- Quoting a test count from memory instead of re-running.
- Declaring CI fixed because the local command exited 0.
- Hiding a red suite behind "unrelated" without classifying it with evidence.
- Treating "should pass" or "looks correct" as a check.

## Completion criteria and uncertainty

Done = every claim has current, labeled evidence, and the unproven ones are
listed as open with what is missing (a push authorization, a credential, a
Linux environment). State those limits in the final report, plainly.
