"""Full-screen TUI with live log tailing (curses, standard library only).

Layout:
  ┌ header + accounts status (refreshed every second) ┐
  │ command output pane                                │
  │ live log tail (from the shared RingBufferHandler)  │
  └ input line — same commands as the terminal; :q quits ┘

The curses loop runs in an executor thread while the asyncio loop keeps
serving the ControlAPI; commands hop over via run_coroutine_threadsafe.
Pure formatting helpers (format_* in cli.commands) are unit-tested without
a terminal.
"""
from __future__ import annotations

import asyncio
import curses
import time
from collections import deque
from typing import Optional

from cli.commands import QuitRequested, run_command
from core.api import ControlAPI
from core.logging_setup import RingBufferHandler

POLL_SECONDS = 0.05
STATUS_INTERVAL = 1.0
LOG_LINES = 12
OUTPUT_LINES = 200


class Tui:
    """Curses front-end over ControlAPI with live status + log tailing."""

    def __init__(self, api: ControlAPI, ring: RingBufferHandler) -> None:
        self.api = api
        self.ring = ring
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def run(self) -> None:
        """Run the TUI until the user quits; then stop all accounts.

        AUDIT FIX: api.stop() runs even when curses init fails (bad TERM /
        non-tty) — accounts never survive a broken TUI launch.
        """
        self._loop = asyncio.get_running_loop()
        try:
            await self._loop.run_in_executor(None, self._curses_entry)
        finally:
            await self.api.stop()

    # ---------------------------------------------------------------- curses
    def _curses_entry(self) -> None:
        curses.wrapper(self._screen_main)

    def _screen_main(self, screen: "curses._CursesWindow") -> None:
        curses.curs_set(1)
        screen.nodelay(True)
        input_buf = ""
        output: deque = deque(maxlen=OUTPUT_LINES)
        output.append("type a command ('help'), :q to quit")
        last_status: Optional[list] = None
        last_fetch = 0.0
        running = True
        while running:
            now = time.monotonic()
            if now - last_fetch > STATUS_INTERVAL:
                last_fetch = now
                try:
                    status = self._call(lambda: self.api.status())
                    last_status = [f"pending waiters: {status['limits'].get('pending') or '-'}"] + [
                        line for line in _status_rows(status)
                    ]
                except Exception as exc:
                    output.append(f"status error: {exc}")
            screen.erase()
            height, width = screen.getmaxyx()
            y = 0
            y = self._draw_block(screen, y, width, [_header()])
            y = self._draw_block(screen, y, width, last_status or ["(waiting for first status…)"])
            log_h = min(LOG_LINES, max(3, height // 4))
            out_h = max(1, height - y - log_h - 2)
            y = self._draw_block(screen, y, width, list(output)[-out_h:])
            y = self._draw_block(screen, y, width, self.ring.tail(LOG_LINES)[-log_h:])
            try:
                screen.addstr(min(y, height - 1), 0, f"> {input_buf}"[: max(width - 1, 1)])
            except curses.error:
                pass
            screen.refresh()
            ch = screen.getch()
            if ch in (10, curses.KEY_ENTER):
                command = input_buf.strip()
                input_buf = ""
                if command in (":q", "quit", "exit"):
                    running = False
                    continue
                if command:
                    output.append(f"> {command}")
                    try:
                        text = self._call(lambda c=command: run_command(self.api, c))
                        if text:
                            output.extend(text.splitlines())
                    except QuitRequested:
                        running = False
                    except Exception as exc:
                        output.append(f"error: {exc}")
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                input_buf = input_buf[:-1]
            elif ch == 3:  # CTRL-C
                running = False
            elif ch != curses.ERR and 32 <= ch < 127:
                input_buf += chr(ch)
            time.sleep(POLL_SECONDS)

    def _draw_block(self, screen, y: int, width: int, lines: list) -> int:
        for line in lines:
            if y >= screen.getmaxyx()[0] - 1:
                break
            try:
                screen.addstr(y, 0, str(line)[: max(width - 1, 1)])
            except curses.error:
                pass
            y += 1
        return y

    def _call(self, coro_factory):
        """Hop from the TUI thread onto the asyncio loop (blocking, bounded)."""
        future = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        return future.result(timeout=10)


def _header() -> str:
    return "SlaughterMudae TUI — commands: help status plugins limits start/stop set-limit …  :q quits"


def _status_rows(status: dict) -> list:
    from cli.commands import format_status_lines

    return format_status_lines(status)[1:]  # first line = pending waiters


__all__ = ["Tui"]
