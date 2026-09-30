"""Interactive terminal management interface (one of the two control surfaces).

Thin shell over cli/commands.py (shared with the TUI) so every surface stays
feature-equal. Type 'help' for the command list.

ERROR AUDIT FIXES: api.stop() + the goodbye now run on EVERY exit path
(quit, EOF, Ctrl-C, mid-command interrupts) and command failures never leak
tracebacks — one 'error: <message>' line each, with the exception type
included for non-domain errors.
"""
from __future__ import annotations

import sys
from typing import Optional, TextIO

from core.api import ControlAPI

from .commands import QuitRequested, help_text, run_command

BANNER = """SlaughterMudae terminal — 'help' for commands."""

#: exception families that already carry a human message
_DOMAIN = ("UsageError", "ContainerError", "AccountError", "RateRuleError",
           "UnsafeRateRuleError", "SettingValidationError", "RoleError",
           "CredentialsError", "AdapterNotAvailable")


def render_error(exc: BaseException) -> str:
    """One-line error text; keeps the type name for non-domain exceptions."""
    name = type(exc).__name__
    if name in _DOMAIN or not str(exc):
        return f"error: {exc}" if str(exc) else f"error: {name}"
    return f"error: {name}: {exc}"


class Terminal:
    """REPL over ControlAPI; injectable stdin/stdout for testing."""

    def __init__(self, api: ControlAPI, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> None:
        self.api = api
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self.running = False

    async def run(self) -> None:
        """Main loop; api.stop() and 'bye.' run on every exit path."""
        self.running = True
        self._print(BANNER)
        try:
            while self.running:
                self._print("slaughtermudae> ", end="")
                try:
                    line = self._in.readline()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    break
                if not await self.execute(line.strip()):
                    break
        finally:
            try:
                await self.api.stop()
            except Exception:
                pass  # never let a stop failure eat the goodbye
            self._print("bye.")

    def _print(self, *args, **kwargs) -> None:
        print(*args, file=self._out, **kwargs)

    async def execute(self, line: str) -> bool:
        """Run one command line; returns False when the REPL should exit."""
        try:
            text = await run_command(self.api, line)
            if text:
                self._print(text)
        except QuitRequested:
            return False
        except KeyboardInterrupt:
            return False  # interrupted command ends the session cleanly
        except Exception as exc:
            self._print(render_error(exc))
        return True


__all__ = ["Terminal", "BANNER", "help_text", "render_error"]
