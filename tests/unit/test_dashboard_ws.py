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
import time
import unittest
import urllib.error
import urllib.request

import interfaces.dashboard.server as server_module
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


def masked_frame(opcode: int, payload: bytes, fin: bool = True,
                 mask_key: bytes = b"\x01\x02\x03\x04") -> bytes:
    """A general masked client frame (opcode/payload/mask of choice)."""
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    assert len(masked) < 126
    first = (0x80 if fin else 0) | opcode
    return bytes([first, 0x80 | len(masked)]) + mask_key + masked


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
        self.assertEqual(frame[1], 2)  # 2-byte struct-packed status code
        self.assertEqual(frame[2:], b"\x03\xe8")  # default 1000 normal closure

    def test_close_frame_carries_code(self) -> None:
        # ROUND-3: close frames must carry a status code — the old frame
        # was a codeless 88 00 while comments claimed 1007
        self.assertEqual(wsproto.encode_close_frame(1002), b"\x88\x02\x03\xea")
        self.assertEqual(wsproto.encode_close_frame(1007), b"\x88\x02\x03\xef")
        self.assertEqual(wsproto.encode_close_frame(None), b"\x88\x00")  # codeless

    def test_echo_close_code(self) -> None:
        # peer close code echoed verbatim; codeless stays codeless
        self.assertEqual(wsproto.echo_close_code(b""), None)
        self.assertEqual(wsproto.echo_close_code(b"\x03\xe8"), 1000)
        self.assertEqual(wsproto.echo_close_code(b"\x03\xef" + b"bye"), 1007)
        # malformed: 1-byte payload / out of range / never-on-the-wire codes
        self.assertEqual(wsproto.echo_close_code(b"\x03"), 1002)
        self.assertEqual(wsproto.echo_close_code(b"\x03\xf6"), 1014)  # in range: echoed
        self.assertEqual(wsproto.echo_close_code(struct.pack(">H", 999)), 1002)
        self.assertEqual(wsproto.echo_close_code(struct.pack(">H", 5000)), 1002)
        for forbidden in (1005, 1006, 1015):
            self.assertEqual(
                wsproto.echo_close_code(struct.pack(">H", forbidden)), 1002)

    def test_decode_rejects_non_minimal_16bit_length(self) -> None:
        # ROUND-3: a 5-byte payload MUST use the 7-bit form — the 16-bit
        # extended form encoding 5 was accepted (smuggling-adjacent)
        frame = (bytes([0x81, 0x80 | 126]) + struct.pack(">H", 5)
                 + b"\x01\x02\x03\x04" + mask(b"hello"))
        with self.assertRaises(wsproto.ProtocolViolation):
            wsproto.decode_frame(frame)

    def test_decode_rejects_non_minimal_64bit_length(self) -> None:
        # ROUND-3: a 5-byte payload MUST NOT use the 64-bit form either
        frame = (bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 5)
                 + b"\x01\x02\x03\x04" + mask(b"hello"))
        with self.assertRaises(wsproto.ProtocolViolation):
            wsproto.decode_frame(frame)

    def test_decode_accepts_minimal_extended_lengths(self) -> None:
        # boundary sanity: exactly-126 in 16-bit form and exactly-65536 in
        # 64-bit form stay legal
        payload = b"x" * 126
        frame = (bytes([0x81, 0x80 | 126]) + struct.pack(">H", 126)
                 + b"\x01\x02\x03\x04" + mask(payload))
        opcode, _fin, decoded, _consumed = wsproto.decode_frame(frame)
        self.assertEqual((opcode, decoded), (wsproto.OP_TEXT, payload))
        payload = b"y" * 65536
        frame = (bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 65536)
                 + b"\x01\x02\x03\x04" + mask(payload))
        opcode, _fin, decoded, _consumed = wsproto.decode_frame(frame)
        self.assertEqual((opcode, decoded), (wsproto.OP_TEXT, payload))


