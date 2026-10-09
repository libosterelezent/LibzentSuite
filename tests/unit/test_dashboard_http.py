"""Dashboard over real HTTP: REST auth, routing, validation, static index.

Uses a duck-typed Fake ControlAPI (records every call, returns canned
realistic payloads shaped like the manager outputs in core/api.py
docstrings + INTEGRATION.md) — no real managers are constructed.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import time
import unittest
import urllib.error
import urllib.request

import interfaces.dashboard.server as server_module
from core.errors import AccountError
from interfaces.dashboard.server import DashboardServer

TOKEN = "s3cret-token"

# --------------------------------------------------------------- canned data
ACCOUNTS = [
    {
        "name": "alpha", "status": "running", "enabled": True, "sync_group": None,
        "discord_id": "111000222", "user_id": "111000222", "latency_ms": 62.0,
        "uptime_s": 300.0, "profiles": ["roll"], "assigned_plugins": ["roll"],
    },
    {
        "name": "beta", "status": "stopped", "enabled": True, "sync_group": "farm",
        "discord_id": None, "user_id": None, "latency_ms": None,
        "uptime_s": 0.0, "profiles": [], "assigned_plugins": [],
    },
]

RATE_STATS = {
    "floor": 1.5,
    "policies": [
        {"level": "global", "owner": None, "action": "message",
         "interval": 5.0, "jitter": [0.5, 2.0]},
        {"level": "account", "owner": "alpha", "action": "message",
         "interval": 7.0, "jitter": [0.5, 2.0]},
    ],
    "cooldowns": {"global": 0.0, "accounts": {"alpha": {"message": 2.5}}},
    "last_fire_ages": {"alpha:message": 3.1},
    "grants": 42, "waits": 20, "avg_wait_s": 1.25,
}

TASK_STATS = {
    "queued": 1, "running": 1, "completed": 8, "failed": 1, "cancelled": 0,
    "avg_duration_s": 0.4, "workers": 2,
}

WARNINGS = [
    {"level": "danger", "code": "below_floor", "message": "policy below floor"},
    {"level": "warn", "code": "no_jitter", "message": "no jitter configured"},
    {"level": "info", "code": "narrow_jitter", "message": "jitter window narrow"},
]

STATUS = {
    "accounts": ACCOUNTS, "ratelimit": RATE_STATS, "tasks": TASK_STATS,
    "warnings": WARNINGS, "uptime_s": 123.4,
}

MANIFESTS = [
    {
        "name": "soulcopy", "version": "1.2", "description": "Copy soul pieces",
        "settings": [
            {"name": "trigger", "type": "str", "default": "$souls",
             "description": "trigger word", "min": None, "max": None},
            {"name": "max_chunks", "type": "int", "default": 8,
             "description": "chunk cap", "min": 1, "max": 20},
            {"name": "chunk_delay", "type": "float", "default": 1.5,
             "description": "delay between chunks", "min": 0.5, "max": 10.0},
            {"name": "mention_user", "type": "bool", "default": True,
             "description": "mention the user", "min": None, "max": None},
        ],
    },
    {
        "name": "roll", "version": "1.0", "description": "Roll on trigger",
        "settings": [
            {"name": "command", "type": "str", "default": "$m",
             "description": "command to send", "min": None, "max": None},
        ],
    },
]

PLUGIN_STATES = [
    {"name": "soulcopy", "enabled": True, "loaded": True,
     "settings": {"trigger": "$souls", "max_chunks": 8, "chunk_delay": 1.5,
                  "mention_user": True},
     "last_error": None, "error_count": 0},
    {"name": "roll", "enabled": False, "loaded": False,
     "settings": {"command": "$m"}, "last_error": "boom", "error_count": 2},
]

ACCOUNT_INFO = {
    "name": "alpha",
    "status": {"connected": True, "state": "running", "latency_ms": 62.0,
               "uptime_s": 300.0},
    "identity": {"name": "alpha", "discord_id": "111000222"},
    "runtime": {"cached_guilds": 3, "cached_channels": 12, "cached_dms": 4,
                "cache_source": "client cache (READY)"},
    "plugins": [{"name": "soulcopy", "enabled": True},
                {"name": "roll", "enabled": False}],
    "profiles": ["roll"],
    "rate_limit": {
        "message": {"interval": 7.0, "jitter": [0.5, 2.0], "source": "account:alpha"},
        "dm": {"interval": 5.0, "jitter": [0.5, 2.0],
               "source": "inherited from global"},
    },
}

PROFILES = [{"name": "roll", "description": "roll only", "plugins": ["roll"]}]

POLICIES = RATE_STATS["policies"]

EFFECTIVE = {
    "action": "message", "interval": 7.0, "jitter": [0.5, 2.0],
    "sources": [
        {"level": "global", "owner": None, "interval": 5.0},
        {"level": "account", "owner": "alpha", "interval": 7.0},
    ],
}


def _task(status: str) -> dict:
    base = {
        "id": 3, "name": "send", "account": "alpha", "plugin": "soulcopy",
        "action": "message", "channel_id": 55, "user_id": None, "content": "hi",
        "priority": 2, "submitted_at": time.time() - 5, "started_at": None,
        "finished_at": None, "attempts": 1, "max_retries": 0, "timeout_s": 30.0,
        "status": status, "error": None, "result_summary": "",
        "grant_wait_ms": 120.0, "grant_jitter_ms": 40.0,
    }
    if status == "running":
        base["started_at"] = time.time() - 4
    elif status == "completed":
        base["finished_at"] = time.time() - 1
    return base


TASKS = [_task("running"), _task("queued")]
HISTORY = [_task("completed"), _task("failed")]

LOGS = [
    {"ts": 1700000000.0, "severity": "INFO", "source": "scheduler",
     "message": "Task 3 completed", "account": "alpha", "plugin": None,
     "task_id": 3, "event": "TaskCompleted"},
    {"ts": 1700000001.0, "severity": "ERROR", "source": "account:alpha",
     "message": "send failed", "account": "alpha", "plugin": "soulcopy",
     "task_id": 3, "event": None},
]

AUDIT = [
    {"ts": 1700000000.0, "account": "alpha", "direction": "out",
     "action": "message", "channel_id": 55, "content": "$m",
     "wait_ms": 120.0, "jitter_ms": 40.0, "note": ""},
]

SCENARIOS = [{"name": "soulcopy_basic", "description": "End-to-end soulcopy flow"}]

SIM_REPORT = {
    "name": "soulcopy_basic", "passed": True, "duration_s": 2.5,
    "failures": [], "log_lines": ["00:00.100 INFO scheduler Task 1 completed"],
    "interactions": AUDIT, "summary": "1 scenario passed",
}



class TestResumeRoute(unittest.IsolatedAsyncioTestCase):
    """POST /api/resume: the dashboard's post-emergency-stop recovery."""

    async def asyncSetUp(self) -> None:
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=TOKEN, port=0)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()

    async def test_resume_route(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, f"{self.base}/api/resume", {})
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["data"]["resumed"])
        self.assertEqual(self.fake.calls[-1], ("system_resume", (), {}))


