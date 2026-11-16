"""Shared CLI command engine (spec §13) — used by the REPL and one-shot mode.

Every command is a thin, STRICTLY-validated wrapper over ``core.api.ControlAPI``
(the only object this module ever touches; duck-typed, so tests inject a fake).
Query commands render through the pure ``format_*`` helpers at the bottom so
payload rendering is unit-testable without a live stack.

Behaviour pinned here:
  * ``run_command(api, line)`` shlex-parses ``line`` and returns display text
    (``None`` for an empty line). It raises ``QuitRequested`` for quit/exit and
    ``UsageError`` for bad usage; every other exception (domain errors like
    ``AccountError``/``UnsafePolicyError`` included) bubbles for the caller's
    error renderer — this engine never swallows failures.
  * Argument counts are validated exactly, with contextual usage strings.

Decisions for things INTEGRATION.md left open (documented per the contract):
  * ``logs``/``audit`` default to the last 20 entries (the old CLI default).
  * ``ratelimit clear`` always takes 3 arguments; the owner placeholder for
    level ``global`` is ``-`` or ``none`` (passed as ``owner=None``).
  * ``simulation run`` without a name raises ``UsageError`` listing the
    available scenarios instead of guessing one.
  * ``accounts add <name> [plugin ...]`` assigns plugins DIRECTLY (payload
    ``{"name": ..., "plugins": [...]}``; the key is omitted when none are
    given). Profile-mediated capabilities were removed from the backend —
    ``profiles`` is never sent (it is rejected as an unknown payload key).
  * ``accounts plugins|plugin-add|plugin-remove`` manage the direct
    account→plugin assignments through the facade; removal is idempotent
    (``existed=false`` renders a "nothing to do" message, never an error).
    Assignments apply immediately — there are no next-start semantics.
  * The legacy ``profiles`` group is removed (profiles never controlled
    which plugins an account runs).
  * ``accounts enable <name> <on|off>`` also accepts true/false; a real bool
    is passed down (ControlAPI.parse_bool re-validates on its side anyway).
  * ``accounts credential <name> <token>`` prints a shell-history warning:
    values are stored encrypted, but the CLI argument itself may linger in
    the shell's history — the dashboard dialog is the preferred entry.
  * ``credentials import-env`` accepts exactly one flag, ``--overwrite``
    (anything else is a usage error, never a silent ignore).
  * ``cooldowns`` with no argument lists every account's cooldowns;
    ``cooldowns clear`` without a key clears ALL cooldowns of that plugin
    on that account.
  * Mutation results are rendered generically (``key=value`` pairs) from the
    JSON payload the facade returns, keeping CLI/dashboard parity by
    construction.
"""

from __future__ import annotations

import math
import shlex
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "HELP",
    "QuitRequested",
    "UsageError",
    "format_account_info",
    "format_account_plugins",
    "format_accounts",
    "format_audit",
    "format_cooldowns",
    "format_credential_status",
    "format_effective",
    "format_group_snapshot",
    "format_groups",
    "format_history",
    "format_import_report",
    "format_logs",
    "format_plugin_state",
    "format_plugins",
    "format_policies",
    "format_simulation_report",
    "format_status",
    "format_tasks",
    "format_warnings",
    "help_text",
    "run_command",
]


class QuitRequested(Exception):
    """Raised by ``run_command`` for quit/exit (the REPL shuts down cleanly)."""


class UsageError(ValueError):
    """Raised when a command line is malformed (bad arg count / bad number)."""


# ------------------------------------------------------------------- HELP
#: command -> (usage signature, description); the single registry both
#: help_text() and the contextual usage errors are built from.
HELP: dict[str, tuple[str, str]] = {
    "help": ("[command]", "show this help (or help for one command)"),
    "status": ("", "overview: accounts, tasks, rate limits, warnings"),
    "accounts": (
        "list|add <name> [plugin ...]|plugins <account>"
        "|plugin-add <account> <plugin>|plugin-remove <account> <plugin>"
        "|remove <name>|info <name>"
        "|credential <name> <token>|enable <name> <on|off>"
        "|start [name|all]|stop [name|all]|estop <name>",
        "account management (capabilities: direct account→plugin"
        " assignment, applied immediately; credentials: dashboard or"
        " 'accounts credential' — CAUTION: the token lands in your shell"
        " history)",
    ),
    "credentials": (
        "status|import-env [--overwrite]",
        "credential management: per-account status; one-shot import of the"
        " legacy .env tokens into encrypted db storage",
    ),
    "cooldowns": (
        "[account]|clear <account> <plugin> [key]",
        "active persistent cooldowns (survive restarts; absolute expiry)",
    ),
    "plugins": (
        "list|state <account>|enable <account> <plugin>"
        "|disable <account> <plugin>|settings <account>"
        "|set <account> <plugin> <key> <value>"
        "|clear <account> <plugin> <key>",
        "plugin management ('clear' resets one setting to its lower"
        " layer/default, unmasking profile + plugin defaults)",
    ),
    "groups": (
        "list|show <group>|create <name> [description]"
        "|update <group> <field> <value>|delete <group> [--detach-members]"
        "|add-member <group> <account>|remove-member <group> <account>"
        "|set-account <account> <group>|clear-account <account>",
        "sync groups (shared pacing membership; update fields: name, description, enabled)",
    ),
    "ratelimit": (
        "show|stats|set <action> <interval> [jitter_min jitter_max] [level owner]"
        "|clear <level> <owner> <action>|effective <action> [account] [plugin]"
        "|warnings|floor <seconds>|lane [on [rate]|off|rate <s>]",
        "rate policies (safety floor: 1.5s/action). The send lane is"
        " REMOVED — 'ratelimit lane' still answers as a documented compat"
        " stub (sync groups + per-account pacing replaced it)",
    ),
    "tasks": (
        "list [status]|cancel <id>|history [n]|cancel-matching [account] [plugin]",
        "task queue + history",
    ),
    "simulation": ("list|run [name]", "offline scenario runs"),
    "logs": (
        "[n] [account] [DEBUG|INFO|WARNING|ERROR]",
        "structured log tail (default: last 20; severity UPPERCASE)",
    ),
    "audit": ("[n] [account]", "interaction audit trail (default: last 20)"),
    "estop": ("[resume]", "GLOBAL emergency stop; 'estop resume' re-arms after one"),
    "quit": ("", "graceful shutdown, then exit (alias: exit)"),
}


def _col(value: Any, width: int) -> str:
    """Left-pad a table cell to ``width`` — but ALWAYS keep a separating
    space: a plain ``{value:<N}`` lets a value at/over the width run into
    the next column ("alpha_long_nameonline")."""
    text = str(value)
    return text.ljust(width) if len(text) < width else text + " "