class TestEventAsDict(unittest.TestCase):
    def test_dataclass_event(self) -> None:
        from interfaces.dashboard.server import event_as_dict

        event = TaskStarted(account="alpha", task_id=7, action="message")
        self.assertEqual(event_as_dict(event),
                         {"account": "alpha", "ts": event.ts,
                          "task_id": 7, "action": "message"})

    def test_non_dataclass_fallback(self) -> None:
        # plain objects (no @dataclass) must not raise — vars()-based
        # fallback previously blew up with a TypeError
        from interfaces.dashboard.server import event_as_dict

        class PlainEvent:
            def __init__(self) -> None:
                self.account = "alpha"
                self.task_id = 7
                self.payload = object()  # non-JSON value: filtered out

        self.assertEqual(event_as_dict(PlainEvent()),
                         {"account": "alpha", "task_id": 7})


class TestDumpsHardening(unittest.TestCase):
    """_dumps must degrade EVERY hostile payload — the caller's loop lives."""

    def test_circular_reference(self) -> None:
        from interfaces.dashboard.server import _dumps

        payload: dict = {"type": "stats"}
        payload["self"] = payload
        out = json.loads(_dumps(payload))
        self.assertEqual(out["type"], "error")
        self.assertIn("circular", out["error"])

    def test_deep_nesting(self) -> None:
        from interfaces.dashboard.server import _dumps

        deep: dict = {}
        inner = deep
        for _ in range(100_000):
            inner["n"] = {}
            inner = inner["n"]
        out = json.loads(_dumps({"type": "stats", "accounts": deep}))
        self.assertEqual(out["type"], "error")
        self.assertIn("nesting", out["error"])

    def test_nan_and_non_serializable(self) -> None:
        from interfaces.dashboard.server import _dumps
        from datetime import datetime

        out = json.loads(_dumps({"a": float("nan")}))
        self.assertEqual(out["error"], "payload contained a non-finite number")
        out = json.loads(_dumps({"when": datetime(2024, 1, 1)}))
        self.assertEqual(out["error"], "payload was not JSON-serializable")

    def test_sane_payload_untouched(self) -> None:
        from interfaces.dashboard.server import _dumps

        self.assertEqual(json.loads(_dumps({"a": 1, "b": [2.5, None]})),
                         {"a": 1, "b": [2.5, None]})


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

    async def test_fragmented_text_frame_closes_connection(self) -> None:
        # fragmentation is never reassembled: a fin=0 TEXT frame must not be
        # delivered as a (truncated) complete message
        def _run() -> int:
            sock, _headers = self.connect()
            try:
                _first, leftover = read_json_frame(sock)
                payload = mask(b"ref")
                frame = bytes([0x01, 0x80 | len(payload)]) + b"\x01\x02\x03\x04" + payload
                sock.sendall(frame)
                opcode, _payload, _rest = read_server_frame(sock, leftover)
                return opcode
            finally:
                sock.close()

        opcode = await asyncio.to_thread(_run)
        self.assertEqual(opcode, wsproto.OP_CLOSE)

    async def test_malformed_websocket_key_rejected(self) -> None:
        def _attempt() -> str:
            sock = socket.create_connection((HOST, self.port), timeout=5)
            try:
                request = (
                    f"GET /ws HTTP/1.1\r\nHost: {HOST}:{self.port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    "Sec-WebSocket-Key: \xff\xfe\r\n"  # non-ASCII
                    f"X-Dashboard-Token: {TOKEN}\r\n\r\n"
                ).encode("latin-1")
                sock.sendall(request)
                headers, _ = read_http_headers(sock)
                return headers
            finally:
                sock.close()

        headers = await asyncio.to_thread(_attempt)
        self.assertIn("400", headers.splitlines()[0])

    async def test_broadcast_loop_survives_nan_stats(self) -> None:
        # NaN in the snapshot must degrade to an error frame, never kill
        # the 1-second broadcast loop (a dead loop froze the live view)
        async def _nan_status():
            return {"accounts": [], "ratelimit": float("nan"),
                    "tasks": {}, "warnings": [], "uptime_s": 1.0}

        self.fake.status = _nan_status

        def _run() -> list[dict]:
            sock, _headers = self.connect()
            try:
                first, leftover = read_json_frame(sock)
                # the next broadcast tick must still arrive (~1s later)
                second, _rest = read_json_frame(sock, leftover)
                return [first, second]
            finally:
                sock.close()

        messages = await asyncio.to_thread(_run)
        self.assertEqual(messages[0]["type"], "error")
        self.assertEqual(messages[1]["type"], "error")

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


