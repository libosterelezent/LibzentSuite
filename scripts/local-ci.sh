#!/usr/bin/env bash
# local-ci — run the FULL CI gate locally before pushing.
#
# Usage:
#   ./scripts/local-ci.sh                 # the full gate (lint, types, export, 3.10 suite, 3.13 suite+coverage, build)
#   ./scripts/local-ci.sh quick           # lint + types + export-sync only (~1 min)
#
# line and requires the full gate (PR review 2026-11-18 — checking
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
# eight per-gate "command not found" verdicts.
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || UV_BIN="$HOME/.local/bin/uv"
if [ ! -x "$UV_BIN" ]; then
  echo "local-ci: no usable uv — looked on PATH and at $HOME/.local/bin/uv" >&2
  exit 2
fi

# install must not fall through to a hardcoded maintainer home that
# may not exist on another machine.

PROD="main.py core adapters interfaces database benchmark scripts parsers plugins"
FAIL=0
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m OK  %s\033[0m\n' "$*"; }
bad()  { printf '\033[31mFAIL %s\033[0m\n' "$*"; FAIL=1; }

# mode word keeps its historical first-position handling.
for arg in "$@"; do
done
  exit 2
fi

# The checkout lock serializes the WHOLE gate (final pre-push review):
# steps 1-4 (ruff, format, export, pyright) and the build also run
# `uv run` against the shared project .venv — locking only the suite
# legs still let two gates mutate the environment under each other's
# early steps. The lock file lives beside the persistent 3.10 venv
# (same checkout-scoped name).
VENV310="/tmp/opencode/local-ci-venv310-$(pwd | cksum | cut -d' ' -f1)"
# the lock parent must exist BEFORE the redirection opens the lock file
# (PR review 2026-11-21): /tmp/opencode is not guaranteed on other
# machines, and a failed redirection under set -e kills the gate at
# line one of the lock section
mkdir -p "$(dirname "$VENV310")"
exec 9>"$VENV310.lock"
if ! flock -n 9; then
  echo "       another gate holds this checkout — waiting for it to finish..."
  flock 9
fi

# Per-run scratch workspace: concurrent runs never share logs or the
# deliberate exception — checkout-scoped and lock-serialized, see
# step 5/8 below (PR review 2026-11-18).
RUN_DIR="$(mktemp -d /tmp/local-ci.XXXXXX)"
# Evidence survives a red gate (twice this session, the trap ate the
# ONLY copy of the review stream and suite logs mid-diagnosis): a
# failing run's artifacts are copied to the stable scratch area before
# cleanup; a green run cleans up completely.
KEEP_ON_FAIL=/tmp/opencode/local-ci-last-failed
# The detached review (own session, no lock fd) must not outlive an
# EARLY gate exit either (PR review round 10): INT/TERM, or a failure
# before the verdict block, previously left it running against a run
# dir this trap was about to delete. Reap the whole session group
# first — the verdict block's own kill is a no-op by then (the group
# is gone) — then handle the evidence.
reap_review() {
  fi
  return 0
}
cleanup_gate() {
  reap_review
  if [ "${FAIL:-0}" -eq 0 ]; then
    rm -rf "$RUN_DIR"
  else
    mkdir -p "$KEEP_ON_FAIL"
    rm -rf "$KEEP_ON_FAIL"/*
    cp -r "$RUN_DIR"/. "$KEEP_ON_FAIL"/
    rm -rf "$RUN_DIR"
  fi
}
trap cleanup_gate EXIT
trap 'cleanup_gate; trap - EXIT; exit 130' INT TERM

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

# CONCURRENTLY with the suites (they are independent checks; the
# contract says both green before push, not sequential). The review
# output lands in $RUN_DIR/review.ndjson.
    exit 2
  fi
  # setsid: the review leads its own session, so the stall path can
  # signal the whole PROCESS GROUP — killing only the subshell left the
  # an inherited lock fd let a stalled review block the NEXT gate's
  # flock until it died on its own (PR review 2026-11-21).
  setsid bash -c 'exec 9>&-; exec "$1" review --agent \
    --base-commit origin/master --fresh \
fi

step "5/8 Full suite on Python 3.10 (the leg that catches dep-split breaks)"
# PERSISTENT but CHECKOUT-SCOPED venv (PR review 2026-11-18): the
# isolated-per-run venv resolved 90 packages from scratch EVERY gate —
# minutes of pure waste — but one FIXED shared path let two checkouts
# with different lockfiles mutate the same environment under each
# other's running tests. The venv is named after THIS checkout's path;
# same-checkout gates are serialized by the whole-gate lock above, and
# --exact prunes dependencies the lock dropped (uv run's default sync
# keeps extraneous packages). The lock is frozen so the environment is
# always exactly the lock's resolution.
if UV_PROJECT_ENVIRONMENT="$VENV310" timeout 590 "$UV_BIN" run --frozen --exact --python 3.10 \
  pytest tests/ -n auto --dist loadscope \
  > "$RUN_DIR/suite-310.log" 2>&1; then
  ok "3.10 suite: $(grep -E "passed" "$RUN_DIR/suite-310.log" | tail -1)"
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
# PARALLEL coverage via pytest-cov (process speed, 2026-11-19):
# pytest-cov forwards coverage into every xdist worker and merges the
# data itself — plain `coverage run --parallel-mode` measured ONLY the
# orchestrating master (xdist workers are separate unmeasured
# processes; the report read 0.0% and failed the floor).
# CONCURRENT-GATE SAFETY (pre-push round): the data file lives under
# COVERAGE_FILE in THIS run's scratch dir — a repo-root .coverage let
# one gate clobber another's data. The environment is serialized by
# the whole-gate checkout lock (see the top of the script).
if COVERAGE_FILE="$RUN_DIR/.coverage" "$UV_BIN" run --frozen --python 3.13 pytest tests/ --cov -n auto --dist loadscope \
  > "$RUN_DIR/suite-313.log" 2>&1 \
  && COVERAGE_FILE="$RUN_DIR/.coverage" "$UV_BIN" run --frozen --python 3.13 coverage report > "$RUN_DIR/cov.log" 2>&1; then
  ok "3.13 suite: $(grep "passed" "$RUN_DIR/suite-313.log" | tail -1) — $(grep TOTAL "$RUN_DIR/cov.log")"
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
  # bounded wait (final pre-push review): a stalled review must hang
  # the verdict forever no more than a stalled suite would — 900s
  # covers the longest observed full-range review with headroom
    sleep 10
  done
    # signal the review's whole SESSION (setsid made it a group
    sleep 2
    FAIL=1
  else
  fi
    FAIL=1
  else
    # Parse the agent stream as NDJSON (PR #27 round 7 review): a
    # literal grep misses spaced JSON, an empty stream read as "0
    # findings", and a skipped review is NOT a clean one.
import json
import sys

findings = 0
status = None
unreviewed = 0
outcome = None
try:
    with open(sys.argv[1]) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "finding":
                findings += 1
            elif rec.get("type") == "complete":
                status = rec.get("status")
                unreviewed = int(rec.get("unreviewedFileCount") or 0)
                outcome = rec.get("outcome")
except OSError:
    print("missing")
    raise SystemExit(0)
if status is None:
    print("incomplete")
elif status != "review_completed":
    # review_skipped, error, failed — every non-success terminal status
    # is a failed review, never a clean one
    print("failed:%s" % status)
elif unreviewed > 0:
    # review_completed can still carry unreviewed files — that is an
    # incomplete review, not a clean zero
    print("failed:%d-unreviewed" % unreviewed)
elif outcome == "failed":
    print("failed:outcome")
else:
    print("%d ok" % findings)
PY
)"
      "0 ok")
        ;;
      skipped|missing|"incomplete"|failed:*)
        FAIL=1
        ;;
      *)
        FAIL=1
        ;;
    esac
  fi
fi
if [ "$FAIL" -eq 0 ]; then
  echo -e "\n\033[32mALL GATES GREEN — safe to push\033[0m"
else
  echo -e "\n\033[31mGATES RED — DO NOT PUSH\033[0m"
fi
exit "$FAIL"