def help_text() -> str:
    """Render the HELP registry as aligned text (with an error footer).

    Long group signatures get their own indented 'usage:' line so the table
    stays readable at 80 columns.
    """
    lines = ["commands:"]
    for name, (sig, desc) in sorted(HELP.items()):
        lines.append(f"  {_col(name, 12)}{desc}")
        if sig:
            lines.append(f"  {_col('', 12)}usage: {name} {sig}")
    lines.append(
        "  errors print as one 'error: ...' line and never exit the REPL;"
        " 'quit' shuts everything down first"
    )
    return "\n".join(lines)


# ------------------------------------------------------------ run_command
async def run_command(api: Any, line: str) -> str | None:
    """Execute one command line against the ControlAPI.

    Expects the raw input line (shlex-parsed here, so quoted arguments work).
    Returns display text, or ``None`` for an empty line (or silent success).

    Raises:
        QuitRequested: for quit/exit.
        UsageError: for unparseable input, wrong argument counts, bad numbers,
            bad booleans, unknown rate-policy levels, unknown commands and
            bad 'help' arguments.
        Anything the facade raises (AccountError, UnsafePolicyError, ...):
        bubbles up unchanged for the caller's error renderer.
    """
    line = (line or "").strip()
    if not line:
        return None
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        # unclosed quotes used to surface as a raw Python-internal message
        raise UsageError(f"cannot parse command (check your quotes): {exc}") from exc
    cmd, args = parts[0].lower(), [str(p) for p in parts[1:]]
    if cmd in ("quit", "exit"):
        raise QuitRequested()
    if cmd == "help":
        if len(args) > 1:
            raise UsageError("usage: help [command]")
        if not args:
            return help_text()
        name = args[0].lower()
        if name == "exit":
            name = "quit"
        if name not in HELP:
            raise UsageError(f"help: unknown command {args[0]!r} — try 'help' alone")
        sig, desc = HELP[name]
        focused = [f"{name}: {desc}"]
        if sig:
            focused.append(f"usage: {name} {sig}")
        return "\n".join(focused)
    if cmd == "status":
        _exact(args, 0, "status")
        return format_status(await api.status())
    if cmd == "accounts":
        return await _accounts_group(api, args)
    if cmd == "credentials":
        return await _credentials_group(api, args)
    if cmd == "cooldowns":
        return await _cooldowns_group(api, args)
    if cmd == "plugins":
        return await _plugins_group(api, args)
    if cmd == "groups":
        return await _groups_group(api, args)
    if cmd == "ratelimit":
        return await _ratelimit_group(api, args)
    if cmd == "tasks":
        return await _tasks_group(api, args)
    if cmd == "simulation":
        return await _simulation_group(api, args)
    if cmd == "logs":
        rest = list(args)
        severity = None
        if rest and rest[-1] in ("DEBUG", "INFO", "WARNING", "ERROR"):
            severity = rest.pop(-1)  # exact-case: account names are lowercase
        limit, account = _tail_args(cmd, rest)
        return format_logs(await api.logs_tail(limit, account=account, severity=severity))
    if cmd == "audit":
        limit, account = _tail_args(cmd, args)
        return format_audit(await api.audit_tail(limit, account=account))
    if cmd == "estop":
        if args and args[0].lower() in ("resume", "unhalt"):
            if len(args) > 1:
                raise UsageError("usage: estop [resume]")
            out = await api.system_resume()
            # the payload embeds the whole post-resume status snapshot:
            # render it as its own block instead of one key=value blob
            status = out.pop("status", None) if isinstance(out, dict) else None
            text = _result(out, verb="resume")
            if isinstance(status, dict):
                text += "\n" + format_status(status)
            return text
        if args:
            raise UsageError("usage: estop [resume]")
        return _result(await api.emergency_stop_all(surface="cli"), verb="EMERGENCY STOP")
    raise UsageError(f"unknown command {cmd!r} — try 'help'")


# ------------------------------------------------------------- sub-usages
_ACCOUNTS_USAGE = {
    "list": "accounts list",
    "add": "accounts add <name> [plugin ...]",
    "plugins": "accounts plugins <account>",
    "plugin-add": "accounts plugin-add <account> <plugin>",
    "plugin-remove": "accounts plugin-remove <account> <plugin>",
    "remove": "accounts remove <name>",
    "info": "accounts info <name>",
    "credential": "accounts credential <name> <token>",
    "enable": "accounts enable <name> <on|off>",
    "start": "accounts start [name|all]",
    "stop": "accounts stop [name|all]",
    "estop": "accounts estop <name>",
}
_CREDENTIALS_USAGE = {
    "status": "credentials status",
    "import-env": "credentials import-env [--overwrite]",
}
_COOLDOWNS_USAGE = {
    "clear": "cooldowns clear <account> <plugin> [key]",
}
_PLUGINS_USAGE = {
    "list": "plugins list",
    "state": "plugins state <account>",
    "enable": "plugins enable <account> <plugin>",
    "disable": "plugins disable <account> <plugin>",
    "settings": "plugins settings <account>",
    "set": "plugins set <account> <plugin> <key> <value>",
    "clear": "plugins clear <account> <plugin> <key>",
}
_GROUPS_USAGE = {
    "list": "groups list",
    "show": "groups show <group>",
    "create": "groups create <name> [description]",
    "update": "groups update <group> <field> <value>",
    "delete": "groups delete <group> [--detach-members]",
    "add-member": "groups add-member <group> <account>",
    "remove-member": "groups remove-member <group> <account>",
    "set-account": "groups set-account <account> <group>",
    "clear-account": "groups clear-account <account>",
}
_RATELIMIT_USAGE = {
    "show": "ratelimit show",
    "stats": "ratelimit stats",
    "set": "ratelimit set <action> <interval> [jitter_min jitter_max] [level owner]",
    "clear": "ratelimit clear <level> <owner> <action>",
    "effective": "ratelimit effective <action> [account] [plugin]",
    "warnings": "ratelimit warnings",
    "floor": "ratelimit floor <seconds>",
    "lane": "ratelimit lane [on [rate]|off|rate <s>]",
}
_TASKS_USAGE = {
    "list": "tasks list [status]",
    "cancel": "tasks cancel <id>",
    "history": "tasks history [n]",
    "cancel-matching": "tasks cancel-matching [account] [plugin]",
}

_LEVELS = ("global", "account", "plugin", "account_plugin")


def _sub(usage: dict[str, str], args: Sequence[str], group: str) -> tuple[str, list[str]]:
    """Split (sub, rest) and validate the subcommand exists."""
    if not args:
        raise UsageError(f"{group}: missing subcommand — usage: {group} {HELP[group][0]}")
    sub, rest = args[0].lower(), list(args[1:])
    if sub not in usage:
        raise UsageError(f"{group}: unknown subcommand {sub!r} — usage: {group} {HELP[group][0]}")
    return sub, rest