def ws_connect(port: int, token: str | None = TOKEN,
               query_token: str | None = None) -> tuple[socket.socket, str, bytes]:
    """Open an upgraded WS socket; returns (sock, header_text, leftover)."""
    sock = socket.create_connection((HOST, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    path = "/ws"
    if query_token is not None:
        path += f"?token={query_token}"
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {HOST}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token:
        lines.append(f"X-Dashboard-Token: {token}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    headers, rest = read_http_headers(sock)
    return sock, headers, rest


class TestRound2WSHardening(unittest.IsolatedAsyncioTestCase):
    """Round-2 hostile-frame / auth-slot / lifecycle / DoS behavior."""

    async def asyncSetUp(self) -> None:
        self._saved: dict = {}
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=TOKEN, port=0)
        self.server.start()
        self.port = self.server.bound_port

    async def asyncTearDown(self) -> None:
        self.server.stop()
        for name, value in self._saved.items():
            setattr(server_module, name, value)

    def _patch(self, name: str, value) -> None:
        self._saved.setdefault(name, getattr(server_module, name))
        setattr(server_module, name, value)

    # ------------------------------------------------------- hostile frames
    async def test_zero_length_text_frame_tolerated(self) -> None:
        def _run() -> int:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                sock.sendall(masked_frame(wsproto.OP_TEXT, b""))
                sock.sendall(masked_frame(wsproto.OP_PING, b"alive?"))
                opcode, pong, _r = read_server_frame(sock, rest)
                return opcode, pong
            finally:
                sock.close()

        opcode, pong = await asyncio.to_thread(_run)
        self.assertEqual(opcode, wsproto.OP_PONG)  # connection stayed alive
        self.assertEqual(pong, b"alive?")

    async def test_125_byte_ping_payload_ponged(self) -> None:
        payload = b"p" * 125  # the RFC control-frame maximum

        def _run() -> bytes:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                sock.sendall(masked_frame(wsproto.OP_PING, payload))
                opcode, pong, _r = read_server_frame(sock, rest)
                assert opcode == wsproto.OP_PONG
                return pong
            finally:
                sock.close()

        self.assertEqual(await asyncio.to_thread(_run), payload)

    async def test_zero_masking_key_decodes_correctly(self) -> None:
        # mask 0x00000000 is legal (identity) — a refresh must still work
        def _run() -> dict:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                sock.sendall(client_text_frame("refresh", b"\x00\x00\x00\x00"))
                message, _r = read_json_frame(sock, rest)
                return message
            finally:
                sock.close()

        message = await asyncio.to_thread(_run)
        self.assertEqual(message["type"], "stats")

    async def test_cont_opcode_first_frame_closes(self) -> None:
        def _run() -> int:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                sock.sendall(masked_frame(wsproto.OP_CONT, b"x"))
                opcode, _p, _r = read_server_frame(sock, rest)
                return opcode
            finally:
                sock.close()

        self.assertEqual(await asyncio.to_thread(_run), wsproto.OP_CLOSE)

    async def test_close_frame_with_payload_answered_then_eof(self) -> None:
        def _run() -> tuple[int, bytes]:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                sock.sendall(masked_frame(wsproto.OP_CLOSE, b"\x03\xe8"))  # 1000
                opcode, payload, rest = read_server_frame(sock, rest)
                sock.settimeout(5)
                tail = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        tail += chunk
                except OSError:
                    pass
                return opcode, payload, tail
            finally:
                sock.close()

        opcode, payload, tail = await asyncio.to_thread(_run)
        self.assertEqual(opcode, wsproto.OP_CLOSE)
        # ROUND-3: the peer's close code is echoed (was a codeless 88 00)
        self.assertEqual(payload, b"\x03\xe8")
        self.assertEqual(tail, b"")  # socket fully closed after the echo

    async def test_close_code_on_protocol_violation_is_1002(self) -> None:
        def _run() -> bytes:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                sock.sendall(bytes([0x81, 0x02]) + b"hi")  # unmasked: §5.1
                _opcode, payload, _r = read_server_frame(sock, rest)
                return payload
            finally:
                sock.close()

        payload = await asyncio.to_thread(_run)
        self.assertEqual(payload, b"\x03\xea")  # 1002 protocol error

    async def test_close_code_on_invalid_utf8_text_is_1007(self) -> None:
        def _run() -> bytes:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                # valid frame, invalid UTF-8 payload (lone 0xFF starter)
                sock.sendall(masked_frame(wsproto.OP_TEXT, b"a\xff\xfeb"))
                _opcode, payload, _r = read_server_frame(sock, rest)
                return payload
            finally:
                sock.close()

        payload = await asyncio.to_thread(_run)
        self.assertEqual(payload, b"\x03\xef")  # 1007 invalid payload data

    async def test_16mb_declared_length_closed_immediately(self) -> None:
        # declare 16MB, send only the mask-key bytes: the length cap must
        # fire at HEADER decode — no 16MB read, no timeout wait
        def _run() -> tuple[int, float]:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                started = time.monotonic()
                sock.sendall(bytes([0x81, 0x80 | 127])
                             + struct.pack(">Q", 16 * 1024 * 1024)
                             + b"\x01\x02\x03\x04")
                opcode, _p, _r = read_server_frame(sock, rest)
                return opcode, time.monotonic() - started
            finally:
                sock.close()

        opcode, elapsed = await asyncio.to_thread(_run)
        self.assertEqual(opcode, wsproto.OP_CLOSE)
        self.assertLess(elapsed, 2.5)  # closed at once, not on a timeout

    async def test_partial_frame_stall_closes_after_deadline(self) -> None:
        self._patch("WS_FRAME_DEADLINE_S", 0.5)

        def _run() -> float:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                started = time.monotonic()
                # header claims a 5-byte masked payload; send 3 bytes, silence
                sock.sendall(b"\x81\x85\x01\x02\x03")
                opcode, _p, _r = read_server_frame(sock, rest)
                assert opcode == wsproto.OP_CLOSE
                return time.monotonic() - started
            finally:
                sock.close()

        elapsed = await asyncio.to_thread(_run)
        self.assertGreaterEqual(elapsed, 0.4)  # not an instant misread close
        self.assertLess(elapsed, 3.0)

    async def test_interleaved_ping_during_partial_frame_not_answered(self) -> None:
        # a PING arriving BEHIND a partial big frame stays queued (stream
        # order) and must not reset the stall clock — the abuser is closed
        self._patch("WS_FRAME_DEADLINE_S", 0.5)

        def _run() -> int:
            sock, _h, rest = ws_connect(self.port)
            try:
                _first, _p0, rest = read_server_frame(sock, rest)
                # declare a 100KB text frame but send only its mask key
                sock.sendall(bytes([0x81, 0x80 | 127])
                             + struct.pack(">Q", 100_000) + b"\x01\x02\x03\x04")
                # ...then a complete, perfectly legal PING
                sock.sendall(masked_frame(wsproto.OP_PING, b"let-me-out"))
                opcode, payload, _r = read_server_frame(sock, rest)
                return opcode, payload
            finally:
                sock.close()

        opcode, payload = await asyncio.to_thread(_run)
        self.assertEqual(opcode, wsproto.OP_CLOSE)  # NOT a pong
        self.assertEqual(payload, b"\x03\xea")  # ROUND-3: 1002 protocol error

    # ------------------------------------------------------------ auth slots
    async def test_ws_token_query_precedence_over_header(self) -> None:
        def _conflict(query: str, header: str) -> str:
            sock, headers, _rest = ws_connect(self.port, token=header,
                                              query_token=query)
            try:
                return headers.splitlines()[0]
            finally:
                sock.close()

        # wrong query token wins the slot and fails closed over a RIGHT header
        status = await asyncio.to_thread(_conflict, "wrong", TOKEN)
        self.assertIn("401", status)
        # right query token + wrong header: query slot wins, upgrade proceeds
        status = await asyncio.to_thread(_conflict, TOKEN, "wrong")
        self.assertIn("101", status)

    # ----------------------------------------------------- hostile snapshots
    async def test_broadcast_loop_survives_hostile_snapshot(self) -> None:
        # deep nesting raised RecursionError out of _dumps and KILLED the
        # 1-second loop (live view frozen forever) — it must degrade instead
        deep: dict = {}
        inner = deep
        for _ in range(100_000):
            inner["n"] = {}
            inner = inner["n"]

        async def _hostile_status():
            return {"accounts": deep, "tasks": {}, "warnings": []}

        self.fake.status = _hostile_status

        def _run() -> list[dict]:
            sock, _h, rest = ws_connect(self.port)
            try:
                sock.settimeout(5)
                first, rest = read_json_frame(sock, rest)
                second, _r = read_json_frame(sock, rest)  # next broadcast tick
                return [first, second]
            finally:
                sock.close()

        messages = await asyncio.to_thread(_run)
        self.assertEqual(messages[0]["type"], "error")
        self.assertEqual(messages[1]["type"], "error")  # the loop LIVES

    # ----------------------------------------------------------- thread DoS
    async def test_connection_cap_sheds_and_recovers(self) -> None:
        self._patch("MAX_CONCURRENT_CONNECTIONS", 3)
        fake = FakeControlAPI()
        server = DashboardServer(fake, token=TOKEN, port=0)
        server.start()
        port = server.bound_port
        try:
            def _pin() -> list[socket.socket]:
                socks = []
                for _ in range(3):  # idle WS clients pin 3 handler threads
                    sock, headers, _rest = ws_connect(port)
                    assert "101" in headers.splitlines()[0]
                    socks.append(sock)
                return socks

            pinned = await asyncio.to_thread(_pin)

            def _get_status():
                request = urllib.request.Request(f"http://{HOST}:{port}/api/status")
                request.add_header("X-Dashboard-Token", TOKEN)
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status
                except Exception:
                    return None

            self.assertIsNone(await asyncio.to_thread(_get_status))  # shed
            pinned[0].close()  # free one slot

            deadline = time.monotonic() + 5.0
            status = None
            while time.monotonic() < deadline:
                status = await asyncio.to_thread(_get_status)
                if status == 200:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(status, 200)  # recovered
            for sock in pinned[1:]:
                sock.close()
        finally:
            server.stop()

    # ---------------------------------------------------------- backpressure
    async def test_never_reading_client_is_dropped(self) -> None:
        # a client that reads NOTHING while pushes pile up: its socket buffer
        # fills, the 0.25s send timeout fires and the server DROPS it (the
        # handler thread and the outbound queue must not pin forever)
        log_service = self.fake.log_service
        self.server.attach_logs(log_service)

        def _run() -> bool:
            sock = socket.create_connection((HOST, self.port), timeout=15)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            except OSError:
                pass
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(ws_request(self.port, key).encode())
            headers, _rest = read_http_headers(sock)
            assert "101" in headers.splitlines()[0]

            def _flood() -> None:
                entry = {"ts": 1.0, "severity": "INFO", "source": "t",
                         "message": "x" * 200, "account": None}
                for _ in range(20_000):
                    log_service.fire(entry)

            import threading
            flooder = threading.Thread(target=_flood)
            flooder.start()
            # read NOTHING — watch the server-side roster instead
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if not self.server._clients:
                    break
                time.sleep(0.1)
            flooder.join()
            dropped = not self.server._clients
            if dropped:  # and the socket really closed underneath us
                sock.settimeout(5)
                try:
                    while sock.recv(65536):
                        pass
                except OSError:
                    pass
            return dropped

        self.assertTrue(await asyncio.to_thread(_run))


if __name__ == "__main__":
    unittest.main()
