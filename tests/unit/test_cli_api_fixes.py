"""Regression suite for the audited CLI/API/logging/audit fixes.

Covers, one class per fix:
  (a) ControlAPI.accounts_remove honors the manager's recreated verdict —
      the name-keyed backstop cascades (credentials/cooldowns/access) and
      the 'mgmt:account.removed' audit row are all SKIPPED, and an
      abort-specific audit row is written instead;
  (b) the new 'plugins clear' verb reaches api.plugins_clear_setting with
      the right positional args;
  (c) format_effective renders a None interval as the unpaced marker
      (never the bare string 'None');
  (d) StdlibBridge renders exc_info tracebacks into the stored message;
  (e) core.audit.format_audit escapes newlines in note/account/action;
  (f) the REPL prompt is flushed before the reader can accept input.
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import time
import unittest
from typing import Any

from core.api import ControlAPI
from core.audit import format_audit
from core.logging_service import LogService, StdlibBridge
from interfaces.cli.commands import format_effective, run_command
from interfaces.cli.repl import Repl


# ---------------------------------------------------------------- helpers
class FakeAccountsManager:
    """Duck-typed AccountManager stub returning a canned remove verdict."""

    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict

    async def remove_account(self, name: str) -> dict:
        return dict(self._verdict)


class RecordingService:
    """Records every call; raises nothing (deletes must be observable)."""

    def __init__(self, *method_names: str) -> None:
        self.calls: list[tuple] = []
        for method in method_names:
            async def stub(*args, _m=method, **kwargs):
                self.calls.append((_m, args, kwargs))
            setattr(self, method, stub)


class RecordingAudit:
    """Duck-typed AuditRecorder recording record() calls."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def record(self, **kwargs: Any) -> dict:
        self.entries.append(kwargs)
        return dict(kwargs)


class _Manager:
    """Least-duck-typed stand-in for the ctor-required collaborators."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected manager call: {name}")


def build_api(**overrides: Any) -> ControlAPI:
    """A ControlAPI over inert stubs; overrides replace any collaborator."""
    kwargs = dict(
        accounts=_Manager(),
        plugins=_Manager(),
        settings=_Manager(),
        limiter=_Manager(),
        scheduler=_Manager(),
        logs=_Manager(),
    )
    kwargs.update(overrides)
    return ControlAPI(**kwargs)


class FakeClearApi:
    """Tiny ControlAPI double for the 'plugins clear' verb test."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def plugins_clear_setting(self, account, plugin, key):
        self.calls.append((account, plugin, key))
        return {"account": account, "plugin": plugin, "key": key,
                "cleared": True, "value": "default"}


# --------------------------------------------------- (a) accounts_remove
class TestAccountsRemoveRecreated(unittest.IsolatedAsyncioTestCase):
    async def test_recreated_verdict_skips_every_backstop_cascade(self) -> None:
        creds = RecordingService("delete")
        cooldowns = RecordingService("delete_account")
        access = RecordingService("forget_account")
        audit = RecordingAudit()
        api = build_api(
            accounts=FakeAccountsManager(
                {"removed": False, "name": "alpha", "recreated": True}),
            credentials_svc=creds,
            cooldowns=cooldowns,
            access=access,
            audit=audit,
        )
        out = await api.accounts_remove("alpha", surface="cli")
        self.assertEqual(out["recreated"], True)
        self.assertEqual(creds.calls, [])
        self.assertEqual(cooldowns.calls, [])
        self.assertEqual(access.calls, [])
        actions = [entry.get("action") for entry in audit.entries]
        self.assertIn("mgmt:account.remove-aborted", actions)
        self.assertNotIn("mgmt:account.removed", actions)

    async def test_successful_remove_still_cascades(self) -> None:
        creds = RecordingService("delete")
        cooldowns = RecordingService("delete_account")
        access = RecordingService("forget_account")
        audit = RecordingAudit()
        api = build_api(
            accounts=FakeAccountsManager(
                {"removed": "alpha", "existed": True}),
            credentials_svc=creds,
            cooldowns=cooldowns,
            access=access,
            audit=audit,
        )
        out = await api.accounts_remove("alpha")
        self.assertEqual(out["removed"], "alpha")
        self.assertEqual([c[0] for c in creds.calls], ["delete"])
        self.assertEqual([c[0] for c in cooldowns.calls], ["delete_account"])
        self.assertEqual([c[0] for c in access.calls], ["forget_account"])
        actions = [entry.get("action") for entry in audit.entries]
        self.assertIn("mgmt:account.removed", actions)
        self.assertNotIn("mgmt:account.remove-aborted", actions)


