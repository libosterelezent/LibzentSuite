"""Dashboard credential/cooldown REST surface tests (agent B).

Real DashboardServer over a duck-typed fake ControlAPI (same harness pattern
as test_dashboard_http.py — ephemeral port, real HTTP). Focus:

  * happy paths for the new routes (credential set/status, .env import,
    cooldowns list/clear, add-account-with-credential);
  * the credential AUTH GATE: 403 (locked) when no DASHBOARD_TOKEN is
    configured, 401 on a wrong token, 200 with the right one;
  * validation (400), duplicate-account conflict (409);
  * the SPA stays servable unauthenticated;
  * grep-proofing: the credential VALUE never appears in any response.
"""
from __future__ import annotations

import asyncio
import json
import unittest
import urllib.error
import urllib.request

from core.errors import AccountError
from interfaces.dashboard.server import DashboardServer

TOKEN = "s3cret-token"
SECRET = "tok-SECRET-7c1e"          # must never appear in any response body
GATE_MESSAGE = "set DASHBOARD_TOKEN in .env to manage credentials"

CREDENTIAL_STATUS = [
    {"name": "alpha", "credential": "db"},
    {"name": "beta", "credential": "missing"},
]

IMPORT_REPORT = {"imported": ["alpha"], "skipped": ["beta"], "failed": {}}

COOLDOWNS = [
    {"account": "alpha", "plugin": "roll", "cooldown_key": "vote",
     "remaining_s": 90.0, "source": "plugin", "reason": ""},
    {"account": "beta", "plugin": "soulcopy", "cooldown_key": "export",
     "remaining_s": 45.0, "source": "plugin", "reason": ""},
]

ACCOUNTS = [
    {"name": "alpha", "status": "running", "enabled": True, "profiles": [],
     "credential": "db", "cooldowns": 1},
    {"name": "beta", "status": "stopped", "enabled": True, "profiles": [],
     "credential": "missing", "cooldowns": 0},
]


class FakeControlAPI:
    """Duck-typed ControlAPI for the credential/cooldown surface."""

    def __init__(self) -> None:
        self.calls = []
        self.fail_with = {}
        self.stored_credentials = {}   # account -> value (TEST-ONLY record)

    def called(self, name):
        for entry in reversed(self.calls):
            if entry[0] == name:
                return entry[1]
        return None

    def called_kwargs(self, name):
        for entry in reversed(self.calls):
            if entry[0] == name:
                return entry[2]
        return None

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        exc = self.fail_with.get(name)
        if exc is not None:
            raise exc

    # ------------------------------------------------------------ accounts
    async def status(self):
        self._record("status")
        return {"accounts": ACCOUNTS, "ratelimit": {"floor": 1.5},
                "tasks": {}, "warnings": [], "uptime_s": 1.0}

    async def accounts_list(self):
        self._record("accounts_list")
        return ACCOUNTS

    async def accounts_add(self, payload, surface="api"):
        self._record("accounts_add", payload, surface=surface)
        if payload["name"] == "alpha":
            raise AccountError(f"account {payload['name']!r} already exists")
        out = {"name": payload["name"], "enabled": True, "profiles": []}
        if payload.get("credential"):
            self.stored_credentials[payload["name"]] = payload["credential"]
            out["credential"] = "db"  # status only — never the value
        return out

    async def accounts_remove(self, name, surface="api"):
        self._record("accounts_remove", name)
        return {"removed": name}

    async def accounts_set_credential(self, name, credential, surface="api"):
        self._record("accounts_set_credential", name, surface=surface)
        if name == "ghost":
            raise AccountError(f"unknown account {name!r}")
        self.stored_credentials[name] = credential  # TEST-ONLY record
        return {"account": name, "credential": "db"}

    async def accounts_credential_status(self):
        self._record("accounts_credential_status")
        return CREDENTIAL_STATUS

    async def credentials_import_env(self, overwrite=False, surface="api"):
        self._record("credentials_import_env", overwrite=overwrite,
                     surface=surface)
        return IMPORT_REPORT

    # ----------------------------------------------------------- cooldowns
    async def cooldowns_list(self, account=None):
        self._record("cooldowns_list", account)
        if account:
            return [r for r in COOLDOWNS if r["account"] == account]
        return COOLDOWNS

    async def cooldowns_clear(self, account, plugin, cooldown_key=None,
                              surface="api"):
        self._record("cooldowns_clear", account, plugin, cooldown_key,
                     surface=surface)
        return {"account": account, "plugin": plugin,
                "cooldown_key": cooldown_key, "cleared": 1}

    # ------------------------------------------------- misc (broadcaster etc.)
    async def emergency_stop_all(self, surface="api"):
        self._record("emergency_stop_all")
        return {"halted": True}

    async def shutdown(self):
        self._record("shutdown")
        return {"shutdown": True}

    def audit_recorder(self):
        return None


