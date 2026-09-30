"""Command routing shared by the terminal REPL, the TUI, and tests.

Every feature with settings is reachable from here AND from the dashboard —
both surfaces call the same ControlAPI methods underneath.
Commands:
  help status plugins accounts add-account remove-account details
  start stop enable disable settings set
  limits set-limit clear-limit set-delay clear-delay warnings
  roles role-add role-del assign unassign
  audit env quit
"""
from __future__ import annotations

import shlex
from typing import List, Optional

from core.api import ControlAPI


class QuitRequested(Exception):
    """Raised by run_command for quit/exit."""


class UsageError(ValueError):
    """Raised when a command is missing arguments."""


HELP = {
    "help": ("", "show this help"),
    "status": ("", "accounts + limits + warnings overview"),
    "plugins": ("", "discovered plugins and their settings"),
    "accounts": ("", "registered accounts with their ids/roles"),
    "add-account": ("<name> [sync=<group>] [role ...]", "register + persist an account"),
    "remove-account": ("<name>", "remove a stopped account"),
    "details": ("<account>", "deep view incl. cached discord data"),
    "start": ("<account|all>", "start one account or all"),
    "stop": ("<account|all>", "stop one account or all"),
    "enable": ("<account> <plugin>", "enable a plugin on an account"),
    "disable": ("<account> <plugin>", "disable a plugin on an account"),
    "settings": ("<account>", "resolved plugin settings for an account"),
    "set": ("<account> <plugin> <key> <value>", "change one plugin setting"),
    "limits": ("", "rate limiter rules, delays and queues"),
    "set-limit": ("<action> <rule> [account]", "e.g. set-limit message 5/12s [alpha] (floor 1.5s/action)"),
    "clear-limit": ("<account> <action>", "remove a per-bot override"),
    "set-delay": ("<action> <min> <max> [account]", "human-like delay range, e.g. set-delay message 0.80 2.40 [alpha]"),
    "clear-delay": ("<action> [account]", "remove a delay range"),
    "warnings": ("", "safety review of current configuration"),
    "roles": ("", "defined roles and the plugins they permit"),
    "role-add": ("<name> <plugin...>", "create/update a role"),
    "role-del": ("<name>", "delete a role"),
    "assign": ("<account> <role>", "grant a role (applies on next start)"),
    "unassign": ("<account> <role>", "revoke a role"),
    "audit": ("[n] [account]", "last n audited interactions"),
    "env": ("", "redacted credentials view"),
    "quit": ("", "stop everything and exit (alias: exit)"),
}


def help_text() -> str:
    """Render the HELP table as aligned text."""
    return "\n".join(
        f"  {name:<14}{sig:<36}{desc}" for name, (sig, desc) in sorted(HELP.items())
    )


