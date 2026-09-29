"""GUI dashboard backend: stdlib JSON API over the shared ControlAPI.

Endpoints (all JSON; POST bodies are JSON):
  GET  /api/status                      -> {ok, data}
  GET  /api/plugins                     -> {ok, data}
  GET  /api/limits                      -> {ok, data}
  POST /api/start      {account?}       -> {ok, data}
  POST /api/stop       {account?}       -> {ok, data}
  POST /api/plugin     {account, plugin, enabled} -> {ok, data}
  POST /api/limit      {action, rule, account?}   -> {ok, data}
  POST /api/clear-limit {account, action}         -> {ok, data}

Auth: if DASHBOARD_TOKEN is set in .env, requests must send header
X-Dashboard-Token with the same value. Serves on 127.0.0.1 by default.

A browser frontend (Phase 4) can be dropped in later against these endpoints.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from core.api import ControlAPI

LOG = logging.getLogger("mudae.dashboard")


class DashboardServer:
    """HTTP JSON facade over ControlAPI, running on a daemon thread."""

    def __init__(
        self,
        api: ControlAPI,
        token: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 8070,
    ) -> None:
        self._api = api
        self._token = token
        self._host, self._port = host, port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._server_ready = threading.Event()

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> "DashboardServer":
        """Start serving (call from inside the running asyncio loop)."""
        self._loop = asyncio.get_running_loop()
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        self._server_ready.set()
        LOG.info("dashboard listening on %s (auth=%s)", self.url, "on" if self._token else "OFF")
        return self

    def stop(self) -> None:
        """Shut the HTTP server down."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None

    # ------------------------------------------------------------- internals
    def call(self, method: str, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Execute an API call on the main loop; returns (http_status, payload)."""
        if self._loop is None or self._loop.is_closed():
            return 503, {"ok": False, "error": "main loop unavailable"}
        future = asyncio.run_coroutine_threadsafe(self._route(method, path, body), self._loop)
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return 500, {"ok": False, "error": str(exc)}

    async def _route(self, method: str, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Map request -> ControlAPI call. Both surfaces share these semantics."""
        api = self._api
        try:
            if (method, path) == ("GET", "/api/status"):
                return 200, {"ok": True, "data": await api.status()}
            if (method, path) == ("GET", "/api/plugins"):
                return 200, {"ok": True, "data": await api.plugins()}
            if (method, path) == ("GET", "/api/limits"):
                return 200, {"ok": True, "data": await api.limits()}
            if (method, path) == ("POST", "/api/start"):
                return 200, {"ok": True, "data": await api.start(body.get("account"))}
            if (method, path) == ("POST", "/api/stop"):
                return 200, {"ok": True, "data": await api.stop(body.get("account"))}
            if (method, path) == ("POST", "/api/plugin"):
                return 200, {
                    "ok": True,
                    "data": await api.set_plugin_enabled(
                        body["account"], body["plugin"], bool(body["enabled"])
                    ),
                }
            if (method, path) == ("POST", "/api/limit"):
                return 200, {
                    "ok": True,
                    "data": await api.set_limit(
                        body["action"], body["rule"], body.get("account")
                    ),
                }
            if (method, path) == ("POST", "/api/clear-limit"):
                return 200, {
                    "ok": True,
                    "data": await api.clear_limit(body["account"], body["action"]),
                }
            return 404, {"ok": False, "error": f"no route {method} {path}"}
        except Exception as exc:
            return 400, {"ok": False, "error": str(exc)}


def _make_handler(server: "DashboardServer") -> type:
    """Build the request-handler class closing over the server instance."""

    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if server._token is None:
                return True
            return self.headers.get("X-Dashboard-Token") == server._token

        def _respond(self, status: int, payload: Dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            if not self._authorized():
                self._respond(401, {"ok": False, "error": "bad or missing X-Dashboard-Token"})
                return
            body: Dict[str, Any] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except json.JSONDecodeError:
                        self._respond(400, {"ok": False, "error": "invalid JSON body"})
                        return
            status, payload = server.call(method, self.path.split("?")[0], body)
            self._respond(status, payload)

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            self._handle("POST")

        def log_message(self, fmt: str, *args: Any) -> None:  # route to our logger
            LOG.debug("%s - %s", self.address_string(), fmt % args)

    return Handler
