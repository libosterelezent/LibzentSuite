"""Dashboard backend: JSON REST + WebSocket live push over ControlAPI.

Serves a single-page app (``static/index.html``, no build step) plus a JSON
REST surface that maps 1:1 onto :class:`core.api.ControlAPI` and a WebSocket
(`/ws`) that pushes live stats/log/audit/task updates.

Threading model (ported from the audited original, 3 audit rounds clean):
  * ``ThreadingHTTPServer`` runs on ONE daemon thread; every request —
    including an accepted WebSocket — gets its own handler thread.
  * Route handlers run on the MAIN asyncio loop via
    ``asyncio.run_coroutine_threadsafe`` (see :meth:`DashboardServer.call`);
    a stuck route surfaces as 504, never a hung handler thread.
  * Each ``WebSocketClient`` owns an outbound queue drained by its reader
    thread; pushes from any thread are queue puts (thread-safe by design).

Auth: when ``token`` is set, REST requires the ``X-Dashboard-Token`` header
or ``?token=`` and the WebSocket requires the same. The static index is
served WITHOUT auth (the SPA prompts for the token itself). Serves on
127.0.0.1 by default — do not expose publicly without the token.

Credential routes (POST /api/accounts/credential, GET /api/credentials/
status, POST /api/credentials/import-env, and POST /api/accounts/add when
its body carries a ``credential``) are gated ONE step further: they require
a CONFIGURED token. With no DASHBOARD_TOKEN set they answer 403 with
"set DASHBOARD_TOKEN in .env to manage credentials" instead of becoming an
open credential-writing surface; with a token set they just ride the normal
401 auth. Credential VALUES never appear in any response — only account
names and db/env/missing status (grep-proof).

Error semantics (ported):
  * missing/invalid fields      -> 400 with an actionable message naming the
                                    field and the route (helpers below)
  * strict bools                -> ``parse_json_bool``: ``{"enabled": "yes"}``
                                    is a 400, never a silent truthy cast
  * ValueError/RuntimeError from the api (the whole domain-error family in
    core/errors.py) -> honest 400 carrying the message
  * anything unexpected         -> 500 with the traceback logged
  * main-loop hop timeout       -> 504 with a real message
  * body > 1MB                  -> 413 after a bounded drain

Live push (WS ``text`` frames, JSON):
  * ``{"type":"stats", **await api.status()}`` every second (broadcaster
    loop survives api errors) and immediately on connect / on "refresh"
  * ``{"type":"log", **entry}``   for every log-service entry (attach_logs)
  * ``{"type":"audit", **entry}`` for every audit entry (auto-subscribed via
    ``api.audit_recorder()`` when the recorder exists)
  * ``{"type":"task", "event": <name>, ...}`` for TaskStarted/TaskCompleted/
    TaskFailed/TaskCancelled bus events

NOTABLE GAP (core/api.py lacked this): ControlAPI exposes no event-bus
accessor, so task events can not be subscribed through the facade. The
server therefore accepts an optional ``bus=`` argument (any object with the
``EventBus.subscribe`` shape) and, failing that, probes a few common
attribute names on the api object (``bus``/``event_bus``). Bootstrap should
pass the real bus explicitly.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import ipaddress
import json
import logging
import math
import os
import queue as thread_queue
import signal
import socket
import threading
import time as _time
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from core.api import ControlAPI
from core.dashboard_ui.errors import UIError
from core.errors import AccountError, DatabaseError
from core.event_bus import (
    Event,
    EventBus,
    TaskCancelled,
    TaskCompleted,
    TaskFailed,
    TaskStarted,
)

from . import ws as wsproto

LOG = logging.getLogger("slaughtermudae.dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"
BROADCAST_INTERVAL = 1.0
#: request/WS-frame sanity caps (ERROR AUDIT: half-open posts pinned threads)
MAX_BODY_BYTES = 1_000_000
#: per-connection socket timeout for plain HTTP requests (slow-loris guard);
#: an upgraded WebSocket detaches the socket and manages its own timeouts
HTTP_SOCKET_TIMEOUT_S = 30.0
# TOTAL deadline for reading one request body: per-recv timeouts reset on
# every byte, so a 1-byte-per-29s trickle could otherwise pin a handler
# slot for length × timeout ( defeating the slow-loris guard ).
HTTP_BODY_DEADLINE_S = 30.0
#: hostnames a loopback-bound dashboard accepts in the Host header
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
#: response headers for every reply (clickjacking / sniffing / caching hardening)
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("Cache-Control", "no-store"),
)
#: how long a route coroutine may take on the main loop before we 504
# strictly greater than the UI registry's SYNC_TIMEOUT_S (10.0) + queue
# margin: an equal budget phantom-504'd actions that queued behind other
# sync callbacks and STILL succeeded (retry = double-apply)
ROUTE_TIMEOUT_S = 13.0
#: wall-clock budget for ONE partially-arrived websocket frame to complete
#: (slow-dribble guard: a frame header that starts arriving must finish)
WS_FRAME_DEADLINE_S = 30.0
#: concurrent handler threads. ``ThreadingHTTPServer`` alone is unbounded —
#: 500 opened sockets pin 500 threads; above the cap the connection is
#: accepted and immediately closed instead (the accept loop never blocks)
MAX_CONCURRENT_CONNECTIONS = 64


class _ConflictingContentLength(ValueError):
    """Duplicate Content-Length headers with DIFFERENT values.

    ROUND-3 AUDIT: ``headers.get()`` silently resolved duplicates to the
    FIRST value — a request-smuggling desync family. Conflicting values
    are now refused with 400 + close.
    """


def _split_host(value: str) -> str:
    """Hostname part of a Host header / URL netloc (port and [] stripped)."""
    value = (value or "").strip().lower()
    if value.startswith("["):  # [::1]:8070
        return value[1:value.find("]")] if "]" in value else value[1:]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _is_loopback_host(host: str) -> bool:
    """True for localhost / 127.0.0.0/8 / ::1 addresses."""
    host = (host or "").strip().lower()
    if host in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

#: task event types forwarded to WS clients (JSON-safe payloads)
TASK_EVENT_TYPES = (TaskStarted, TaskCompleted, TaskFailed, TaskCancelled)

#: routes that carry credential MATERIAL (a token value, or the power to
#: import/copy one). Beyond the normal token auth they REQUIRE a configured
#: DASHBOARD_TOKEN: with no token configured they answer 403 naming the fix —
#: an accidentally-open loopback dashboard must never become a credential
#: writing surface (spec §12). ``POST /api/accounts/add`` joins this set
#: only when its body actually carries a ``credential``.
CREDENTIAL_GATED_ROUTES = {
    ("POST", "/api/accounts/credential"),
    ("GET", "/api/credentials/status"),
    ("POST", "/api/credentials/import-env"),
}
CREDENTIAL_GATE_MESSAGE = "set DASHBOARD_TOKEN in .env to manage credentials"


def _dumps(payload: Any) -> str:
    """NaN-safe JSON: a non-finite float must never emit the bare `NaN`
    token (invalid JSON — WS clients drop such frames SILENTLY, freezing
    the live view with no error). Non-JSON-serializable values (datetime,
    sets, exceptions) degrade the same way instead of raising. Circular
    references, pathologically deep nesting and payloads mutated by
    another thread mid-serialization degrade too — the caller's loop must
    NEVER die over one hostile snapshot."""
    try:
        return json.dumps(payload, allow_nan=False)
    except ValueError as exc:
        if "circular" in str(exc).lower():
            return json.dumps({"type": "error",
                               "error": "payload contained a circular reference"})
        return json.dumps({"type": "error",
                           "error": "payload contained a non-finite number"})
    except TypeError:
        return json.dumps({"type": "error",
                           "error": "payload was not JSON-serializable"})
    except RecursionError:
        return json.dumps({"type": "error",
                           "error": "payload nesting exceeded the serializer limit"})
    except RuntimeError:
        return json.dumps({"type": "error",
                           "error": "payload changed during serialization"})


def parse_json_bool(value: Any, field: str) -> bool:
    """Strict boolean parsing for API payloads.

    ERROR AUDIT FIX: bool("false") == True silently did the OPPOSITE of what
    the user asked. JSON surfaces carry real booleans — anything else is a
    client bug and gets a 400.
    """
    if isinstance(value, bool):
        return value
    raise ValueError(f"field '{field}' must be a boolean (true/false), got {value!r}")


def require_field(body: Dict[str, Any], key: str, route: str) -> Any:
    """Fetch a required body field with an actionable 400 message.

    ERROR AUDIT FIX: missing keys previously leaked bare KeyError text
    like {"error": "'plugin'"}.
    """
    if not isinstance(body, dict) or key not in body:
        raise ValueError(f"missing required field '{key}' in POST {route}")
    return body[key]


def require_str(body: Dict[str, Any], key: str, route: str) -> str:
    """Required non-empty string field (actionable 400 on missing/empty/wrong type)."""
    value = require_field(body, key, route)
    if not isinstance(value, str) or not value:
        raise ValueError(f"field '{key}' in POST {route} must be a non-empty string")
    return value


def require_number(body: Dict[str, Any], key: str, route: str) -> float:
    """Required numeric field; finite-ness is checked separately by callers.

    Bools are rejected (parity with require_int): ``float(True)`` would
    silently set a 1-second interval from ``{"interval": true}``.
    """
    value = require_field(body, key, route)
    if isinstance(value, bool):
        raise ValueError(f"field '{key}' in POST {route} must be a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"field '{key}' in POST {route} must be a number") from exc


def require_finite(value: float, key: str, route: str) -> float:
    """Reject NaN/inf numbers (NaN slips past plain comparisons)."""
    if not math.isfinite(value):
        raise ValueError(f"field '{key}' in POST {route} must be a finite number")
    return value


def require_int(body: Dict[str, Any], key: str, route: str) -> int:
    """Required integer field (bools excluded; integral floats tolerated)."""
    value = require_field(body, key, route)
    if isinstance(value, bool):
        raise ValueError(f"field '{key}' in POST {route} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"field '{key}' in POST {route} must be an integer, got {value!r}")


def require_json_list(body: Dict[str, Any], key: str, route: str) -> List[Any]:
    """Required list field whose items must all be integers.

    AUDIT FIX: /api/admin/ids/set and /api/admin/perms/set referenced
    this helper before it existed — both routes 500'd with a NameError.
    Mirrors require_int's strictness per item (bools rejected, integral
    floats tolerated) and produces an actionable 400 otherwise.
    """
    value = require_field(body, key, route)
    if not isinstance(value, list):
        raise ValueError(f"field '{key}' in POST {route} must be a list of integers")
    out: List[int] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"field '{key}' in POST {route} must contain integers, got {item!r}")
        if isinstance(item, int):
            out.append(item)
        elif isinstance(item, float) and item.is_integer():
            out.append(int(item))
        else:
            raise ValueError(
                f"field '{key}' in POST {route} must contain integers, got {item!r}")
    return out


def q_str(query: Dict[str, List[str]], key: str, route: str) -> str:
    """Required non-empty query-string parameter (actionable 400 when absent)."""
    value = (query.get(key) or [""])[0].strip()
    if not value:
        raise ValueError(f"missing ?{key}= parameter in GET {route}")
    return value


def q_opt(query: Dict[str, List[str]], key: str) -> Optional[str]:
    """Optional query-string parameter; empty/absent becomes None."""
    value = (query.get(key) or [""])[0].strip()
    return value or None


def q_int(query: Dict[str, List[str]], key: str, default: int, route: str) -> int:
    """Integer query-string parameter with a default; 400 on garbage."""
    raw = (query.get(key) or [""])[0].strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"?{key}= in GET {route} must be an integer, got {raw!r}") from exc


def body_opt_str(body: Dict[str, Any], key: str, route: str) -> Optional[str]:
    """Optional string body field; null/empty -> None; non-string -> 400."""
    value = body.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"field '{key}' in POST {route} must be a string")
    return value


def body_opt_number(body: Dict[str, Any], key: str, default: float, route: str) -> float:
    """Optional numeric body field (finite-checked); absent -> default."""
    if key not in body or body[key] is None:
        return default
    return require_finite(require_number(body, key, route), key, route)


def body_opt_owner(body: Dict[str, Any], route: str) -> Optional[str]:
    """Policy owner: null/"" means global scope; must be a string otherwise."""
    return body_opt_str(body, "owner", route)


def entry_as_dict(entry: Any) -> Dict[str, Any]:
    """Normalize a log/audit entry (dict or dataclass-like) to a JSON dict."""
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "as_dict"):
        try:
            return dict(entry.as_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    return {"message": str(entry)}


def event_as_dict(event: Event) -> Dict[str, Any]:
    """JSON-safe dict of a bus event (dataclass fields; falls back to vars)."""
    if is_dataclass(event):
        return dict(asdict(event))
    return {k: v for k, v in getattr(event, "__dict__", {}).items()
            if isinstance(v, (str, int, float, bool, type(None)))}


class WebSocketClient:
    """One browser connection: outbound queue drained by its reader thread."""

    def __init__(self, sock: socket.socket, server: "DashboardServer",
                 initial_buffer: bytes = b"") -> None:
        self._sock = sock
        self._server = server
        self._sendq: "thread_queue.SimpleQueue[Tuple[str, bytes]]" = thread_queue.SimpleQueue()
        self.alive = True
        self._refresh_inflight = False
        self._last_refresh = 0.0
        # bytes read past the handshake while still in the buffered reader —
        # pipelined first frames the upgrade must not lose
        self._initial_buffer = bytes(initial_buffer)

    def push_text(self, text: str) -> None:
        """Queue a text frame (thread-safe; sent by the owning reader loop).

        PHASE-4 (S5-C3): the outbound queue is CAPPED — a client that
        stops reading (zero-window TCP, backgrounded tab) must not grow
        server memory forever. On overflow the client is marked dead and
        closed; its reader loop already tolerates dead sockets.
        """
        if not self.alive:
            return
        if self._sendq.qsize() >= 256:
            self.alive = False
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        self._sendq.put(("text", wsproto.encode_text_frame(text)))

    def push_json(self, payload: Dict[str, Any]) -> None:
        self.push_text(_dumps(payload))

    def serve(self) -> None:
        """Reader loop (runs in the handler thread that did the upgrade)."""
        self._sock.settimeout(0.25)
        buffer = self._initial_buffer
        self._initial_buffer = b""
        partial_since: Optional[float] = None  # arrival clock of a partial frame
        try:
            while self.alive and not self._server._clients_closed:
                # AUDIT: a client stranded by a stop()-race registration must
                # not idle in recv ticks forever holding a handler slot.
                self._drain()
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    chunk = None
                except OSError:
                    break
                if chunk == b"":  # peer closed the connection
                    break
                if chunk is None and not buffer:
                    continue  # idle tick: no partial frame is pending
                if chunk:
                    buffer += chunk
                stalled = False
                while buffer:
                    try:
                        opcode, fin, payload, consumed = wsproto.decode_frame(buffer)
                    except wsproto.IncompleteFrame:
                        # SLOW-DRIBBLE AUDIT: a frame header that has started
                        # arriving must COMPLETE within WS_FRAME_DEADLINE_S —
                        # otherwise a 3-bytes-of-16MB client pins this thread
                        # (and its queue) forever. A PING interleaved after a
                        # partial data frame stays queued behind it (stream
                        # order) and does not reset the clock.
                        now = _time.monotonic()
                        if partial_since is None:
                            partial_since = now
                        elif now - partial_since > WS_FRAME_DEADLINE_S:
                            stalled = True
                        break  # wait for more bytes
                    except wsproto.ProtocolViolation as exc:
                        # ERROR AUDIT: fail the connection instead of staying
                        # deaf-but-alive swallowing every later frame
                        LOG.warning("websocket protocol violation: %s", exc)
                        self._sendq.put(("raw", wsproto.encode_close_frame(1002)))
                        self._drain()
                        return
                    buffer = buffer[consumed:]
                    if opcode in (wsproto.OP_CLOSE, wsproto.OP_PING):
                        if not fin:  # RFC 6455 §5.5: control frames are whole
                            LOG.warning("websocket: fragmented control frame")
                            self._sendq.put(("raw", wsproto.encode_close_frame(1002)))
                            self._drain()
                            return
                        if opcode == wsproto.OP_CLOSE:
                            # peer-initiated close: echo its status code
                            # (codeless when it carried none, 1002 when
                            # malformed — see ws.echo_close_code)
                            self._sendq.put((
                                "raw",
                                wsproto.encode_close_frame(
                                    wsproto.echo_close_code(payload)),
                            ))
                            self._drain()
                            return
                        self._sendq.put(("raw", wsproto.encode_pong(payload)))
                    elif opcode == wsproto.OP_TEXT:
                        if not fin:  # fragmentation is never reassembled —
                            # a partial message must not look like data
                            LOG.warning("websocket: fragmented text frame")
                            self._sendq.put(("raw", wsproto.encode_close_frame(1002)))
                            self._drain()
                            return
                        try:
                            text = payload.decode("utf-8")
                        except UnicodeDecodeError:
                            # RFC 6455 §8.1: invalid UTF-8 in a text frame
                            # fails the connection (1007), not a quiet U+FFFD
                            LOG.warning("websocket: invalid UTF-8 in text frame")
                            self._sendq.put(("raw", wsproto.encode_close_frame(1007)))
                            self._drain()
                            return
                        self._handle_text(text)
                    elif opcode == wsproto.OP_PONG:
                        pass  # unsolicited pongs are legal (RFC 6455 §5.5.3)
                    elif opcode in (wsproto.OP_CONT, wsproto.OP_BINARY):
                        # one-word app protocol: fragmentation and binary
                        # frames are protocol violations, not data
                        LOG.warning("websocket: unexpected frame (opcode=%d)", opcode)
                        self._sendq.put(("raw", wsproto.encode_close_frame(1002)))
                        self._drain()
                        return
                    else:
                        # reserved opcodes (0x3-0x7, 0xB-0xF): RFC 6455 §5.2
                        # requires failing the connection, not ignoring them
                        LOG.warning("websocket: reserved opcode %d", opcode)
                        self._sendq.put(("raw", wsproto.encode_close_frame(1002)))
                        self._drain()
                        return
                if stalled:
                    LOG.warning("websocket: partial frame stalled; closing")
                    self._sendq.put(("raw", wsproto.encode_close_frame(1002)))
                    self._drain()
                    return
                if not buffer:
                    partial_since = None
        finally:
            self.alive = False
            self._server.unregister_threadsafe(self)
            try:
                self._sock.close()
            except OSError:
                pass

    def _handle_text(self, text: str) -> None:
        """Minimal app-level protocol: 'refresh' forces an immediate push.

        Debounced: a flooding client cannot schedule unbounded status
        fetches — at most one in flight, with a 0.1s minimum interval.
        """
        if text.strip() != "refresh":
            return
        import time as _time
        now = _time.monotonic()
        if self._refresh_inflight or now - self._last_refresh < 0.1:
            return
        self._refresh_inflight = True
        self._last_refresh = now

        def _done(_fut: Any = None) -> None:
            self._refresh_inflight = False

        future = self._server.push_stats_to(self)
        if future is not None:
            future.add_done_callback(_done)
        else:  # loop unavailable: nothing was scheduled
            self._refresh_inflight = False

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
    """HTTP JSON + WebSocket facade over ControlAPI, on a daemon thread.

    ``port=0`` binds an ephemeral port; the REAL port is exposed via
    :attr:`bound_port` (and reflected in :attr:`url`) after :meth:`start`.
    """

    def __init__(
        self,
        api: ControlAPI,
        token: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 8070,
        bus: Optional[EventBus] = None,
        exit_signal: bool = False,
    ) -> None:
        self._api = api
        self._token = token or None  # "" behaves like "no token" everywhere
                                        # (WS + REST parity)
        self._exit_signal = bool(exit_signal)  # /api/shutdown may signal the
        # host process to exit (main.py only — never in tests/embedded use)
        self._host, self._port = host, port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._clients: List[WebSocketClient] = []
        # FINAL-PHASE (F3): shared per-tick stats snapshot (one API build
        # per broadcast second, reused by per-client refreshes)
        self._stats_snapshot: Optional[str] = None
        self._stats_ts: float = 0.0
        self._clients_closed = False
        self._stop_lock = threading.Lock()  # stop() is idempotent + concurrent-safe
        self._broadcaster: Optional[asyncio.Task] = None
        self._log_unsub: Optional[Any] = None
        self._audit_unsub: Optional[Any] = None
        self._audit_recorder: Any = None  # idempotent attach_audit guard
        self._task_unsubs: List[Any] = []
        # ControlAPI has no bus accessor (documented gap) — accept one
        # explicitly or probe the usual attribute names, duck-typed.
        self._bus: Optional[Any] = bus
        if self._bus is None:
            for attr in ("bus", "event_bus", "_bus"):
                candidate = getattr(api, attr, None)
                if candidate is not None and hasattr(candidate, "subscribe"):
                    self._bus = candidate
                    break

    @property
    def bound_port(self) -> int:
        """The REAL bound port (useful when constructed with port=0)."""
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.bound_port}"

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "DashboardServer":
        """Start serving (call from inside the running asyncio loop)."""
        self._loop = asyncio.get_running_loop()
        handler = _make_handler(self)
        self._httpd = _BoundedThreadingHTTPServer((self._host, self._port), handler)
        self._port = int(self._httpd.server_address[1])  # real port when port=0
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="dashboard", daemon=True
        )
        self._thread.start()
        self._broadcaster = self._loop.create_task(self._broadcast_loop())
        self._attach_audit_recorder()
        self._attach_task_events()
        LOG.info(
            "dashboard listening on %s (auth=%s)",
            self.url,
            "on" if self._token else "OFF",
        )
        return self

    def stop(self) -> None:
        """Shut the HTTP server + broadcaster + subscriptions down.

        Live websocket clients are torn down too: their handler threads
        would otherwise sit on open sockets until each PEER disconnects.

        AUDIT FIX: concurrent/idempotent — two racing stop() calls (or a
        second stop after the first) previously raced on ``self._httpd``
        (None-check then use) and double-cancelled; all shared state is
        now swapped out under a lock and torn down from locals.
        """
        with self._stop_lock:
            self._clients_closed = True  # a queued _register must not re-add
            clients, self._clients = list(self._clients), []
            for client in clients:
                client.alive = False
                try:
                    client._sock.shutdown(socket.SHUT_RDWR)  # noqa: SLF001 — own class
                except OSError:
                    pass
            broadcaster, self._broadcaster = self._broadcaster, None
            log_unsub, self._log_unsub = self._log_unsub, None
            audit_unsub, self._audit_unsub = self._audit_unsub, None
            task_unsubs, self._task_unsubs = list(self._task_unsubs), []
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None
        if broadcaster is not None:
            try:
                # ROUND-2 AUDIT: Task.cancel() is NOT thread-safe — a stop()
                # from a non-loop thread could wedge the task in 'cancelling'
                # forever and hang loop teardown. Hop through the loop.
                broadcaster.get_loop().call_soon_threadsafe(broadcaster.cancel)
            except RuntimeError:  # ERROR AUDIT: loop already closed
                pass
        # AUDIT FIX: the handles above were swapped into locals under the
        # lock — unsubscribe via THOSE. (The previous code re-read the
        # already-nulled attributes, so the unsubs were dead code and the
        # log/audit/bus subscriptions leaked past stop().)
        if log_unsub is not None:
            try:
                log_unsub()
            except Exception:
                pass
        if audit_unsub is not None:
            try:
                audit_unsub()
            except Exception:
                pass
        self._audit_recorder = None
        for unsub in task_unsubs:
            try:
                unsub()
            except Exception:
                pass
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()  # AUDIT FIX: release the listening socket
        if thread is not None:
            thread.join(timeout=2.0)  # shutdown() already signalled it

    def attach_logs(self, log_service: Any) -> "DashboardServer":
        """Stream every new log entry from a LogService to WS clients.

        Entries arrive as dicts or LogEntry-like objects; either way the
        frame payload is ``{"type":"log", **entry}``. The push hops to the
        main loop via ``call_soon_threadsafe`` before touching clients.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        def _on_entry(entry: Any) -> None:
            self._broadcast_payload({"type": "log", **entry_as_dict(entry)})

        self._log_unsub = log_service.subscribe(_on_entry)
        return self

    def attach_audit(self, recorder: Any) -> "DashboardServer":
        """Stream every audited interaction to WS clients (manual form).

        IDEMPOTENT: ``start()`` auto-attaches the api's recorder, and
        ``main.py``/callers attach it manually — a second call with the
        SAME recorder is a no-op (the pre-fix double subscription
        broadcast every audit entry twice, rendering duplicated lines on
        the Audit page and leaking the first unsub handle).
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._audit_recorder is recorder and self._audit_unsub is not None:
            return self  # already attached: never double-subscribe
        if self._audit_unsub is not None:  # different recorder: swap cleanly
            try:
                self._audit_unsub()
            except Exception:
                pass
            self._audit_unsub = None
        self._audit_recorder = recorder

        def _on_entry(entry: Any) -> None:
            self._broadcast_payload({"type": "audit", **entry_as_dict(entry)})

        self._audit_unsub = recorder.subscribe(_on_entry)
        return self

    def _attach_audit_recorder(self) -> None:
        """Auto-subscribe the api's audit recorder when one exists."""
        try:
            recorder = self._api.audit_recorder()
        except Exception:
            return
        if recorder is not None and hasattr(recorder, "subscribe"):
            self.attach_audit(recorder)

    def _attach_task_events(self) -> None:
        """Subscribe Task* bus events (when a bus is available — see module
        docstring for the ControlAPI gap this works around)."""
        bus = self._bus
        if bus is None:
            return

        def _on_task_event(event: Event) -> None:
            self._broadcast_payload(
                {"type": "task", "event": event.name, **event_as_dict(event)}
            )

        for event_type in TASK_EVENT_TYPES:
            self._task_unsubs.append(bus.subscribe(event_type, _on_task_event))

    def _broadcast_payload(self, payload: Dict[str, Any]) -> None:
        """Hop to the main loop, then queue one JSON payload on every client.

        Safe from any thread AND from the loop itself (call_soon_threadsafe
        is only a scheduled callback when already on the loop thread).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # PERF: the zero-clients check must happen on the LOOP side (inside
        # _push), not here — checking the roster from the firing thread
        # drops events that race a just-landing registration (the loop-side
        # re-check preserves FIFO ordering and still skips the per-event
        # JSON serialization when nobody is attached).

        def _push() -> None:
            clients = list(self._clients)
            if not clients:
                return
            text = _dumps(payload)
            for client in clients:
                client.push_text(text)

        try:
            loop.call_soon_threadsafe(_push)
        except RuntimeError:  # loop closed between check and call
            pass

    # ---------------------------------------------------------- registration
    def register_threadsafe(self, client: WebSocketClient) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._register(client), self._loop).result(timeout=2)

    async def _register(self, client: WebSocketClient) -> None:
        if not client.alive or self._clients_closed:  # no ghosts, and none
            # after stop() tore the sockets down
            if client.alive:
                # AUDIT: a client that upgraded right as stop() flipped the
                # flag would otherwise park its handler thread in recv ticks
                # forever — kill the socket so serve() unblocks.
                client.alive = False
                try:
                    client._sock.shutdown(socket.SHUT_RDWR)  # noqa: SLF001 — own class
                except OSError:
                    pass
            return  # after stop() tore the sockets down
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
    def push_stats_to(self, client: WebSocketClient) -> Optional["concurrent.futures.Future[None]"]:
        """Queue a stats snapshot for one client (async fetch, thread-safe).

        Returns the completion future (None when no loop is available) so
        callers can debounce on actual completion.

        FINAL-PHASE (F3): reuses the shared per-tick snapshot when it is
        fresh (<0.9s old — built by the broadcast loop) instead of
        building a per-client status: a refresh-storming client costs
        zero extra API builds within a tick.
        """
        if self._loop is None or self._loop.is_closed():
            return None

        async def _fetch_and_push() -> None:
            try:
                snapshot = self._stats_snapshot
                if snapshot is None or _time.monotonic() - self._stats_ts >= 0.9:
                    status = await self._api.status()
                    snapshot = _dumps({"type": "stats", **status})
                    self._stats_snapshot = snapshot
                    self._stats_ts = _time.monotonic()
                client.push_text(snapshot)
            except Exception as exc:  # never kill anything downstream
                LOG.warning("stats snapshot push failed: %s", exc)
                client.push_json({"type": "error", "error": "status snapshot failed"})

        try:
            return asyncio.run_coroutine_threadsafe(_fetch_and_push(), self._loop)
        except RuntimeError:  # loop closed between check and call
            return None

    async def _broadcast_loop(self) -> None:
        """Push a stats snapshot to every connected client once per second.

        AUDIT FIX: one API failure no longer permanently kills the loop.
        FINAL-PHASE (F3): the payload is stored as the shared snapshot —
        per-client refreshes within the tick reuse it (one build/tick).
        """
        try:
            while True:
                await asyncio.sleep(BROADCAST_INTERVAL)
                if not self._clients:
                    continue
                try:
                    status = await self._api.status()
                    # ROUND-2 AUDIT: serialization happens INSIDE the guard —
                    # a hostile snapshot (deep nesting / circular / mutated
                    # live by another thread) raised RecursionError/RuntimeError
                    # out of _dumps and KILLED this loop permanently, freezing
                    # every connected client's live view
                    payload = _dumps({"type": "stats", **status})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("broadcast snapshot failed: %s", exc)
                    continue
                # FINAL-PHASE (F3): shared per-tick snapshot — per-client
                # refreshes within the tick reuse it (one build/tick)
                self._stats_snapshot = payload
                self._stats_ts = _time.monotonic()
                for client in list(self._clients):
                    client.push_text(payload)
        except asyncio.CancelledError:
            return

    # ---------------------------------------------------------------- routing
    def call(
        self,
        method: str,
        path: str,
        body: Dict[str, Any],
        query: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Execute an API call on the main loop; returns (http_status, payload).

        ERROR AUDIT FIX: timeouts now return 504 with a real message (was an
        empty 500 error string); unexpected failures are logged with the
        traceback and reported as 500 instead of masquerading as 400s.
        """
        if self._loop is None or self._loop.is_closed():
            return 503, {"ok": False, "error": "main loop unavailable"}
        future = asyncio.run_coroutine_threadsafe(
            self._route(method, path, body, query or {}), self._loop
        )
        # scenario runs outlive the generic route budget (the runner itself
        # bounds them at ~185s incl. teardown) — a 10s 504 would drop the
        # report while the run still completes server-side
        timeout = (
            200.0 if (method, path) == ("POST", "/api/simulation/run")
            else ROUTE_TIMEOUT_S
        )
        try:
            return future.result(timeout=timeout)
        except (concurrent.futures.TimeoutError, asyncio.TimeoutError, TimeoutError):
            # all three spelled: they only alias each other on 3.11+
            return 504, {
                "ok": False,
                "error": f"{method} {path} timed out after {timeout:.0f}s (may still complete)",
            }
        except concurrent.futures.CancelledError:
            # loop shutdown racing a request — CancelledError is a
            # BaseException and used to escape into handle_error as a
            # raw traceback with no response
            return 503, {"ok": False, "error": "server shutting down"}
        except Exception as exc:
            LOG.exception("dashboard API call failed: %s %s", method, path)
            return 500, {"ok": False, "error": f"internal error ({type(exc).__name__})"}

    async def _route(
        self, method: str, path: str, body: Dict[str, Any], query: Dict[str, List[str]]
    ) -> Tuple[int, Dict[str, Any]]:
        """Map a request to one ControlAPI call. Runs ON the main loop.

        Every route returns ``{ok: true, data}``; domain errors
        (ValueError/RuntimeError family) become honest 400s and anything
        unexpected becomes a logged 500.
        """
        api = self._api
        try:
            # ---------------------------------------------------------------- system
            if (method, path) == ("GET", "/api/status"):
                return 200, {"ok": True, "data": await api.status()}
            if (method, path) == ("POST", "/api/estop"):
                return 200, {"ok": True, "data": await api.emergency_stop_all(surface="dashboard")}
            if (method, path) == ("POST", "/api/shutdown"):
                if self._exit_signal:
                    # scheduler + accounts stop now; the PROCESS exits through
                    # the graceful path — a delayed SIGINT (what Ctrl-C sends)
                    # unwinds run_app: REPL readline raises KeyboardInterrupt /
                    # headless's signal handler sets its stop event, and the
                    # finally-block owns the ordered teardown (persistence
                    # drains, then db.close). The delay lets this response
                    # reach the browser first. Opt-in (main.py) so embedded
                    # servers (tests) never signal their host process.
                    def _request_exit() -> None:
                        os.kill(os.getpid(), signal.SIGINT)

                    timer = threading.Timer(0.25, _request_exit)
                    timer.daemon = True  # never fire during interpreter exit
                    timer.start()
                return 200, {"ok": True, "data": await api.shutdown()}
            if (method, path) == ("POST", "/api/resume"):
                return 200, {"ok": True, "data": await api.system_resume()}
            # -------------------------------------------------------------- accounts
            if (method, path) == ("GET", "/api/accounts"):
                return 200, {"ok": True, "data": await api.accounts_list()}
            if (method, path) == ("POST", "/api/accounts/add"):
                require_str(body, "name", path)  # api re-validates the full payload
                if "enabled" in body:
                    body["enabled"] = parse_json_bool(body["enabled"], "enabled")
                # a 'credential' in the body flows ONE way: request ->
                # CredentialService (encrypted at rest); the response data
                # is the account status dict, never the value. A duplicate
                # name is a conflict, not a bad request.
                try:
                    return 200, {"ok": True, "data": await api.accounts_add(body)}
                except AccountError as exc:
                    if "already exists" in str(exc):
                        return 409, {"ok": False, "error": str(exc)}
                    raise
            if (method, path) == ("POST", "/api/accounts/remove"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_remove(require_str(body, "name", path)),
                }
            if (method, path) == ("GET", "/api/accounts/info"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_info(q_str(query, "account", path)),
                }
            if (method, path) == ("POST", "/api/accounts/enable"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_enable(
                        require_str(body, "name", path),
                        parse_json_bool(require_field(body, "enabled", path), "enabled"),
                    ),
                }
            if (method, path) == ("POST", "/api/accounts/start"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_start(body_opt_str(body, "account", path)),
                }
            if (method, path) == ("POST", "/api/accounts/stop"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_stop(body_opt_str(body, "account", path)),
                }
            if (method, path) == ("POST", "/api/accounts/emergency-stop"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_emergency_stop(require_str(body, "name", path)),
                }
            if (method, path) == ("POST", "/api/accounts/credential"):
                # credential VALUE: one-way (request -> CredentialService).
                # The response carries the account name + new STATUS only —
                # grep-proof by construction, nothing echoes the token.
                return 200, {
                    "ok": True,
                    "data": await api.accounts_set_credential(
                        require_str(body, "name", path),
                        require_str(body, "credential", path),
                        surface="dashboard",
                    ),
                }

            # ------------------------------------------------ account plugins
            # Direct account→plugin capability assignment (the profile-
            # mediated model is gone from the UI; these are the surfaces
            # the dashboard drives). Unknown account/plugin names are
            # domain errors from the api -> honest 400s below.
            # ------------------------------------------------------- UI extension
            if (method, path) == ("POST", "/api/plugins/reload"):
                return 200, {"ok": True, "data": await api.plugins_reload(
                    require_str(body, "plugin", path), surface="dashboard")}
            if (method, path) == ("POST", "/api/plugins/unload"):
                return 200, {"ok": True, "data": await api.plugins_unload(
                    require_str(body, "plugin", path), surface="dashboard")}

            if (method, path) == ("GET", "/api/ui/stats"):
                return 200, {"ok": True, "data": await api.ui_stats(
                    q_str(query, "account", path))}
            if (method, path) == ("GET", "/api/ui/panels"):
                return 200, {"ok": True, "data": await api.ui_panels()}

            # ------------------------------------------- dashboard UI (Stage D)
            # Builder-based plugin pages: navigation for the sidebar, one
            # page's serialized component tree, action dispatch and data
            # providers. Unknown plugin/page/provider is a MISSING READ
            # (404), not a client payload fault — action dispatch errors
            # stay UIError/ValueError -> 400 below (they are domain
            # outcomes the frontend renders inline).
            if (method, path) == ("GET", "/api/dashboard-ui/navigation"):
                return 200, {"ok": True,
                             "data": await api.dashboard_ui_navigation()}
            if (method, path) == ("GET", "/api/dashboard-ui/page"):
                page = await api.dashboard_ui_page(
                    q_str(query, "plugin", path),
                    q_str(query, "page_id", path),
                    account=q_opt(query, "account"))
                if page is None:
                    return 404, {"ok": False,
                                 "error": "unknown plugin page"}
                return 200, {"ok": True, "data": page}
            if (method, path) == ("POST", "/api/dashboard-ui/action"):
                payload = body.get("payload")
                if payload is None:
                    payload = {}
                if not isinstance(payload, dict):
                    return 400, {"ok": False,
                                 "error": "field 'payload' in POST "
                                          "/api/dashboard-ui/action must "
                                          "be an object"}
                account = body.get("account")
                if account is not None and not isinstance(account, str):
                    return 400, {"ok": False,
                                 "error": "field 'account' must be a string"}
                return 200, {
                    "ok": True,
                    "data": await api.dashboard_ui_action(
                        require_str(body, "plugin", path),
                        require_str(body, "component_id", path),
                        payload,
                        surface="dashboard",
                        account=account,
                    ),
                }
            if (method, path) == ("GET", "/api/dashboard-ui/data"):
                try:
                    data = await api.dashboard_ui_data(
                        q_str(query, "plugin", path),
                        q_str(query, "component_id", path),
                        account=q_opt(query, "account"))
                except UIError as exc:
                    # unknown plugin/component, hidden, or no provider:
                    # the read cannot be served — 404, not a 400
                    return 404, {"ok": False, "error": str(exc)}
                return 200, {"ok": True, "data": data}

            # ------------------------------------------------------------ admin access
            if (method, path) == ("GET", "/api/admin/overview"):
                return 200, {"ok": True, "data": await api.admin_overview()}
            if (method, path) == ("POST", "/api/admin/ids/set"):
                return 200, {"ok": True, "data": await api.admin_set_ids(
                    [int(x) for x in require_json_list(
                        body, "admins", path)],
                    surface="dashboard")}
            if (method, path) == ("POST", "/api/admin/ids/add"):
                return 200, {"ok": True, "data": await api.admin_add_id(
                    require_int(body, "user_id", path), surface="dashboard")}
            if (method, path) == ("POST", "/api/admin/ids/remove"):
                return 200, {"ok": True, "data": await api.admin_remove_id(
                    require_int(body, "user_id", path), surface="dashboard")}
            if (method, path) == ("POST", "/api/admin/account-flag"):
                return 200, {"ok": True, "data": await api.admin_account_flag(
                    require_str(body, "account", path),
                    parse_json_bool(body.get("is_admin"), "is_admin"),
                    surface="dashboard")}
            if (method, path) == ("GET", "/api/admin/perms"):
                return 200, {"ok": True, "data": await api.admin_perms(
                    q_str(query, "account", path))}
            if (method, path) == ("POST", "/api/admin/perms/set"):
                return 200, {"ok": True, "data": await api.admin_perms_set(
                    require_str(body, "account", path),
                    [int(x) for x in require_json_list(
                        body, "allowed_user_ids", path)],
                    surface="dashboard")}
            if (method, path) == ("POST", "/api/admin/perms/add"):
                return 200, {"ok": True, "data": await api.admin_perms_add(
                    require_str(body, "account", path),
                    require_int(body, "user_id", path), surface="dashboard")}
            if (method, path) == ("POST", "/api/admin/perms/remove"):
                return 200, {"ok": True, "data": await api.admin_perms_remove(
                    require_str(body, "account", path),
                    require_int(body, "user_id", path), surface="dashboard")}
            if (method, path) == ("GET", "/api/admin/prefix"):
                return 200, {"ok": True, "data": await api.admin_prefix(
                    q_str(query, "account", path))}
            if (method, path) == ("POST", "/api/admin/prefix/set"):
                return 200, {"ok": True, "data": await api.admin_prefix_set(
                    require_str(body, "account", path),
                    require_str(body, "mode", path),
                    str(body.get("symbol") or ""),
                    surface="dashboard")}
            if (method, path) == ("POST", "/api/admin/forget-account"):
                return 200, {"ok": True, "data": await api.admin_forget_account(
                    require_str(body, "account", path), surface="dashboard")}
            if (method, path) == ("GET", "/api/admin/mudae-id"):
                return 200, {"ok": True, "data": await api.admin_mudae_id()}
            if (method, path) == ("POST", "/api/admin/mudae-id/set"):
                raw = body.get("mudae_bot_id", None)
                return 200, {"ok": True, "data": await api.admin_mudae_id_set(
                    raw, surface="dashboard")}

            if (method, path) == ("GET", "/api/accounts/plugins"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_plugins_list(
                        q_str(query, "account", path)
                    ),
                }
            if (method, path) == ("POST", "/api/accounts/plugins/assign"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_plugin_assign(
                        require_str(body, "account", path),
                        require_str(body, "plugin", path),
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/accounts/plugins/unassign"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_plugin_unassign(
                        require_str(body, "account", path),
                        require_str(body, "plugin", path),
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/accounts/plugins/set"):
                plugins = require_field(body, "plugins", path)
                if not isinstance(plugins, list) or not all(
                        isinstance(p, str) for p in plugins):
                    raise ValueError(
                        f"field 'plugins' in POST {path} must be a list"
                        " of plugin names"
                    )
                return 200, {
                    "ok": True,
                    "data": await api.accounts_plugins_set(
                        require_str(body, "account", path),
                        plugins,
                        surface="dashboard",
                    ),
                }

            # -------------------------------------------- account plugin profiles
            # Profile bindings + merged settings + the channel picker's
            # data source (the profile CRUD lives under /api/plugins/*).
            # 'profile' may be null = clear the binding (plugin defaults).
            if (method, path) == ("GET", "/api/accounts/plugin-profiles"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_plugin_profiles(
                        q_str(query, "account", path)
                    ),
                }
            if (method, path) == ("POST", "/api/accounts/plugin-profile"):
                profile = body.get("profile")
                if profile is not None and (not isinstance(profile, str)
                                            or not profile):
                    raise ValueError(
                        f"field 'profile' in POST {path} must be a non-empty"
                        " string or null")
                return 200, {
                    "ok": True,
                    "data": await api.accounts_plugin_profile(
                        require_str(body, "account", path),
                        require_str(body, "plugin", path),
                        profile,
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("GET", "/api/accounts/effective-settings"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_effective_settings(
                        q_str(query, "account", path),
                        q_str(query, "plugin", path),
                    ),
                }
            if (method, path) == ("GET", "/api/accounts/channels"):
                return 200, {
                    "ok": True,
                    "data": await api.accounts_channels(
                        q_str(query, "account", path)
                    ),
                }

            # ------------------------------------------------------- credentials
            if (method, path) == ("GET", "/api/credentials/status"):
                return 200, {"ok": True, "data": await api.accounts_credential_status()}
            if (method, path) == ("POST", "/api/credentials/import-env"):
                overwrite = body.get("overwrite")
                if overwrite is None:
                    overwrite = False  # absent/null -> False; anything else strict
                overwrite = parse_json_bool(overwrite, "overwrite")
                return 200, {
                    "ok": True,
                    "data": await api.credentials_import_env(
                        overwrite=overwrite, surface="dashboard"
                    ),
                }

            # --------------------------------------------------------- cooldowns
            if (method, path) == ("GET", "/api/cooldowns"):
                return 200, {
                    "ok": True,
                    "data": await api.cooldowns_list(q_opt(query, "account")),
                }
            if (method, path) == ("POST", "/api/cooldowns/clear"):
                return 200, {
                    "ok": True,
                    "data": await api.cooldowns_clear(
                        require_str(body, "account", path),
                        require_str(body, "plugin", path),
                        body_opt_str(body, "cooldown_key", path),
                        surface="dashboard",
                    ),
                }

            # --------------------------------------------------------------- plugins
            if (method, path) == ("GET", "/api/plugins"):
                return 200, {"ok": True, "data": await api.plugins_manifests()}
            if (method, path) == ("GET", "/api/plugins/state"):
                return 200, {
                    "ok": True,
                    "data": await api.plugins_state(q_str(query, "account", path)),
                }
            if (method, path) == ("POST", "/api/plugins/set-enabled"):
                return 200, {
                    "ok": True,
                    "data": await api.plugins_set_enabled(
                        require_str(body, "account", path),
                        require_str(body, "plugin", path),
                        parse_json_bool(require_field(body, "enabled", path), "enabled"),
                    ),
                }
            if (method, path) == ("GET", "/api/plugins/settings"):
                return 200, {
                    "ok": True,
                    "data": await api.plugins_settings(q_str(query, "account", path)),
                }
            if (method, path) == ("POST", "/api/plugins/set-setting"):
                # 'value' may be ANY JSON type (incl. null = reset to default);
                # schema validation happens in the api and 400s with the path
                return 200, {
                    "ok": True,
                    "data": await api.plugins_set_setting(
                        require_str(body, "account", path),
                        require_str(body, "plugin", path),
                        require_str(body, "key", path),
                        require_field(body, "value", path),
                    ),
                }

            # -------------------------------------------------- plugin profiles
            # Per-plugin named setting bundles (migration v7). 'settings'
            # flows through raw: schema + channels-scope validation is the
            # api's job and 400s with the offending field named.
            if (method, path) == ("GET", "/api/plugins/profiles"):
                return 200, {
                    "ok": True,
                    "data": await api.plugin_profiles_list(
                        q_opt(query, "plugin")
                    ),
                }
            if (method, path) == ("POST", "/api/plugins/profiles/create"):
                return 200, {
                    "ok": True,
                    "data": await api.plugin_profile_create(
                        require_str(body, "plugin", path),
                        require_str(body, "name", path),
                        body.get("description", ""),
                        body.get("settings"),
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/plugins/profiles/update"):
                # at least one NON-null field (nulls mean "keep" below)
                if not any(body.get(key) is not None for key in
                           ("name_new", "description", "settings")):
                    raise ValueError(
                        f"POST {path} needs at least one of "
                        "'name_new', 'description', 'settings'"
                    )
                return 200, {
                    "ok": True,
                    "data": await api.plugin_profile_update(
                        require_str(body, "plugin", path),
                        require_str(body, "name", path),
                        name_new=body_opt_str(body, "name_new", path),
                        description=body.get("description")
                        if "description" in body else None,
                        settings=body.get("settings")
                        if "settings" in body else None,
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/plugins/profiles/delete"):
                return 200, {
                    "ok": True,
                    "data": await api.plugin_profile_delete(
                        require_str(body, "plugin", path),
                        require_str(body, "name", path),
                        surface="dashboard",
                    ),
                }

            # -------------------------------------------------------------- profiles
            if (method, path) == ("GET", "/api/profiles"):
                return 200, {"ok": True, "data": await api.profiles_list()}
            if (method, path) == ("POST", "/api/profiles/create"):
                plugins = require_field(body, "plugins", path)
                if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
                    raise ValueError(
                        f"field 'plugins' in POST {path} must be a list of plugin names"
                    )
                return 200, {
                    "ok": True,
                    "data": await api.profiles_create(
                        require_str(body, "name", path),
                        plugins,
                        body.get("description", ""),
                    ),
                }
            if (method, path) == ("POST", "/api/profiles/delete"):
                return 200, {
                    "ok": True,
                    "data": await api.profiles_delete(require_str(body, "name", path)),
                }
            if (method, path) == ("POST", "/api/profiles/assign"):
                return 200, {
                    "ok": True,
                    "data": await api.profiles_assign(
                        require_str(body, "account", path),
                        require_str(body, "profile", path),
                    ),
                }
            if (method, path) == ("POST", "/api/profiles/unassign"):
                return 200, {
                    "ok": True,
                    "data": await api.profiles_unassign(
                        require_str(body, "account", path),
                        require_str(body, "profile", path),
                    ),
                }

            # ------------------------------------------------------------ sync groups
            if (method, path) == ("GET", "/api/groups"):
                return 200, {"ok": True, "data": await api.groups_list()}
            if (method, path) == ("GET", "/api/groups/info"):
                return 200, {
                    "ok": True,
                    "data": await api.groups_snapshot(q_str(query, "group", path)),
                }
            if (method, path) == ("POST", "/api/groups/create"):
                payload = {"name": require_str(body, "name", path)}
                if "description" in body and body["description"] is not None:
                    if not isinstance(body["description"], str):
                        raise ValueError(
                            f"field 'description' in POST {path} must be a string"
                        )
                    payload["description"] = body["description"]
                if "enabled" in body and body["enabled"] is not None:
                    payload["enabled"] = parse_json_bool(
                        body["enabled"], "enabled")
                return 200, {
                    "ok": True,
                    "data": await api.groups_create(payload, surface="dashboard"),
                }
            if (method, path) == ("POST", "/api/groups/update"):
                payload: Dict[str, Any] = {}
                for field in ("name", "description"):
                    if field in body and body[field] is not None:
                        if not isinstance(body[field], str):
                            raise ValueError(
                                f"field {field!r} in POST {path} must be a string"
                            )
                        payload[field] = body[field]
                if "enabled" in body and body["enabled"] is not None:
                    payload["enabled"] = parse_json_bool(
                        body["enabled"], "enabled")
                if not payload:
                    return 400, {
                        "ok": False,
                        "error": "provide at least one of 'name',"
                                 " 'description', 'enabled'",
                    }
                return 200, {
                    "ok": True,
                    "data": await api.groups_update(
                        require_str(body, "group", path), payload,
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/groups/delete"):
                detach = body.get("detach_members", False)
                if detach is None:
                    detach = False
                detach = parse_json_bool(detach, "detach_members")
                return 200, {
                    "ok": True,
                    "data": await api.groups_delete(
                        require_str(body, "group", path),
                        detach_members=detach, surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/groups/add-member"):
                return 200, {
                    "ok": True,
                    "data": await api.groups_add_account(
                        require_str(body, "group", path),
                        require_str(body, "account", path),
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/groups/remove-member"):
                return 200, {
                    "ok": True,
                    "data": await api.groups_remove_account(
                        require_str(body, "group", path),
                        require_str(body, "account", path),
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/groups/set-account"):
                return 200, {
                    "ok": True,
                    "data": await api.groups_set_account(
                        require_str(body, "account", path),
                        require_str(body, "group", path),
                        surface="dashboard",
                    ),
                }
            if (method, path) == ("POST", "/api/groups/clear-account"):
                return 200, {
                    "ok": True,
                    "data": await api.groups_clear_account(
                        require_str(body, "account", path),
                        surface="dashboard",
                    ),
                }

            # ------------------------------------------------------------ rate limits
            if (method, path) == ("GET", "/api/ratelimit/policies"):
                return 200, {"ok": True, "data": await api.ratelimit_policies()}
            if (method, path) == ("POST", "/api/ratelimit/set"):
                action = require_str(body, "action", path)
                interval = require_finite(require_number(body, "interval", path), "interval", path)
                jitter_min = body_opt_number(body, "jitter_min", 0.0, path)
                jitter_max = body_opt_number(body, "jitter_max", 0.0, path)
                level = body.get("level", "global")
                if not isinstance(level, str) or not level:
                    raise ValueError(f"field 'level' in POST {path} must be a non-empty string")
                return 200, {
                    "ok": True,
                    "data": await api.ratelimit_set(
                        action, interval, jitter_min, jitter_max, level,
                        body_opt_owner(body, path),
                    ),
                }
            if (method, path) == ("POST", "/api/ratelimit/clear"):
                return 200, {
                    "ok": True,
                    "data": await api.ratelimit_clear(
                        require_str(body, "level", path),
                        body_opt_owner(body, path),
                        require_str(body, "action", path),
                    ),
                }
            if (method, path) == ("GET", "/api/ratelimit/effective"):
                return 200, {
                    "ok": True,
                    "data": await api.ratelimit_effective(
                        q_str(query, "action", path),
                        q_opt(query, "account"),
                        q_opt(query, "plugin"),
                    ),
                }
            if (method, path) == ("GET", "/api/ratelimit/stats"):
                return 200, {"ok": True, "data": await api.ratelimit_stats()}
            if (method, path) == ("GET", "/api/ratelimit/warnings"):
                return 200, {"ok": True, "data": await api.ratelimit_warnings()}
            if (method, path) == ("POST", "/api/ratelimit/floor"):
                seconds = require_finite(
                    require_number(body, "seconds", path), "seconds", path
                )
                return 200, {"ok": True, "data": await api.ratelimit_set_floor(seconds)}
            if (method, path) == ("GET", "/api/ratelimit/lane"):
                return 200, {"ok": True, "data": await api.ratelimit_lane()}
            if (method, path) == ("POST", "/api/ratelimit/lane"):
                enabled = body.get("enabled")
                if enabled is not None and not isinstance(enabled, bool):
                    return 400, {"ok": False, "error": "field 'enabled' must be a boolean"}
                prefixrate = body.get("prefixrate")
                if prefixrate is not None:
                    try:
                        prefixrate = float(prefixrate)
                    except (TypeError, ValueError):
                        return 400, {"ok": False, "error": "field 'prefixrate' must be a number"}
                    prefixrate = require_finite(prefixrate, "prefixrate", path)
                    if prefixrate < 0:
                        return 400, {"ok": False, "error": "prefixrate must be >= 0 (0 pauses the lane)"}
                if enabled is None and prefixrate is None:
                    return 400, {"ok": False, "error": "provide 'enabled' and/or 'prefixrate'"}
                return 200, {
                    "ok": True,
                    "data": await api.ratelimit_set_lane(enabled, prefixrate),
                }

            # ------------------------------------------------------------------ tasks
            if (method, path) == ("GET", "/api/tasks"):
                return 200, {
                    "ok": True,
                    "data": await api.tasks_list(
                        status=q_opt(query, "status"), account=q_opt(query, "account")
                    ),
                }
            if (method, path) == ("POST", "/api/tasks/cancel"):
                return 200, {
                    "ok": True,
                    "data": await api.tasks_cancel(require_int(body, "task_id", path)),
                }
            if (method, path) == ("POST", "/api/tasks/cancel-matching"):
                return 200, {
                    "ok": True,
                    "data": await api.tasks_cancel_matching(
                        account=body_opt_str(body, "account", path),
                        plugin=body_opt_str(body, "plugin", path),
                    ),
                }
            if (method, path) == ("GET", "/api/tasks/history"):
                return 200, {
                    "ok": True,
                    "data": await api.tasks_history(
                        limit=q_int(query, "limit", 50, path),
                        account=q_opt(query, "account"),
                    ),
                }

            # ------------------------------------------------------------- simulation
            if (method, path) == ("GET", "/api/simulation/list"):
                return 200, {"ok": True, "data": await api.simulation_list()}
            if (method, path) == ("POST", "/api/simulation/run"):
                return 200, {
                    "ok": True,
                    "data": await api.simulation_run(require_str(body, "name", path)),
                }

            # ------------------------------------------------------- logs + audit
            if (method, path) == ("GET", "/api/logs"):
                return 200, {
                    "ok": True,
                    "data": await api.logs_tail(
                        n=q_int(query, "n", 100, path),
                        account=q_opt(query, "account"),
                        severity=q_opt(query, "severity"),
                    ),
                }
            if (method, path) == ("GET", "/api/audit"):
                return 200, {
                    "ok": True,
                    "data": await api.audit_tail(
                        n=q_int(query, "n", 100, path),
                        account=q_opt(query, "account"),
                    ),
                }

            return 404, {"ok": False, "error": f"no route {method} {path}"}
        except DatabaseError as exc:
            # server-side persistence fault: NOT the client's bad request.
            # Detail goes to the log; the client gets the class only.
            LOG.error("dashboard route database error: %s %s: %s", method, path, exc)
            return 500, {"ok": False, "error": f"database error ({type(exc).__name__})"}
        except (ValueError, RuntimeError) as exc:
            # domain errors -> honest 400 (AccountError/SchedulerError are
            # RuntimeError; RatePolicy/Setting/Profile validation ValueError)
            return 400, {"ok": False, "error": str(exc)}
        except Exception as exc:  # unexpected -> honest 500 + traceback
            LOG.exception("dashboard route failed: %s %s", method, path)
            return 500, {"ok": False, "error": f"internal error ({type(exc).__name__})"}


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a concurrent-connection cap.

    The stock server spawns one thread per accepted connection with NO
    bound — 500 opened sockets (never mind requests) pin 500 threads.
    Above MAX_CONCURRENT_CONNECTIONS live handlers the next connection is
    accepted and immediately closed; the accept loop itself never blocks,
    so a socket flood cannot wedge the healthy clients.
    """

    def __init__(self, address: Any, handler: type) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)
        super().__init__(address, handler)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            LOG.warning("dashboard connection shed (over %d concurrent handlers)",
                        MAX_CONCURRENT_CONNECTIONS)
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _make_handler(server: "DashboardServer") -> type:
    """Build the request-handler class closing over the server instance."""

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1: enables keep-alive so the browser's connection pool
        # doesn't race the HTTP/1.0 per-request close (fixes 'server
        # returned 200 with a non-JSON body' — fetch() getting a truncated
        # body when ThreadingHTTPServer closed the connection mid-read)
        protocol_version = "HTTP/1.1"
        timeout = HTTP_SOCKET_TIMEOUT_S  # stalled requests free their thread
        # AUDIT: unbuffered reads — nothing may sit in a userspace read
        # buffer past the request headers, or bytes a WS client pipelined
        # with its handshake would be lost when the socket is detached
        # (they now stay in the kernel buffer, visible after detach).
        rbufsize = 0
        # AUDIT: the default banner leaks the stdlib/Python version on every
        # response (incl. stdlib-generated error pages).
        server_version = "STMudae"
        sys_version = ""

        def end_headers(self) -> None:  # noqa: N802
            # stdlib error pages (501 for unimplemented methods, malformed
            # request 400s) bypass _respond — this override carries the
            # security headers onto EVERY response, including them.
            # (Appending after send_error() returned was a no-op: the
            # headers had already been flushed.)
            try:
                for header, value in SECURITY_HEADERS:
                    self.send_header(header, value)
            except OSError:
                pass
            super().end_headers()

        def send_error(self, code: int, message: Optional[str] = None,
                       explain: Optional[str] = None) -> None:  # noqa: N802
            try:
                super().send_error(code, message, explain)
            except OSError:
                pass  # peer vanished mid-error — nothing to answer

        def _authorized(self, provided: Optional[str]) -> bool:
            """Accept the token via X-Dashboard-Token header OR ?token= query.

            Constant-time comparison (hmac): no timing side channel on the
            token. An empty provided value never matches (WS + REST parity).
            """
            if server._token is None:
                return True
            if provided is None:
                return False
            provided = provided.strip()
            if not provided:
                return False
            return hmac.compare_digest(
                provided.encode("utf-8"), server._token.encode("utf-8")
            )

        def _origin_allowed(self) -> bool:
            """Reject cross-site requests (CSRF / DNS rebinding / WS hijacking).

            Any web page the operator visits can fire requests at a loopback
            service, so the dashboard — which can start accounts and send
            messages — must not trust the network position alone:
              * a loopback-bound server only accepts loopback ``Host`` values
                (defeats DNS rebinding);
              * a browser-supplied ``Origin`` must match the ``Host`` it
                addressed (defeats cross-site fetch/XHR and WebSocket).
            Non-browser clients (curl, tests) send no Origin and still work.
            """
            host_header = self.headers.get("Host", "")
            if _is_loopback_host(server._host):
                if not _is_loopback_host(_split_host(host_header)):
                    return False
            origin = self.headers.get("Origin")
            if origin is not None:
                if origin.strip().lower() == "null":  # sandboxed iframe / file://
                    return False
                netloc = urlparse(origin).netloc.lower()
                if netloc != host_header.strip().lower():
                    return False
            return True

        def _respond(
            self, status: int, payload: Dict[str, Any], content_type: str = "application/json",
            *, close: bool = False,
        ) -> None:
            if content_type == "application/json":
                # allow_nan=False: a stored NaN must not serialize to the
                # bare `NaN` token (invalid JSON — breaks every JSON client).
                # TypeError (datetime/set/exception in the payload) must
                # answer with JSON, not crash the handler thread and drop
                # the connection mid-response.
                try:
                    data = json.dumps(payload, allow_nan=False).encode("utf-8")
                except ValueError:
                    data = json.dumps(
                        {"ok": False, "error": "response contained a non-finite number"}
                    ).encode("utf-8")
                except TypeError:
                    data = json.dumps(
                        {"ok": False, "error": "response was not JSON-serializable"}
                    ).encode("utf-8")
                except RuntimeError:
                    # ROUND-2 AUDIT: a stats dict mutated by another thread
                    # mid-serialization must not kill the handler thread
                    data = json.dumps(
                        {"ok": False, "error": "response changed during serialization"}
                    ).encode("utf-8")
            else:
                data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            # security headers ride the end_headers() override — one
            # source of truth covering _respond AND stdlib error pages
            if close or self.close_connection:
                # tell the client the socket will not be reused (a drained
                # GET body / rejected request leaves no parseable stream)
                self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(data)))
            try:
                # ROUND-2 AUDIT: the header flush is a socket write too — a
                # peer that vanished before end_headers() raised out of
                # _respond and dumped a traceback via socketserver
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass  # ERROR AUDIT: client aborted mid-response — not an error worth a traceback

        def _content_length(self) -> int:
            """Single-valued Content-Length (ROUND-3 duplicate-header audit).

            Identical duplicate values collapse to one (RFC 7230 §3.3.2
            allows recipients to merge them); CONFLICTING values raise
            ``_ConflictingContentLength`` and garbage values raise
            ``ValueError`` — callers decide how to answer.
            """
            values = [v.strip() for v in (self.headers.get_all("Content-Length") or [])]
            values = [v for v in values if v]  # present-but-empty == absent
            distinct = set(values)
            if len(distinct) > 1:
                raise _ConflictingContentLength(
                    "conflicting duplicate Content-Length headers"
                )
            if not distinct:
                return 0
            return int(distinct.pop())  # ValueError on garbage -> caller answers

        def _reject(
            self, status: int, payload: Dict[str, Any], content_type: str = "application/json"
        ) -> None:
            """Respond + CLOSE the connection (keep-alive safety).

            Used on paths that answer BEFORE the request body was consumed
            (pre-auth rejects, header rejects): with HTTP/1.1 keep-alive the
            unread body would be parsed as the NEXT request line and corrupt
            the connection. The explicit ``Connection: close`` header lets
            browser pools drop the socket instead of reusing it.
            """
            self.close_connection = True
            self._respond(status, payload, content_type, close=True)

        def _handle(self, method: str) -> None:
            if not self._origin_allowed():
                self._reject(403, {"ok": False, "error": "cross-site request refused"})
                return
            if method != "POST":
                # a non-POST body is never consumed by any handler; drain it
                # bounded (and refuse the connection reuse) so unread bytes
                # cannot be parsed as the NEXT request line on the keep-alive
                # stream (an upgrade request carrying a body would likewise
                # corrupt the websocket frame stream)
                if self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                else:
                    try:
                        extra = self._content_length()
                    except _ConflictingContentLength:
                        # ROUND-3: smuggling family — refuse, do not guess
                        self._reject(400, {
                            "ok": False,
                            "error": "conflicting duplicate Content-Length headers",
                        })
                        return
                    except ValueError:
                        extra = -1
                    if extra:
                        remaining = extra
                        deadline = _time.monotonic() + HTTP_BODY_DEADLINE_S
                        while remaining > 0 and remaining <= 16 * MAX_BODY_BYTES:
                            if _time.monotonic() > deadline:
                                # trickle-drain pinned a handler thread
                                # for hours without this bound
                                self.close_connection = True
                                break
                            try:
                                chunk = self.rfile.read(min(remaining, 65536))
                            except OSError:
                                break  # ROUND-2 AUDIT: stalled/dead peer mid-drain
                            if not chunk:
                                break
                            remaining -= len(chunk)
                        self.close_connection = True
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/ws":
                self._handle_websocket(query)
                return
            if method == "GET" and path in ("/", "/index.html"):
                # the SPA is served WITHOUT auth: it prompts for the token
                # itself and the demo-link flow needs it loadable first
                index = STATIC_DIR / "index.html"
                if index.is_file():
                    self._respond(200, index.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._respond(404, {"ok": False, "error": "index.html missing"})
                return
            if method == "GET" and (path.startswith("/styles/") or path.startswith("/js/")):
                # Frontend assets (CSS/JS) of the SPA — same no-auth rule as
                # the index: they must load before the token is known and
                # contain no secrets. Resolved inside STATIC_DIR with a
                # traversal guard; correct MIME (X-Content-Type-Options:
                # nosniff is set on every response).
                asset = (STATIC_DIR / path.lstrip("/")).resolve()
                if not asset.is_relative_to(STATIC_DIR.resolve()) or not asset.is_file():
                    self._reject(404, {"ok": False, "error": "not found"})
                    return
                mime = ("text/css; charset=utf-8" if asset.suffix == ".css"
                        else "text/javascript; charset=utf-8")
                self._respond(200, asset.read_bytes(), mime)
                return
            if not self._authorized(
                self.headers.get("X-Dashboard-Token") or (query.get("token") or [None])[0]
            ):
                self._reject(
                    401,
                    {"ok": False, "error": "bad or missing token (X-Dashboard-Token header or ?token=)"},
                )
                return
            body: Dict[str, Any] = {}
            if method == "POST":
                if self.headers.get("Transfer-Encoding"):
                    # chunked bodies are unsupported: answering without
                    # consuming them poisons the keep-alive stream
                    self._reject(411, {
                        "ok": False,
                        "error": "Transfer-Encoding not supported — send Content-Length",
                    })
                    return
                try:
                    length = self._content_length()
                except ValueError as exc:
                    # ROUND-3: covers BOTH garbage values and conflicting
                    # duplicates (smuggling family) — answer and close
                    self._reject(400, {
                        "ok": False,
                        "error": str(exc) or "invalid Content-Length header",
                    })
                    return
                if length < 0:  # read(-1) would block until the peer closes
                    self._reject(400, {"ok": False, "error": "invalid Content-Length header"})
                    return
                if length > MAX_BODY_BYTES:
                    # drain a bounded amount so the client's send completes,
                    # then reject (a bare early-close breaks the client's write)
                    remaining = length
                    deadline = _time.monotonic() + HTTP_BODY_DEADLINE_S
                    while remaining > 0 and remaining <= 16 * MAX_BODY_BYTES:
                        if _time.monotonic() > deadline:
                            self.close_connection = True
                            break
                        try:
                            chunk = self.rfile.read(min(remaining, 65536))
                        except OSError:
                            break  # ROUND-2 AUDIT: stalled/dead peer mid-drain
                        if not chunk:
                            break
                        remaining -= len(chunk)
                    self._reject(
                        413,
                        {"ok": False, "error": f"request body too large (> {MAX_BODY_BYTES} bytes)"},
                    )
                    return
                if length:
                    deadline = _time.monotonic() + HTTP_BODY_DEADLINE_S
                    try:
                        # ROUND-3 AUDIT: read in chunks under a TOTAL deadline
                        # — a peer trickling 1 byte per recv-timeout could
                        # otherwise pin this handler slot for length×timeout.
                        chunks = []
                        remaining = length
                        while remaining > 0:
                            if _time.monotonic() > deadline:
                                raise OSError("body deadline exceeded")
                            chunk = self.rfile.read(min(remaining, 65536))
                            if not chunk:
                                # EOF with a short body: keep the pinned
                                # contract — parse what arrived and let the
                                # JSON layer reject it with 400
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        raw = b"".join(chunks)
                    except OSError:
                        # ROUND-2 AUDIT: lying Content-Length (claim 5, send 3,
                        # hold the socket) pinned this thread for the full
                        # socket timeout and then crashed it with a traceback —
                        # answer 408 and close instead (also covers the
                        # dribble deadline above)
                        self._reject(408, {
                            "ok": False,
                            "error": "request body incomplete (connection stalled)",
                        })
                        return
                    ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                    if ctype not in ("", "application/json"):
                        # AUDIT: classic JSON-CSRF enabler (text/plain forms);
                        # origin checks already block browsers, this is
                        # defense-in-depth for future non-loopback deploys.
                        self._respond(415, {
                            "ok": False,
                            "error": "Content-Type must be application/json for API POSTs",
                        })
                        return
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._respond(400, {"ok": False, "error": "invalid JSON body"})
                        return
                    except RecursionError:
                        # AUDIT: a deeply-nested [[[[… body raises this from
                        # the JSON scanners — answer, don't crash the thread.
                        self._respond(400, {"ok": False, "error": "JSON nesting too deep"})
                        return
                    if not isinstance(body, dict):
                        self._respond(400, {"ok": False, "error": "request body must be a JSON object"})
                        return
            # credential-material routes additionally require a CONFIGURED
            # token: with auth off entirely they are refused (403) instead of
            # silently writable. Runs after the general check above, so a
            # configured-but-wrong token has already been answered with 401.
            if (method, path) in CREDENTIAL_GATED_ROUTES or (
                (method, path) == ("POST", "/api/accounts/add")
                and isinstance(body, dict)
                and body.get("credential") not in (None, "")
            ):
                if server._token is None:
                    self._respond(403, {"ok": False, "error": CREDENTIAL_GATE_MESSAGE})
                    return
            status, payload = server.call(method, path, body, query)
            self._respond(status, payload)

        def _handle_websocket(self, query: Dict[str, List[str]]) -> None:
            # ERROR AUDIT: validate the upgrade before hijacking the socket
            if self.command != "GET":
                # ROUND-3 DESYNC AUDIT: a non-GET body is never consumed
                # (the drain above covers non-POST only, and this path
                # returns before the POST body read) — answering without
                # Connection: close left the unread body to be parsed as
                # the NEXT request line on the keep-alive stream
                self._reject(
                    400,
                    {"ok": False, "error": "not a websocket upgrade request (need GET + Upgrade: websocket)"},
                )
                return
            if (
                self.headers.get("Upgrade", "").lower() != "websocket"
                or "upgrade" not in self.headers.get("Connection", "").lower()
            ):
                # GET upgrade failures: any GET body was drained above, so
                # the stream is clean — keep-alive reuse stays safe here
                self._respond(
                    400,
                    {"ok": False, "error": "not a websocket upgrade request (need GET + Upgrade: websocket)"},
                )
                return
            if self.headers.get("Sec-WebSocket-Version") != "13":
                self._respond(
                    400,
                    {"ok": False, "error": "unsupported Sec-WebSocket-Version (need 13)"},
                )
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self._respond(400, {"ok": False, "error": "missing Sec-WebSocket-Key"})
                return
            try:
                handshake = wsproto.handshake_response(key)
            except ValueError:  # incl. non-ASCII keys (UnicodeEncodeError)
                self._respond(400, {"ok": False, "error": "malformed Sec-WebSocket-Key"})
                return
            if not self._authorized(
                (query.get("token") or [None])[0] or self.headers.get("X-Dashboard-Token")
            ):
                self._reject(
                    401,
                    {"ok": False, "error": "bad or missing ws token (use ?token= or the X-Dashboard-Token header)"},
                )
                return
            # the socket is about to be HIJACKED (detached from the handler):
            # the stdlib keep-alive loop must never try to read another
            # request from the detached file object after serve() returns
            self.close_connection = True
            # AUDIT: bytes already buffered in rfile when the 101 is sent
            # (client pipelined its first frames with the handshake) belong
            # to the WS stream — hand them to the client instead of losing
            # them with the detached reader. peek() would BLOCK waiting for
            # more wire bytes, so the socket is flipped non-blocking for the
            # harvest and restored afterwards.
            leftover = b""
            try:
                self.connection.sendall(handshake)
            except OSError:
                return  # peer vanished before the 101 — nothing to serve
            fd = self.connection.detach()  # we own the socket now
            sock = socket.socket(fileno=fd)
            client = WebSocketClient(sock, server, initial_buffer=leftover)
            try:
                server.register_threadsafe(client)
            except Exception:
                LOG.exception("websocket registration failed; closing socket")
                client.alive = False
                # the registration coroutine may ALREADY be queued on the
                # loop (run_coroutine_threadsafe fired before the timeout):
                # mark dead (checked by _register) and schedule an explicit
                # unregister so no ghost client keeps receiving pushes
                server.unregister_threadsafe(client)
                try:
                    sock.close()
                except OSError:
                    pass
                return
            client.serve()  # blocks this handler thread until the socket closes

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            self._handle("POST")

        def log_message(self, fmt: str, *args: Any) -> None:  # route to our logger
            # redact the query: the ?token= demo-link flow would otherwise
            # persist auth tokens into DEBUG logs on every page load
            try:
                request = fmt % args
                if "?" in request:
                    request = request.split("?", 1)[0] + " ?[redacted]"
            except Exception:
                request = fmt
            LOG.debug("%s - %s", self.address_string(), request)

    return Handler
