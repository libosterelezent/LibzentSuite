"""Dashboard over real HTTP: REST auth, routing, validation, static index.

Uses a duck-typed Fake ControlAPI (records every call, returns canned
realistic payloads shaped like the manager outputs in core/api.py
docstrings + INTEGRATION.md) — no real managers are constructed.
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest
import urllib.error
import urllib.request

from interfaces.dashboard.server import DashboardServer

TOKEN = "s3cret-token"

# --------------------------------------------------------------- canned data
ACCOUNTS = [
    {
        "name": "alpha", "status": "running", "enabled": True, "sync_group": None,
        "discord_id": "111000222", "user_id": "111000222", "latency_ms": 62.0,
        "uptime_s": 300.0, "profiles": ["roll"],
    },
    {
        "name": "beta", "status": "stopped", "enabled": True, "sync_group": "farm",
        "discord_id": None, "user_id": None, "latency_ms": None,
        "uptime_s": 0.0, "profiles": [],
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


def get_json(text: str) -> dict:
    return json.loads(text)


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
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/remove"), None, TOKEN, big
        )
        self.assertEqual(status, 413)
        self.assertIn("too large", get_json(body)["error"])

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
        payload = {"name": "gamma", "sync_group": "farm", "profiles": ["roll"]}
        status, _ = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"), payload
        )
        self.assertEqual(status, 200)
        args = self.fake.called("accounts_add")
        self.assertEqual(args[0]["name"], "gamma")
        self.assertEqual(args[0]["profiles"], ["roll"])

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


if __name__ == "__main__":
    unittest.main()