def _exact(args: Sequence[str], count: int, ctx: str) -> Any:
    """Validate the EXACT argument count or raise a contextual usage error."""
    if len(args) != count:
        usage = _usage_for(ctx)
        raise UsageError(
            f"{ctx}: expects {count} argument(s), got {len(args)}"
            + (f" — usage: {usage}" if usage else "")
        )
    return args[0] if count == 1 else list(args)


def _usage_for(ctx: str) -> str:
    """Look up the concise usage string for a '<group> <sub>' context."""
    group, _, sub = ctx.partition(" ")
    table = {
        "accounts": _ACCOUNTS_USAGE,
        "plugins": _PLUGINS_USAGE,
        "groups": _GROUPS_USAGE,
        "ratelimit": _RATELIMIT_USAGE,
        "tasks": _TASKS_USAGE,
        "credentials": _CREDENTIALS_USAGE,
        "cooldowns": _COOLDOWNS_USAGE,
    }.get(group, {})
    return table.get(sub, "")


def _number(token: str, ctx: str, label: str) -> float:
    """Parse a float with a helpful message on failure."""
    try:
        value = float(token)
    except ValueError as exc:
        # raw float() messages have zero context — give the user the input,
        # what it was for, and how to use the command
        usage = _usage_for(ctx) or f"{ctx}"
        raise UsageError(f"{ctx}: {label} {token!r} is not a number — usage: {usage}") from exc
    if not math.isfinite(value):
        usage = _usage_for(ctx) or f"{ctx}"
        raise UsageError(f"{ctx}: {label} must be a finite number, got {token!r} — usage: {usage}")
    return value


def _integer(token: str, ctx: str, label: str, *, negative_ok: bool = False) -> int:
    """Parse an int with a helpful message on failure (rejects negatives)."""
    try:
        value = int(token)
    except ValueError as exc:
        usage = _usage_for(ctx) or f"{ctx}"
        raise UsageError(f"{ctx}: {label} {token!r} is not an integer — usage: {usage}") from exc
    if value < 0 and not negative_ok:
        raise UsageError(f"{ctx}: {label} must be a non-negative integer, got {value}")
    return value


def _on_off(token: str, ctx: str) -> bool:
    """Strict on/off (plus true/false) parsing for boolean flags."""
    flag = token.strip().lower()
    if flag in ("on", "true"):
        return True
    if flag in ("off", "false"):
        return False
    raise UsageError(f"{ctx}: expected <on|off>, got {token!r}")


def _check_level_owner(level: str, owner: str | None, ctx: str) -> None:
    """Validate a policy level/owner pair (owner format per INTEGRATION.md)."""
    if level not in _LEVELS:
        raise UsageError(f"{ctx}: unknown level {level!r} — expected one of {'|'.join(_LEVELS)}")
    if level == "global" and owner is not None:
        raise UsageError(f"{ctx}: level 'global' takes no owner")
    if level != "global" and owner is None:
        raise UsageError(f"{ctx}: level {level!r} needs an owner")
    if level == "account_plugin" and "/" not in owner:
        raise UsageError(f"{ctx}: account_plugin owner must be '<account>/<plugin>', got {owner!r}")


def _tail_args(cmd: str, args: Sequence[str]) -> tuple[int, str | None]:
    """Positional '[n] [account]' parsing for logs/audit (old-CLI pattern)."""
    limit, account = 20, None
    rest = list(args)
    if rest and rest[0].lstrip("-").isdigit():
        limit = _integer(rest[0], cmd, "n")
        rest = rest[1:]
    if len(rest) > 1:
        raise UsageError(f"usage: {cmd} {HELP[cmd][0]}")
    if rest:
        account = rest[0]
    return limit, account


# ------------------------------------------------------------ group impls
async def _accounts_group(api: Any, args: list[str]) -> str:
    """accounts list|add|plugins|plugin-add|plugin-remove|remove|info|
    credential|enable|start|stop|estop."""
    sub, rest = _sub(_ACCOUNTS_USAGE, args, "accounts")
    ctx = f"accounts {sub}"
    if sub == "list":
        _exact(rest, 0, ctx)
        return format_accounts(await api.accounts_list())
    if sub == "add":
        if not rest:
            raise UsageError(f"usage: {_ACCOUNTS_USAGE['add']}")
        if not rest[0]:
            raise UsageError(f"account name cannot be empty — usage: {_ACCOUNTS_USAGE['add']}")
        payload: dict[str, Any] = {"name": rest[0]}
        if rest[1:]:
            # direct account→plugin assignment (the capability model);
            # 'profiles' is deliberately NOT sent — the backend rejects it
            payload["plugins"] = rest[1:]
        return _result(await api.accounts_add(payload, surface="cli"), verb="added")
    if sub == "plugins":
        account = _exact(rest, 1, ctx)
        return format_account_plugins(account, await api.accounts_plugins_list(account))
    if sub == "plugin-add":
        account, plugin = _exact(rest, 2, ctx)
        return _result(
            await api.accounts_plugin_assign(account, plugin, surface="cli"),
            verb="plugin added",
        )
    if sub == "plugin-remove":
        account, plugin = _exact(rest, 2, ctx)
        out = await api.accounts_plugin_unassign(account, plugin, surface="cli")
        if isinstance(out, dict) and out.get("existed") is False:
            # idempotent removal: the assignment was already gone
            return f"plugin removed: {plugin} was not assigned to {account} — nothing to do"
        return _result(out, verb="plugin removed")
    if sub == "credential":
        name, token = _exact(rest, 2, ctx)
        out = await api.accounts_set_credential(name, token, surface="cli")
        # SECURITY: only the account name + new status are echoed — never
        # the token (it already lives in the shell history, which we say).
        return (
            f"{_result(out, verb='credential stored')}\n"
            "  warning: the token may now sit in your shell history —"
            " prefer the dashboard dialog, or clear that history"
        )
    if sub == "remove":
        name = _exact(rest, 1, ctx)
        return _result(await api.accounts_remove(name, surface="cli"), verb="removed")
    if sub == "info":
        return format_account_info(await api.accounts_info(_exact(rest, 1, ctx)))
    if sub == "enable":
        name, flag = _exact(rest, 2, ctx)
        return _result(await api.accounts_enable(name, _on_off(flag, ctx)), verb="updated")
    if sub in ("start", "stop"):
        if len(rest) > 1:
            raise UsageError(f"usage: {_ACCOUNTS_USAGE[sub]}")
        name: str | None = None
        if rest and rest[0].lower() != "all":
            if not rest[0]:  # a quoted empty token must never mean "all"
                raise UsageError(
                    f"account name cannot be empty — use 'all' for every account "
                    f"(usage: {_ACCOUNTS_USAGE[sub]})"
                )
            name = rest[0]
        method = api.accounts_start if sub == "start" else api.accounts_stop
        return _result(await method(name, surface="cli"), verb=sub)
    if sub == "estop":
        name = _exact(rest, 1, ctx)
        return _result(
            await api.accounts_emergency_stop(name, surface="cli"),
            verb="emergency stop",
        )
    raise UsageError(f"accounts: unknown subcommand {sub!r}")  # pragma: no cover


