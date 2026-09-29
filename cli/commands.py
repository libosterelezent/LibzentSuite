"""Command routing shared by the terminal REPL and the full-screen TUI.

Every command returns plain text (possibly multiline) so any surface can
render it. 'quit' raises QuitRequested; callers decide how to exit.
"""
from __future__ import annotations

import shlex
from typing import Optional

from core.api import ControlAPI


class QuitRequested(Exception):
    """Raised by run_command for quit/exit."""


HELP = {
    "help": ("", "show this help"),
    "status": ("", "accounts + limits overview"),
    "plugins": ("", "discovered plugins and their settings"),
    "start": ("<account|all>", "start one account or all"),
    "stop": ("<account|all>", "stop one account or all"),
    "enable": ("<account> <plugin>", "enable a plugin on an account"),
    "disable": ("<account> <plugin>", "disable a plugin on an account"),
    "limits": ("", "rate limiter buckets and queues"),
    "set-limit": ("<action> <rule> [account]", "e.g. set-limit message 5/12s — or per bot: set-limit message 1/20s alpha"),
    "clear-limit": ("<account> <action>", "remove a per-bot override"),
    "env": ("", "redacted credentials view"),
    "quit": ("", "stop everything and exit"),
}


def help_text() -> str:
    """Render the HELP table as aligned text."""
    return "\n".join(
        f"  {name:<12}{sig:<28}{desc}" for name, (sig, desc) in sorted(HELP.items())
    )


async def run_command(api: ControlAPI, line: str) -> Optional[str]:
    """Execute one command line against the ControlAPI.

    Expects: raw input line (shlex-parsed here).
    Returns: text to display, or None for silent success.
    Raises: QuitRequested for quit/exit; exceptions bubble for the caller's
    error renderer (bad rules, unknown accounts...).
    """
    line = (line or "").strip()
    if not line:
        return None
    parts = shlex.split(line)
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("quit", "exit"):
        raise QuitRequested()
    if cmd == "help":
        return help_text()
    if cmd == "status":
        status = await api.status()
        return "\n".join(format_status_lines(status))
    if cmd == "plugins":
        return "\n".join(format_plugins(await api.plugins()))
    if cmd == "start":
        await api.start(None if args == ["all"] else _need(args, 1))
        return "started."
    if cmd == "stop":
        await api.stop(None if args == ["all"] else _need(args, 1))
        return "stopped."
    if cmd in ("enable", "disable"):
        account, plugin = _need(args, 2)
        await api.set_plugin_enabled(account, plugin, cmd == "enable")
        return f"{cmd}d {plugin} on {account}."
    if cmd == "limits":
        return "\n".join(format_limits(await api.limits()))
    if cmd == "set-limit":
        if len(args) == 2:
            action, rule = args
            result = await api.set_limit(action, rule, None)
        elif len(args) == 3:
            action, rule, account = args
            result = await api.set_limit(action, rule, account)
        else:
            return f"usage: set-limit {HELP['set-limit'][0]}"
        return f"rule set: {result}"
    if cmd == "clear-limit":
        account, action = _need(args, 2)
        return str(await api.clear_limit(account, action))
    if cmd == "env":
        return "\n".join(
            f"  {key}={value or '<unset>'}"
            for key, value in sorted(api.credentials_summary().items())
        )
    return f"unknown command {cmd!r} — 'help'"


# ------------------------------------------------------------ pure formatters
def format_status_lines(status: dict) -> list:
    """Status payload -> one line per account (TUI + REPL + tests)."""
    lines = [f"pending rate-limit waiters: {status['limits'].get('pending') or '-'}"]
    for account in status["accounts"]:
        plugins = ", ".join(
            f"{name}{'+' if info['enabled'] else '-'}"
            for name, info in account["plugins"].items()
        ) or "-"
        lines.append(
            f"  {account['name']:<10}{account['status']:<13}"
            f"grp={account['sync_group'] or '-':<10}[{plugins}]"
        )
    return lines


def format_plugins(manifests: list) -> list:
    """Plugin manifests -> display lines incl. settings they support."""
    lines = []
    for manifest in manifests:
        lines.append(f"  {manifest['name']} v{manifest['version']} — {manifest['description']}")
        for setting in manifest["settings"]:
            lines.append(
                f"      {setting['key']}:{setting['type']}={setting['default']!r} {setting['description']}"
            )
    return lines or ["  (no plugins discovered)"]


def format_limits(stats: dict) -> list:
    """Limiter stats -> display lines (defaults, overrides, queues)."""
    lines = []
    for action, info in stats["actions"].items():
        lines.append(
            f"  {action:<8}default={info['default_rule']:<12}"
            f"overrides={str(info['overrides'] or '-'):<24}pending={stats['pending'] or '-'}"
        )
    return lines or ["  (no rules configured)"]


def _need(args: list, count: int) -> tuple | str:
    """Validate argument count or raise a usage error."""
    if len(args) < count:
        raise UsageError("wrong number of arguments")
    return args if count > 1 else args[0]


class UsageError(ValueError):
    """Raised when a command is missing arguments."""
