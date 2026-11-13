---
name: test-driven-development
description: Regression-test-first repair workflow for LibzentSuite defects. Use when fixing a bug or changing behavior that a test can express — write the failing test that demonstrates the defect, watch it fail for the intended reason, then fix.
---

## When this activates

Fixing a defect, changing persisted semantics, repairing a CI failure, or
altering any externally observable contract. Skip it only for changes no test
can meaningfully express — and then say why, in the report.

## Ordered steps

1. **Write the regression test first.** Encode the *observable contract*
   (return value, persisted row, HTTP status, process behavior), located with
   the existing suites: `tests/unit` (domain rules), `tests/integration`
   (real composition over the simulator), `tests/simulation`, `tests/stress`.
   Follow neighboring patterns: `unittest.IsolatedAsyncioTestCase`,
   `Database(":memory:")`, temp dirs, injected clocks — no wall-clock sleeps,
   no network, no real credentials (`AGENTS.md` §7).
2. **Run it and watch it fail for the intended reason.** The assertion that
   fails must be the one that encodes the defect. A collection error, import
   error, or unrelated assertion failure proves nothing.
3. **Fix minimally** at the owning boundary. No drive-by refactors.
4. **Watch it pass**, then run the focused suite and the adjacent suites that
   share the touched contract. Record commands and exit codes as you go.
5. **Run the relevant broader suite** before claiming completion (full
   `python -m unittest discover -s tests -t .` when feasible — note it takes
   ~7 minutes and is slower on Windows).

## Rules

- Never weaken, delete, or skip an existing assertion to reach green. If a
  test encodes obsolete behavior that an approved task supersedes, update the
  test *and explain why* in the commit (`AGENTS.md` §7).
- Preserve meaningful coverage: a fix that makes an old test meaningless
  means the test needs rewriting, not deleting.
- Fixture drift is a defect class here: stale fixtures and stale counts have
  produced real CI failures (channel-scope shape, migration count). When a
  fixture is stale, update it to the current contract — do not relax
  validation to match the fixture.

## Evidence gates and completion

Before the fix: one captured failing run naming the intended assertion.
After: captured passing runs for the focused test, its suite, and adjacent
suites, each with its exit code. Assertions preserved or rewritten with
reasons. Anything not run is listed as not run — never implied green.

## Reporting uncertainty

If the defect cannot be expressed as a test (pure environment, timing outside
the simulator's reach), state what was attempted, why it cannot assert the
contract, and what manual evidence substitutes.
