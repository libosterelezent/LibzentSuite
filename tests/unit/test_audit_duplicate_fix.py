"""Audit-page duplicate-line fix: attach_audit must be idempotent.

DashboardServer.start() auto-attaches the api's audit recorder
(_attach_audit_recorder), and main.py attaches it manually — the second
subscription broadcast every audit entry twice, rendering duplicated
lines on the Audit page (and leaking the first unsubscribe handle).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace as NS

from core.audit import AuditRecorder

from interfaces.dashboard.server import DashboardServer


def _fake_api(audit=None):
    return NS(
        status=lambda *a, **k: NS(),
        audit_recorder=lambda: audit,
        logs_service=lambda: NS(subscribe=lambda cb: (lambda: None),
                                 stats=lambda: {}),
        dashboard_token=None,
    )


class AttachAuditIdempotentTests(unittest.IsolatedAsyncioTestCase):
    async def _server(self, audit=None) -> DashboardServer:
        server = DashboardServer(
            _fake_api(audit=audit), token="t", port=0)
        server.start()
        self.addAsyncCleanup(server.stop)
        return server

    async def test_double_attach_subscribes_once(self) -> None:
        recorder = AuditRecorder(db_repo=None)
        server = await self._server(recorder)
        self.assertEqual(len(recorder._subscribers), 1)  # auto-attach
        server.attach_audit(recorder)  # the main.py duplicate (pre-fix: 2)
        self.assertEqual(len(recorder._subscribers), 1)

    async def test_different_recorder_swaps_cleanly(self) -> None:
        recorder = AuditRecorder(db_repo=None)
        other = AuditRecorder(db_repo=None)
        server = await self._server(recorder)
        server.attach_audit(other)
        self.assertEqual(len(recorder._subscribers), 0)  # old unsubscribed
        self.assertEqual(len(other._subscribers), 1)

    async def test_no_recorder_no_subscription(self) -> None:
        server = await self._server(audit=None)
        self.assertIsNone(server._audit_unsub)


if __name__ == "__main__":
    unittest.main()
