"""Dashboard WebSocket: RFC 6455 subset unit tests + live server behavior.

Protocol tests ported from the audited original (``git show
HEAD:tests/test_ws.py``); live tests exercise the real handshake, the
"refresh" app message, log/audit pushes and task-event pushes over raw
sockets against a running DashboardServer (ephemeral port, fake ControlAPI).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import struct
import unittest

from core.event_bus import EventBus, TaskCompleted, TaskStarted
from interfaces.dashboard import ws as wsproto
from interfaces.dashboard.server import DashboardServer

from .test_dashboard_http import TOKEN, FakeControlAPI

HOST = "127.0.0.1"


def mask(payload: bytes, key: bytes = b"\x01\x02\x03\x04") -> bytes:
    """Client-style masking for building inbound frames in tests."""
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def client_text_frame(text: str, mask_key: bytes = b"\x01\x02\x03\x04") -> bytes:
    """A real masked client TEXT frame carrying `text`."""
    payload = mask(text.encode("utf-8"), mask_key)
    assert len(payload) < 126
    return bytes([0x81, 0x80 | len(payload)]) + mask_key + payload


def read_exactly(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        data += chunk
    return data


def read_http_headers(sock: socket.socket) -> tuple[str, bytes]:
    """Read up to end of headers; returns (header_text, leftover_bytes)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before headers finished")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode(), rest


def read_server_frame(sock: socket.socket, buffer: bytes = b"") -> tuple[int, bytes, bytes]:
    """Read ONE complete server (unmasked) frame.

    Returns (opcode, payload, leftover_bytes).
    """
    while True:
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
                return buffer[0] & 0x0F, payload, buffer[offset + length:]
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed mid-frame")
        buffer += chunk


def read_json_frame(sock: socket.socket, buffer: bytes = b"") -> tuple[dict, bytes]:
    opcode, payload, leftover = read_server_frame(sock, buffer)
    if opcode != wsproto.OP_TEXT:
        raise AssertionError(f"expected a text frame, got opcode {opcode}")
    return json.loads(payload.decode()), leftover


# ------------------------------------------------------------ protocol units
class TestHandshake(unittest.TestCase):
    def test_accept_key_rfc6455_vector(self) -> None:
        # RFC 6455 §1.3 worked example
        self.assertEqual(
            wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_accept_key_definition(self) -> None:
        key = "a-bogus-key=="
        expected = base64.b64encode(hashlib.sha1((key + wsproto.GUID).encode()).digest()).decode()
        self.assertEqual(wsproto.accept_key(key), expected)

    def test_handshake_response_shape(self) -> None:
        response = wsproto.handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
        text = response.decode()
        self.assertIn("101 Switching Protocols", text)
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", text)
        self.assertTrue(text.endswith("\r\n\r\n"))


class TestFrames(unittest.TestCase):
    def test_encode_text_small(self) -> None:
        frame = wsproto.encode_text_frame("hi")
        self.assertEqual(frame[0] & 0x80, 0x80)  # FIN
        self.assertEqual(frame[0] & 0x0F, wsproto.OP_TEXT)
        self.assertEqual(frame[1], 2)  # unmasked, len 2
        self.assertEqual(frame[2:], b"hi")

    def test_encode_extended_lengths(self) -> None:
        small = wsproto.encode_frame(0x1, b"x" * 126)
        self.assertEqual(small[1], 126)
        self.assertEqual(struct.unpack(">H", small[2:4])[0], 126)
        big = wsproto.encode_frame(0x1, b"y" * 70_000)
        self.assertEqual(big[1], 127)
        self.assertEqual(struct.unpack(">Q", big[2:10])[0], 70_000)

    def test_roundtrip_masked_client_frame(self) -> None:
        payload = b'{"type":"refresh"}'
        masked = mask(payload)
        frame = bytes([0x81, 0x80 | len(masked)]) + b"\x01\x02\x03\x04" + masked
        opcode, fin, decoded, consumed = wsproto.decode_frame(frame + b"TRAILING")
        self.assertEqual(opcode, wsproto.OP_TEXT)
        self.assertTrue(fin)
        self.assertEqual(decoded, payload)
        self.assertEqual(consumed, len(frame))

    def test_decode_rejects_incomplete(self) -> None:
        with self.assertRaises(ValueError):
            wsproto.decode_frame(b"\x81")
        with self.assertRaises(ValueError):
            wsproto.decode_frame(b"\x81\x85ab")  # claims 5-byte payload, has none

    def test_decode_rejects_unmasked_client_frame(self) -> None:
        with self.assertRaises(wsproto.ProtocolViolation):
            wsproto.decode_frame(bytes([0x81, 0x02]) + b"hi")

    def test_decode_rejects_rsv_bits(self) -> None:
        frame = bytes([0x81 | 0x40, 0x80]) + b"\x01\x02\x03\x04"
        with self.assertRaises(wsproto.ProtocolViolation):
            wsproto.decode_frame(frame)

    def test_decode_rejects_absurd_length_claim(self) -> None:
        frame = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 2**62) + b"\x01\x02\x03\x04"
        with self.assertRaises(wsproto.ProtocolViolation):
            wsproto.decode_frame(frame)

    def test_decode_rejects_oversized_control_frame(self) -> None:
        frame = bytes([0x89, 0x80 | 126]) + struct.pack(">H", 200) + b"\x01\x02\x03\x04"
        with self.assertRaises(wsproto.ProtocolViolation):
            wsproto.decode_frame(frame)

    def test_close_frame(self) -> None:
        frame = wsproto.encode_close_frame()
        self.assertEqual(frame[0] & 0x0F, 0x8)
        self.assertEqual(len(frame), 2)