class TestLaneRoutes(unittest.IsolatedAsyncioTestCase):
    """Sequential send lane: GET state + POST configuration."""

    async def asyncSetUp(self) -> None:
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=TOKEN, port=0)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()

    async def test_get_lane(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, f"{self.base}/api/ratelimit/lane")
        self.assertEqual(status, 200)
        data = get_json(body)["data"]
        self.assertIn("prefixrate", data)
        self.assertIn("paused", data)

    async def test_post_lane_happy(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, f"{self.base}/api/ratelimit/lane",
            {"enabled": True, "prefixrate": 2.0})
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["ok"])
        self.assertEqual(self.fake.calls[-1], ("ratelimit_set_lane", (True, 2.0)))

    async def test_post_lane_validation(self) -> None:
        for payload in (
            {"enabled": "yes"},       # strict bool
            {"prefixrate": -1},       # negative spacing
            {"prefixrate": "fast"},   # not a number
            {},                       # nothing provided
        ):
            status, body = await asyncio.to_thread(
                http_post, f"{self.base}/api/ratelimit/lane", payload)
            self.assertEqual(status, 400, payload)
            self.assertFalse(get_json(body)["ok"])


class FakeSubscribable:
    """LogService/AuditRecorder-shaped subscribe() double."""

    def __init__(self) -> None:
        self.callbacks = []

    def subscribe(self, callback):
        self.callbacks.append(callback)

        def _unsubscribe() -> None:
            if callback in self.callbacks:
                self.callbacks.remove(callback)

        return _unsubscribe

    def fire(self, entry) -> None:
        for callback in list(self.callbacks):
            callback(entry)


