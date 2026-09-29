"""Interactive terminal management interface (one of the two control surfaces).

Thin shell over ControlAPI so terminal and GUI dashboard stay feature-equal.
Commands:
  help | status | plugins | start <acct> | stop <acct> | start-all | stop-all
  enable <acct> <plugin> | disable <acct> <plugin>
  limits | set-limit <action> <rule> [acct] | clear-limit <acct> <action>
  env | quit
"""
from __future__ import annotations

import shlex
import sys
from typing import Optional, TextIO

from core.api import ControlAPI

BANNER = """MudaeScripts terminal — 'help' for commands."""

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


class Terminal:
    """REPL over ControlAPI; injectable stdin/stdout for testing."""

    def __init__(self, api: ControlAPI, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> None:
        self.api = api
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self.running = False

    async def run(self) -> None:
        """Main loop; returns when the user quits."""
        self.running = True
        self._print(BANNER)
        while self.running:
            self._print("mudae> ", end="")
            try:
                line = self._in.readline()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            if not await self.execute(line.strip()):
                break
        await self.api.stop()
        self._print("bye.")

    def _print(self, *args, **kwargs) -> None:
        print(*args, file=self._out, **kwargs)

    async def execute(self, line: str) -> bool:
        """Run one command line; returns False when the REPL should exit."""
        if not line:
            return True
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self._print(f"parse error: {exc}")
            return True
        cmd, args = parts[0].lower(), parts[1:]
        try:
            return await self._dispatch(cmd, args)
        except Exception as exc:
            self._print(f"error: {exc}")
            return True

    async def _dispatch(self, cmd: str, args: list) -> bool:
        """Route one parsed command; the only place returning False is 'quit'."""
        if cmd in ("quit", "exit"):
            return False
        if cmd == "help":
            for name, (sig, desc) in sorted(HELP.items()):
                self._print(f"  {name:<12}{sig:<28}{desc}")
        elif cmd == "status":
            status = await self.api.status()
            for acct in status["accounts"]:
                plugins = ", ".join(n for n, s in acct["plugins"].items() if s["enabled"]) or "-"
                self._print(
                    f"  {acct['name']:<10}{acct['status']:<10}group={acct['sync_group'] or '-':<10}[{plugins}]"
                )
        elif cmd == "plugins":
            for manifest in await self.api.plugins():
                self._print(f"  {manifest['name']} v{manifest['version']} — {manifest['description']}")
                for setting in manifest["settings"]:
                    self._print(
                        f"      {setting['key']}:{setting['type']}={setting['default']!r} {setting['description']}"
                    )
        elif cmd == "start":
            await self.api.start(None if args == ["all"] else self._need(args, 1, cmd))
            self._print("started.")
        elif cmd == "stop":
            await self.api.stop(None if args == ["all"] else self._need(args, 1, cmd))
            self._print("stopped.")
        elif cmd in ("enable", "disable"):
            account, plugin = self._need(args, 2, cmd)
            await self.api.set_plugin_enabled(account, plugin, cmd == "enable")
            self._print("done.")
        elif cmd == "limits":
            stats = await self.api.limits()
            for action, info in stats["actions"].items():
                self._print(
                    f"  {action:<8}default={info['default_rule']:<10}"
                    f"overrides={info['overrides'] or '-'} pending={stats['pending'] or '-'}"
                )
        elif cmd == "set-limit":
            if len(args) == 2:
                action, rule = args
                result = await self.api.set_limit(action, rule, None)
            elif len(args) == 3:
                action, rule, account = args
                result = await self.api.set_limit(action, rule, account)
            else:
                self._print(HELP["set-limit"])
                return True
            self._print(f"rule set: {result}")
        elif cmd == "clear-limit":
            account, action = self._need(args, 2, cmd)
            self._print(await self.api.clear_limit(account, action))
        elif cmd == "env":
            for key, value in sorted(self.api.credentials_summary().items()):
                self._print(f"  {key}={value or '<unset>'}")
        else:
            self._print(f"unknown command {cmd!r} — 'help'")
        return True

    def _need(self, args: list, count: int, cmd: str) -> tuple | list:
        """Validate arg count for a command (or print usage)."""
        if len(args) < count:
            self._print(f"usage: {cmd} {HELP.get(cmd, ('',))[0]}")
            raise SystemExit(2)
        return args if count > 1 else args[0]