async def run_command(api: ControlAPI, line: str) -> Optional[str]:
    """Execute one command line against the ControlAPI.

    Expects: raw input line (shlex-parsed here).
    Returns: text to display, or None for silent success.
    Raises: QuitRequested for quit/exit; UsageError/ValueError/... bubble for
    the caller's error renderer (bad rules, unknown accounts, floor rejects).
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
        return "\n".join(
            format_status_lines(status)
            + [""] + format_warnings(
                [type("W", (), {"level": w["level"], "code": w["code"], "message": w["message"]})() for w in status.get("warnings", [])]
            )
        )
    if cmd == "plugins":
        return "\n".join(format_plugins(await api.plugins()))
    if cmd == "accounts":
        return "\n".join(format_accounts((await api.status())["accounts"]))
    if cmd == "add-account":
        return str(await api.add_account(_parse_account_payload(args)))
    if cmd == "remove-account":
        return str(await api.remove_account(_need(args, 1, cmd)))
    if cmd == "details":
        return "\n".join(format_details(await api.account_details(_need(args, 1, cmd))))
    if cmd == "start":
        await api.start(None if args == ["all"] else _need(args, 1))
        return "started."
    if cmd == "stop":
        await api.stop(None if args == ["all"] else _need(args, 1))
        return "stopped."
    if cmd in ("enable", "disable"):
        account, plugin = _need(args, 2, cmd)
        await api.set_plugin_enabled(account, plugin, cmd == "enable")
        return f"{cmd}d {plugin} on {account}."
    if cmd == "settings":
        return "\n".join(format_account_settings(await api.account_plugins(_need(args, 1, cmd))))
    if cmd == "set":
        account, plugin, key, value = _need(args, 4, cmd)
        return str(api.set_plugin_setting(account, plugin, key, value))
    if cmd == "limits":
        return "\n".join(format_limits(await api.limits()))
    if cmd == "set-limit":
        if len(args) == 2:
            action, rule = args
            return str(await api.set_limit(action, rule, None))
        if len(args) == 3:
            action, rule, account = args
            return str(await api.set_limit(action, rule, account))
        raise UsageError(f"usage: set-limit {HELP['set-limit'][0]}")
    if cmd == "clear-limit":
        account, action = _need(args, 2, cmd)
        return str(await api.clear_limit(account, action))
    if cmd == "set-delay":
        if len(args) == 3:
            action, minimum, maximum = args
            return str(await api.set_delay(action, float(minimum), float(maximum)))
        if len(args) == 4:
            action, minimum, maximum, account = args
            return str(await api.set_delay(action, float(minimum), float(maximum), account))
        raise UsageError(f"usage: set-delay {HELP['set-delay'][0]}")
    if cmd == "clear-delay":
        if len(args) == 2:
            return str(await api.clear_delay(args[0], args[1]))
        return str(await api.clear_delay(_need(args, 1)))
    if cmd == "warnings":
        return "\n".join(api.format_warnings_text())
    if cmd == "roles":
        return "\n".join(format_roles(await api.roles()))
    if cmd == "role-add":
        if len(args) < 2:
            raise UsageError(f"usage: role-add {HELP['role-add'][0]}")
        name, plugins = args[0], args[1:]
        return str(await api.set_role(name, plugins))
    if cmd == "role-del":
        return str(await api.delete_role(_need(args, 1)))
    if cmd == "assign":
        account, role = _need(args, 2)
        return str(await api.assign_role(account, role, True))
    if cmd == "unassign":
        account, role = _need(args, 2)
        return str(await api.assign_role(account, role, False))
    if cmd == "audit":
        # AUDIT FIX: positional parsing — audit [n] [account], repeats rejected
        limit, account = 20, None
        rest = args
        if rest and rest[0].isdigit():
            limit = int(rest[0])
            rest = rest[1:]
        if len(rest) > 1:
            raise UsageError(f"usage: audit {HELP['audit'][0]}")
        if rest:
            account = rest[0]
        from core.audit import format_audit

        entries = [type("E", (), e)() for e in api.audit(limit=limit, account=account)]
        return "\n".join(format_audit(entries))
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


def format_accounts(accounts: list) -> List[str]:
    """Account list: name prominent, id/roles/status under it."""
    if not accounts:
        return ["  (no accounts registered — 'add-account <name>')"]
    lines = []
    for account in accounts:
        lines.append(f"  {account['name']}")
        lines.append(
            f"      id: {account.get('user_id') if account.get('user_id') is not None else '(not running)'}"
            f"   status: {account['status']}"
            f"   group: {account.get('sync_group') or '-'}"
        )
        roles = account.get("roles")
        if roles:
            lines.append(f"      roles: {', '.join(roles)}")
    return lines


def format_details(details: dict) -> List[str]:
    """Account details page: status, roles, cached guild/channel inventory."""
    lines = [
        f"  {details['name']} — {details['status']}",
        f"      user id : {details.get('user_id') if details.get('user_id') is not None else '(not running)'}",
        f"      group   : {details.get('sync_group') or '-'}",
        f"      roles   : {', '.join(details.get('roles') or []) or '-'}",
    ]
    cached = details.get("cached") or {}
    if cached.get("available"):
        user = cached.get("user") or {}
        lines.append(f"      cached  : {user.get('name')} ({user.get('id')}) [in-memory only]")
        for guild in cached.get("guilds", []):
            channels = guild.get("channels", [])
            names = ", ".join(c.get("name") or str(c.get("id")) for c in channels[:8])
            more = f" (+{len(channels) - 8} more)" if len(channels) > 8 else ""
            lines.append(f"      guild   : {guild.get('name')} [{len(channels)} ch] {names}{more}")
        dms = cached.get("dm_channels", [])
        if dms:
            lines.append(f"      dm chan : {len(dms)} open")
    else:
        lines.append("      cached  : (unavailable until the account runs)")
    for plugin in details.get("plugins", []):
        flag = "+" if plugin["enabled"] else "-"
        lines.append(f"      plugin {flag} {plugin['name']}")
    return lines


def format_account_settings(plugins: list) -> List[str]:
    """Per-account resolved settings (the 'settings <account>' view)."""
    lines = []
    for plugin in plugins:
        flag = "+" if plugin["enabled"] else "-"
        lines.append(f"  {flag} {plugin['name']}")
        for key, value in sorted(plugin["settings"].items()):
            lines.append(f"        {key} = {value!r}")
    return lines or ["  (no plugins on this account)"]


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
    """Limiter stats -> display lines (rules, delays, overrides, queues)."""
    lines = []
    for action, info in stats["actions"].items():
        delay = info.get("delay", {})
        # AUDIT FIX: overrides render as k=v pairs, not a raw dict repr
        overrides = ", ".join(f"{k}={v}" for k, v in (info["overrides"] or {}).items()) or "-"
        lines.append(
            f"  {action:<8}default={info['default_rule']:<12}"
            f"delay={delay.get('global', 'off'):<18}"
            f"overrides={overrides:<24}pending={stats['pending'] or '-'}"
        )
        for account, account_delay in (delay.get("accounts") or {}).items():
            lines.append(f"           {account:<8}delay={account_delay}")
    return lines or ["  (no rules configured)"]


def format_roles(roles: list) -> List[str]:
    """Roles -> display lines with permitted plugins."""
    if not roles:
        return ["  (no roles defined — 'role-add <name> <plugin...>')"]
    return [
        f"  {role['name']:<14}[{', '.join(role['plugins'])}]"
        + (f" — {role['description']}" if role["description"] else "")
        for role in roles
    ]


def format_warnings(warnings: list) -> List[str]:
    """Warnings -> display lines (duck-typed .level/.code/.message)."""
    if not warnings:
        return ["  safety: no warnings — configuration looks sane"]
    icons = {"info": "[i]", "warn": "[!]", "danger": "[X]"}
    return [
        f"  {icons.get(w.level, '[?]')} {w.code:<16}{w.message}" for w in warnings
    ]


# ----------------------------------------------------------------- internals
def _parse_account_payload(args: list) -> dict:
    """'add-account bob sync=grinders roll snipe' -> payload dict."""
    if not args:
        raise UsageError(f"usage: add-account {HELP['add-account'][0]}")
    payload: dict = {"name": args[0]}
    roles: List[str] = []
    seen_sync = False
    for arg in args[1:]:
        if arg.startswith("sync="):
            if seen_sync:  # AUDIT FIX: a repeated sync= is a typo, not last-wins
                raise UsageError("add-account accepts at most one sync=<group>")
            seen_sync = True
            payload["sync_group"] = arg[len("sync="):] or None
        else:
            roles.append(arg)
    if roles:
        payload["roles"] = roles
    return payload


def _need(args: list, count: int, cmd: str = "") -> tuple | str:
    """Validate the EXACT argument count or raise a contextual usage error."""
    if len(args) != count:
        raise UsageError(
            f"{cmd or 'command'} expects {count} argument(s), got {len(args)}"
        )
    return args if count > 1 else args[0]
