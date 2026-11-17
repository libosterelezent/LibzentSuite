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
set -euo pipefail
cd "$(dirname "$0")/.."

PROD="main.py core adapters interfaces database benchmark scripts parsers plugins"
FAIL=0
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m OK  %s\033[0m\n' "$*"; }
bad()  { printf '\033[31mFAIL %s\033[0m\n' "$*"; FAIL=1; }

step "1/8 Ruff lint (production tree)"
if ~/.local/bin/uv run --frozen ruff check $PROD; then ok "ruff check"; else bad "ruff check"; fi

step "2/8 Ruff format check (production tree)"
if ~/.local/bin/uv run --frozen ruff format --check $PROD; then ok "ruff format"; else bad "ruff format"; fi

step "3/8 requirements.txt export sync (AGENTS.md §8)"
if ~/.local/bin/uv export --format requirements-txt --no-hashes --no-dev --no-emit-project -o /tmp/local-ci-req.txt -q \
   && diff <(tail -n +3 requirements.txt) <(tail -n +3 /tmp/local-ci-req.txt) >/dev/null; then
  ok "export in sync"
else
  bad "export drifted — regenerate: uv export ... -o requirements.txt"
fi

step "4/8 Mypy ratchet (errors must equal the baseline exactly)"
MYPY_ERRS=$(~/.local/bin/uv run --frozen mypy &> /tmp/local-ci-mypy.log || true \
  && grep -c "error:" /tmp/local-ci-mypy.log || true)
BASELINE=$(<.github/mypy-baseline)
echo "mypy errors: $MYPY_ERRS (baseline: $BASELINE)"
if [ "$MYPY_ERRS" -eq "$BASELINE" ]; then ok "mypy == baseline"
elif [ "$MYPY_ERRS" -lt "$BASELINE" ]; then bad "mypy improved — lower .github/mypy-baseline to $MYPY_ERRS in this push"
else bad "mypy grew ($BASELINE -> $MYPY_ERRS)"; fi

if [ "${1:-}" = "quick" ]; then
  step "quick mode — suites skipped"
  [ "$FAIL" -eq 0 ] && echo -e "\n\033[32mALL QUICK GATES GREEN\033[0m" || echo -e "\n\033[31mGATES RED — DO NOT PUSH\033[0m"
  exit "$FAIL"
fi

step "5/8 Full suite on Python 3.10 (the leg that catches dep-split breaks)"
rm -rf state
if UV_PROJECT_ENVIRONMENT=/tmp/local-ci-venv310 timeout 590 ~/.local/bin/uv run --frozen --python 3.10 \
  python -m unittest discover -s tests -t . > /tmp/local-ci-310.log 2>&1; then
  ok "3.10 suite: $(grep 'Ran' /tmp/local-ci-310.log | head -1)"
else
  bad "3.10 suite — $(grep -E '^(FAILED|ERROR)' /tmp/local-ci-310.log | head -2)"
  grep -B2 -A8 "ERROR:\|FAIL:" /tmp/local-ci-310.log | head -40
fi

step "6/8 Full suite + coverage floor on 3.13"
rm -rf state
if ~/.local/bin/uv run --frozen coverage run -m unittest discover -s tests -t . > /tmp/local-ci-313.log 2>&1 \
  && ~/.local/bin/uv run --frozen coverage report > /tmp/local-ci-cov.log 2>&1; then
  ok "3.13 suite: $(grep 'Ran' /tmp/local-ci-313.log | head -1) — $(grep TOTAL /tmp/local-ci-cov.log)"
else
  bad "3.13 suite/coverage — $(grep -E '^(FAILED|ERROR)|fail_under' /tmp/local-ci-313.log /tmp/local-ci-cov.log | head -3)"
  grep -B2 -A8 "ERROR:\|FAIL:" /tmp/local-ci-313.log | head -30
fi

step "7/8 Build (sdist + wheel)"
if timeout 120 ~/.local/bin/uv build > /tmp/local-ci-build.log 2>&1; then
  ok "build: $(grep Successfully /tmp/local-ci-build.log | head -1)"
  rm -rf dist
else
  bad "build — $(tail -5 /tmp/local-ci-build.log)"
fi

step "8/8 Verdict"
if [ "$FAIL" -eq 0 ]; then
  echo -e "\n\033[32mALL GATES GREEN — safe to push\033[0m"
else
  echo -e "\n\033[31mGATES RED — DO NOT PUSH\033[0m"
fi
exit "$FAIL"
