"""WebSocket protocol helpers: handshake keys, frame encode/decode."""
from __future__ import annotations

import base64
import hashlib
import struct
import unittest

from dashboard.ws import (
    GUID,
    OP_TEXT,
    decode_frame,
    encode_close_frame,
    encode_frame,
    encode_text_frame,
    handshake_response,
    accept_key,
)


def mask(payload: bytes, key: bytes = b"\x01\x02\x03\x04") -> bytes:
    """Client-style masking for building inbound frames in tests."""
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class TestHandshake(unittest.TestCase):
    def test_accept_key_rfc6455_vector(self) -> None:
        # RFC 6455 §1.3 worked example
        self.assertEqual(
            accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_accept_key_definition(self) -> None:
        key = "a-bogus-key=="
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.assertEqual(accept_key(key), expected)

    def test_handshake_response_shape(self) -> None:
        response = handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
        text = response.decode()
        self.assertIn("101 Switching Protocols", text)
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", text)
        self.assertTrue(text.endswith("\r\n\r\n"))


class TestFrames(unittest.TestCase):
    def test_encode_text_small(self) -> None:
        frame = encode_text_frame("hi")
        self.assertEqual(frame[0] & 0x80, 0x80)  # FIN
        self.assertEqual(frame[0] & 0x0F, OP_TEXT)
        self.assertEqual(frame[1], 2)  # unmasked, len 2
        self.assertEqual(frame[2:], b"hi")

    def test_encode_extended_lengths(self) -> None:
        small = encode_frame(0x1, b"x" * 126)
        self.assertEqual(small[1], 126)
        self.assertEqual(struct.unpack(">H", small[2:4])[0], 126)
        big = encode_frame(0x1, b"y" * 70_000)
        self.assertEqual(big[1], 127)
        self.assertEqual(struct.unpack(">Q", big[2:10])[0], 70_000)

    def test_roundtrip_masked_client_frame(self) -> None:
        payload = b'{"type":"refresh"}'
        masked = mask(payload)
        frame = bytes([0x81, 0x80 | len(masked)]) + b"\x01\x02\x03\x04" + masked
        opcode, fin, decoded, consumed = decode_frame(frame + b"TRAILING")
        self.assertEqual(opcode, OP_TEXT)
        self.assertTrue(fin)
        self.assertEqual(decoded, payload)
        self.assertEqual(consumed, len(frame))

    def test_decode_rejects_incomplete(self) -> None:
        with self.assertRaises(ValueError):
            decode_frame(b"\x81")
        with self.assertRaises(ValueError):
            decode_frame(b"\x81\x85ab")  # claims 5-byte payload, has none

    def test_close_frame(self) -> None:
        frame = encode_close_frame()
        self.assertEqual(frame[0] & 0x0F, 0x8)
        self.assertEqual(len(frame), 2)


if __name__ == "__main__":
    unittest.main()