async def _credentials_group(api: Any, args: list[str]) -> str:
    """credentials status|import-env [--overwrite]."""
    sub, rest = _sub(_CREDENTIALS_USAGE, args, "credentials")
    ctx = f"credentials {sub}"
    if sub == "status":
        _exact(rest, 0, ctx)
        return format_credential_status(await api.accounts_credential_status())
    # import-env: the ONLY accepted flag is --overwrite (typo trap)
    overwrite = False
    if rest and rest[0] == "--overwrite":
        overwrite = True
        rest = rest[1:]
    if rest:
        raise UsageError(f"usage: {_CREDENTIALS_USAGE['import-env']}")
    return format_import_report(
        await api.credentials_import_env(overwrite=overwrite, surface="cli")
    )


async def _cooldowns_group(api: Any, args: list[str]) -> str:
    """cooldowns [account] | cooldowns clear <account> <plugin> [key]."""
    if args and args[0].lower() == "clear":
        rest = args[1:]
        if not 2 <= len(rest) <= 3:
            raise UsageError(
                f"cooldowns clear: expects 2 or 3 arguments, got {len(rest)}"
                f" — usage: {_COOLDOWNS_USAGE['clear']}"
            )
        cooldown_key = rest[2] if len(rest) == 3 else None
        return _result(
            await api.cooldowns_clear(rest[0], rest[1], cooldown_key, surface="cli"),
            verb="cooldown cleared",
        )
    if len(args) > 1:
        raise UsageError(f"usage: cooldowns {HELP['cooldowns'][0]}")
    account = args[0] if args else None
    if account == "":
        raise UsageError("account name cannot be empty — usage: cooldowns [account]")
    return format_cooldowns(await api.cooldowns_list(account))


async def _plugins_group(api: Any, args: list[str]) -> str:
    """plugins list|state|enable|disable|settings|set|clear."""
    sub, rest = _sub(_PLUGINS_USAGE, args, "plugins")
    ctx = f"plugins {sub}"
    if sub == "list":
        _exact(rest, 0, ctx)
        return format_plugins(await api.plugins_manifests())
    if sub == "state":
        return format_plugin_state(await api.plugins_state(_exact(rest, 1, ctx)))
    if sub in ("enable", "disable"):
        account, plugin = _exact(rest, 2, ctx)
        out = await api.plugins_set_enabled(account, plugin, sub == "enable")
        return f"{sub}d {plugin} on {account}. {_result(out)}".rstrip()
    if sub == "settings":
        return format_plugin_state(await api.plugins_settings(_exact(rest, 1, ctx)))
    if sub == "set":
        account, plugin, key, value = _exact(rest, 4, ctx)
        return _result(await api.plugins_set_setting(account, plugin, key, value), verb="set")
    if sub == "clear":
        # parity with the dashboard's value:null reset: the CLI needs its
        # own verb because shlex cannot express a JSON null argument
        account, plugin, key = _exact(rest, 3, ctx)
        return _result(
            await api.plugins_clear_setting(account, plugin, key),
            verb="cleared",
        )
    raise UsageError(f"plugins: unknown subcommand {sub!r}")  # pragma: no cover


async def _groups_group(api: Any, args: list[str]) -> str:
    """groups list|show|create|update|delete|add-member|remove-member|
    set-account|clear-account (thin wrappers over ControlAPI)."""
    sub, rest = _sub(_GROUPS_USAGE, args, "groups")
    ctx = f"groups {sub}"
    if sub == "list":
        _exact(rest, 0, ctx)
        return format_groups(await api.groups_list())
    if sub == "show":
        return format_group_snapshot(await api.groups_snapshot(_exact(rest, 1, ctx)))
    if sub == "create":
        if not rest:
            raise UsageError(f"groups create: needs a name — usage: {_GROUPS_USAGE['create']}")
        payload: dict[str, Any] = {"name": rest[0]}
        if rest[1:]:
            payload["description"] = " ".join(rest[1:])
        return _result(await api.groups_create(payload, surface="cli"), verb="created")
    if sub == "update":
        group, field, value = _exact(rest, 3, ctx)
        if field not in ("name", "description", "enabled"):
            raise UsageError(
                f"groups update: unknown field {field!r} — expected name|description|enabled"
            )
        payload = {"enabled": _on_off(value, ctx)} if field == "enabled" else {field: value}
        return _result(
            await api.groups_update(group, payload, surface="cli"),
            verb="updated",
        )
    if sub == "delete":
        detach = "--detach-members" in rest
        rest = [token for token in rest if token != "--detach-members"]
        group = _exact(rest, 1, ctx)
        return _result(
            await api.groups_delete(group, detach_members=detach, surface="cli"),
            verb="deleted",
        )
    if sub == "add-member":
        group, account = _exact(rest, 2, ctx)
        return _result(
            await api.groups_add_account(group, account, surface="cli"),
            verb="member added",
        )
    if sub == "remove-member":
        group, account = _exact(rest, 2, ctx)
        return _result(
            await api.groups_remove_account(group, account, surface="cli"),
            verb="member removed",
        )
    if sub == "set-account":
        account, group = _exact(rest, 2, ctx)
        return _result(
            await api.groups_set_account(account, group, surface="cli"),
            verb="group set",
        )
    if sub == "clear-account":
        account = _exact(rest, 1, ctx)
        return _result(
            await api.groups_clear_account(account, surface="cli"),
            verb="group cleared",
        )
    raise UsageError(f"groups: unknown subcommand {sub!r}")  # pragma: no cover


