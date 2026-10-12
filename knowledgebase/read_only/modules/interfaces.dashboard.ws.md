# Module `interfaces.dashboard.ws`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `interfaces/dashboard/ws.py`

```
Minimal RFC 6455 WebSocket server subset (text frames only).

Implements exactly what the dashboard needs: handshake response generation,
outbound text/pong/close frames, inbound (masked) frame decoding with ping
support. Fragmentation is not reassembled (dashboard messages are small);
control frames other than ping/close are ignored per opcode.

Ported VERBATIM from the audited original (`git show HEAD:dashboard/ws.py`)
— it survived 3 audit rounds; do not "improve" it without re-auditing.

ROUND-3 AUTHORIZED DEVIATION (the only one): ``encode_close_frame`` now
carries a status code (struct-packed >H; was a codeless ``88 00`` while
comments claimed 1007), plus ``echo_close_code`` for answering a peer
close, and ``decode_frame`` rejects non-minimal extended length
encodings. Everything else stays verbatim.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `IncompleteFrame` | exception | yes | Buffer holds a partial frame — wait for more bytes (not an error). |
| `ProtocolViolation` | exception | yes | Client broke RFC 6455 — the connection must be closed. |

## Functions

- `def accept_key(client_key: str) -> str`
  - Sec-WebSocket-Accept = b64(sha1(key + GUID)) per RFC 6455 §1.3.
- `def handshake_response(client_key: str, subprotocol: str | None=None) -> bytes`
  - 101 Switching Protocols response bytes for an upgrade request.
- `def encode_frame(opcode: int, payload: bytes) -> bytes`
  - Server->client frame: FIN=1, no masking, 7/16/64-bit lengths.
- `def encode_text_frame(text: str) -> bytes`
  - Encode one server->client TEXT frame (FIN=1, unmasked).
- `def encode_pong(payload: bytes) -> bytes`
  - Encode a PONG control frame echoing the ping payload (RFC 6455 §5.5.2).
- `def encode_close_frame(code: Optional[int]=1000) -> bytes`
  - Encode a CLOSE frame (RFC 6455 §5.5.1) carrying ``code`` (struct-packed
- `def echo_close_code(payload: bytes) -> Optional[int]`
  - Status code to answer a peer CLOSE frame with (RFC 6455 §5.5.1/§7.4).
- `def decode_frame(buffer: bytes) -> Tuple[int, bool, bytes, int]`
  - Decode ONE client->server frame from the head of `buffer`.
