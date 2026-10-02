#!/usr/bin/env bash
# Run the full local simulation test suite (no network, no tokens).
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -t . -v