class FakeControlAPI:
    """Duck-typed ControlAPI: records (method, args, kwargs) for every call,
    returns canned realistic payloads. ``fail_with[method] = exc`` injects a
    domain error (400 path); anything else raising tests the 500 path."""

    def __init__(self) -> None:
        self.calls = []
        self.fail_with = {}
        self.log_service = FakeSubscribable()
        self.audit = FakeSubscribable()

    # ---------------------------------------------------------- introspection
    def called(self, name):
        """Positional args of the LAST call to `name`, or None."""
        for entry in reversed(self.calls):
            if entry[0] == name:
                return entry[1]
        return None

    def called_kwargs(self, name):
        """Keyword args of the LAST call to `name`, or None."""
        for entry in reversed(self.calls):
            if entry[0] == name:
                return entry[2]
        return None

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        exc = self.fail_with.get(name)
        if exc is not None:
            raise exc

    # ---------------------------------------------------------------- system
    async def status(self):
        self._record("status")
        return STATUS

    async def emergency_stop_all(self):
        self._record("emergency_stop_all")
        return {"cancelled_tasks": 3, "accounts": {"stopped": 2}, "halted": True}

    async def shutdown(self):
        self._record("shutdown")
        return {"shutdown": True}

    # -------------------------------------------------------------- accounts
    async def accounts_list(self):
        self._record("accounts_list")
        return ACCOUNTS

    async def accounts_add(self, payload):
        self._record("accounts_add", payload)
        return {"name": payload["name"], "enabled": True, "sync_group": payload.get("sync_group")}

    async def accounts_remove(self, name):
        self._record("accounts_remove", name)
        return {"removed": name}

    async def accounts_info(self, name):
        self._record("accounts_info", name)
        return ACCOUNT_INFO

    async def accounts_enable(self, name, enabled):
        self._record("accounts_enable", name, enabled)
        return {"name": name, "enabled": enabled}

    async def accounts_start(self, name=None):
        self._record("accounts_start", name)
        return {"started": [name] if name else ["alpha", "beta"]}

    async def accounts_stop(self, name=None):
        self._record("accounts_stop", name)
        return {"stopped": [name] if name else ["alpha", "beta"]}

    async def accounts_emergency_stop(self, name):
        self._record("accounts_emergency_stop", name)
        return {"account": name, "cancelled": 2, "stopped": True}

    # ------------------------------------------------- account plugin assignment
    async def accounts_plugins_list(self, account):
        self._record("accounts_plugins_list", account)
        return ["roll", "soulcopy"]

    async def accounts_plugin_assign(self, account, plugin, surface="api"):
        self._record("accounts_plugin_assign", account, plugin, surface=surface)
        return {"account": account, "plugin": plugin}

    async def accounts_plugin_unassign(self, account, plugin, surface="api"):
        self._record("accounts_plugin_unassign", account, plugin, surface=surface)
        return {"account": account, "plugin": plugin, "existed": True}

    async def accounts_plugins_set(self, account, plugins, surface="api"):
        self._record("accounts_plugins_set", account, plugins, surface=surface)
        return {"account": account, "plugins": sorted(plugins)}

    # --------------------------------------------------------------- plugins
    async def plugins_manifests(self):
        self._record("plugins_manifests")
        return MANIFESTS

    async def plugins_state(self, account):
        self._record("plugins_state", account)
        return PLUGIN_STATES

    async def plugins_set_enabled(self, account, plugin, enabled):
        self._record("plugins_set_enabled", account, plugin, enabled)
        return {"account": account, "plugin": plugin, "enabled": enabled}

    async def plugins_settings(self, account):
        self._record("plugins_settings", account)
        return [{"name": s["name"], "settings": s["settings"]} for s in PLUGIN_STATES]

    async def plugins_set_setting(self, account, plugin, key, value):
        self._record("plugins_set_setting", account, plugin, key, value)
        return {"account": account, "plugin": plugin, "key": key, "value": value}

    # -------------------------------------------------------------- profiles
    async def profiles_list(self):
        self._record("profiles_list")
        return PROFILES

    async def profiles_create(self, name, plugins, description=""):
        self._record("profiles_create", name, plugins, description)
        return {"name": name, "plugins": plugins, "description": description}

    async def profiles_delete(self, name):
        self._record("profiles_delete", name)
        return {"deleted": name}

    async def profiles_assign(self, account, profile):
        self._record("profiles_assign", account, profile)
        return {"account": account, "profile": profile}

    async def profiles_unassign(self, account, profile):
        self._record("profiles_unassign", account, profile)
        return {"account": account, "profile": profile}

    # ------------------------------------------------------------ rate limits
    async def ratelimit_policies(self):
        self._record("ratelimit_policies")
        return POLICIES

    async def ratelimit_set(self, action, interval, jitter_min=0.0, jitter_max=0.0,
                            level="global", owner=None):
        self._record("ratelimit_set", action, interval, jitter_min, jitter_max,
                     level, owner)
        return {"action": action, "level": level, "owner": owner,
                "policy": f"{interval:.2f}s +{jitter_min:.2f}..{jitter_max:.2f}s"}

    async def ratelimit_clear(self, level, owner, action):
        self._record("ratelimit_clear", level, owner, action)
        return {"cleared": True, "level": level, "owner": owner, "action": action}

    async def ratelimit_effective(self, action, account=None, plugin=None):
        self._record("ratelimit_effective", action, account, plugin)
        return EFFECTIVE

    async def ratelimit_stats(self):
        self._record("ratelimit_stats")
        return RATE_STATS

    async def ratelimit_warnings(self):
        self._record("ratelimit_warnings")
        return WARNINGS

    async def system_resume(self):
        self.calls.append(("system_resume", (), {}))
        return {"resumed": True}

    async def ratelimit_lane(self):
        return {"enabled": False, "prefixrate": 1.5, "paused": False,
                "waiting": 0, "grants": 0, "last_grant_age_s": None,
                "held_off_seconds": 0.0}

    async def ratelimit_set_lane(self, enabled=None, prefixrate=None):
        self.calls.append(("ratelimit_set_lane", (enabled, prefixrate)))
        return {"enabled": bool(enabled) if enabled is not None else False,
                "prefixrate": prefixrate if prefixrate is not None else 1.5,
                "paused": False, "waiting": 0, "grants": 0,
                "last_grant_age_s": None, "held_off_seconds": 0.0}

    async def ratelimit_set_floor(self, seconds):
        self._record("ratelimit_set_floor", seconds)
        return {"floor": seconds}

    # ------------------------------------------------------------------ tasks
    async def tasks_list(self, status=None, account=None):
        self._record("tasks_list", status, account)
        return TASKS

    async def tasks_cancel(self, task_id):
        self._record("tasks_cancel", task_id)
        return {"cancelled": True, "task_id": task_id}

    async def tasks_cancel_matching(self, account=None, plugin=None):
        self._record("tasks_cancel_matching", account=account, plugin=plugin)
        return {"cancelled": 2}

    async def tasks_history(self, limit=50, account=None):
        self._record("tasks_history", limit=limit, account=account)
        return HISTORY

    # ------------------------------------------------------------- simulation
    async def simulation_list(self):
        self._record("simulation_list")
        return SCENARIOS

    async def simulation_run(self, name):
        self._record("simulation_run", name)
        return SIM_REPORT

    # ------------------------------------------------------------------- logs
    async def logs_tail(self, n=100, account=None, severity=None):
        self._record("logs_tail", n=n, account=account, severity=severity)
        return LOGS

    async def audit_tail(self, n=100, account=None):
        self._record("audit_tail", n=n, account=account)
        return AUDIT

    def logs_service(self):
        self._record("logs_service")
        return self.log_service

    def audit_recorder(self):
        self._record("audit_recorder")
        return self.audit