# ---------------------------------------------------------------- live server
def ws_request(port: int, key: str, path: str = "/ws", token: str | None = TOKEN,
               version: str = "13") -> str:
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {HOST}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        f"Sec-WebSocket-Version: {version}",
    ]
    if token:
        lines.append(f"X-Dashboard-Token: {token}")
    return "\r\n".join(lines) + "\r\n\r\n"


class TestDashboardWS(unittest.IsolatedAsyncioTestCase):
    """Live WebSocket behavior over raw sockets (ephemeral port)."""

    async def asyncSetUp(self) -> None:
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=TOKEN, port=0)
        self.server.start()
        self.port = self.server.bound_port
        self.assertNotEqual(self.port, 0)

    async def asyncTearDown(self) -> None:
        self.server.stop()

    def connect(self, token: str | None = TOKEN, version: str = "13",
                path: str = "/ws") -> tuple[socket.socket, str]:
        """Open a socket, do the upgrade, return (sock, header_text)."""
        sock = socket.create_connection((HOST, self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(ws_request(self.port, key, path=path, token=token,
                                version=version).encode())
        headers, _rest = read_http_headers(sock)
        return sock, headers

    # ------------------------------------------------------- handshake guards
    async def test_handshake_rejects_missing_upgrade(self) -> None:
        def _attempt() -> str:
            sock = socket.create_connection((HOST, self.port), timeout=5)
            try:
                sock.sendall(
                    f"GET /ws HTTP/1.1\r\nHost: {HOST}:{self.port}\r\n"
                    f"X-Dashboard-Token: {TOKEN}\r\n\r\n".encode()
                )
                headers, _ = read_http_headers(sock)
                return headers
            finally:
                sock.close()

        headers = await asyncio.to_thread(_attempt)
        self.assertIn("400", headers.splitlines()[0])

    async def test_handshake_rejects_wrong_version(self) -> None:
        sock, headers = await asyncio.to_thread(self.connect, TOKEN, "8")
        try:
            self.assertIn("400", headers.splitlines()[0])
        finally:
            sock.close()

    async def test_handshake_rejects_bad_token(self) -> None:
        def _attempt() -> tuple[str, str]:
            sock = socket.create_connection((HOST, self.port), timeout=5)
            try:
                key = base64.b64encode(os.urandom(16)).decode()
                sock.sendall(ws_request(self.port, key, token="wrong").encode())
                headers, rest = read_http_headers(sock)
                # the JSON body may still be in flight — honor Content-Length
                length = 0
                for line in headers.splitlines():
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                while len(rest) < length:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    rest += chunk
                return headers, rest[:length].decode("utf-8", "replace")
            finally:
                sock.close()

        headers, body = await asyncio.to_thread(_attempt)
        self.assertIn("401", headers.splitlines()[0])
        self.assertIn("token", body)

    # ------------------------------------------------------ happy connect path
    async def test_connect_pushes_immediate_stats(self) -> None:
        def _run() -> dict:
            sock, headers = self.connect()
            try:
                self.assertIn("101", headers.splitlines()[0])
                self.assertIn("sec-websocket-accept", headers.lower())
                message, _ = read_json_frame(sock)
                return message
            finally:
                sock.close()

        message = await asyncio.to_thread(_run)
        self.assertEqual(message["type"], "stats")
        self.assertEqual(len(message["accounts"]), 2)
        self.assertEqual(message["tasks"]["queued"], 1)

    async def test_refresh_app_message_triggers_stats_push(self) -> None:
        def _run() -> dict:
            sock, _headers = self.connect()
            try:
                _first, leftover = read_json_frame(sock)  # on-connect snapshot
                sock.sendall(client_text_frame("refresh"))
                message, _ = read_json_frame(sock, leftover)
                return message
            finally:
                sock.close()

        message = await asyncio.to_thread(_run)
        self.assertEqual(message["type"], "stats")
        self.assertEqual(self.fake.called("status") is not None, True)

    async def test_ping_answered_with_pong(self) -> None:
        def _run() -> tuple[int, bytes]:
            sock, _headers = self.connect()
            try:
                _first, leftover = read_json_frame(sock)
                payload = b"ping-payload"
                masked = mask(payload)
                frame = bytes([0x89, 0x80 | len(masked)]) + b"\x01\x02\x03\x04" + masked
                sock.sendall(frame)
                opcode, pong, _rest = read_server_frame(sock, leftover)
                return opcode, pong
            finally:
                sock.close()

        opcode, pong = await asyncio.to_thread(_run)
        self.assertEqual(opcode, wsproto.OP_PONG)
        self.assertEqual(pong, b"ping-payload")

    async def test_protocol_violation_closes_connection(self) -> None:
        def _run() -> int:
            sock, _headers = self.connect()
            try:
                _first, leftover = read_json_frame(sock)
                # unmasked client text frame — RFC 6455 §5.1 violation
                sock.sendall(bytes([0x81, 0x02]) + b"hi")
                opcode, _payload, _rest = read_server_frame(sock, leftover)
                return opcode
            finally:
                sock.close()

        opcode = await asyncio.to_thread(_run)
        self.assertEqual(opcode, wsproto.OP_CLOSE)

    # --------------------------------------------------------------- live push
    async def test_log_push_reaches_client_after_attach_logs(self) -> None:
        log_service = self.fake.log_service
        self.server.attach_logs(log_service)

        def _run() -> dict:
            sock, _headers = self.connect()
            try:
                _first, leftover = read_json_frame(sock)
                entry = {
                    "ts": 1700000000.0, "severity": "WARNING", "source": "account:alpha",
                    "message": "reconnect scheduled", "account": "alpha",
                    "plugin": None, "task_id": None, "event": None,
                }
                log_service.fire(entry)  # invokes the captured subscribe callback
                message, _ = read_json_frame(sock, leftover)
                return message
            finally:
                sock.close()

        message = await asyncio.to_thread(_run)
        self.assertEqual(message["type"], "log")
        self.assertEqual(message["message"], "reconnect scheduled")
        self.assertEqual(message["severity"], "WARNING")

    async def test_audit_push_via_api_recorder_subscription(self) -> None:
        # start() auto-subscribes api.audit_recorder() when it exists
        self.assertEqual(len(self.fake.audit.callbacks), 1)

        def _run() -> dict:
            sock, _headers = self.connect()
            try:
                _first, leftover = read_json_frame(sock)
                self.fake.audit.fire({
                    "ts": 1700000000.0, "account": "alpha", "direction": "out",
                    "action": "message", "channel_id": 55, "content": "$m",
                    "wait_ms": 120.0, "jitter_ms": 40.0, "note": "",
                })
                message, _ = read_json_frame(sock, leftover)
                return message
            finally:
                sock.close()

        message = await asyncio.to_thread(_run)
        self.assertEqual(message["type"], "audit")
        self.assertEqual(message["direction"], "out")

    async def test_task_events_pushed_from_the_bus(self) -> None:
        fake = FakeControlAPI()
        bus = EventBus()
        server = DashboardServer(fake, token=None, port=0, bus=bus)
        server.start()
        try:
            port = server.bound_port
            loop = asyncio.get_running_loop()

            def _connect_and_collect() -> dict:
                sock = socket.create_connection((HOST, port), timeout=5)
                try:
                    key = base64.b64encode(os.urandom(16)).decode()
                    sock.sendall(ws_request(port, key, token=None).encode())
                    headers, rest = read_http_headers(sock)
                    assert "101" in headers.splitlines()[0]
                    _first, leftover = read_json_frame(sock, rest)
                    # publish ON the loop exactly like the managers do
                    async def _publish() -> None:
                        await bus.publish(TaskStarted(account="alpha", task_id=7,
                                                      action="message"))
                        await bus.publish(TaskCompleted(account="alpha", task_id=7,
                                                        duration_s=0.4, summary="sent"))

                    future = asyncio.run_coroutine_threadsafe(_publish(), loop)
                    future.result(timeout=5)
                    started, leftover = read_json_frame(sock, leftover)
                    completed, _rest = read_json_frame(sock, leftover)
                    return {"started": started, "completed": completed}
                finally:
                    sock.close()

            result = await asyncio.to_thread(_connect_and_collect)
        finally:
            server.stop()

        self.assertEqual(result["started"]["type"], "task")
        self.assertEqual(result["started"]["event"], "TaskStarted")
        self.assertEqual(result["started"]["task_id"], 7)
        self.assertEqual(result["started"]["account"], "alpha")
        self.assertEqual(result["started"]["action"], "message")
        self.assertEqual(result["completed"]["event"], "TaskCompleted")
        self.assertEqual(result["completed"]["summary"], "sent")


if __name__ == "__main__":
    unittest.main()
