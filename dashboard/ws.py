"""Minimal RFC 6455 WebSocket server subset (text frames only).

Implements exactly what the dashboard needs: handshake response generation,
outbound text/pong/close frames, inbound (masked) frame decoding with ping
support. Fragmentation is not reassembled (dashboard messages are small);
control frames other than ping/close are ignored per opcode.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from typing import Tuple

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(client_key: str) -> str:
    """Sec-WebSocket-Accept = b64(sha1(key + GUID)) per RFC 6455 §1.3."""
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(client_key: str, subprotocol: str | None = None) -> bytes:
    """101 Switching Protocols response bytes for an upgrade request."""
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(client_key)}",
    ]
    if subprotocol:
        lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """Server->client frame: FIN=1, no masking, 7/16/64-bit lengths."""
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        return header + bytes([length]) + payload
    if length < 65536:
        return header + bytes([126]) + struct.pack(">H", length) + payload
    return header + bytes([127]) + struct.pack(">Q", length) + payload


def encode_text_frame(text: str) -> bytes:
    return encode_frame(OP_TEXT, text.encode("utf-8"))


def encode_pong(payload: bytes) -> bytes:
    return encode_frame(OP_PONG, payload)


def encode_close_frame() -> bytes:
    return encode_frame(OP_CLOSE, b"")


def decode_frame(buffer: bytes) -> Tuple[int, bool, bytes, int]:
    """Decode ONE client->server frame from the head of `buffer`.

    Client frames MUST be masked (RFC §5.1); the payload is unmasked here.
    Returns: (opcode, fin, payload, consumed_bytes).
    Raises: ValueError when the buffer holds an incomplete frame.
    """
    if len(buffer) < 2:
        raise ValueError("incomplete frame header")
    first, second = buffer[0], buffer[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            raise ValueError("incomplete extended length")
        (length,) = struct.unpack(">H", buffer[offset:offset + 2])
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            raise ValueError("incomplete extended length")
        (length,) = struct.unpack(">Q", buffer[offset:offset + 8])
        offset += 8
    mask_key = b""
    if masked:
        if len(buffer) < offset + 4:
            raise ValueError("incomplete mask")
        mask_key = buffer[offset:offset + 4]
        offset += 4
    if len(buffer) < offset + length:
        raise ValueError("incomplete payload")
    payload = buffer[offset:offset + length]
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload, offset + length
