"""Interactive terminal management interface (one of the two control surfaces).

Thin shell over cli/commands.py (shared with the TUI) so every surface stays
feature-equal. Type 'help' for the command list.
"""
from __future__ import annotations

import sys
from typing import Optional, TextIO

from core.api import ControlAPI

from .commands import QuitRequested, help_text, run_command

BANNER = """SlaughterMudae terminal — 'help' for commands."""


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
            self._print("slaughtermudae> ", end="")
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
        try:
            text = await run_command(self.api, line)
            if text:
                self._print(text)
        except QuitRequested:
            return False
        except Exception as exc:
            self._print(f"error: {exc}")
        return True


__all__ = ["Terminal", "BANNER", "help_text"]