async def _ratelimit_group(api: Any, args: list[str]) -> str:
    """ratelimit show|set|clear|effective|warnings|floor."""
    sub, rest = _sub(_RATELIMIT_USAGE, args, "ratelimit")
    ctx = f"ratelimit {sub}"
    if sub == "show":
        _exact(rest, 0, ctx)
        return format_policies(await api.ratelimit_policies())
    if sub == "stats":
        _exact(rest, 0, ctx)
        stats = await api.ratelimit_stats()
        lines = []
        for key, value in stats.items():
            if isinstance(value, dict):
                inner = " ".join(f"{k}={_fmt_val(v)}" for k, v in value.items())
                lines.append(f"{key}: {inner or '-'}")
            elif isinstance(value, list):
                lines.append(f"{key}: {len(value)} entr{'y' if len(value) == 1 else 'ies'}")
            else:
                lines.append(f"{key}: {_fmt_val(value)}")
        return "\n".join(lines) or "  (no stats)"
    if sub == "set":
        if len(rest) not in (2, 4, 6):
            raise UsageError(
                f"ratelimit set: expects 2, 4 or 6 arguments, got {len(rest)}"
                f" — usage: {_RATELIMIT_USAGE['set']}"
            )
        action = rest[0]
        interval = _number(rest[1], ctx, "interval")
        jitter_min = jitter_max = 0.0
        level, owner = "global", None
        if len(rest) >= 4:
            jitter_min = _number(rest[2], ctx, "jitter_min")
            jitter_max = _number(rest[3], ctx, "jitter_max")
        if len(rest) == 6:
            level, owner = rest[4].lower(), rest[5]
            _check_level_owner(level, owner, ctx)
        out = await api.ratelimit_set(
            action,
            interval,
            jitter_min=jitter_min,
            jitter_max=jitter_max,
            level=level,
            owner=owner,
        )
        return _result(out, verb="policy set")
    if sub == "clear":
        level, owner_token, action = _exact(rest, 3, ctx)
        level = level.lower()
        owner = None if owner_token.lower() in ("-", "none") else owner_token
        _check_level_owner(level, owner, ctx)
        return _result(await api.ratelimit_clear(level, owner, action), verb="cleared")
    if sub == "effective":
        if not 1 <= len(rest) <= 3:
            raise UsageError(f"usage: {_RATELIMIT_USAGE['effective']}")
        action = rest[0]
        account = rest[1] if len(rest) >= 2 else None
        plugin = rest[2] if len(rest) == 3 else None
        return format_effective(await api.ratelimit_effective(action, account, plugin))
    if sub == "warnings":
        _exact(rest, 0, ctx)
        return format_warnings(await api.ratelimit_warnings())
    if sub == "floor":
        seconds = _number(_exact(rest, 1, ctx), ctx, "seconds")
        return _result(await api.ratelimit_set_floor(seconds), verb="floor set")
    if sub == "lane":
        return await _lane_subcommand(api, rest, ctx)
    raise UsageError(f"ratelimit: unknown subcommand {sub!r}")  # pragma: no cover


async def _lane_subcommand(api: Any, rest: list[str], ctx: str) -> str:
    """``ratelimit lane`` — show/configure the sequential send lane.

    ``lane``                show state · ``lane on [rate]`` enable (+spacing)
    ``lane off``            disable ·  ``lane rate <s>`` set spacing (0=pause)
    """
    if not rest:
        return format_lane(await api.ratelimit_lane())
    verb = rest[0].lower()
    if verb in ("on", "enable"):
        if len(rest) > 2:
            raise UsageError(f"usage: {_RATELIMIT_USAGE['lane']}")
        prefixrate = _number(rest[1], ctx, "rate") if len(rest) > 1 else None
        return format_lane(await api.ratelimit_set_lane(enabled=True, prefixrate=prefixrate))
    if verb in ("off", "disable"):
        if len(rest) > 1:
            raise UsageError(f"usage: {_RATELIMIT_USAGE['lane']}")
        return format_lane(await api.ratelimit_set_lane(enabled=False))
    if verb == "rate":
        rate = _number(_exact(rest[1:], 1, ctx), ctx, "rate")
        return format_lane(await api.ratelimit_set_lane(prefixrate=rate))
    raise UsageError(f"ratelimit lane: unknown verb {verb!r} — usage: {_RATELIMIT_USAGE['lane']}")


async def _tasks_group(api: Any, args: list[str]) -> str:
    """tasks list|cancel|history|cancel-matching."""
    sub, rest = _sub(_TASKS_USAGE, args, "tasks")
    ctx = f"tasks {sub}"
    if sub == "list":
        if len(rest) > 1:
            raise UsageError(f"usage: {_TASKS_USAGE['list']}")
        status = rest[0] if rest else None
        return format_tasks(await api.tasks_list(status=status))
    if sub == "cancel":
        task_id = _integer(_exact(rest, 1, ctx), ctx, "id", negative_ok=True)
        return _result(await api.tasks_cancel(task_id), verb="cancel")
    if sub == "history":
        if len(rest) > 1:
            raise UsageError(f"usage: {_TASKS_USAGE['history']}")
        limit = _integer(rest[0], ctx, "n") if rest else 50
        return format_history(await api.tasks_history(limit=limit))
    if sub == "cancel-matching":
        if len(rest) > 2:
            raise UsageError(f"usage: {_TASKS_USAGE['cancel-matching']}")
        account = rest[0] if rest else None
        plugin = rest[1] if len(rest) == 2 else None
        return _result(
            await api.tasks_cancel_matching(account, plugin),
            verb="cancel-matching",
        )
    raise UsageError(f"tasks: unknown subcommand {sub!r}")  # pragma: no cover


async def _simulation_group(api: Any, args: list[str]) -> str:
    """simulation list|run [name]."""
    sub, rest = _sub(
        {"list": "simulation list", "run": "simulation run [name]"}, args, "simulation"
    )
    if sub == "list":
        _exact(rest, 0, "simulation list")
        scenarios = await api.simulation_list()
        if not scenarios:
            return "  (no scenarios available)"
        return "\n".join(
            f"  {_col(str(s.get('name', '?')), 24)}{s.get('description', '')}" for s in scenarios
        )
    # run
    if not rest:
        scenarios = await api.simulation_list()
        names = ", ".join(str(s.get("name")) for s in scenarios) or "(none)"
        raise UsageError(f"simulation run: needs a scenario name — available: {names}")
    return format_simulation_report(await api.simulation_run(_exact(rest, 1, "simulation run")))


# ---------------------------------------------------------------- formatters
def format_status(status: dict) -> str:
    """Overview payload (api.status) -> aligned text: uptime/tasks/accounts/
    policies + the safety-warning block."""
    lines: list[str] = []
    tasks = status.get("tasks") or {}
    if tasks:
        lines.append("tasks: " + " ".join(f"{key}={_fmt_val(val)}" for key, val in tasks.items()))
    uptime = status.get("uptime_s")
    if uptime is not None:
        lines[0:0] = [f"uptime: {_fmt_duration(float(uptime))}"]
    lines.append("")
    lines.append("accounts:")
    lines.extend(format_accounts(status.get("accounts") or []).splitlines())
    lines.append("")
    lines.append("rate limits:")
    stats = status.get("ratelimit") or {}
    lines.extend(format_policies(stats.get("policies") or []).splitlines())
    lines.append("")
    lines.append(format_warnings(status.get("warnings") or []))
    return "\n".join(lines)


