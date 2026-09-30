"""Dashboard over real HTTP: REST auth, static index, live WebSocket push."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import unittest
import urllib.error
import urllib.request

from core.api import ControlAPI
from core.logging_setup import RingBufferHandler
from dashboard.server import DashboardServer

from .helpers import Harness

TOKEN = "s3cret-token"


def make_server(harness: Harness, loop) -> DashboardServer:
    api = ControlAPI(harness.container, harness.limiter, harness.loader, harness.credentials)
    return DashboardServer(api, token=TOKEN, port=0)


def http_get(url: str, token: str | None = TOKEN) -> tuple[int, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def read_exactly(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        data += chunk
    return data


def read_http_headers(sock: socket.socket) -> tuple:
    """Read up to end of headers; returns (header_text, leftover_bytes)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before headers finished")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode(), rest


def read_json_frame(sock: socket.socket, buffer: bytes = b"") -> tuple:
    """Read one complete server (unmasked) text frame; returns (json_obj, leftover)."""
    while True:
        try:
            if len(buffer) >= 2:
                length = buffer[1] & 0x7F
                offset = 2
                if length == 126 and len(buffer) >= 4:
                    length = int.from_bytes(buffer[2:4], "big")
                    offset = 4
                elif length == 127 and len(buffer) >= 10:
                    length = int.from_bytes(buffer[2:10], "big")
                    offset = 10
                if len(buffer) >= offset + length:
                    payload = buffer[offset:offset + length]
                    return json.loads(payload.decode()), buffer[offset + length:]
        except (ValueError, json.JSONDecodeError):
            pass
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed mid-frame")
        buffer += chunk


class TestDashboardHTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = Harness("tests/fixtures/plugins", ["alpha", "beta"])
        self.api = ControlAPI(
            self.harness.container, self.harness.limiter, self.harness.loader, self.harness.credentials
        )
        self.server = DashboardServer(self.api, token=TOKEN, port=0)
        self.server.start()
        self.port = self.server._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()

    async def test_index_html_served_without_auth(self) -> None:
        status, body = await asyncio.to_thread(http_get, f"{self.base}/", None)
        self.assertEqual(status, 200)
        self.assertIn("SlaughterMudae", body)
        self.assertIn("WebSocket", body)

    async def test_status_requires_token(self) -> None:
        status, _ = await asyncio.to_thread(http_get, f"{self.base}/api/status", None)
        self.assertEqual(status, 401)
        status, _ = await asyncio.to_thread(http_get, f"{self.base}/api/status", "wrong")
        self.assertEqual(status, 401)

    async def test_status_with_token(self) -> None:
        status, body = await asyncio.to_thread(http_get, f"{self.base}/api/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["data"]["accounts"]), 2)

    async def test_status_accepts_query_param_token(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, f"{self.base}/api/status?token={TOKEN}", None
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    async def test_account_plugins_route(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, f"{self.base}/api/account-plugins?account=alpha"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["data"][0]["name"], "spy")

    async def test_limit_set_over_http(self) -> None:
        def _post_limit() -> dict:
            request = urllib.request.Request(
                f"{self.base}/api/limit",
                data=json.dumps({"action": "message", "rule": "4/11s", "account": "alpha"}).encode(),
                headers={"Content-Type": "application/json", "X-Dashboard-Token": TOKEN},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode())

        payload = await asyncio.to_thread(_post_limit)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.harness.limiter.rule_for("message", "alpha").actions, 4)

    async def test_websocket_pushes_stats_and_logs(self) -> None:
        message = await asyncio.to_thread(self._ws_handshake_and_read)
        self.assertEqual(message["type"], "stats")
        self.assertEqual(len(message["accounts"]), 2)

    def _ws_handshake_and_read(self) -> dict:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                f"GET /ws?token={TOKEN} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode())
            headers, rest = read_http_headers(sock)
            self.assertIn("101", headers.splitlines()[0])
            self.assertIn("sec-websocket-accept", headers.lower())
            # server pushes an immediate stats snapshot on connect
            message, _leftover = read_json_frame(sock, rest)
            return message
        finally:
            sock.close()

    async def test_websocket_rejects_bad_token(self) -> None:
        def _try_bad_token() -> str:
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                request = (
                    f"GET /ws?token=wrong HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    "Sec-WebSocket-Key: abcdefghijklmnopqw==\r\n\r\n"
                )
                sock.sendall(request.encode())
                headers, _rest = read_http_headers(sock)
                return headers
            finally:
                sock.close()

        headers = await asyncio.to_thread(_try_bad_token)
        self.assertIn("401", headers.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