# ------------------------------------------------- (b) plugins clear verb
class TestPluginsClearVerb(unittest.IsolatedAsyncioTestCase):
    async def test_clear_reaches_plugins_clear_setting(self) -> None:
        api = FakeClearApi()
        text = await run_command(api, "plugins clear alpha exporter trigger")
        self.assertEqual(
            api.calls, [("alpha", "exporter", "trigger")])
        self.assertIn("cleared", text)

    async def test_clear_usage_errors(self) -> None:
        from interfaces.cli.commands import UsageError

        for line in ("plugins clear a p", "plugins clear a p k extra",
                     "plugins clear"):
            with self.subTest(line=line), self.assertRaises(UsageError):
                await run_command(FakeClearApi(), line)

    async def test_help_advertises_clear(self) -> None:
        from interfaces.cli.commands import help_text

        text = help_text()
        self.assertIn("clear <account> <plugin> <key>", text)


# ---------------------------------------------- (c) unpaced None interval
class TestFormatEffectiveUnpaced(unittest.TestCase):
    def test_none_interval_renders_unpaced_marker(self) -> None:
        text = format_effective({"action": "message", "interval": None,
                                 "jitter": None, "sources": []})
        self.assertIn("- (unpaced)", text)
        self.assertNotIn("None", text)

    def test_none_source_interval_renders_dash(self) -> None:
        text = format_effective({
            "action": "message", "interval": None, "jitter": None,
            "sources": [{"level": "global", "owner": None, "interval": None}],
        })
        self.assertIn("- (unpaced)", text.splitlines()[0])
        self.assertIn("-", text.splitlines()[1])
        self.assertNotIn("0.00s", text)


# --------------------------------------- (d) StdlibBridge exc_info render
class TestStdlibBridgeTraceback(unittest.TestCase):
    def test_exc_info_traceback_lands_in_the_entry_message(self) -> None:
        service = LogService()
        bridge = StdlibBridge(service)
        try:
            raise ValueError("kaboom")
        except ValueError:
            record = logging.LogRecord(
                "libzent.core", logging.ERROR, "f", 1,
                "it broke", None, sys.exc_info())
        bridge.emit(record)
        message = service.tail(1)[0]["message"]
        self.assertIn("it broke", message)
        self.assertIn("Traceback (most recent call last)", message)
        self.assertIn("ValueError: kaboom", message)

    def test_plain_records_are_unchanged(self) -> None:
        service = LogService()
        bridge = StdlibBridge(service)
        record = logging.LogRecord(
            "libzent.core", logging.INFO, "f", 1, "fine", None, None)
        bridge.emit(record)
        self.assertEqual(service.tail(1)[0]["message"], "fine")


# -------------------------------------- (e) audit newline escaping (forgery)
class TestAuditNewlineEscaping(unittest.TestCase):
    TS = 1700000000.5

    def test_note_and_account_newlines_are_escaped(self) -> None:
        lines = format_audit([
            {"ts": self.TS, "account": "evil\naccount", "direction": "out",
             "action": "message\nFORGED", "channel_id": 1, "content": "x",
             "note": "surface=cli\nplugin=roll"},
        ])
        self.assertEqual(len(lines), 1)  # one entry -> ONE rendered line
        self.assertIn("evil\\naccount", lines[0])
        self.assertIn("message\\nFORGED", lines[0])
        self.assertIn("surface=cli\\nplugin=roll", lines[0])
        self.assertNotIn("\nFORGED", lines[0].replace("\\n", ""))  # no raw \n


# ------------------------------------------------ (f) REPL prompt flushes
class TtyLikeOutput(io.StringIO):
    """Simulates a tty: writes stay INVISIBLE until flush() runs."""

    def __init__(self) -> None:
        super().__init__()
        self._visible = io.StringIO()

    def write(self, text: str) -> int:  # buffer only — a tty needs a flush
        super().write(text)
        return len(text)

    def flush(self) -> None:
        pending = self.getvalue()
        self.seek(0)
        self.truncate(0)
        self._visible.write(pending)

    def visible(self) -> str:
        return self._visible.getvalue()


class PromptAwareStdin:
    """readline() blocks until the prompt is VISIBLE (flushed) — exactly
    what a human at a terminal experiences — then feeds one line."""

    def __init__(self, out: TtyLikeOutput, *lines: str) -> None:
        self._out = out
        self._lines = list(lines)

    def readline(self) -> str:
        deadline = time.monotonic() + 5.0
        while "sm> " not in self._out.visible():
            if time.monotonic() > deadline:
                raise AssertionError(
                    "prompt bytes never became visible before input read")
            time.sleep(0.001)
        return self._lines.pop(0) if self._lines else ""


class _QuitApi:
    async def shutdown(self) -> None:
        pass


class TestReplPromptFlush(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_flushed_before_input_is_read(self) -> None:
        out = TtyLikeOutput()
        stdin = PromptAwareStdin(out, "quit\n")
        repl = Repl(_QuitApi(), stdin=stdin, stdout=out)
        await asyncio.wait_for(repl.run(), timeout=10)
        # the reader only released once the flushed prompt was visible
        self.assertIn("sm> ", out.visible())
        self.assertIn("bye.", out.visible())