# --------------------------------------------------------------- http helpers
def http_get(url: str, token: str | None = TOKEN) -> tuple[int, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read().decode("utf-8")


def http_post(url: str, payload, token: str | None = TOKEN) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read().decode("utf-8")


def get_json(text: str) -> dict:
    return json.loads(text)


class _ServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self, token: str | None = TOKEN) -> None:
        self.fake = FakeControlAPI()
        self.server = DashboardServer(self.fake, token=token, port=0)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()

    def url(self, path: str) -> str:
        return f"{self.base}{path}"


class TestCredentialRoutesHappy(_ServerTestCase):
    async def asyncSetUp(self, token: str | None = TOKEN) -> None:
        await super().asyncSetUp(token)

    async def test_set_credential_roundtrip(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"name": "alpha", "credential": SECRET},
        )
        self.assertEqual(status, 200)
        data = get_json(body)["data"]
        self.assertEqual(data, {"account": "alpha", "credential": "db"})
        self.assertEqual(self.fake.called("accounts_set_credential"), ("alpha",))
        self.assertEqual(
            self.fake.called_kwargs("accounts_set_credential").get("surface"),
            "dashboard")
        self.assertNotIn(SECRET, body)  # value never echoed

    async def test_credential_status(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/credentials/status"))
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"], CREDENTIAL_STATUS)

    async def test_import_env_default_and_overwrite(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/credentials/import-env"), {})
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"], IMPORT_REPORT)
        self.assertIs(self.fake.called_kwargs("credentials_import_env")
                      .get("overwrite"), False)
        await asyncio.to_thread(
            http_post, self.url("/api/credentials/import-env"),
            {"overwrite": True},
        )
        self.assertIs(self.fake.called_kwargs("credentials_import_env")
                      .get("overwrite"), True)

    async def test_add_account_with_credential_stored_not_echoed(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"),
            {"name": "gamma", "credential": SECRET},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.stored_credentials.get("gamma"), SECRET)
        data = get_json(body)["data"]
        self.assertEqual(data.get("credential"), "db")   # status only
        self.assertNotIn(SECRET, body)

    async def test_add_account_without_credential_untouched(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"), {"name": "gamma"})
        self.assertEqual(status, 200)
        self.assertNotIn("credential", self.fake.stored_credentials)
        self.assertNotIn(SECRET, body)

    async def test_duplicate_add_maps_to_409(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"), {"name": "alpha"})
        self.assertEqual(status, 409)
        self.assertFalse(get_json(body)["ok"])
        self.assertIn("already exists", get_json(body)["error"])

    async def test_cooldowns_list_all_and_filtered(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/cooldowns"))
        self.assertEqual(status, 200)
        self.assertEqual(len(get_json(body)["data"]), 2)
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/cooldowns?account=alpha"))
        self.assertEqual(status, 200)
        rows = get_json(body)["data"]
        self.assertEqual([r["account"] for r in rows], ["alpha"])
        self.assertEqual(self.fake.called("cooldowns_list"), ("alpha",))

    async def test_cooldowns_clear_with_and_without_key(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/cooldowns/clear"),
            {"account": "alpha", "plugin": "roll", "cooldown_key": "vote"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.fake.called("cooldowns_clear"), ("alpha", "roll", "vote"))
        await asyncio.to_thread(
            http_post, self.url("/api/cooldowns/clear"),
            {"account": "alpha", "plugin": "roll"},
        )
        self.assertEqual(
            self.fake.called("cooldowns_clear"), ("alpha", "roll", None))


class TestCredentialValidation(_ServerTestCase):
    async def asyncSetUp(self, token: str | None = TOKEN) -> None:
        await super().asyncSetUp(token)

    async def test_set_credential_requires_fields(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"name": "alpha"},
        )
        self.assertEqual(status, 400)
        self.assertIn("'credential'", get_json(body)["error"])
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"credential": SECRET},
        )
        self.assertEqual(status, 400)
        self.assertIn("'name'", get_json(body)["error"])
        self.assertNotIn(SECRET, body)

    async def test_set_credential_rejects_non_string(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"name": "alpha", "credential": 12345},
        )
        self.assertEqual(status, 400)
        self.assertIn("non-empty string", get_json(body)["error"])

    async def test_import_env_strict_bool(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/credentials/import-env"),
            {"overwrite": "yes"},
        )
        self.assertEqual(status, 400)
        self.assertIn("boolean", get_json(body)["error"])

    async def test_cooldowns_clear_requires_account_and_plugin(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/cooldowns/clear"),
            {"account": "alpha"},
        )
        self.assertEqual(status, 400)
        self.assertIn("'plugin'", get_json(body)["error"])
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/cooldowns/clear"), {},
        )
        self.assertEqual(status, 400)
        self.assertIn("'account'", get_json(body)["error"])

    async def test_unknown_account_is_400(self) -> None:
        self.fake.fail_with["accounts_set_credential"] = AccountError(
            "unknown account 'ghost'")
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"name": "ghost", "credential": SECRET},
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown account", get_json(body)["error"])


