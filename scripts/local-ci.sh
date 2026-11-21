#!/usr/bin/env bash
# local-ci — the code-quality gate. Run before every push.
set -euo pipefail
cd "$(dirname "$0")/.."
PROD="main.py core adapters interfaces database benchmark scripts parsers plugins"
FAIL=0
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m OK  %s\033[0m\n' "$*"; }
bad()  { printf '\033[31mFAIL %s\033[0m\n' "$*"; FAIL=1; }
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || UV_BIN="$HOME/.local/bin/uv"
[ -x "$UV_BIN" ] || { echo "no usable uv" >&2; exit 2; }
RUN_DIR="$(mktemp -d /tmp/local-ci.XXXXXX)"
trap 'rm -rf "$RUN_DIR"' EXIT
step "1/7 Ruff lint"
"$UV_BIN" run --frozen ruff check $PROD && ok "ruff check" || bad "ruff check"
step "2/7 Ruff format"
"$UV_BIN" run --frozen ruff format --check $PROD && ok "ruff format" || bad "ruff format"
step "3/7 Export sync"
"$UV_BIN" export --locked --format requirements-txt --no-hashes --no-dev --no-emit-project -o "$RUN_DIR/req.txt" -q
diff <(tail -n +3 requirements.txt) <(tail -n +3 "$RUN_DIR/req.txt") >/dev/null 2>&1 && ok "export in sync" || bad "export drifted"
step "4/7 Pyright ratchet"
"$UV_BIN" run --frozen pyright --outputjson core adapters database interfaces plugins benchmark scripts parsers main.py > "$RUN_DIR/pyright.json" 2>/dev/null || true
PYRIGHT_ERRS=$(python3 -c "import json; print(json.load(open('$RUN_DIR/pyright.json'))['summary']['errorCount'])" 2>/dev/null || echo "?")
PYRIGHT_BASE=$(cat .github/pyright-baseline)
echo "pyright: $PYRIGHT_ERRS (baseline: $PYRIGHT_BASE)"
[ "$PYRIGHT_ERRS" -eq "$PYRIGHT_BASE" ] && ok "pyright == baseline" || bad "pyright: $PYRIGHT_BASE -> $PYRIGHT_ERRS"
if [ "${1:-}" = "quick" ]; then
  echo ""
  [ "$FAIL" -eq 0 ] && echo "ALL QUICK GATES GREEN" || echo "GATES RED"
  exit "$FAIL"
fi
step "5/7 Suite on 3.10"
VENV310="/tmp/opencode/local-ci-310-$(basename "$(pwd)")"
UV_PROJECT_ENVIRONMENT="$VENV310" timeout 590 "$UV_BIN" run --frozen --python 3.10 pytest tests/ -q -n auto --dist loadscope > "$RUN_DIR/suite-310.log" 2>&1 && ok "3.10: $(grep 'passed' "$RUN_DIR/suite-310.log" | tail -1)" || { bad "3.10 failed"; grep 'FAILED' "$RUN_DIR/suite-310.log" | head -3; }
step "6/7 Suite + coverage on 3.13"
"$UV_BIN" run --frozen --python 3.13 pytest tests/ -q -n auto --dist loadscope --cov=core --cov=adapters --cov=database --cov=interfaces --cov=plugins --cov=benchmark --cov=scripts --cov=parsers --cov-report=term-missing > "$RUN_DIR/suite-313.log" 2>&1 && ok "3.13: $(grep 'passed' "$RUN_DIR/suite-313.log" | tail -1) — $(grep TOTAL "$RUN_DIR/suite-313.log")" || { bad "3.13 failed"; grep 'FAILED' "$RUN_DIR/suite-313.log" | head -3; grep 'Coverage failure' "$RUN_DIR/suite-313.log" | head -1; }
step "7/7 Build"
timeout 120 "$UV_BIN" build > "$RUN_DIR/build.log" 2>&1 && { ok "build"; rm -rf dist; } || bad "build: $(tail -3 "$RUN_DIR/build.log")"
echo ""
[ "$FAIL" -eq 0 ] && echo "ALL GATES GREEN — safe to push" || echo "GATES RED — DO NOT PUSH"
exit "$FAIL"
