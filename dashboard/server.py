"""GUI dashboard backend: browser frontend + JSON REST + WebSocket live push.

Routes:
  GET  /                      -> static single-page app (dashboard/static/index.html)
  GET  /api/status            -> {ok, data}
  GET  /api/plugins           -> {ok, data}
  GET  /api/account-plugins?account=X -> {ok, data}
  GET  /api/limits            -> {ok, data}
  POST /api/start      {account?}       -> {ok, data}
  POST /api/stop       {account?}       -> {ok, data}
  POST /api/plugin     {account, plugin, enabled} -> {ok, data}
  POST /api/limit      {action, rule, account?}   -> {ok, data}
  POST /api/clear-limit {account, action}         -> {ok, data}
  GET  /ws[?token=...]        -> WebSocket; server pushes:
          {"type":"stats", "accounts":..., "limits":...}   every second
          {"type":"log", "line":"..."}                     live log tail

Auth: when DASHBOARD_TOKEN is set (see .env), REST requires header
X-Dashboard-Token and the WebSocket requires ?token=<value>. Serves on
127.0.0.1 by default — do not expose publicly without the token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue as thread_queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from core.api import ControlAPI

from . import ws as wsproto

LOG = logging.getLogger("slaughtermudae.dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"
BROADCAST_INTERVAL = 1.0


class WebSocketClient:
    """One browser connection: outbound queue drained by its reader thread."""

    def __init__(self, sock: socket.socket, server: "DashboardServer") -> None:
        self._sock = sock
        self._server = server
        self._sendq: "thread_queue.SimpleQueue[Tuple[str, bytes]]" = thread_queue.SimpleQueue()
        self.alive = True

    def push_text(self, text: str) -> None:
        """Queue a text frame (thread-safe; sent by the owning reader loop)."""
        if self.alive:
            self._sendq.put(("text", wsproto.encode_text_frame(text)))

    def push_json(self, payload: Dict[str, Any]) -> None:
        self.push_text(json.dumps(payload))

    def push_log_line(self, line: str) -> None:
        self.push_json({"type": "log", "line": line})

    def serve(self) -> None:
        """Reader loop (runs in the handler thread that did the upgrade)."""
        self._sock.settimeout(0.25)
        buffer = b""
        try:
            while self.alive:
                self._drain()
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while buffer:
                    try:
                        opcode, _fin, payload, consumed = wsproto.decode_frame(buffer)
                    except ValueError:
                        break  # wait for more bytes
                    buffer = buffer[consumed:]
                    if opcode == wsproto.OP_CLOSE:
                        self._sendq.put(("raw", wsproto.encode_close_frame()))
                        self._drain()
                        return
                    if opcode == wsproto.OP_PING:
                        self._sendq.put(("raw", wsproto.encode_pong(payload)))
                    elif opcode == wsproto.OP_TEXT:
                        self._handle_text(payload.decode("utf-8", "replace"))
        finally:
            self.alive = False
            self._server.unregister_threadsafe(self)
            try:
                self._sock.close()
            except OSError:
                pass

    def _handle_text(self, text: str) -> None:
        """Minimal app-level protocol: 'refresh' forces an immediate push."""
        if text.strip() == "refresh":
            self._server.push_stats_to(self)

    def _drain(self) -> None:
        while True:
            try:
                _kind, frame = self._sendq.get_nowait()
            except thread_queue.Empty:
                return
            try:
                self._sock.sendall(frame)
            except OSError:
                self.alive = False
                return


class DashboardServer:
    """HTTP JSON + WebSocket facade over ControlAPI, on a daemon thread."""

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
        self._clients: List[WebSocketClient] = []
        self._broadcaster: Optional[asyncio.Task] = None
        self._log_unsub: Optional[Any] = None
        self._audit_unsub: Optional[Any] = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "DashboardServer":
        """Start serving (call from inside the running asyncio loop)."""
        self._loop = asyncio.get_running_loop()
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        self._broadcaster = asyncio.get_running_loop().create_task(self._broadcast_loop())
        LOG.info("dashboard listening on %s (auth=%s)", self.url, "on" if self._token else "OFF")
        return self

    def stop(self) -> None:
        """Shut the HTTP server + broadcaster down."""
        if self._broadcaster is not None:
            self._broadcaster.cancel()
            self._broadcaster = None
        if self._log_unsub is not None:
            self._log_unsub()
            self._log_unsub = None
        if self._audit_unsub is not None:
            self._audit_unsub()
            self._audit_unsub = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()  # AUDIT FIX: release the listening socket
            self._httpd = None

    def attach_logs(self, ring) -> "DashboardServer":
        """Stream every new log line from a RingBufferHandler to WS clients."""
        loop = self._loop if self._loop is not None else asyncio.get_running_loop()

        def _on_line(line: str) -> None:
            def _push() -> None:
                for client in list(self._clients):
                    client.push_log_line(line)
            loop.call_soon_threadsafe(_push)

        self._log_unsub = ring.subscribe(_on_line)
        return self

    def attach_audit(self, recorder) -> "DashboardServer":
        """Stream every audited interaction to WS clients (Audit page live view)."""
        loop = self._loop if self._loop is not None else asyncio.get_running_loop()

        def _on_entry(entry) -> None:
            def _push() -> None:
                for client in list(self._clients):
                    client.push_json({"type": "audit", **entry.as_dict()})
            loop.call_soon_threadsafe(_push)

        self._audit_unsub = recorder.subscribe(_on_entry)
        return self

    # ---------------------------------------------------------- registration
    def register_threadsafe(self, client: WebSocketClient) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._register(client), self._loop).result(timeout=2)

    async def _register(self, client: WebSocketClient) -> None:
        self._clients.append(client)
        LOG.info("websocket client connected (%d online)", len(self._clients))
        self.push_stats_to(client)

    def unregister_threadsafe(self, client: WebSocketClient) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._unregister(client), self._loop).result(timeout=2)
        except Exception:
            pass

    async def _unregister(self, client: WebSocketClient) -> None:
        if client in self._clients:
            self._clients.remove(client)
        LOG.info("websocket client gone (%d online)", len(self._clients))

    # ------------------------------------------------------------ broadcast
    def push_stats_to(self, client: WebSocketClient) -> None:
        """Queue a fresh stats snapshot for one client (async fetch, thread-safe)."""
        async def _fetch_and_push() -> None:
            try:
                status = await self._api.status()
                client.push_json({"type": "stats", **status})
            except Exception as exc:  # never kill the broadcaster
                client.push_json({"type": "error", "error": str(exc)})
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(_fetch_and_push(), self._loop)

    async def _broadcast_loop(self) -> None:
        """Push status+limits to every connected client once per second.

        AUDIT FIX: one API failure no longer permanently kills the loop.
        """
        try:
            while True:
                await asyncio.sleep(BROADCAST_INTERVAL)
                if not self._clients:
                    continue
                try:
                    status = await self._api.status()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("broadcast snapshot failed: %s", exc)
                    continue
                payload = json.dumps({"type": "stats", **status})
                for client in list(self._clients):
                    client.push_text(payload)
        except asyncio.CancelledError:
            return

    # ---------------------------------------------------------------- routing
    def call(self, method: str, path: str, body: Dict[str, Any], query: Optional[Dict[str, List[str]]] = None) -> Tuple[int, Dict[str, Any]]:
        """Execute an API call on the main loop; returns (http_status, payload)."""
        if self._loop is None or self._loop.is_closed():
            return 503, {"ok": False, "error": "main loop unavailable"}
        future = asyncio.run_coroutine_threadsafe(
            self._route(method, path, body, query or {}), self._loop
        )
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return 500, {"ok": False, "error": str(exc)}

    async def _route(
        self, method: str, path: str, body: Dict[str, Any], query: Dict[str, List[str]]
    ) -> Tuple[int, Dict[str, Any]]:
        """Map request -> ControlAPI call / static file. Shared by both surfaces."""
        api = self._api
        try:
            if (method, path) == ("GET", "/api/status"):
                return 200, {"ok": True, "data": await api.status()}
            if (method, path) == ("GET", "/api/plugins"):
                return 200, {"ok": True, "data": await api.plugins()}
            if (method, path) == ("GET", "/api/account-plugins"):
                account = (query.get("account") or [""])[0]
                if not account:
                    return 400, {"ok": False, "error": "missing ?account="}
                return 200, {"ok": True, "data": await api.account_plugins(account)}
            if (method, path) == ("GET", "/api/limits"):
                return 200, {"ok": True, "data": await api.limits()}
            if (method, path) == ("GET", "/api/warnings"):
                return 200, {"ok": True, "data": await api.warnings()}
            if (method, path) == ("GET", "/api/audit"):
                limit = int((query.get("limit") or ["100"])[0])
                account = (query.get("account") or [None])[0]
                return 200, {"ok": True, "data": api.audit(limit=limit, account=account)}
            if (method, path) == ("POST", "/api/account"):
                return 200, {"ok": True, "data": await api.add_account(body)}
            if (method, path) == ("POST", "/api/account-remove"):
                return 200, {"ok": True, "data": await api.remove_account(body["name"])}
            if (method, path) == ("GET", "/api/account-details"):
                account = (query.get("account") or [""])[0]
                if not account:
                    return 400, {"ok": False, "error": "missing ?account="}
                return 200, {"ok": True, "data": await api.account_details(account)}
            if (method, path) == ("POST", "/api/setting"):
                return 200, {
                    "ok": True,
                    "data": api.set_plugin_setting(
                        body["account"], body["plugin"], body["key"], body["value"]
                    ),
                }
            if (method, path) == ("POST", "/api/delay"):
                return 200, {
                    "ok": True,
                    "data": await api.set_delay(
                        body["action"], float(body["min"]), float(body["max"]),
                        body.get("account"),
                    ),
                }
            if (method, path) == ("POST", "/api/clear-delay"):
                return 200, {
                    "ok": True,
                    "data": await api.clear_delay(body["action"], body.get("account")),
                }
            if (method, path) == ("GET", "/api/roles"):
                return 200, {"ok": True, "data": await api.roles()}
            if (method, path) == ("POST", "/api/role"):
                return 200, {
                    "ok": True,
                    "data": await api.set_role(body["name"], body.get("plugins") or [], body.get("description", "")),
                }
            if (method, path) == ("POST", "/api/role-delete"):
                return 200, {"ok": True, "data": await api.delete_role(body["name"])}
            if (method, path) == ("POST", "/api/assign"):
                return 200, {
                    "ok": True,
                    "data": await api.assign_role(body["account"], body["role"], bool(body["assigned"])),
                }
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
        def _authorized(self, provided: Optional[str]) -> bool:
            """Accept the token via X-Dashboard-Token header OR ?token= query."""
            if server._token is None:
                return True
            return provided is not None and provided.strip() == server._token

        def _respond(self, status: int, payload: Dict[str, Any], content_type: str = "application/json") -> None:
            if content_type == "application/json":
                data = json.dumps(payload).encode("utf-8")
            else:
                data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/ws":
                self._handle_websocket(query)
                return
            if method == "GET" and path in ("/", "/index.html"):
                index = STATIC_DIR / "index.html"
                if index.is_file():
                    self._respond(200, index.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._respond(404, {"ok": False, "error": "index.html missing"}, )
                return
            if not self._authorized(
                self.headers.get("X-Dashboard-Token") or (query.get("token") or [None])[0]
            ):
                self._respond(
                    401,
                    {"ok": False, "error": "bad or missing token (X-Dashboard-Token header or ?token=)"},
                )
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
            status, payload = server.call(method, path, body, query)
            self._respond(status, payload)

        def _handle_websocket(self, query: Dict[str, List[str]]) -> None:
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self._respond(400, {"ok": False, "error": "missing Sec-WebSocket-Key"})
                return
            if not self._authorized((query.get("token") or [None])[0]):
                self._respond(401, {"ok": False, "error": "bad or missing ws token"})
                return
            self.connection.sendall(wsproto.handshake_response(key))
            fd = self.connection.detach()  # we own the socket now
            sock = socket.socket(fileno=fd)
            client = WebSocketClient(sock, server)
            server.register_threadsafe(client)
            client.serve()  # blocks this handler thread until the socket closes

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            self._handle("POST")

        def log_message(self, fmt: str, *args: Any) -> None:  # route to our logger
            LOG.debug("%s - %s", self.address_string(), fmt % args)

    return Handler
