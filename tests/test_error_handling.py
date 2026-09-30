"""Regression tests for the error-handling audit round (agents 16-23)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cli.commands import UsageError, run_command
from cli.terminal import Terminal
from core.api import ControlAPI
from core.audit import AuditRecorder
from core.container import ContainerError
from core.database import Database
from core.roles import RoleRegistry

from .helpers import Harness

TOKEN = "err-audit-token"


class ApiSurface:
    def __init__(self, tmp: str) -> None:
        self.database = Database(Path(tmp) / "s.db")
        self.roles = RoleRegistry(self.database)
        self.roles.set_role("copier", ["spy"])
        self.audit = AuditRecorder(self.database)
        self.harness = Harness(
            "tests/fixtures/plugins", ["alpha"], state=self.database,
            roles=self.roles, audit=self.audit,
        )
        self.api = ControlAPI(
            self.harness.container, self.harness.limiter, self.harness.loader,
            self.harness.credentials, state=self.database, roles=self.roles,
            audit=self.audit,
        )


class TestStrictBoolAndFields(unittest.IsolatedAsyncioTestCase):
    async def test_string_false_no_longer_enables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(ValueError) as ctx:
                await surface.api.set_plugin_enabled("alpha", "spy", "false")
            self.assertIn("boolean", str(ctx.exception))

    async def test_roles_string_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(ContainerError) as ctx:
                await surface.api.add_account({"name": "zed", "roles": "copier"})
            self.assertIn("list of strings", str(ctx.exception))

    async def test_unknown_payload_keys_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(ContainerError) as ctx:
                await surface.api.add_account({"name": "zed", "rolez": ["copier"]})
            self.assertIn("rolez", str(ctx.exception))

    async def test_assign_unknown_role_rejected_with_known_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(ContainerError) as ctx:
                await surface.api.assign_role("alpha", "ghost", True)
            self.assertIn("ghost", str(ctx.exception))
            self.assertIn("copier", str(ctx.exception))

    async def test_add_account_db_failure_rolls_back_runtime(self) -> None:
        class Bomber:
            def upsert_account(self, config):
                raise RuntimeError("disk full")

        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            surface.api._state = Bomber()
            with self.assertRaises(RuntimeError):
                await surface.api.add_account({"name": "zed"})
            self.assertNotIn("zed", surface.harness.container.account_names())

    async def test_remove_account_reports_implicit_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.start("alpha")
            result = await surface.api.remove_account("alpha")
            self.assertTrue(result["was_running"])
            self.assertEqual(result["message"], "stopped and removed")


class TestVisibilityFixes(unittest.IsolatedAsyncioTestCase):
    async def test_status_includes_last_error_and_plugin_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            await surface.api.start("alpha")
            plugin = surface.harness.container._accounts["alpha"].plugins["spy"]

            async def explosive(ctx, event):
                raise RuntimeError("plugin boom")

            plugin.instance.handle_event = explosive
            surface.harness.world.deliver(500, surface.harness.owner_id, "boom")
            await asyncio.sleep(0.1)
            status = (await surface.api.status())["accounts"][0]
            # ERROR AUDIT: counters may include the ReadyEvent dispatch; the
            # boom crash must be the LAST recorded error
            self.assertGreaterEqual(status["plugins"]["spy"]["error_count"], 1)
            self.assertIn("plugin boom", status["plugins"]["spy"]["last_error"])
            self.assertIn("last_error", status)  # session-level key present
            await surface.api.stop("alpha")

    async def test_session_failure_reason_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            harness = surface.harness
            real_factory = harness.container._client_factory

            def failing_factory(config, credentials):
                raise RuntimeError("bad token")

            harness.container._client_factory = failing_factory
            with self.assertRaises(Exception):
                await surface.api.start("alpha")
            status = (await surface.api.status())["accounts"][0]
            self.assertEqual(status["status"], "failed")
            self.assertIn("bad token", status["last_error"])

    async def test_collect_messages_rejects_bad_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(ValueError):
                await surface.harness.container.collect_messages("alpha", lambda e: True, 0)

    async def test_unknown_account_message_lists_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(ContainerError) as ctx:
                await surface.api.start("ghost")
            self.assertIn("registered", str(ctx.exception))
            self.assertIn("alpha", str(ctx.exception))


class TestCliErrorHandling(unittest.IsolatedAsyncioTestCase):
    async def test_set_delay_non_numeric_is_usageerror_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(UsageError) as ctx:
                await run_command(surface.api, "set-delay message abc 2.0")
            self.assertIn("not a number", str(ctx.exception))
            self.assertIn("set-delay", str(ctx.exception))

    async def test_unclosed_quote_is_usageerror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(UsageError) as ctx:
                await run_command(surface.api, "set-limit 'message")
            self.assertIn("quotes", str(ctx.exception))

    async def test_audit_negative_n_is_usageerror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            with self.assertRaises(UsageError) as ctx:
                await run_command(surface.api, "audit -5")
            self.assertIn("non-negative", str(ctx.exception))

    async def test_help_mentions_error_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            text = await run_command(surface.api, "help")
            self.assertIn("never exit", text)

    async def test_terminal_stops_accounts_on_every_exit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            import io

            stops: list = []

            async def fake_stop(account=None):
                stops.append(account)

            surface.api.stop = fake_stop
            out = io.StringIO()
            term = Terminal(surface.api, stdin=io.StringIO("details ghost\nquit\n"), stdout=out)
            await term.run()
            text = out.getvalue()
            self.assertIn("error:", text)          # one sane line, no traceback
            self.assertNotIn("Traceback", text)
            self.assertIn("bye.", text)
            self.assertEqual(len(stops), 1)        # stop always runs

    async def test_terminal_stops_when_command_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            surface = ApiSurface(tmp)
            import io

            stops: list = []

            async def fake_stop(account=None):
                stops.append(account)

            surface.api.stop = fake_stop

            async def interrupted_command(api, line):
                raise KeyboardInterrupt

            import cli.terminal as terminal_mod
            original = terminal_mod.run_command
            terminal_mod.run_command = interrupted_command
            try:
                out = io.StringIO()
                term = Terminal(surface.api, stdin=io.StringIO("status\n"), stdout=out)
                await term.run()
            finally:
                terminal_mod.run_command = original
            self.assertEqual(len(stops), 1)        # Ctrl-C path also stops


class TestServerErrorContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.surface = ApiSurface(self.tmp.name)
        from dashboard.server import DashboardServer

        self.server = DashboardServer(self.surface.api, token=TOKEN, port=0)
        self.server.start()
        self.port = self.server._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    def _post(self, path: str, raw: str, token: str = TOKEN):
        def call():
            request = urllib.request.Request(
                f"{self.base}{path}", data=raw.encode(),
                headers={"Content-Type": "application/json", "X-Dashboard-Token": token},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        return asyncio.to_thread(call)

    async def test_missing_fields_have_actionable_messages(self) -> None:
        status, body = await self._post("/api/plugin", json.dumps({"account": "alpha"}))
        self.assertEqual(status, 400)
        self.assertIn("missing required field 'plugin'", body["error"])
        self.assertIn("/api/plugin", body["error"])

    async def test_non_object_body_rejected_cleanly(self) -> None:
        status, body = await self._post("/api/start", "[1,2]")
        self.assertEqual(status, 400)
        self.assertIn("JSON object", body["error"])

    async def test_string_false_rejected_not_inverted(self) -> None:
        status, body = await self._post(
            "/api/plugin", json.dumps({"account": "alpha", "plugin": "spy", "enabled": "false"})
        )
        self.assertEqual(status, 400)
        self.assertIn("boolean", body["error"])

    async def test_role_string_plugins_rejected(self) -> None:
        status, body = await self._post("/api/role", json.dumps({"name": "r", "plugins": "spy"}))
        self.assertEqual(status, 400)
        self.assertIn("list of plugin names", body["error"])

    async def test_bad_delay_number_field_named(self) -> None:
        status, body = await self._post(
            "/api/delay", json.dumps({"action": "message", "min": ["0.8"], "max": "2"})
        )
        self.assertEqual(status, 400)
        self.assertIn("'min'", body["error"])
        self.assertIn("number", body["error"])

    async def test_body_too_large_rejected(self) -> None:
        status, body = await self._post("/api/setting", "x" * 1_100_000)
        self.assertEqual(status, 413)
        self.assertIn("too large", body["error"])

    async def test_account_list_payload_rejected(self) -> None:
        status, body = await self._post("/api/start", json.dumps({"account": ["alpha"]}))
        self.assertEqual(status, 400)
        self.assertIn("must be a string", body["error"])


class TestWsProtocolEnforcement(unittest.TestCase):
    def test_incomplete_vs_violation_distinction(self) -> None:
        from dashboard.ws import IncompleteFrame, ProtocolViolation, decode_frame

        with self.assertRaises(IncompleteFrame):
            decode_frame(b"\x81")
        # unmasked client frame -> violation
        with self.assertRaises(ProtocolViolation):
            decode_frame(bytes([0x81, 0x02]) + b"hi")
        # RSV bit set -> violation
        with self.assertRaises(ProtocolViolation):
            decode_frame(bytes([0xC1, 0x80, 1, 2, 3, 4]) + b"ab")

    def test_oversized_frame_claim_rejected(self) -> None:
        import struct

        from dashboard.ws import ProtocolViolation, decode_frame

        header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 2**40) + b"\x01\x02\x03\x04"
        with self.assertRaises(ProtocolViolation):
            decode_frame(header)


if __name__ == "__main__":
    unittest.main()
