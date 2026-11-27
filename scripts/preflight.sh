#!/usr/bin/env bash
# preflight — fast local checks before every commit (seconds, not minutes).
# The full suite runs on the CI runner: that is the enforcement point.
# Replaces the retired local-ci.sh full gate (2026-11-27: the runner is
# the better oracle — isolated hardware, zero environmental false
# positives, faster feedback).
set -euo pipefail
cd "$(dirname "$0")/.."
PROD="main.py core adapters interfaces database benchmark scripts parsers plugins"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m OK  %s\033[0m\n' "$*"; }
bad()  { printf '\033[31mFAIL %s\033[0m\n' "$*"; exit 1; }

UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || UV_BIN="$HOME/.local/bin/uv"
[ -x "$UV_BIN" ] || { echo "no usable uv" >&2; exit 2; }

step "Ruff lint"
"$UV_BIN" run --frozen ruff check $PROD && ok "ruff check" || bad "ruff check"

step "Ruff format"
"$UV_BIN" run --frozen ruff format --check $PROD && ok "ruff format" || bad "ruff format"

step "Pyright ratchet"
PYRIGHT_SUMMARY=$("$UV_BIN" run --frozen pyright core adapters database interfaces plugins benchmark scripts parsers main.py 2>&1 | tail -1)
# strict format match: pyright's summary line is "N errors, N warnings, ..."
# (starts with a digit, then " errors, "). Rejects anything else — a
# malformed line like "version 0: errors" must not read as zero.
case "$PYRIGHT_SUMMARY" in
  [0-9]*" errors, "*)
    PYRIGHT_ERRS="${PYRIGHT_SUMMARY%% errors*}"
    ;;
  *)
    echo "pyright produced no valid diagnostic summary: $PYRIGHT_SUMMARY" >&2
    bad "pyright failed (summary does not match 'N errors, N warnings, ...')"
    ;;
esac
PYRIGHT_BASE=$(cat .github/pyright-baseline 2>/dev/null || echo 0)
echo "pyright: $PYRIGHT_ERRS (baseline: $PYRIGHT_BASE)"
[ "$PYRIGHT_ERRS" -eq "$PYRIGHT_BASE" ] && ok "pyright == baseline" || bad "pyright: $PYRIGHT_BASE -> $PYRIGHT_ERRS"

echo ""
echo "PREFLIGHT GREEN — the CI runner does the rest"
