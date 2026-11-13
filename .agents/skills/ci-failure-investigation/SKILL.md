---
name: ci-failure-investigation
description: Investigate and repair failing GitHub Actions runs for LibzentSuite — inspect the real run, job, step, and logs before touching files; classify the cause (app defect, stale fixture/expectation, workflow config, environment); fix the root cause and re-verify the actual run. Use on any red X, timeout, or stuck Actions run.
---

## When this activates

A workflow failure, a red commit status, a job stuck `in_progress` past its
suite duration, or a reported Actions error on `libosterelezent/LibzentSuite`.

## Ordered steps

1. **Inspect the actual run first.** Workflow: `.github/workflows/tests.yml`
   — job `unittest (3.11)` on `ubuntu-latest`, steps: install deps →
   **pyflakes gate** → `python -m unittest discover -s tests -t . -v`,
   40-minute job timeout. No `gh` CLI on this machine: use the REST API with
   the stored git credentials — runs → jobs → step conclusions, then the job
   log. Record run ID, head SHA, failing step, exact error text.
2. **Pin the commit under test.** Compare the failing SHA against local HEAD:
   is the failure from current code, an older commit, or a force-push? Check
   run history for when the step last passed (`git log` between the green and
   red SHAs names the introducing change).
3. **Reproduce the exact CI command locally** where the platform allows:
   `python -m pyflakes main.py core adapters interfaces database benchmark
   scripts` and the discover command above. CI is Linux/3.11; local Windows
   differences (`resource` module, symlink privileges, file-lock races,
   timing floors) are environment noise — classify, don't chase.
4. **Classify before fixing.** Exactly one of:
   - *Application defect* — production code wrong → fix code, add regression.
   - *Stale fixture* — test input predates the current contract → update the
     fixture to the contract; never loosen validation to match it.
   - *Stale expectation* — the test's count/shape is behind an intentional,
     already-shipped change → update the expectation and its comment.
   - *Workflow configuration* — the YAML, paths, or gate ordering is wrong →
     minimal workflow fix; never to skip checks.
   - *Environment/infrastructure* — runner, network, platform → document
     with evidence; do not "fix" code for it.
   Precedent examples (2026-10-24/25, for calibration only — do not assume
   recurrence): pyflakes gate blocked 20+ runs on one unused import;
   channel-scope fixtures used a removed envelope (3 setup errors);
   a migration-count expectation lagged the fleet migration (5≠4);
   a job hung ~20 min after its summary on leaked non-daemon aiosqlite
   threads from an unclosed store.
5. **Fix the confirmed root cause** per its class. Prohibited: raising the
   timeout to absorb a hang, `continue-on-error`, skipping tests, weakening
   assertions, blanket bypasses. A hang is a resource-lifecycle defect —
   hand it to the `systematic-debugging` skill (threads, tasks, subprocesses,
   cleanup paths on failure, not just happy teardown).
6. **Validate in order:** targeted regression tests for the fix → the
   affected suites → the full discover suite (or state precisely what could
   not run locally and why) → pyflakes on the CI command.
7. **Commit per `COMMIT_RULESET.md`** (branch, granularity, honest messages).
8. **Recheck the real run.** Push only when authorized; then watch the new
   run via the API to `completed` and read the step conclusions. Local green
   is not CI green.

## Evidence gates

Do not edit files before step 1–4 produce: the run ID + head SHA, the failing
step and exact error, and a classification with evidence. Do not claim fixed
before step 8 shows the new run `success` (or, if push is pending, say
exactly that).

## Common mistakes

- Assuming the newest push caused a failure that older runs also have.
- Debugging a Windows-only artifact that Linux CI never sees (and vice versa).
- Reading the last log line instead of the failing step's full log.
- Fixing the test to fit a regression in production code.

## Uncertainty and handoff

If the log is unavailable (permissions, expiry) or the failure is not
reproducible from the repo, say so with what was checked, and hand resource
leaks or hangs to `systematic-debugging`; final claims go through
`verification-before-completion`.