def format_accounts(accounts: list[dict]) -> str:
    """Account list payload (api.accounts_list) -> one block per account.

    Capabilities render from ``assigned_plugins`` (the direct account→plugin
    assignment); legacy ``profiles`` stay out of this view — they no longer
    gate anything.
    """
    if not accounts:
        return "  (no accounts registered — 'accounts add <name>')"
    lines: list[str] = []
    for account in accounts:
        name = str(account.get("name", "?"))
        state = str(account.get("status") or "-")
        uid = account.get("user_id")
        if uid is None:
            uid = account.get("discord_id")
        enabled = account.get("enabled")
        flag = "" if enabled is None else (" [enabled]" if enabled else " [disabled]")
        lines.append(
            f"  {_col(name, 16)}{_col(state, 14)}id={_fmt_val(uid) if uid is not None else '-'}{flag}"
        )
        plugins = account.get("assigned_plugins") or []
        if plugins:
            lines.append(f"      plugins: {', '.join(str(p) for p in plugins)}")
    return "\n".join(lines)


def format_account_info(details: dict) -> str:
    """Account details payload (api.accounts_info) -> the spec §8 page.

    Sections: Status / Identity / Runtime (clearly marked as CLIENT CACHE —
    runtime data only ever comes from the live client's cached state) /
    Plugins (the assigned set + enabled flags) / Profiles (informational
    retained infrastructure — NOT capabilities) / Rate Limit (sources like
    'inherited from global').
    """
    name = details.get("name", "?")
    status = details.get("status") or {}
    identity = details.get("identity") or {}
    runtime = details.get("runtime") or {}

    def section(title: str) -> None:
        lines.append("")
        lines.append(title)
        lines.append("-" * 16)

    lines: list[str] = [f"Account: {name}"]
    section("Status")
    connected = status.get("connected")
    lines.append("Connected: " + ("yes" if connected else "no" if connected is not None else "-"))
    lines.append(f"State:     {status.get('state', '-')}")
    latency = status.get("latency_ms")
    lines.append("Latency:   " + (f"{float(latency):.0f} ms" if latency is not None else "-"))
    uptime = status.get("uptime_s")
    lines.append("Uptime:    " + (_fmt_duration(float(uptime)) if uptime is not None else "-"))
    section("Identity")
    lines.append(f"Name: {identity.get('name', '-')}")
    lines.append(f"ID:   {identity.get('discord_id', '-')}")
    section("Runtime (client cache)")
    lines.append(f"Cache source:    {runtime.get('cache_source', '-')}")
    lines.append(f"Cached guilds:   {runtime.get('cached_guilds', '-')}")
    lines.append(f"Cached channels: {runtime.get('cached_channels', '-')}")
    lines.append(f"Cached DMs:      {runtime.get('cached_dms', '-')}")
    section("Plugins")
    plugins = details.get("plugins") or []
    if plugins:
        for plugin in plugins:
            state = plugin.get("enabled")
            flag = "enabled" if state else "disabled" if state is not None else "-"
            lines.append(f"{_col(str(plugin.get('name', '?')), 14)}{flag}")
    else:
        lines.append("(none assigned — 'accounts plugin-add <account> <plugin>')")
    section("Rate Limit")
    rate = details.get("rate_limit") or {}
    if rate:
        for action, info in rate.items():
            info = info or {}
            jitter = info.get("jitter") or [0.0, 0.0]
            source = info.get("source", "-")
            lines.append(
                f"{_col(str(action), 14)}{_col(_fmt_policy(info.get('interval'), jitter[0], jitter[1]), 22)}{source}"
            )
    else:
        lines.append("(no actions paced)")
    return "\n".join(lines)


def format_account_plugins(account: str, plugins: Sequence[str]) -> str:
    """Assigned-plugin list (api.accounts_plugins_list) -> sorted lines.

    The assignment (not the plugin's enabled flag) is what renders here —
    see ``plugins state <account>`` for enabled/disabled detail.
    """
    names = sorted(str(p) for p in plugins or [])
    if not names:
        return f"  (no plugins assigned to {account} — 'accounts plugin-add {account} <plugin>')"
    return "\n".join(f"  {name}" for name in names)


def format_plugins(manifests: list[dict]) -> str:
    """Plugin manifests (api.plugins_manifests) -> metadata + settings schema."""
    if not manifests:
        return "  (no plugins discovered)"
    lines: list[str] = []
    for manifest in manifests:
        lines.append(
            f"  {manifest.get('name', '?')} v{manifest.get('version', '?')}"
            f" — {manifest.get('description', '')}"
        )
        for setting in manifest.get("settings") or []:
            key = setting.get("name") or setting.get("key") or "?"
            bounds = ""
            if setting.get("min") is not None or setting.get("max") is not None:
                bounds = f" [{setting.get('min', '')}..{setting.get('max', '')}]"
            lines.append(
                f"      {key}:{setting.get('type', '?')}"
                f"={setting.get('default')!r} {setting.get('description', '')}{bounds}"
            )
    return "\n".join(lines)


def format_plugin_state(states: list[dict]) -> str:
    """Per-account plugin state (api.plugins_state / plugins_settings).

    Renders '[+]' enabled / '[-]' disabled / '[*]' unknown (the settings-only
    payload carries no enabled flag), resolved settings, and error counters.
    """
    if not states:
        return "  (no plugins on this account)"
    lines: list[str] = []
    for state in states:
        enabled = state.get("enabled")
        flag = "[+]" if enabled else "[-]" if enabled is not None else "[*]"
        errors = state.get("error_count") or 0
        suffix = ""
        if errors:
            suffix = f"  errors: {errors}"
            if state.get("last_error"):
                suffix += f" (last: {state['last_error']})"
        lines.append(f"  {flag} {state.get('name', '?')}{suffix}")
        for key, value in sorted((state.get("settings") or {}).items()):
            lines.append(f"      {key} = {value!r}")
    return "\n".join(lines)


def format_groups(groups: list[dict]) -> str:
    """Sync groups payload (api.groups_list) -> name, members, description."""
    if not groups:
        return "  (no sync groups — 'groups create <name>')"
    lines: list[str] = []
    for group in groups:
        count = group.get("member_count", 0)
        flag = "" if group.get("enabled", True) else " [disabled]"
        desc = group.get("description") or ""
        lines.append(
            f"  {_col(str(group.get('name', '?')), 18)}{count} member(s){flag}"
            + (f" — {desc}" if desc else "")
        )
    return "\n".join(lines)


