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

# Per-run scratch (kept on failure for diagnostics: the path is printed)
RUN_DIR="$(mktemp -d /tmp/local-ci.XXXXXX)"
KEEP_ON_FAIL=0
cleanup() {
  if [ "$FAIL" -eq 0 ] || [ "$KEEP_ON_FAIL" -eq 1 ]; then
    rm -rf "$RUN_DIR"  # covers VENV310 (inside the scratch dir)
  else
    echo "diagnostics preserved: $RUN_DIR"
  fi
}
trap cleanup EXIT
# 0/7 (silent): self-install the pre-push hook -- the machine-checkable
# half of the push protocol (AGENTS.md 10). Idempotent, works across
# worktrees (git-path resolves this checkout's hooks dir).
HOOKS_DIR="$(git rev-parse --git-path hooks)"
cp scripts/hooks/pre-push "$HOOKS_DIR/pre-push" 2>/dev/null \
  && chmod +x "$HOOKS_DIR/pre-push" 2>/dev/null \
  || echo "pre-push hook not installed this run"

step "1/6 Ruff lint"
"$UV_BIN" run --frozen ruff check $PROD && ok "ruff check" || bad "ruff check"

step "2/6 Ruff format"
"$UV_BIN" run --frozen ruff format --check $PROD && ok "ruff format" || bad "ruff format"

step "3/6 Export sync (errors recorded, remaining checks continue)"
EXPORT_ERR=0
"$UV_BIN" export --locked --format requirements-txt --no-hashes --no-dev --no-emit-project -o "$RUN_DIR/req.txt" -q 2>"$RUN_DIR/export.err" || EXPORT_ERR=1
if [ "$EXPORT_ERR" -eq 0 ] && diff <(tail -n +3 requirements.txt) <(tail -n +3 "$RUN_DIR/req.txt") >/dev/null 2>&1; then
  ok "export in sync"
else
  bad "export drifted or failed: $(cat "$RUN_DIR/export.err" 2>/dev/null | head -1)"
fi

step "4/6 Pyright ratchet"
"$UV_BIN" run --frozen pyright --outputjson core adapters database interfaces plugins benchmark scripts parsers main.py > "$RUN_DIR/pyright.json" 2>/dev/null || true
# the count is parsed by the SAME locked interpreter that just ran pyright,
# and the report reaches it on STDIN: a bare `python3` is whatever the system
# has (on Windows often the Microsoft Store stub alias, which prints nothing
# and exits), and a /tmp path embedded in a Python string literal crosses
# into a native Windows interpreter unconverted
PYRIGHT_ERRS=$("$UV_BIN" run --frozen python -c "import json, sys; print(json.load(sys.stdin)['summary']['errorCount'])" < "$RUN_DIR/pyright.json" 2>/dev/null || echo "?")
PYRIGHT_BASE=$(cat .github/pyright-baseline)
echo "pyright: $PYRIGHT_ERRS (baseline: $PYRIGHT_BASE)"
[ "$PYRIGHT_ERRS" -eq "$PYRIGHT_BASE" ] && ok "pyright == baseline" || bad "pyright: $PYRIGHT_BASE -> $PYRIGHT_ERRS"

if [ "${1:-}" = "quick" ]; then
  echo ""
  [ "$FAIL" -eq 0 ] && echo "ALL QUICK GATES GREEN" || echo "GATES RED"
  exit "$FAIL"
fi

# A hung leg must not hang the gate forever, so a timed leg gets a ceiling
# with roughly 3x headroom over its slowest HONEST run — and the honest run
# time is the platform's. A full leg takes ~2m45s-3m15s on the Linux gate
# box and ~10-14 minutes under Git Bash on Windows (measured 2026-11-23).
# One Linux-calibrated 590s cut the Windows leg off at exactly 590s, before
# pytest could print a summary; the workers then died with their controller
# ("node down: Not properly terminated"), which reads like a crash and is
# only the kill. LZ_GATE_LEG_TIMEOUT overrides the ceiling.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) LEG_TIMEOUT_S=1800 ;;
  *) LEG_TIMEOUT_S=590 ;;
esac
LEG_TIMEOUT_S="${LZ_GATE_LEG_TIMEOUT:-$LEG_TIMEOUT_S}"

step "5/6 Suite + coverage on 3.10"
# per-checkout venv (unique by full path hash + lock hash), synced to drop stale packages
VENV_KEY="$(echo "$(pwd)_$(sha256sum uv.lock | cut -c1-8)" | sha256sum | cut -c1-12)"
VENV310="$RUN_DIR/venv310"  # inside the per-run scratch: no external parent dir, unique per run, trap-cleaned
# ONE suite leg on the ONE gated interpreter: CI runs 3.10 (the owner's
# platform) and this gate proves the same. The 3.13 leg was retired
# 2026-11-25 as duplicated effort CI never ran (~10 minutes per gate
# run). Coverage is measured HERE (the only suite leg), so the floor
# in pyproject [tool.coverage.report] stays enforced on every full run.
UV_PROJECT_ENVIRONMENT="$VENV310" COVERAGE_FILE="$RUN_DIR/.coverage" timeout "$LEG_TIMEOUT_S" "$UV_BIN" run --frozen --python 3.10 pytest tests/ -n auto --dist loadscope --cov=core --cov=adapters --cov=database --cov=interfaces --cov=plugins --cov=benchmark --cov=scripts --cov=parsers --cov-report=term-missing --cov-report="json:$RUN_DIR/cov.json" > "$RUN_DIR/suite-310.log" 2>&1 && ok "3.10: $(grep 'passed' "$RUN_DIR/suite-310.log" | tail -1) — $(grep TOTAL "$RUN_DIR/suite-310.log")" || { rc=$?; bad "3.10 failed"; if [ "$rc" -eq 124 ]; then echo "       the leg hit its ${LEG_TIMEOUT_S}s ceiling before pytest could print a summary (raise LZ_GATE_LEG_TIMEOUT or quiet the machine)"; fi; grep 'FAILED' "$RUN_DIR/suite-310.log" | head -3; grep 'Coverage failure' "$RUN_DIR/suite-310.log" | head -1; }

step "6/6 Build (into the run's scratch dir)"
CHECKOUT_DIR="$(pwd)"  # before the cd: $PWD/$(pwd) expand post-cd otherwise
if (cd "$RUN_DIR" && timeout 120 "$UV_BIN" build --out-dir "$RUN_DIR/dist" "$CHECKOUT_DIR" > "$RUN_DIR/build.log" 2>&1); then
  ok "build: $(grep Successfully "$RUN_DIR/build.log" | head -1)"
else
  # uv build may not support --project; fall back to repo root but clean dist
  if timeout 120 "$UV_BIN" build > "$RUN_DIR/build.log" 2>&1; then
    ok "build"
    rm -rf dist
  else
    bad "build: $(tail -3 "$RUN_DIR/build.log")"
  fi
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "ALL GATES GREEN — safe to push"
  # The stamp is the INDEX tree (git write-tree): the exact tree this
  # gate just validated. The pre-push hook compares HEAD's tree against
  # it and refuses the push on any mismatch -- stage before gating,
  # commit after, never edit in between. Quick mode (no suites) never
  # stamps: only the full path does.
  if TREE="$(git write-tree 2>/dev/null)"; then
    printf '%s\n' "$TREE" > "$(git rev-parse --git-path gate-stamp)" || true
  else
    echo "gate-stamp not written -- pre-push will refuse pushes until it is"
  fi
else
  echo "GATES RED — DO NOT PUSH"
fi
exit "$FAIL"
