#!/usr/bin/env bash
# local-ci — run the FULL CI gate locally before pushing.
#
# Usage:
#   ./scripts/local-ci.sh                 # the full gate (lint, types, export, 3.10 suite, 3.13 suite+coverage, build)
#   ./scripts/local-ci.sh quick           # lint + types + export-sync only (~1 min)
#   ./scripts/local-ci.sh --review        # full gate + CodeRabbit review running concurrent with the suites
#
# --review is an OPTION, not a mode: it parses anywhere on the command
# line and requires the full gate (PR review 2026-11-18 — checking
# only $2 let "./local-ci.sh --review" skip the review while the
# verdict still said "safe to push").
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

# --review anywhere on the command line (PR review 2026-11-18); the
# mode word keeps its historical first-position handling.
REVIEW_FLAG=0
for arg in "$@"; do
  if [ "$arg" = "--review" ]; then REVIEW_FLAG=1; fi
done
if [ "$REVIEW_FLAG" -eq 1 ] && [ "${1:-}" = "quick" ]; then
  echo "local-ci: --review needs the full gate (quick mode has no suites to review against)" >&2
  exit 2
fi

# Per-run scratch workspace: concurrent runs never share logs or the
# export diff (CodeRabbit PR #25 review). The 3.10 venv is the one
# deliberate exception — checkout-scoped and lock-serialized, see
# step 5/8 below (PR review 2026-11-18).
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
step "4/8 Pyright ratchet (errors must equal the baseline exactly)"
"$UV_BIN" run --frozen pyright --outputjson \
  core adapters database interfaces plugins benchmark scripts parsers main.py \
  > "$RUN_DIR/pyright.json" 2>/dev/null || true  # pyright exits 1 on findings
PYRIGHT_ERRS=$(python3 -c "import json; print(json.load(open('$RUN_DIR/pyright.json'))['summary']['errorCount'])" 2>/dev/null || echo "?")
PYRIGHT_BASE=$(<.github/pyright-baseline)
echo "pyright errors: $PYRIGHT_ERRS (baseline: $PYRIGHT_BASE)"
if [ "$PYRIGHT_ERRS" -eq "$PYRIGHT_BASE" ]; then ok "pyright == baseline"
elif [ "$PYRIGHT_ERRS" -lt "$PYRIGHT_BASE" ]; then bad "pyright improved — lower .github/pyright-baseline to $PYRIGHT_ERRS in this push"
else bad "pyright grew ($PYRIGHT_BASE -> $PYRIGHT_ERRS)"; fi

if [ "${1:-}" = "quick" ]; then
  step "quick mode — suites skipped"
  [ "$FAIL" -eq 0 ] && echo -e "\n\033[32mALL QUICK GATES GREEN\033[0m" || echo -e "\n\033[31mGATES RED — DO NOT PUSH\033[0m"
  exit "$FAIL"
fi

# full mode supports a --review flag: the CodeRabbit review runs
# CONCURRENTLY with the suites (they are independent checks; the
# contract says both green before push, not sequential). The review
# output lands in $RUN_DIR/review.ndjson.
REVIEW_PID=""
if [ "$REVIEW_FLAG" -eq 1 ]; then
  step "0/8 CodeRabbit review (concurrent with the suites)"
  ("/home/libosterelezent/.local/bin/coderabbit" review --agent \
    --base-commit origin/master --fresh \
    > "$RUN_DIR/review.ndjson" 2>&1) &
  REVIEW_PID=$!
  echo "       review running as PID $REVIEW_PID"
fi

step "5/8 Full suite on Python 3.10 (the leg that catches dep-split breaks)"
# PERSISTENT but CHECKOUT-SCOPED venv (PR review 2026-11-18): the
# isolated-per-run venv resolved 90 packages from scratch EVERY gate —
# minutes of pure waste — but one FIXED shared path let two checkouts
# with different lockfiles mutate the same environment under each
# other's running tests. The venv is named after THIS checkout's path,
# and an flock held through the 3.10 leg serializes same-checkout
# gates; different checkouts never contend. The lock is frozen so the
# environment is always exactly the lock's resolution.
VENV310="/tmp/opencode/local-ci-venv310-$(pwd | cksum | cut -d' ' -f1)"
exec 9>"$VENV310.lock"
if ! flock -n 9; then
  echo "       another gate holds this checkout's 3.10 venv — waiting for it to finish..."
  flock 9
fi
if UV_PROJECT_ENVIRONMENT="$VENV310" timeout 590 "$UV_BIN" run --frozen --python 3.10 \
  pytest tests/ -q --ignore=./tests/unit/test_rich_model.py -n auto --dist loadscope \
  > "$RUN_DIR/suite-310.log" 2>&1 \
  && UV_PROJECT_ENVIRONMENT="$VENV310" timeout 590 "$UV_BIN" run --frozen --python 3.10 \
  python -m unittest tests.unit.test_rich_model >> "$RUN_DIR/suite-310.log" 2>&1; then
  ok "3.10 suite: $(grep -E "passed|Ran" "$RUN_DIR/suite-310.log" | tail -2 | head -1)"
else
  # no-match greps stay nonfatal (set -e + pipefail would otherwise
  # kill the gate before the verdict) and fall back to a bounded tail
  # — a timeout-killed run has no FAIL:/ERROR: lines at all.
  reason="$(grep -E '^(FAILED|ERROR)' "$RUN_DIR/suite-310.log" | head -2 || true)"
  [ -n "$reason" ] || reason="$(tail -15 "$RUN_DIR/suite-310.log" || true)"
  bad "3.10 suite — $reason"
  grep -B2 -A8 "ERROR:\|FAIL:" "$RUN_DIR/suite-310.log" | head -40 || true
fi
exec 9>&-  # release the checkout's 3.10 venv — the 3.13 leg uses the project .venv
report_state_residue

step "6/8 Full suite + coverage floor on 3.13"
# --python 3.13 on both calls: with no .python-version pin, uv may
# otherwise pick any interpreter >= 3.10 and the label would lie.
if "$UV_BIN" run --frozen --python 3.13 coverage run -m pytest tests/ -q \
  --ignore=./tests/unit/test_rich_model.py -n0 \
  > "$RUN_DIR/suite-313.log" 2>&1 \
  && "$UV_BIN" run --frozen --python 3.13 coverage run -m pytest tests/unit/test_rich_model.py -q --append -n0 \
  >> "$RUN_DIR/suite-313.log" 2>&1 \
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
if [ -n "$REVIEW_PID" ]; then
  echo "       waiting for the concurrent review (PID $REVIEW_PID)..."
  REVIEW_EXIT=0
  wait "$REVIEW_PID" || REVIEW_EXIT=$?
  if [ "$REVIEW_EXIT" -ne 0 ]; then
    echo -e "\033[31mREVIEW: command failed (exit $REVIEW_EXIT)\033[0m"
    FAIL=1
  elif grep -q '"type":"finding"' "$RUN_DIR/review.ndjson" 2>/dev/null; then
    FINDINGS=$(grep -c '"type":"finding"' "$RUN_DIR/review.ndjson")
    echo -e "\033[31mREVIEW: $FINDINGS finding(s) — see $RUN_DIR/review.ndjson\033[0m"
    FAIL=1
  else
    echo -e "\033[32mREVIEW: 0 findings\033[0m"
  fi
fi
if [ "$FAIL" -eq 0 ]; then
  echo -e "\n\033[32mALL GATES GREEN — safe to push\033[0m"
else
  echo -e "\n\033[31mGATES RED — DO NOT PUSH\033[0m"
fi
exit "$FAIL"
