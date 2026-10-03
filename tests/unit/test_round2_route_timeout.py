"""Round-2 audit pin: dashboard ROUTE_TIMEOUT_S behavior (server.py).

``DashboardServer.call`` funnels every HTTP route through the main loop
with a hard wait (ROUTE_TIMEOUT_S, 10s). Agent 10 flagged the sim-run
route (POST /api/simulation/run awaits the whole run inline): a scenario
longer than the budget answers 504 while the run may still complete.
These pins hold the OBSERVED contract so a regression in the guard
itself (504 degrading to an empty 500, or the loop being wedged by the
slow coroutine) cannot slip through: the timeout is patched DOWN so the
pin runs in milliseconds, not 10s.
"""
from __future__ import annotations

import asyncio
import unittest

import interfaces.dashboard.server as server_module
from interfaces.dashboard.server import DashboardServer


class SlowStatusAPI:
    """Duck-typed ControlAPI: /api/status with an controllable delay."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.finished = False

    async def status(self) -> dict:
        await asyncio.sleep(self.delay)
        self.finished = True
        return {"accounts": [], "uptime_s": 1.0}


class TestRouteTimeout(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.api = SlowStatusAPI(delay=0.5)
        self.server = DashboardServer(self.api, port=0)
        self.server.start()

    async def asyncTearDown(self) -> None:
        self.server.stop()

    async def test_slow_route_answers_504_not_500(self) -> None:
        old = server_module.ROUTE_TIMEOUT_S
        server_module.ROUTE_TIMEOUT_S = 0.05
        try:
            status, payload = await asyncio.to_thread(
                self.server.call, "GET", "/api/status", {})
        finally:
            server_module.ROUTE_TIMEOUT_S = old
        self.assertEqual(status, 504)
        self.assertFalse(payload["ok"])
        self.assertIn("timed out", payload["error"])

    async def test_timed_out_route_does_not_wedge_the_loop(self) -> None:
        old = server_module.ROUTE_TIMEOUT_S
        server_module.ROUTE_TIMEOUT_S = 0.05
        try:
            await asyncio.to_thread(self.server.call, "GET", "/api/status", {})
        finally:
            server_module.ROUTE_TIMEOUT_S = old

        self.api.delay = 0.01
        status, payload = await asyncio.to_thread(
            self.server.call, "GET", "/api/status", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    async def test_504_means_may_still_complete(self) -> None:
        old = server_module.ROUTE_TIMEOUT_S
        server_module.ROUTE_TIMEOUT_S = 0.05
        try:
            await asyncio.to_thread(self.server.call, "GET", "/api/status", {})
        finally:
            server_module.ROUTE_TIMEOUT_S = old
        self.assertFalse(self.api.finished)  # not cancelled by the 504
        for _ in range(100):  # bounded poll: the coroutine still finishes
            if self.api.finished:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.api.finished)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