def format_group_snapshot(snapshot: dict) -> str:
    """Group snapshot (api.groups_snapshot) -> the full group page."""
    lines = [
        f"Group: {snapshot.get('name', '?')}  (id={snapshot.get('id', '?')})",
        f"  Members: {snapshot.get('member_count', 0)}"
        f"  pacing key: {snapshot.get('pacing_key', '-')}",
    ]
    desc = snapshot.get("description") or ""
    if desc:
        lines.append(f"  Description: {desc}")
    lines.append("  Enabled: " + ("yes" if snapshot.get("enabled", True) else "no"))
    members = snapshot.get("members") or []
    if members:
        for member in members:
            if isinstance(member, dict):
                status = member.get("status") or "-"
                lines.append(f"    {_col(str(member.get('name', '?')), 18)}{status}")
            else:
                lines.append(f"    {member}")
    else:
        lines.append("    (none — accounts join with 'groups add-member')")
    return "\n".join(lines)


def format_lane(lane: dict) -> str:
    """Lane state -> display lines (the send lane is REMOVED).

    The backend keeps ratelimit_lane/ratelimit_set_lane as documented
    compat stubs whose payloads carry ``removed``/``note``/``reason`` —
    render those VERBATIM so nobody reads the stub as a live "off" lane
    (legacy non-removed payloads still render the old detail view).
    """
    if lane.get("removed"):
        text = "  send lane: REMOVED — fleet lane replaced by sync groups + per-account pacing"
        note = lane.get("note") or lane.get("reason") or ""
        if note:
            text += f"\n      {note}"
        return text
    if not lane.get("enabled"):
        return "  send lane: off (sends are paced per-account only)"
    state = "PAUSED — all outbound actions hold" if lane.get("paused") else "active"
    last = lane.get("last_grant_age_s")
    last_text = f"{last:g}s ago" if last is not None else "never"
    return (
        f"  send lane: on — server-wide sequential sends ({state})\n"
        f"      spacing   : {lane.get('prefixrate', 0):g}s between consecutive sends"
        "  (0 pauses)\n"
        f"      waiting   : {lane.get('waiting', 0)}   grants: {lane.get('grants', 0)}"
        f"   last grant: {last_text}"
    )


def format_policies(policies: list[dict]) -> str:
    """Stored policies (api.ratelimit_policies) -> scope/action/policy table."""
    if not policies:
        return "  (no policies set — 'ratelimit set <action> <interval>')"
    lines: list[str] = []
    for policy in sorted(
        policies,
        key=lambda p: (
            str(p.get("level", "")),
            str(p.get("owner") or ""),
            str(p.get("action", "")),
        ),
    ):
        scope = str(policy.get("level", "?"))
        if policy.get("owner"):
            scope += f"/{policy['owner']}"
        lines.append(
            f"  {_col(scope, 24)}{_col(str(policy.get('action', '?')), 16)}"
            f"{_fmt_policy(policy.get('interval'), *(policy.get('jitter') or [0.0, 0.0]))}"
        )
    return "\n".join(lines)


def format_effective(effective: dict) -> str:
    """Effective pacing (api.ratelimit_effective) -> interval + source stack.

    The effective interval is the MAX across every applicable level; the
    source that wins is marked '<- pacing source'.
    """
    jitter = effective.get("jitter") or [0.0, 0.0]
    lines = [
        f"  {effective.get('action', '?')}: "
        f"{_fmt_policy(effective.get('interval'), jitter[0], jitter[1])}"
    ]
    interval = effective.get("interval")
    for source in effective.get("sources") or []:
        scope = str(source.get("level", "?"))
        if source.get("owner"):
            scope += f"/{source['owner']}"
        mark = ""
        src_interval = source.get("interval")
        if (
            interval is not None
            and src_interval is not None
            and abs(float(src_interval) - float(interval)) < 1e-9
        ):
            mark = "  <- pacing source"
        src_text = "-" if src_interval is None else f"{float(src_interval):.2f}s"
        lines.append(f"      {_col(scope, 24)}{src_text}{mark}")
    return "\n".join(lines)


def format_tasks(tasks: list[dict]) -> str:
    """Queued/running tasks (api.tasks_list) -> a compact table."""
    if not tasks:
        return "  (no tasks — 'tasks list [status]')"
    lines: list[str] = []
    for task in tasks:
        channel = task.get("channel_id")
        lines.append(
            f"  #{_col(str(task.get('id', '?')), 5)}{_col(str(task.get('status', '-')), 11)}"
            f"{_col(str(task.get('account') or '-'), 12)}{_col(str(task.get('plugin') or '-'), 12)}"
            f"{_col(str(task.get('action') or '-'), 10)}"
            f"ch={channel if channel is not None else '-'}"
        )
    return "\n".join(lines)


def format_history(tasks: list[dict]) -> str:
    """Finished tasks (api.tasks_history) -> outcome + duration per task."""
    if not tasks:
        return "  (no history yet)"
    lines: list[str] = []
    for task in tasks:
        duration = task.get("duration_s")
        if duration is None and task.get("started_at") and task.get("finished_at"):
            duration = float(task["finished_at"]) - float(task["started_at"])
        tail = "-"
        if task.get("error"):
            tail = f"error: {task['error']}"
        elif task.get("result_summary") or task.get("summary"):
            # in-memory rows say result_summary; repository history rows
            # (TaskHistoryRepository.recent) say summary
            tail = str(task.get("result_summary") or task.get("summary"))
        dur = f"{float(duration):.1f}s" if duration is not None else "-"
        lines.append(
            f"  #{_col(str(task.get('id', '?')), 5)}{_col(str(task.get('status', '-')), 11)}"
            f"{_col(str(task.get('account') or '-'), 12)}{_col(str(task.get('plugin') or '-'), 12)}"
            f"{_col(str(task.get('action') or '-'), 10)}{dur:>7}  {tail}"
        )
    return "\n".join(lines)


def format_logs(entries: list[dict]) -> str:
    """Structured log entries (api.logs_tail) -> 'HH:MM:SS SEVERITY source msg'."""
    if not entries:
        return "  (no log entries)"
    lines: list[str] = []
    for entry in entries:
        context = []
        if entry.get("account"):
            context.append(f"account={entry['account']}")
        if entry.get("plugin"):
            context.append(f"plugin={entry['plugin']}")
        if entry.get("task_id") is not None:
            context.append(f"task={entry['task_id']}")
        if entry.get("event"):
            context.append(f"event={entry['event']}")
        suffix = ("  (" + " ".join(context) + ")") if context else ""
        lines.append(
            f"  {_hms(entry.get('ts'))} {_col(str(entry.get('severity', '-')), 8)}"
            f"{_col(str(entry.get('source', '-')), 18)}{entry.get('message', '')}{suffix}"
        )
    return "\n".join(lines)


