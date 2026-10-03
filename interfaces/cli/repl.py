"""Interactive REPL + one-shot mode for the CLI surface (spec §13).

Thin shell over :mod:`interfaces.cli.commands` — the shared engine — with the
error rendering and every-exit-path shutdown ported from the audited old
``cli/terminal.py``:

  * ``api.shutdown()`` and the ``bye.`` line run on EVERY exit path (quit,
    EOF, Ctrl-C at the prompt, Ctrl-C mid-command);
  * command failures NEVER leak tracebacks — exactly one ``error: ...`` line
    each, with the exception type name kept only for non-domain errors;
  * stdout goes ONLY through the injected stream (no logging side effects).

Decisions for things INTEGRATION.md left open:
  * ``run_once`` is a coroutine (``run_command`` awaits the async facade, so
    it must run inside the caller's event loop — typically
    ``asyncio.run(run_once(api, argv))`` from ``main.py``);
  * ``run_once`` does NOT call ``api.shutdown()`` — the one-shot lifecycle is
    owned by ``main.py``; ``quit`` there just prints ``bye.`` and returns 0.
"""
from __future__ import annotations

import asyncio
import shlex
import sys
import threading
from typing import Any, Optional, TextIO

from core.errors import (
    AccountError,
    CredentialsError,
    PluginError,
    ProfileError,
    RatePolicyError,
    SchedulerError,
    SettingsValidationError,
    UnsafePolicyError,
)

from .commands import QuitRequested, UsageError, help_text, run_command

__all__ = ["Repl", "run_once", "render_error", "PROMPT", "BANNER"]

PROMPT = "sm> "
BANNER = "SlaughterMudae control terminal — 'help' for commands, 'quit' to exit."

#: Domain families already carry a human-readable message: rendered bare.
#: Everything else keeps its exception type name so bugs stay identifiable.
_DOMAIN_ERRORS = (
    UsageError,
    AccountError,
    PluginError,
    ProfileError,
    SettingsValidationError,
    RatePolicyError,
    UnsafePolicyError,
    SchedulerError,
    CredentialsError,
    ValueError,
    RuntimeError,
)


def render_error(exc: BaseException) -> str:
    """One-line error text; keeps the type name for non-domain exceptions."""
    name = type(exc).__name__
    message = " ".join(str(exc).split())  # a message with \n stays ONE line
    if isinstance(exc, _DOMAIN_ERRORS) or not message:
        return f"error: {message}" if message else f"error: {name}"
    return f"error: {name}: {message}"


class Repl:
    """Interactive loop over the ControlAPI; injectable stdin/stdout for tests."""

    def __init__(
        self,
        api: Any,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Wire the facade plus the (injectable) standard streams.

        ``stop_event`` (optional) ends the loop promptly when set — the
        host surface (main.py's SIGINT/SIGTERM handlers) sets it because
        ``loop.add_signal_handler`` suppresses the KeyboardInterrupt the
        old cancellation path relied on.
        """
        self.api = api
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._stop_event = stop_event
        self.running = False

    async def run(self) -> None:
        """Main loop; ``api.shutdown()`` and ``bye.`` run on every exit path.

        Input is read by a DAEMON reader thread feeding an asyncio queue:
        a synchronous readline() on the loop thread would freeze the entire
        event loop at the prompt (dashboard 504s, WS stalls, gateway
        heartbeats starve) — the exact failure the queue hop prevents.
        Ctrl-C still works two ways: when the host installed a signal
        handler it passes the stop event in here (the loop wakes on it),
        and without one SIGINT raises KeyboardInterrupt in the main thread,
        which is parked in ``await`` and unwinds through the cancellation
        path below.
        """
        self.running = True
        self._write(BANNER)
        loop = asyncio.get_running_loop()
        lines: "asyncio.Queue[str]" = asyncio.Queue()
        reader_done = loop.create_future()
        stop_wait = (
            asyncio.ensure_future(self._stop_event.wait())
            if self._stop_event is not None else None
        )

        def _reader() -> None:
            try:
                while True:
                    try:
                        line = self._in.readline()
                    except (EOFError, KeyboardInterrupt, UnicodeError):
                        line = ""
                    try:
                        loop.call_soon_threadsafe(lines.put_nowait, line)
                    except RuntimeError:  # loop closed: process exiting
                        return
                    if not line:
                        return
            finally:
                try:
                    loop.call_soon_threadsafe(_finish_reader)
                except RuntimeError:
                    pass

        def _finish_reader() -> None:
            if not reader_done.done():
                reader_done.set_result(None)

        threading.Thread(
            target=_reader, name="repl-reader", daemon=True
        ).start()
        try:
            while self.running:
                if stop_wait is not None and stop_wait.done():
                    break
                self._write(PROMPT, end="")
                get = asyncio.ensure_future(lines.get())
                watched = {get, reader_done}
                if stop_wait is not None:
                    watched.add(stop_wait)
                done, _ = await asyncio.wait(
                    watched, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_wait is not None and stop_wait in done:
                    get.cancel()  # host signalled exit (SIGINT/SIGTERM)
                    break
                if get not in done:
                    get.cancel()  # reader died (EOF seen): nothing more
                    break
                line = get.result()
                if not line:  # EOF
                    break
                if not await self.execute(line.strip()):
                    break
        except KeyboardInterrupt:
            pass  # Ctrl-C anywhere ends the session cleanly
        finally:
            if stop_wait is not None and not stop_wait.done():
                stop_wait.cancel()
            try:
                await self.api.shutdown()
            except Exception:
                pass  # never let a shutdown failure eat the goodbye
            self._write("bye.")
            self.running = False

    async def execute(self, line: str) -> bool:
        """Run one command line; returns False when the REPL should exit."""
        try:
            text = await run_command(self.api, line)
            if text:
                self._write(text)
        except QuitRequested:
            return False
        except KeyboardInterrupt:
            return False  # interrupted command ends the session cleanly
        except Exception as exc:
            self._write(render_error(exc))
        return True

    def _write(self, text: str, end: str = "\n") -> None:
        print(text, end=end, file=self._out)


async def run_once(api: Any, argv: list, stdout: Optional[TextIO] = None) -> int:
    """One-shot mode: run a single command line built from ``argv``.

    ``argv == []`` prints the help text and returns 0. Returns 0 on success
    (and for ``quit`` — printing ``bye.``), 1 when the command raised.
    """
    out = stdout or sys.stdout
    if not argv:
        out.write(help_text() + "\n")
        return 0
    try:
        text = await run_command(api, shlex.join(argv))
    except QuitRequested:
        out.write("bye.\n")
        return 0
    except Exception as exc:
        out.write(render_error(exc) + "\n")
        return 1
    if text:
        out.write(text + "\n")
    return 0
