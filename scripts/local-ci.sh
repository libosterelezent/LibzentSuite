#!/usr/bin/env bash
# local-ci — run the FULL CI gate locally before pushing.
#
# Usage:
#   ./scripts/local-ci.sh          # the full gate (lint, types, export, 3.10 suite, 3.13 suite+coverage, build)
#   ./scripts/local-ci.sh quick    # lint + types + export-sync only (~1 min)
#
# Mirrors .github/workflows/tests.yml exactly. CI must never see a
# failure this script did not see first (owner rule, 2026-11-17 —
# born from the httpx/starlette 3.10 break that local-only-on-3.13
# verification missed).
#
# The gate NEVER touches state/ (AGENTS.md §12): state/ holds the
# live databases and the credentials key. Suite residue there is
# reported, never deleted.
set -euo pipefail
cd "$(dirname "$0")/.."

# Resolve uv once: PATH first, then the official installer default
# (CodeRabbit PR #25). Failing fast with one clear message beats
# eight per-gate "command not found" verdicts.
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || UV_BIN="$HOME/.local/bin/uv"
if [ ! -x "$UV_BIN" ]; then
  echo "local-ci: no usable uv — looked on PATH and at $HOME/.local/bin/uv" >&2
  exit 2
fi

PROD="main.py core adapters interfaces database benchmark scripts parsers plugins"
FAIL=0
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m OK  %s\033[0m\n' "$*"; }
bad()  { printf '\033[31mFAIL %s\033[0m\n' "$*"; FAIL=1; }

# Per-run scratch workspace: concurrent runs never share logs, the
# export diff, or the 3.10 environment (CodeRabbit PR #25 review).
RUN_DIR="$(mktemp -d /tmp/local-ci.XXXXXX)"
trap 'rm -rf "$RUN_DIR"' EXIT

# state/ is live data. Snapshot before the suites so anything they
# leave behind is REPORTED — bootstrap places credentials.key,
# libzent.db, and plugin-data.db there; none of it is ever removed.
state_snapshot() { ls -A state 2>/dev/null | sort || true; }
STATE_BEFORE="$(state_snapshot)"
report_state_residue() {
  local fresh
  fresh="$(comm -13 <(printf '%s\n' "$STATE_BEFORE") <(printf '%s\n' "$(state_snapshot)") | grep -v '^$' || true)"
  if [ -n "$fresh" ]; then
    printf '\033[33mnote %s\033[0m\n' "suite wrote test residue into state/ (left untouched): $(echo "$fresh" | tr '\n' ' ')"
    printf '\033[33m     %s\033[0m\n' "isolation gap — tests should use disposable storage (AGENTS.md §7)"
  fi
}

step "1/8 Ruff lint (production tree)"
if "$UV_BIN" run --frozen ruff check $PROD; then ok "ruff check"; else bad "ruff check"; fi

step "2/8 Ruff format check (production tree)"
if "$UV_BIN" run --frozen ruff format --check $PROD; then ok "ruff format"; else bad "ruff format"; fi

step "3/8 requirements.txt export sync (AGENTS.md §8)"
# --locked: a stale lock FAILS here instead of being silently
# re-exported — the repair would mask exactly the drift CI rejects.
if "$UV_BIN" export --locked --format requirements-txt --no-hashes --no-dev --no-emit-project -o "$RUN_DIR/req.txt" -q \
   && diff <(tail -n +3 requirements.txt) <(tail -n +3 "$RUN_DIR/req.txt") >/dev/null; then
  ok "export in sync"
else
  bad "export drifted — regenerate: uv export ... -o requirements.txt"
fi

# One ratchet per tree. Exit status matters: 0 = clean run, 1 = errors
# emitted, anything else = the run did not complete — its (possibly
# empty) log must never count as zero errors.
mypy_ratchet() {
  local label="$1" baseline_file="$2" log status=0 errs base
  log="$RUN_DIR/mypy-$label.log"
  shift 2
  "$UV_BIN" run --frozen mypy "$@" &> "$log" || status=$?
  if [ "$status" -gt 1 ]; then
    bad "mypy $label did not complete (exit $status) — count rejected"
    tail -20 "$log"
    return
  fi
  errs=$(grep -c "error:" "$log" || true)
  base=$(<"$baseline_file")
  echo "mypy $label errors: $errs (baseline: $base)"
  if [ "$errs" -eq "$base" ]; then ok "$label == baseline"
  else
    # diagnostics BEFORE the exit trap cleans RUN_DIR — a failed gate
    # must carry its own evidence (CodeRabbit pre-review 2026-11-17).
    # `|| true`: an empty match must not kill the gate mid-verdict.
    grep "error:" "$log" | head -20 || true
    if [ "$errs" -lt "$base" ]; then bad "$label improved — lower $baseline_file to $errs in this push"
    else bad "$label grew ($base -> $errs)"; fi
  fi
}

step "4/8 Mypy ratchets — production + tests (split baselines)"
mypy_ratchet production .github/mypy-baseline main.py core adapters database interfaces plugins benchmark scripts parsers
mypy_ratchet tests .github/mypy-tests-baseline tests

if [ "${1:-}" = "quick" ]; then
  step "quick mode — suites skipped"
  [ "$FAIL" -eq 0 ] && echo -e "\n\033[32mALL QUICK GATES GREEN\033[0m" || echo -e "\n\033[31mGATES RED — DO NOT PUSH\033[0m"
  exit "$FAIL"
fi

step "5/8 Full suite on Python 3.10 (the leg that catches dep-split breaks)"
if UV_PROJECT_ENVIRONMENT="$RUN_DIR/venv310" timeout 590 "$UV_BIN" run --frozen --python 3.10 \
  python -m unittest discover -s tests -t . > "$RUN_DIR/suite-310.log" 2>&1; then
  ok "3.10 suite: $(grep 'Ran' "$RUN_DIR/suite-310.log" | head -1)"
else
  # no-match greps stay nonfatal (set -e + pipefail would otherwise
  # kill the gate before the verdict) and fall back to a bounded tail
  # — a timeout-killed run has no FAIL:/ERROR: lines at all.
  reason="$(grep -E '^(FAILED|ERROR)' "$RUN_DIR/suite-310.log" | head -2 || true)"
  [ -n "$reason" ] || reason="$(tail -15 "$RUN_DIR/suite-310.log" || true)"
  bad "3.10 suite — $reason"
  grep -B2 -A8 "ERROR:\|FAIL:" "$RUN_DIR/suite-310.log" | head -40 || true
fi
report_state_residue

step "6/8 Full suite + coverage floor on 3.13"
# --python 3.13 on both calls: with no .python-version pin, uv may
# otherwise pick any interpreter >= 3.10 and the label would lie.
if "$UV_BIN" run --frozen --python 3.13 coverage run -m unittest discover -s tests -t . > "$RUN_DIR/suite-313.log" 2>&1 \
  && "$UV_BIN" run --frozen --python 3.13 coverage report > "$RUN_DIR/cov.log" 2>&1; then
  ok "3.13 suite: $(grep 'Ran' "$RUN_DIR/suite-313.log" | head -1) — $(grep TOTAL "$RUN_DIR/cov.log")"
else
  # tests and coverage fail separately: the suite log carries
  # FAILED/ERROR, the coverage log carries the floor line (which reads
  # "Coverage failure: … fail-under" — hyphen, own line, own file).
  reason="$(grep -E '^(FAILED|ERROR)' "$RUN_DIR/suite-313.log" | head -3 || true)"
  reason="$reason$(grep 'Coverage failure' "$RUN_DIR/cov.log" || true)"
  [ -n "$reason" ] || reason="$(tail -15 "$RUN_DIR/suite-313.log")$(tail -5 "$RUN_DIR/cov.log" 2>/dev/null || true)"
  bad "3.13 suite/coverage — $reason"
  grep -B2 -A8 "ERROR:\|FAIL:" "$RUN_DIR/suite-313.log" | head -30 || true
fi
report_state_residue

step "7/8 Build (sdist + wheel)"
if timeout 120 "$UV_BIN" build > "$RUN_DIR/build.log" 2>&1; then
  ok "build: $(grep Successfully "$RUN_DIR/build.log" | head -1)"
  rm -rf dist
else
  bad "build — $(tail -5 "$RUN_DIR/build.log")"
fi

step "8/8 Verdict"
if [ "$FAIL" -eq 0 ]; then
  echo -e "\n\033[32mALL GATES GREEN — safe to push\033[0m"
else
  echo -e "\n\033[31mGATES RED — DO NOT PUSH\033[0m"
fi
exit "$FAIL"