def format_audit(entries: list[dict]) -> str:
    """Audit entries (api.audit_tail) -> compact interaction lines.

    Format: ``HH:MM:SS -> account action ch=... content (wait Xms, jitter Yms)``
    with ``->`` for outbound and ``<-`` for inbound interactions.
    """
    if not entries:
        return "  (no audited interactions)"
    lines: list[str] = []
    for entry in entries:
        arrow = "<-" if entry.get("direction") == "in" else "->"
        channel = entry.get("channel_id")
        content = _clip(str(entry.get("content") or ""), 42)
        wait = entry.get("wait_ms") or 0.0
        jitter = entry.get("jitter_ms") or 0.0
        lines.append(
            f"  {_hms(entry.get('ts'))} {arrow} {_col(str(entry.get('account') or '-'), 10)}"
            f"{_col(str(entry.get('action') or '-'), 10)}"
            f"ch={channel if channel is not None else '-'}  {content!r}"
            f" (wait {float(wait):.0f}ms, jitter {float(jitter):.0f}ms)"
        )
    return "\n".join(lines)


def format_warnings(warnings: list[dict]) -> str:
    """Safety warnings (api.ratelimit_warnings) -> icon lines.

    Icons: ``[i]`` informational, ``[!]`` worth a look, ``[X]`` dangerous.
    """
    if not warnings:
        return "  safety: no warnings — configuration looks sane"
    icons = {"info": "[i]", "warning": "[!]", "warn": "[!]", "danger": "[X]", "error": "[X]"}
    lines: list[str] = []
    for warning in warnings:
        level = str(warning.get("level", "")).lower()
        icon = icons.get(level, "[?]")
        lines.append(
            f"  {icon} {_col(str(warning.get('code', '?')), 16)}{warning.get('message', '')}"
        )
    return "\n".join(lines)


def format_simulation_report(report: dict) -> str:
    """Simulation report (api.simulation_run) -> verdict + first 30 log lines.

    Long transcripts are capped at 30 timed lines with a '(+N more lines)'
    marker so one runaway scenario cannot flood the terminal.
    """
    passed = bool(report.get("passed"))
    duration = report.get("duration_s")
    verdict = "PASS" if passed else "FAIL"
    lines = [
        f"simulation {report.get('name', '?')!r}: {verdict}"
        + (f"  ({float(duration):.1f}s)" if duration is not None else "")
    ]
    failures = report.get("failures") or []
    if failures:
        lines.append("failures:")
        lines.extend(f"  - {failure}" for failure in failures)
    else:
        lines.append("failures: none")
    log_lines = report.get("log_lines") or []
    if log_lines:
        lines.append("log:")
        lines.extend(f"  {entry}" for entry in log_lines[:30])
        if len(log_lines) > 30:
            lines.append(f"  (+{len(log_lines) - 30} more lines)")
    summary = report.get("summary")
    if summary:
        lines.append(f"summary: {summary}")
    return "\n".join(lines)


def format_credential_status(rows: list[dict]) -> str:
    """Credential status payload (api.accounts_credential_status) -> table.

    One line per account: name + db/env/missing chip. NEVER a value — the
    payload does not even carry one.
    """
    if not rows:
        return "  (no accounts registered — 'accounts add <name>')"
    labels = {"db": "db (encrypted)", "env": "env (.env fallback)"}
    lines: list[str] = []
    for row in rows:
        state = row.get("credential")
        state_text = labels.get(state, str(state or "missing"))
        lines.append(f"  {_col(str(row.get('name', '?')), 20)}credential: {state_text}")
    return "\n".join(lines)


def format_import_report(report: dict) -> str:
    """Import report (api.credentials_import_env) -> names-only summary."""
    imported = report.get("imported") or []
    skipped = report.get("skipped") or []
    failed = report.get("failed") or {}
    lines = [f"  imported: {', '.join(imported) if imported else '(none)'}"]
    lines.append(
        f"  skipped : {', '.join(skipped) if skipped else '(none)'}"
        "  (already stored; use --overwrite)"
    )
    if failed:
        lines.append("  failed  :")
        lines.extend(f"      {name}: {reason}" for name, reason in failed.items())
    else:
        lines.append("  failed  : (none)")
    return "\n".join(lines)


def format_cooldowns(rows: list[dict]) -> str:
    """Active cooldowns (api.cooldowns_list) -> account/plugin/key table."""
    if not rows:
        return "  (no active cooldowns)"
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"  {_col(str(row.get('account', '?')), 16)}"
            f"{_col(str(row.get('plugin') or '-'), 16)}"
            f"{_col(str(row.get('cooldown_key') or '-'), 20)}"
            f"{_fmt_duration(row.get('remaining_s') or 0.0):>10}"
            f"  {row.get('source') or '-'!s}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------- internals
def _result(out: Any, verb: str = "") -> str:
    """Render a mutation payload as one human-friendly ``key=value`` line."""
    if isinstance(out, dict):
        body = " ".join(f"{key}={_fmt_val(val)}" for key, val in out.items())
        text = f"{verb}: {body}" if (verb and body) else (body or str(verb))
    else:
        text = f"{verb}: {out}" if verb else str(out)
    return text.rstrip()


def _fmt_val(value: Any) -> str:
    """Compact scalar rendering for status/result lines."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt_val(item) for item in value) + "]"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _fmt_policy(interval: Any, jitter_min: Any = 0.0, jitter_max: Any = 0.0) -> str:
    """Interval + jitter -> '2.50s +0.20..0.80s' (jitter omitted when zero).

    ``interval=None`` (no applicable policy) renders as the explicit
    unpaced marker — never as the bare string 'None'.
    """
    if interval is None:
        return "- (unpaced)"
    try:
        text = f"{float(interval):.2f}s"
        lo, hi = float(jitter_min or 0.0), float(jitter_max or 0.0)
        if lo or hi:
            text += f" +{lo:.2f}..{hi:.2f}s"
        return text
    except (TypeError, ValueError):
        return str(interval)


def _fmt_duration(seconds: float) -> str:
    """Seconds -> the spec §8 style '2h 14m' (or '14m 2s' / '42s')."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _hms(ts: Any) -> str:
    """Epoch timestamp -> local 'HH:MM:SS' (log/audit line prefix)."""
    try:
        moment = time.localtime(float(ts))
        return f"{moment.tm_hour:02d}:{moment.tm_min:02d}:{moment.tm_sec:02d}"
    except (TypeError, ValueError):
        return "--:--:--"


def _clip(text: str, width: int) -> str:
    """Shorten audit content so one line never blows past the terminal."""
    return text if len(text) <= width else text[: width - 1] + "…"
