#!/usr/bin/env bash
# Run the full local test suite (no network, no tokens).
#
# pytest is the project's runner (unittest discover was retired with the
# pytest + xdist adoption). The interpreter is resolved portably: the exact
# locked environment when uv is available, then the project virtualenv in
# either layout (POSIX ``bin/``, Windows ``Scripts/``), then a system
# ``python3`` — never assumed, because on Windows a bare ``python3`` can be
# the Microsoft Store stub alias, which prints nothing and exits.
#
# Extra arguments go to pytest, e.g. ``scripts/run_tests.sh -n auto -k name``.
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v uv >/dev/null 2>&1; then
  exec uv run --frozen pytest tests/ "$@"
elif [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" -m pytest tests/ "$@"
elif [ -x ".venv/Scripts/python.exe" ]; then
  exec ".venv/Scripts/python.exe" -m pytest tests/ "$@"
elif command -v python3 >/dev/null 2>&1; then
  exec python3 -m pytest tests/ "$@"
fi
echo "no interpreter found: install uv (https://docs.astral.sh/uv/) or create a virtualenv" >&2
exit 2