class TestCredentialGateNoToken(_ServerTestCase):
    """Server running with NO token: credential routes stay LOCKED (403)."""

    async def asyncSetUp(self, token: str | None = None) -> None:
        await super().asyncSetUp(token)

    async def test_spa_served_unauthenticated(self) -> None:
        status, body = await asyncio.to_thread(http_get, self.url("/"), None)
        self.assertEqual(status, 200)
        self.assertIn("LibzentSuite", body)

    async def test_normal_routes_still_open(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/cooldowns"), None)
        self.assertEqual(status, 200)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"), {"name": "gamma"})
        self.assertEqual(status, 200)

    async def test_credential_routes_403_with_gate_message(self) -> None:
        for path, payload in (
            ("/api/accounts/credential", {"name": "alpha", "credential": SECRET}),
            ("/api/credentials/import-env", {}),
        ):
            status, body = await asyncio.to_thread(
                http_post, self.url(path), payload, None)
            self.assertEqual(status, 403, path)
            self.assertEqual(get_json(body)["error"], GATE_MESSAGE)
            self.assertFalse(get_json(body)["ok"])
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/credentials/status"), None)
        self.assertEqual(status, 403)
        self.assertEqual(get_json(body)["error"], GATE_MESSAGE)

    async def test_add_with_credential_locked_without_credential_open(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"),
            {"name": "gamma", "credential": SECRET}, None,
        )
        self.assertEqual(status, 403)
        self.assertEqual(get_json(body)["error"], GATE_MESSAGE)
        self.assertNotIn("gamma", self.fake.stored_credentials)
        # the SAME route without a credential is a plain add (auth is off)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"), {"name": "gamma"}, None)
        self.assertEqual(status, 200)


class TestCredentialGateWrongToken(_ServerTestCase):
    """Token configured: wrong/missing credentials answer 401, not 403."""

    async def asyncSetUp(self, token: str | None = TOKEN) -> None:
        await super().asyncSetUp(token)

    async def test_wrong_token_401(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"name": "alpha", "credential": SECRET}, "totally-wrong",
        )
        self.assertEqual(status, 401)
        self.assertIn("token", get_json(body)["error"])
        self.assertNotIn(SECRET, body)
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/credentials/status"), "totally-wrong")
        self.assertEqual(status, 401)

    async def test_missing_token_401(self) -> None:
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/credentials/import-env"), {}, None)
        self.assertEqual(status, 401)


class TestNoSecretLeak(_ServerTestCase):
    async def asyncSetUp(self, token: str | None = TOKEN) -> None:
        await super().asyncSetUp(token)

    async def test_no_secret_in_any_response(self) -> None:
        # happy path first: store via both credential-bearing routes
        await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"name": "alpha", "credential": SECRET})
        await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"),
            {"name": "gamma", "credential": SECRET})
        # error path: a rejecting payload still must not echo the value
        await asyncio.to_thread(
            http_post, self.url("/api/accounts/credential"),
            {"name": "alpha"})  # 400 missing credential — no value sent
        await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"),
            {"name": "alpha", "credential": SECRET})  # 409 duplicate
        for path in ("/api/status", "/api/accounts", "/api/credentials/status",
                     "/api/cooldowns", "/"):
            _status, body = await asyncio.to_thread(http_get, self.url(path))
            self.assertNotIn(SECRET, body, path)


class TestCredentialTokenSlots(_ServerTestCase):
    """Round-2: conflicting token slots on credential routes fail closed."""

    async def asyncSetUp(self, token: str | None = TOKEN) -> None:
        await super().asyncSetUp(token)

    async def test_wrong_header_beats_right_query_token_401(self) -> None:
        # token supplied in TWO places (header + query) with the header
        # WRONG: the header slot wins the comparison and the request is
        # refused — no bypass, no credential stored
        url = self.url(f"/api/accounts/credential?token={TOKEN}")
        data = json.dumps({"name": "alpha", "credential": SECRET}).encode()
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Dashboard-Token", "totally-wrong")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status, body = response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode("utf-8")
        self.assertEqual(status, 401)
        self.assertIn("token", get_json(body)["error"])
        self.assertNotIn(SECRET, body)
        self.assertNotIn("alpha", self.fake.stored_credentials)

    async def test_gate_ordering_401_before_403(self) -> None:
        # with a token CONFIGURED, a bad credential must answer 401 (auth)
        # — never the 403 gate (which only applies with auth disabled)
        for provided in (None, "wrong"):
            status, body = await asyncio.to_thread(
                http_post, self.url("/api/credentials/import-env"),
                {"overwrite": False}, provided)
            self.assertEqual(status, 401, provided)
            self.assertIn("token", get_json(body)["error"])


if __name__ == "__main__":
    unittest.main()
