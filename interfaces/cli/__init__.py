"""CLI interface package: shared command engine + REPL/one-shot surface.

Both the REPL and one-shot mode route through :func:`run_command`, which calls
ONLY ``core.api.ControlAPI`` methods — the same facade the dashboard uses, so
the two interfaces can never drift apart in capability.
"""
from .commands import (
    HELP,
    QuitRequested,
    UsageError,
    format_account_info,
    format_account_plugins,
    format_accounts,
    format_audit,
    format_effective,
    format_history,
    format_logs,
    format_plugin_state,
    format_plugins,
    format_policies,
    format_profiles,
    format_simulation_report,
    format_status,
    format_tasks,
    format_warnings,
    help_text,
    run_command,
)
from .repl import Repl, run_once

__all__ = [
    "Repl",
    "run_command",
    "run_once",
    "HELP",
    "help_text",
    "QuitRequested",
    "UsageError",
]
