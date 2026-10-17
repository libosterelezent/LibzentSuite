#!/usr/bin/env bash
# Run the full local simulation test suite (no network, no tokens).
set -euo pipefail
cd "$(dirname "$0")/.."
# Prefer the project virtualenv when present; fall back to system python3.
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi
exec "$PY" -m unittest discover -s tests -t . -v
