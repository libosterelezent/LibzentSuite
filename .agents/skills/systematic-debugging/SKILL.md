---
name: Systematic Debugging
description: Reproduce-then-fix workflow for any bug, test failure, hang, or unexpected behavior in LibzentSuite. Use BEFORE editing code whenever an error, failed assertion, wrong outcome, or stall appears. Enforces a tested root-cause hypothesis over symptom patching.
---

## When this activates

Any defect investigation: a failing test, an unexpected API/UI outcome, a hang
or leaked resource, a wrong persisted value, an exception in logs. Use it
before the first fix attempt — not after blind fixes have failed.

## Ordered steps

1. **Reproduce.** Turn the report into a deterministic command that fails
   (focused `python -m unittest tests.<module> -v`, a curl against the
   dashboard, a CLI one-shot). If it will not reproduce, record what differs
   (environment, data, timing) — that difference is itself evidence.
2. **Read the complete evidence.** Full traceback, complete log region (not
   just the last line), and the failing code plus its callers and tests.
   A truncated log invites a wrong cause.
3. **Trace the data flow** across module boundaries. `INTEGRATION.md` §3
   owns the module map: find the owner of the misbehaving state before
   changing anything. Do not fix a symptom in a consumer when the owner
   contract is the defect.
4. **State a hypothesis** with a predicted observation: "X holds a stale
   reference; if true, <cheap probe> will show Y."
5. **Test the hypothesis** with the cheapest instrument — a log line, a REPL
   query against the database, a focused breakpoint run. Do this before
   writing the fix; the probe result confirms or kills the hypothesis.
6. **Fix the root cause** — the smallest change at the owning boundary,
   preserving architecture contracts (`ARCHITECTURE.md` invariants).
7. **Prove it.** Write the regression test first where practical (see the
   `test-driven-development` skill), then verify exit codes and process
   behavior (see the `verification-before-completion` skill).

## Prohibited

- Speculative patches: changing code without a hypothesis or probe evidence.
- Repeated blind variation of the same fix attempt.
- Treating a symptom (silencing a log, widening an except, loosening an
  assertion) as the fix. A block must fail with its human-readable reason.
- Bypassing validation, pacing, persistence, or task-scheduling semantics to
  make a failure disappear.

## Common mistakes

- Fixing in the UI what belongs in the backend (or vice versa) — CLI and
  dashboard share `core.api.ControlAPI`; a defect there is fixed once.
- Stopping at the first plausible cause. One confirmed mechanism beats three
  guessed ones.
- Missing cleanup paths: investigate what happens when setup fails midway,
  not only the happy teardown (leaks hide there — see the plugin-data store
  shutdown leak fixed 2026-10-25, `eb3871d`).

## Evidence gates and completion

Do not start step 6 without: a reproduction, the full error evidence, and a
hypothesis whose probe observation came true. Do not declare done without:
a regression test (or a stated reason none is practical), the focused suite
green with a recorded exit code, and the relevant broader suite run.

## Reporting uncertainty

If the hypothesis fails or evidence is insufficient, stop and report: what
was reproduced, what was ruled out (with evidence), the current leading
theory, and what access or data is missing. Say "unverified" explicitly.
