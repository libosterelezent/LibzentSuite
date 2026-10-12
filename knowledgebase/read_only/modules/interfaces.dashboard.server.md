# Module `interfaces.dashboard.server`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `interfaces/dashboard/server.py`

```
Dashboard backend: JSON REST + WebSocket live push over ControlAPI.

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
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `_ConflictingContentLength` | exception | no | Duplicate Content-Length headers with DIFFERENT values. |
| `WebSocketClient` | class | yes | One browser connection: outbound queue drained by its reader thread. |
| `DashboardServer` | class | yes | HTTP JSON + WebSocket facade over ControlAPI, on a daemon thread. |
| `_BoundedThreadingHTTPServer` | class | no | ThreadingHTTPServer with a concurrent-connection cap. |

### `WebSocketClient`

- `def push_text(self, text: str) -> None`
  - Queue a text frame (thread-safe; sent by the owning reader loop).
- `def push_json(self, payload: Dict[str, Any]) -> None`
- `def serve(self) -> None`
  - Reader loop (runs in the handler thread that did the upgrade).

### `DashboardServer`

- `def bound_port(self) -> int`
  - The REAL bound port (useful when constructed with port=0).
- `def url(self) -> str`
- `def start(self) -> 'DashboardServer'` — side effects: `creates_tasks`
  - Start serving (call from inside the running asyncio loop).
- `def stop(self) -> None`
  - Shut the HTTP server + broadcaster + subscriptions down.
- `def attach_logs(self, log_service: Any) -> 'DashboardServer'`
  - Stream every new log entry from a LogService to WS clients.
- `def attach_audit(self, recorder: Any) -> 'DashboardServer'`
  - Stream every audited interaction to WS clients (manual form).
- `def register_threadsafe(self, client: WebSocketClient) -> None`
- `def unregister_threadsafe(self, client: WebSocketClient) -> None`
- `def push_stats_to(self, client: WebSocketClient) -> Optional['concurrent.futures.Future[None]']`
  - Queue a stats snapshot for one client (async fetch, thread-safe).
- `def call(self, method: str, path: str, body: Dict[str, Any], query: Optional[Dict[str, List[str]]]=None) -> Tuple[int, Dict[str, Any]]`
  - Execute an API call on the main loop; returns (http_status, payload).

### `_BoundedThreadingHTTPServer`

- `def process_request(self, request: socket.socket, client_address: Any) -> None`
- `def process_request_thread(self, request: socket.socket, client_address: Any) -> None`

## Functions

- `def parse_json_bool(value: Any, field: str) -> bool`
  - Strict boolean parsing for API payloads.
- `def require_field(body: Dict[str, Any], key: str, route: str) -> Any`
  - Fetch a required body field with an actionable 400 message.
- `def require_str(body: Dict[str, Any], key: str, route: str) -> str`
  - Required non-empty string field (actionable 400 on missing/empty/wrong type).
- `def require_number(body: Dict[str, Any], key: str, route: str) -> float`
  - Required numeric field; finite-ness is checked separately by callers.
- `def require_finite(value: float, key: str, route: str) -> float`
  - Reject NaN/inf numbers (NaN slips past plain comparisons).
- `def require_int(body: Dict[str, Any], key: str, route: str) -> int`
  - Required integer field (bools excluded; integral floats tolerated).
- `def require_json_list(body: Dict[str, Any], key: str, route: str) -> List[Any]`
  - Required list field whose items must all be integers.
- `def q_str(query: Dict[str, List[str]], key: str, route: str) -> str`
  - Required non-empty query-string parameter (actionable 400 when absent).
- `def q_opt(query: Dict[str, List[str]], key: str) -> Optional[str]`
  - Optional query-string parameter; empty/absent becomes None.
- `def q_int(query: Dict[str, List[str]], key: str, default: int, route: str) -> int`
  - Integer query-string parameter with a default; 400 on garbage.
- `def body_opt_str(body: Dict[str, Any], key: str, route: str) -> Optional[str]`
  - Optional string body field; null/empty -> None; non-string -> 400.
- `def body_opt_number(body: Dict[str, Any], key: str, default: float, route: str) -> float`
  - Optional numeric body field (finite-checked); absent -> default.
- `def body_opt_owner(body: Dict[str, Any], route: str) -> Optional[str]`
  - Policy owner: null/"" means global scope; must be a string otherwise.
- `def entry_as_dict(entry: Any) -> Dict[str, Any]`
  - Normalize a log/audit entry (dict or dataclass-like) to a JSON dict.
- `def event_as_dict(event: Event) -> Dict[str, Any]`
  - JSON-safe dict of a bus event (dataclass fields; falls back to vars).
