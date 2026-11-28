"""User-facing interface packages: `cli` (REPL + one-shot) and `dashboard`.

Interfaces are thin surfaces over `core.api.ControlAPI` ONLY — parity between
the CLI and the dashboard is guaranteed because both call the exact same
facade methods (spec: "One Core API" design goal).
"""