# --------------------------------------------------------------- http helpers
def http_get(url: str, token: str | None = TOKEN) -> tuple[int, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def http_post(url: str, payload, token: str | None = TOKEN,
              raw_body: bytes | None = None) -> tuple[int, str]:
    data = raw_body if raw_body is not None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def http_post_h(url: str, payload, token: str | None = TOKEN,
                 raw_body: bytes | None = None) -> tuple[int, str, dict]:
    import http.client
    from urllib.parse import urlparse

    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    data = raw_body if raw_body is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Dashboard-Token"] = token
    try:
        conn.request("POST", parsed.path, body=data, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8"), dict(resp.getheaders())
    finally:
        conn.close()


def get_json(text: str) -> dict:
    return json.loads(text)


def read_http_response(sock: socket.socket) -> tuple[str, bytes]:
    """Read ONE full HTTP response (headers + Content-Length body) from a
    raw socket; returns (header_text, body)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before headers finished")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.decode("latin-1").splitlines():
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head.decode("latin-1"), rest[:length]


def header_value(head: str, name: str) -> str:
    for line in head.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


class TestDashboardHTTP(unittest.IsolatedAsyncioTestCase):
    """REST surface over a real ThreadingHTTPServer on an ephemeral port."""

    async def asyncSetUp(self) -> None:
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=TOKEN, port=0)
        self.server.start()
        self.port = self.server.bound_port  # REAL bound port (port=0)
        self.assertNotEqual(self.port, 0)
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    # ------------------------------------------------------------ auth + static
    async def test_index_served_without_auth(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/"), None)
        self.assertEqual(status, 200)
        self.assertIn("SlaughterMudae", body)

    async def test_api_requires_token(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/status"), None)
        self.assertEqual(status, 401)
        self.assertIn("token", get_json(body)["error"])
        status, _ = await asyncio.to_thread(http_get, self.url("/api/status"), "wrong")
        self.assertEqual(status, 401)

    async def test_token_via_header_and_query_param(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/status"))
        self.assertEqual(status, 200)
        payload = get_json(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["data"]["accounts"]), 2)
        status, body = await asyncio.to_thread(
            http_get, self.url(f"/api/status?token={TOKEN}"), None
        )
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["ok"])

    async def test_unknown_route_404(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/nope"))
        self.assertEqual(status, 404)
        self.assertIn("no route", get_json(body)["error"])

    async def test_invalid_json_body_400(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/remove"), None, TOKEN, b"{not json"
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid JSON", get_json(body)["error"])

    async def test_body_over_cap_413(self) -> None:
        big = b'{"pad": "' + b"x" * 1_100_000 + b'"}'
        status, body, headers = await asyncio.to_thread(
            http_post_h, self.url("/api/accounts/remove"), None, TOKEN, big
        )
        self.assertEqual(status, 413)
        self.assertIn("too large", get_json(body)["error"])
        # keep-alive safety: a partially-drained body must close the stream
        self.assertEqual(headers.get("Connection", "").lower(), "close")

    async def test_get_with_body_closes_connection(self) -> None:
        # keep-alive safety: an unread non-POST body would be parsed as the
        # NEXT request line — the server must drain it and refuse reuse
        import http.client
        from urllib.parse import urlparse

        def _attempt() -> tuple[int, str, str]:
            parsed = urlparse(self.url("/api/status"))
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port,
                                              timeout=10)
            headers = {"X-Dashboard-Token": TOKEN,
                       "Content-Length": "9"}
            try:
                conn.request("GET", parsed.path, body=b"JUNKJUNK!",
                             headers=headers)
                resp = conn.getresponse()
                return (resp.status, resp.read().decode("utf-8"),
                        resp.getheader("Connection") or "")
            finally:
                conn.close()

        status, body, conn_header = await asyncio.to_thread(_attempt)
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["ok"])
        self.assertEqual(conn_header.lower(), "close")

    async def test_non_serializable_payload_answers_json(self) -> None:
        # a datetime (or set/exception) in the payload must not crash the
        # handler thread and drop the connection — it answers honest JSON
        from datetime import datetime

        async def _broken_status():
            return {"accounts": [], "when": datetime(2024, 1, 1)}

        self.fake.status = _broken_status
        status, body = await asyncio.to_thread(http_get, self.url("/api/status"))
        self.assertEqual(status, 200)
        payload = get_json(body)
        self.assertFalse(payload["ok"])
        self.assertIn("serializable", payload["error"])

    # ------------------------------------------------------------------ system
    async def test_status(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/status"))
        self.assertEqual(status, 200)
        data = get_json(body)["data"]
        self.assertEqual(data["tasks"]["queued"], 1)
        self.assertEqual(self.fake.called("status"), ())

    async def test_estop(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/estop"), {}
        )
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["data"]["halted"])
        self.assertEqual(self.fake.called("emergency_stop_all"), ())

    async def test_shutdown(self) -> None:
        status, _ = await asyncio.to_thread(http_post, self.url("/api/shutdown"), {})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("shutdown"), ())

    # ---------------------------------------------------------------- accounts
    async def test_accounts_list(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/accounts"))
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"][0]["name"], "alpha")
        self.assertEqual(self.fake.called("accounts_list"), ())

    async def test_accounts_add_passes_payload(self) -> None:
        payload = {"name": "gamma", "sync_group": "farm", "plugins": ["roll"]}
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"), payload
        )
        self.assertEqual(status, 200)
        args = self.fake.called("accounts_add")
        self.assertEqual(args[0]["name"], "gamma")
        self.assertEqual(args[0]["plugins"], ["roll"])

    async def test_accounts_info_requires_query(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/accounts/info"))
        self.assertEqual(status, 400)
        self.assertIn("account", get_json(body)["error"])

    async def test_accounts_info(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/accounts/info?account=alpha")
        )
        self.assertEqual(status, 200)
        self.assertIn("cache_source", get_json(body)["data"]["runtime"])
        self.assertEqual(self.fake.called("accounts_info"), ("alpha",))

    async def test_accounts_enable_strict_bool(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/enable"),
            {"name": "alpha", "enabled": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("accounts_enable"), ("alpha", True))

        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/enable"),
            {"name": "alpha", "enabled": "yes"},
        )
        self.assertEqual(status, 400)
        self.assertIn("boolean", get_json(body)["error"])

    async def test_accounts_start_optional_account(self) -> None:
        status, _ = await asyncio.to_thread(http_post, self.url("/api/accounts/start"), {})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("accounts_start"), (None,))
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/accounts/start"), {"account": "alpha"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("accounts_start"), ("alpha",))

    async def test_accounts_emergency_stop(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/accounts/emergency-stop"), {"name": "alpha"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("accounts_emergency_stop"), ("alpha",))

    # -------------------------------------------- account plugin assignment
    async def test_accounts_plugins_list(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/accounts/plugins?account=alpha")
        )
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"], ["roll", "soulcopy"])
        self.assertEqual(self.fake.called("accounts_plugins_list"), ("alpha",))

    async def test_accounts_plugins_list_requires_account(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/accounts/plugins")
        )
        self.assertEqual(status, 400)
        self.assertIn("account", get_json(body)["error"])

    async def test_accounts_plugin_assign_and_unassign(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/assign"),
            {"account": "alpha", "plugin": "soulcopy"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"],
                         {"account": "alpha", "plugin": "soulcopy"})
        self.assertEqual(
            self.fake.called("accounts_plugin_assign"), ("alpha", "soulcopy")
        )
        self.assertEqual(self.fake.called_kwargs("accounts_plugin_assign"),
                         {"surface": "dashboard"},
                         "mutations must audit through the dashboard surface")
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/unassign"),
            {"account": "alpha", "plugin": "soulcopy"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["data"]["existed"])
        self.assertEqual(
            self.fake.called("accounts_plugin_unassign"), ("alpha", "soulcopy")
        )

    async def test_accounts_plugin_assign_missing_fields(self) -> None:
        for payload in ({"account": "alpha"}, {"plugin": "roll"}, {}):
            status, body = await asyncio.to_thread(
                http_post, self.url("/api/accounts/plugins/assign"), payload
            )
            self.assertEqual(status, 400, payload)
            error = get_json(body)["error"]
            self.assertIn("required", error)
            self.assertIn("/api/accounts/plugins/assign", error)

    async def test_accounts_plugins_set_happy_path(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/set"),
            {"account": "alpha", "plugins": ["soulcopy", "roll"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"]["plugins"],
                         ["roll", "soulcopy"])
        self.assertEqual(self.fake.called("accounts_plugins_set"),
                         ("alpha", ["soulcopy", "roll"]))
        self.assertEqual(self.fake.called_kwargs("accounts_plugins_set"),
                         {"surface": "dashboard"})

    async def test_accounts_plugins_set_validates_list_of_strings(self) -> None:
        for payload in (
            {"account": "alpha", "plugins": "roll"},          # not a list
            {"account": "alpha", "plugins": ["roll", 3]},     # non-string entry
            {"account": "alpha"},                              # plugins missing
            {"plugins": ["roll"]},                             # account missing
        ):
            status, body = await asyncio.to_thread(
                http_post, self.url("/api/accounts/plugins/set"), payload
            )
            self.assertEqual(status, 400, payload)
            self.assertFalse(get_json(body)["ok"])

    async def test_accounts_plugins_unknown_plugin_maps_to_400(self) -> None:
        # all-or-nothing: an unknown name rejects the whole set
        self.fake.fail_with["accounts_plugins_set"] = ValueError(
            "unknown plugin(s) ['nope'] (known plugins: ['roll', 'soulcopy'])"
        )
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/set"),
            {"account": "alpha", "plugins": ["roll", "nope"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown plugin", get_json(body)["error"])

    async def test_accounts_plugins_unknown_account_maps_to_400(self) -> None:
        self.fake.fail_with["accounts_plugin_assign"] = AccountError(
            "unknown account: zeta"
        )
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/assign"),
            {"account": "zeta", "plugin": "roll"},
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown account", get_json(body)["error"])

    # ----------------------------------------------------------------- plugins
    async def test_plugins_manifests(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/plugins"))
        self.assertEqual(status, 200)
        data = get_json(body)["data"]
        self.assertEqual(data[0]["name"], "soulcopy")
        self.assertEqual(len(data[0]["settings"]), 4)
        self.assertEqual(self.fake.called("plugins_manifests"), ())

    async def test_plugins_state(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/plugins/state?account=alpha")
        )
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"][0]["name"], "soulcopy")
        self.assertEqual(self.fake.called("plugins_state"), ("alpha",))

    async def test_plugins_set_enabled(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/plugins/set-enabled"),
            {"account": "alpha", "plugin": "roll", "enabled": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("plugins_set_enabled"), ("alpha", "roll", False)
        )

    async def test_plugins_set_setting(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/plugins/set-setting"),
            {"account": "alpha", "plugin": "soulcopy",
             "key": "max_chunks", "value": 12},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("plugins_set_setting"),
            ("alpha", "soulcopy", "max_chunks", 12),
        )

    # ---------------------------------------------------------------- profiles
    async def test_profiles_create(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/profiles/create"),
            {"name": "farmer", "plugins": ["roll", "soulcopy"],
             "description": "farm duty"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("profiles_create"),
            ("farmer", ["roll", "soulcopy"], "farm duty"),
        )

    async def test_profiles_assign_and_unassign(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/profiles/assign"),
            {"account": "alpha", "profile": "roll"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("profiles_assign"), ("alpha", "roll"))
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/profiles/unassign"),
            {"account": "alpha", "profile": "roll"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("profiles_unassign"), ("alpha", "roll"))

    async def test_profiles_create_rejects_non_list(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/profiles/create"),
            {"name": "bad", "plugins": "roll"},
        )
        self.assertEqual(status, 400)
        self.assertIn("list of plugin names", get_json(body)["error"])

    # ------------------------------------------------------------- rate limits
    async def test_ratelimit_set_full_args(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/ratelimit/set"),
            {"action": "message", "interval": 7.5, "jitter_min": 0.5,
             "jitter_max": 2.0, "level": "account", "owner": "alpha"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("ratelimit_set"),
            ("message", 7.5, 0.5, 2.0, "account", "alpha"),
        )

    async def test_ratelimit_set_defaults(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/ratelimit/set"),
            {"action": "message", "interval": 5},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("ratelimit_set"),
            ("message", 5.0, 0.0, 0.0, "global", None),
        )

    async def test_ratelimit_set_rejects_nan(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/ratelimit/set"),
            None, TOKEN, b'{"action": "message", "interval": NaN}',
        )
        self.assertEqual(status, 400)
        self.assertIn("finite", get_json(body)["error"])

    async def test_ratelimit_clear(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/ratelimit/clear"),
            {"level": "account", "owner": "alpha", "action": "message"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("ratelimit_clear"), ("account", "alpha", "message")
        )

    async def test_ratelimit_effective_filters(self) -> None:
        status, _ = await asyncio.to_thread(
            http_get, self.url("/api/ratelimit/effective?action=message&account=alpha&plugin=soulcopy")
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("ratelimit_effective"),
            ("message", "alpha", "soulcopy"),
        )

    async def test_ratelimit_stats_and_warnings(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/ratelimit/stats"))
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"]["floor"], 1.5)
        status, body = await asyncio.to_thread(http_get, self.url("/api/ratelimit/warnings"))
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"][0]["level"], "danger")

    async def test_ratelimit_floor(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/ratelimit/floor"), {"seconds": 2.5}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("ratelimit_set_floor"), (2.5,))

    # -------------------------------------------------------------------- tasks
    async def test_tasks_list_filters(self) -> None:
        status, _ = await asyncio.to_thread(
            http_get, self.url("/api/tasks?status=running&account=alpha")
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("tasks_list"), ("running", "alpha"))

    async def test_tasks_cancel_requires_int(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/tasks/cancel"), {"task_id": 3}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.called("tasks_cancel"), (3,))
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/tasks/cancel"), {"task_id": "abc"}
        )
        self.assertEqual(status, 400)
        self.assertIn("integer", get_json(body)["error"])
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/tasks/cancel"), {}
        )
        self.assertEqual(status, 400)
        self.assertIn("task_id", get_json(body)["error"])

    async def test_tasks_cancel_matching(self) -> None:
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/tasks/cancel-matching"),
            {"account": "alpha", "plugin": "soulcopy"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called_kwargs("tasks_cancel_matching"),
            {"account": "alpha", "plugin": "soulcopy"},
        )

    async def test_tasks_history_limit(self) -> None:
        status, _ = await asyncio.to_thread(
            http_get, self.url("/api/tasks/history?limit=5&account=alpha")
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called_kwargs("tasks_history"), {"limit": 5, "account": "alpha"}
        )
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/tasks/history?limit=zzz")
        )
        self.assertEqual(status, 400)
        self.assertIn("integer", get_json(body)["error"])

    # --------------------------------------------------------------- simulation
    async def test_simulation_list_and_run(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/api/simulation/list"))
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"][0]["name"], "soulcopy_basic")
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/simulation/run"), {"name": "soulcopy_basic"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["data"]["passed"])
        self.assertEqual(self.fake.called("simulation_run"), ("soulcopy_basic",))

    # ------------------------------------------------------------ logs + audit
    async def test_logs_tail_filters(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/logs?n=5&account=alpha&severity=ERROR")
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(get_json(body)["data"]), 2)  # fake returns the tail unfiltered
        self.assertEqual(
            self.fake.called_kwargs("logs_tail"),
            {"n": 5, "account": "alpha", "severity": "ERROR"},
        )

    async def test_audit_tail(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/audit?n=3&account=alpha")
        )
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"][0]["direction"], "out")
        self.assertEqual(
            self.fake.called_kwargs("audit_tail"), {"n": 3, "account": "alpha"}
        )

    # --------------------------------------------------------- error semantics
    async def test_domain_error_maps_to_400(self) -> None:
        self.fake.fail_with["ratelimit_set"] = ValueError(
            "policy interval 0.5s is below the safety floor 1.5s"
        )
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/ratelimit/set"),
            {"action": "message", "interval": 0.5},
        )
        self.assertEqual(status, 400)
        self.assertIn("safety floor", get_json(body)["error"])

    async def test_runtime_error_maps_to_400(self) -> None:
        self.fake.fail_with["accounts_start"] = RuntimeError("account alpha is running")
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/start"), {"account": "alpha"}
        )
        self.assertEqual(status, 400)
        self.assertIn("running", get_json(body)["error"])

    async def test_unexpected_error_maps_to_500(self) -> None:
        self.fake.fail_with["status"] = KeyError("boom")
        status, body = await asyncio.to_thread(http_get, self.url("/api/status"))
        self.assertEqual(status, 500)
        self.assertIn("internal error", get_json(body)["error"])

    async def test_missing_required_field_names_field_and_route(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/remove"), {}
        )
        self.assertEqual(status, 400)
        error = get_json(body)["error"]
        self.assertIn("'name'", error)
        self.assertIn("/api/accounts/remove", error)

    # ------------------------------------------- round-2: token slot precedence
    async def test_rest_token_header_precedence_over_query(self) -> None:
        # conflicting tokens in TWO places: the header slot wins and a WRONG
        # header must fail closed even when the query token is correct
        status, _ = await asyncio.to_thread(
            http_get, self.url(f"/api/status?token={TOKEN}"), "wrong")
        self.assertEqual(status, 401)
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/status?token=wrong"), TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["ok"])

    # ------------------------------------- round-2: peer abort mid-response
    async def test_peer_close_before_response_no_traceback(self) -> None:
        def _slam() -> None:
            for _ in range(3):
                sock = socket.create_connection(("127.0.0.1", self.port),
                                                 timeout=5)
                try:
                    sock.sendall(
                        f"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        f"X-Dashboard-Token: {TOKEN}\r\n\r\n".encode()
                    )
                finally:
                    sock.close()  # vanish before reading the response
            time.sleep(0.3)  # let the handler threads hit the dead socket

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            await asyncio.to_thread(_slam)
        self.assertNotIn("Traceback", captured.getvalue())
        self.assertNotIn("Exception occurred", captured.getvalue())
        # and the server is unharmed
        status, _ = await asyncio.to_thread(http_get, self.url("/api/status"))
        self.assertEqual(status, 200)

    # ------------------------------------- round-2: stop() + restart on port
    async def test_concurrent_double_stop_and_restart_same_port(self) -> None:
        port = self.server.bound_port
        await asyncio.gather(*[
            asyncio.to_thread(self.server.stop) for _ in range(4)
        ])
        self.server.stop()  # idempotent second stop (teardown stops a 3rd time)
        replacement = DashboardServer(self.fake, token=TOKEN, port=port)
        replacement.start()
        try:
            self.assertEqual(replacement.bound_port, port)  # SO_REUSEADDR bind
            status, body = await asyncio.to_thread(
                http_get, f"http://127.0.0.1:{port}/api/status")
            self.assertEqual(status, 200)
            self.assertTrue(get_json(body)["ok"])
        finally:
            replacement.stop()


class TestRound2BodyHardening(unittest.IsolatedAsyncioTestCase):
    """Round-2 network-attacker cases: lying/ambiguous Content-Length,
    chunked bodies, HTTP/1.0 close semantics, pipelining after drain."""

    async def asyncSetUp(self) -> None:
        self._saved_timeout = server_module.HTTP_SOCKET_TIMEOUT_S
        server_module.HTTP_SOCKET_TIMEOUT_S = 1.0  # fast stall detection
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=None, port=0)
        self.server.start()
        self.port = self.server.bound_port

    async def asyncTearDown(self) -> None:
        self.server.stop()
        server_module.HTTP_SOCKET_TIMEOUT_S = self._saved_timeout

    def _open(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.settimeout(8)
        return sock

    async def test_content_length_mismatch_times_out_to_408(self) -> None:
        # claim 5 bytes, send 3, HOLD the socket open: the handler must not
        # hang for the full socket timeout and dump a traceback — it answers
        # a clean 408 and closes
        def _attack() -> tuple[str, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"POST /api/accounts/remove HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 5\r\n\r\nabc")
                return read_http_response(sock)
            finally:
                sock.close()

        head, body = await asyncio.to_thread(_attack)
        self.assertIn("408", head.splitlines()[0])
        self.assertIn("incomplete", get_json(body)["error"])
        self.assertEqual(header_value(head, "Connection").lower(), "close")

    async def test_content_length_mismatch_eof_answers_400(self) -> None:
        # claim 5, send 3, then half-close: read() returns the short body at
        # EOF and JSON parsing rejects it — a 400, never a hang
        def _attack() -> tuple[str, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"POST /api/accounts/remove HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 5\r\n\r\nabc")
                sock.shutdown(socket.SHUT_WR)
                return read_http_response(sock)
            finally:
                sock.close()

        head, body = await asyncio.to_thread(_attack)
        self.assertIn("400", head.splitlines()[0])
        self.assertIn("invalid JSON", get_json(body)["error"])

    async def test_lying_huge_content_length_refused_without_drain(self) -> None:
        # a 17MB claim exceeds the 16MB drain bound: reject IMMEDIATELY —
        # the drain loop must not even start (and not wait on the timeout)
        def _attack() -> tuple[str, bytes, float]:
            sock = self._open()
            try:
                started = time.monotonic()
                sock.sendall(
                    b"POST /api/accounts/remove HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 17000000\r\n\r\n")
                head, body = read_http_response(sock)
                return head, body, time.monotonic() - started
            finally:
                sock.close()

        head, body, elapsed = await asyncio.to_thread(_attack)
        self.assertIn("413", head.splitlines()[0])
        self.assertIn("too large", get_json(body)["error"])
        self.assertLess(elapsed, 0.8)  # far under the 1s stall timeout

    async def test_chunked_post_rejected_411(self) -> None:
        def _attack() -> tuple[str, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"POST /api/accounts/remove HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n")
                return read_http_response(sock)
            finally:
                sock.close()

        head, body = await asyncio.to_thread(_attack)
        self.assertIn("411", head.splitlines()[0])
        self.assertIn("Content-Length", get_json(body)["error"])
        self.assertEqual(header_value(head, "Connection").lower(), "close")

    async def test_http10_client_answered_and_closed(self) -> None:
        def _attempt() -> tuple[str, bytes, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"GET /api/status HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                head, body = read_http_response(sock)
                return head, body, sock.recv(4096)  # EOF expected next
            finally:
                sock.close()

        head, body, tail = await asyncio.to_thread(_attempt)
        self.assertIn("200", head.splitlines()[0])
        self.assertTrue(get_json(body)["ok"])
        self.assertEqual(header_value(head, "Connection").lower(), "close")
        self.assertEqual(tail, b"")  # HTTP/1.0: connection closed after reply

    async def test_pipelined_request_after_drain_refusal_not_parsed(self) -> None:
        # GET-with-body forces a bounded drain + Connection: close; a request
        # pipelined behind it must NEVER be parsed as the next stream bytes
        def _attempt() -> tuple[str, bytes, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 9\r\n\r\nJUNKJUNK!"
                    b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                head, body = read_http_response(sock)
                return head, body, sock.recv(4096)
            finally:
                sock.close()

        head, body, tail = await asyncio.to_thread(_attempt)
        self.assertIn("200", head.splitlines()[0])
        self.assertTrue(get_json(body)["ok"])
        self.assertEqual(header_value(head, "Connection").lower(), "close")
        self.assertEqual(tail, b"")  # socket closed; second request unanswered


class TestRound3HttpHardening(unittest.IsolatedAsyncioTestCase):
    """Round-3: non-GET /ws keep-alive desync + duplicate Content-Length."""

    async def asyncSetUp(self) -> None:
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=None, port=0)
        self.server.start()
        self.port = self.server.bound_port

    async def asyncTearDown(self) -> None:
        self.server.stop()

    def _open(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sock.settimeout(8)
        return sock

    async def test_post_ws_with_body_rejected_with_close(self) -> None:
        # a POST /ws body is never consumed (the drain covers non-POST
        # only) — the 400 MUST carry Connection: close or the unread body
        # desyncs the keep-alive stream (garbage-parsed next request)
        def _attack() -> tuple[str, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"POST /ws HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 9\r\n\r\nJUNKJUNK!")
                return read_http_response(sock)
            finally:
                sock.close()

        head, body = await asyncio.to_thread(_attack)
        self.assertIn("400", head.splitlines()[0])
        self.assertIn("websocket upgrade", get_json(body)["error"])
        self.assertEqual(header_value(head, "Connection").lower(), "close")

    async def test_get_ws_upgrade_failure_keeps_alive_cleanly(self) -> None:
        # a bodyless GET upgrade failure leaves a CLEAN stream — the
        # connection stays reusable (unlike the non-GET case above)
        def _attempt() -> tuple[str, str]:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("GET", "/ws")
                resp = conn.getresponse()
                first_status = str(resp.status)
                conn_header = resp.getheader("Connection") or ""
                resp.read()
                # prove no desync: the NEXT request on this socket parses
                conn.request("GET", "/api/status")
                resp2 = conn.getresponse()
                body2 = resp2.read().decode("utf-8")
                return first_status + " " + conn_header, body2
            finally:
                conn.close()

        combined, body2 = await asyncio.to_thread(_attempt)
        status, conn_header = combined.split(" ", 1)
        self.assertEqual(status, "400")
        self.assertNotEqual(conn_header.lower(), "close")  # reuse is safe
        self.assertTrue(get_json(body2)["ok"])  # stream stayed parseable

    async def test_conflicting_duplicate_content_length_post_rejected(self) -> None:
        # two DIFFERENT Content-Length values: first-value-wins silently
        # is the smuggling family — must 400 and close
        def _attack() -> tuple[str, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"POST /api/accounts/remove HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}xx")
                return read_http_response(sock)
            finally:
                sock.close()

        head, body = await asyncio.to_thread(_attack)
        self.assertIn("400", head.splitlines()[0])
        self.assertIn("conflicting duplicate Content-Length",
                      get_json(body)["error"])
        self.assertEqual(header_value(head, "Connection").lower(), "close")

    async def test_identical_duplicate_content_length_accepted(self) -> None:
        # RFC 7230 §3.3.2: recipients may merge IDENTICAL duplicates —
        # the request is processed normally (here: a normal domain 400)
        def _attempt() -> tuple[str, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"POST /api/accounts/remove HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
                return read_http_response(sock)
            finally:
                sock.close()

        head, body = await asyncio.to_thread(_attempt)
        self.assertIn("400", head.splitlines()[0])  # missing 'name' domain error
        error = get_json(body)["error"]
        self.assertIn("'name'", error)
        self.assertNotIn("Content-Length", error)  # NOT the duplicate rejection

    async def test_conflicting_duplicate_content_length_get_rejected(self) -> None:
        # the non-POST drain path must refuse conflicting duplicates too
        def _attack() -> tuple[str, bytes]:
            sock = self._open()
            try:
                sock.sendall(
                    b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello!")
                return read_http_response(sock)
            finally:
                sock.close()

        head, body = await asyncio.to_thread(_attack)
        self.assertIn("400", head.splitlines()[0])
        self.assertIn("conflicting duplicate Content-Length",
                      get_json(body)["error"])
        self.assertEqual(header_value(head, "Connection").lower(), "close")


if __name__ == "__main__":
    unittest.main()
