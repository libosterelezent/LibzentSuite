"""ControlAPI credential/cooldown management tests (agent B).

Real ControlAPI over duck-typed fakes implementing the PINNED manager
contracts (async accounts manager, async CredentialService/CooldownService
foundations, sync rate-limit engine) — plus the REAL CredentialService and
CooldownService over tiny in-memory repositories, so store/status/import
semantics are exercised for real.

Security assertions run throughout: the token VALUE must never appear in
any response payload, audit entry or error message the facade produces.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.api import ControlAPI
from core.cooldowns import CooldownService
from core.credential_service import CredentialService
from core.credentials import Credentials
from core.errors import AccountError, SettingsValidationError
from core.event_bus import EventBus
from core.settings_manager import Setting, SettingsManager

SECRET = "tok-SECRET-5f3a91c2"      # the value that must never leak
OTHER_SECRET = "tok-OTHER-9b7d31e4"


# ------------------------------------------------------------------- fakes
class FakeCredentialsRepo:
    """In-memory credentials repository (ciphertext in, ciphertext out)."""

    def __init__(self) -> None:
        self.rows: dict[str, bytes] = {}

    async def store(self, account: str, ciphertext: bytes) -> None:
        self.rows[account] = ciphertext

    async def load(self, account: str) -> bytes | None:
        return self.rows.get(account)

    async def delete(self, account: str) -> bool:
        return self.rows.pop(account, None) is not None

    async def accounts_with_credential(self) -> list[str]:
        return sorted(self.rows)


class FakeCooldownRepo:
    """In-memory cooldown rows with absolute expiry (CooldownService shape)."""

    def __init__(self) -> None:
        self.rows: dict[tuple, dict[str, Any]] = {}
        self.deleted_accounts: list[str] = []

    async def upsert(self, account, plugin, cooldown_key, started_at,
                     expires_at, source="plugin", reason="", metadata=None):
        self.rows[(account, plugin, cooldown_key)] = {
            "account": account, "plugin": plugin, "cooldown_key": cooldown_key,
            "started_at": started_at, "expires_at": expires_at,
            "source": source, "reason": reason, "metadata": metadata or {},
        }

    async def get(self, account, plugin, cooldown_key, now=None):
        row = self.rows.get((account, plugin, cooldown_key))
        if row and row["expires_at"] > (now or time.time()):
            return dict(row)
        return None

    async def active_for(self, account, plugin=None, now=None):
        now = now or time.time()
        return [dict(r) for r in self.rows.values()
                if r["account"] == account and r["expires_at"] > now
                and (plugin is None or r["plugin"] == plugin)]

    async def clear(self, account, plugin, cooldown_key) -> bool:
        return self.rows.pop((account, plugin, cooldown_key), None) is not None

    async def clear_expired(self, now=None) -> int:
        now = now or time.time()
        dead = [k for k, r in self.rows.items() if r["expires_at"] <= now]
        for k in dead:
            self.rows.pop(k, None)
        return len(dead)

    async def delete_account(self, account) -> int:
        self.deleted_accounts.append(account)
        keys = [k for k in self.rows if k[0] == account]
        for k in keys:
            self.rows.pop(k, None)
        return len(keys)


class FakeAccountsManager:
    """Async manager per the pinned contract (credential field included)."""

    def __init__(self, names=("alpha", "beta")) -> None:
        self._names = list(names)
        self.added: list[dict[str, Any]] = []
        self.removed: list[str] = []

    async def accounts(self) -> list[dict[str, Any]]:
        return [{"name": n, "status": "stopped", "enabled": True,
                 "profiles": []} for n in self._names]

    async def add_account(self, name, *, token_ref=None, enabled=True,
                          workspace=None, plugins=None):
        if name in self._names:
            raise AccountError(f"account {name!r} already exists")
        self._names.append(name)
        payload = {"name": name, "status": "created", "enabled": enabled,
                   "workspace": workspace, "plugins": plugins or []}
        self.added.append(payload)
        return dict(payload)

    async def remove_account(self, name):
        if name not in self._names:
            raise AccountError(f"unknown account {name!r}")
        self._names.remove(name)
        self.removed.append(name)
        return {"removed": name, "existed": True}

    def get(self, name):  # pinned: raises the domain error when unknown
        if name not in self._names:
            raise AccountError(f"unknown account {name!r}")
        return SimpleNamespace(name=name)

    async def details(self, name):
        self.get(name)
        return {"name": name, "status": {"state": "stopped"},
                "identity": {}, "runtime": {}, "plugins": [], "profiles": [],
                "rate_limit": {}}

    async def set_enabled(self, name, enabled):
        self.get(name)
        return {"name": name, "enabled": enabled}

    async def start(self, name):
        self.get(name)
        return {"started": name}

    async def stop(self, name):
        self.get(name)
        return {"stopped": name}

    async def start_all(self):
        return {"started": list(self._names), "failed": {}, "skipped": []}

    async def stop_all(self):
        return {"stopped": list(self._names), "failed": {}}

    async def emergency_stop(self, name):
        self.get(name)
        return {"account": name, "cancelled_tasks": 0}

    async def load(self):  # pinned ctor/load seam (no-op here)
        return None


class FakeSyncCollaborators:
    """plugins/settings/scheduler/logs minimal duck-typed stand-ins."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    # ---- plugin manager surface used by the facade
    def manifests(self):
        return []

    def states(self, account):
        return []

    async def set_enabled(self, account, plugin, enabled):
        return None

    def set_setting(self, account, plugin, key, value):
        return value

    # ---- scheduler
    def stats(self):
        return {"queued": 0, "running": 0}

    def cancel_matching(self, account=None, plugin=None):
        return 0

    async def stop(self, drain=True):
        return None

    def tasks(self, status=None, account=None):
        return []

    def cancel(self, task_id):
        return False

    def history(self, limit=50, account=None):
        return []

    # ---- settings manager
    def account_settings(self, name):
        return {}

    # ---- logs
    def record(self, severity, source, message, **kwargs):
        self.entries.append({"severity": severity, "source": source,
                             "message": message, **kwargs})

    def info(self, source, message, **kwargs):
        self.record("INFO", source, message, **kwargs)

    def error(self, source, message, **kwargs):
        self.record("ERROR", source, message, **kwargs)

    def tail(self, n=100, account=None, severity=None):
        return self.entries

    def subscribe(self, callback):
        return lambda: None


class FakeLimiter:
    """Sync memory-only engine per the pinned contract."""

    def __init__(self) -> None:
        self.policies: dict[tuple, Any] = {}
        self.lane = {"enabled": False, "prefixrate": 1.5, "paused": False,
                     "waiting": 0, "grants": 0, "last_grant_age_s": None}

    def set_policy_values(self, action, interval, jitter_min=0.0,
                          jitter_max=0.0, level="global", owner=None):
        policy = SimpleNamespace(action=action, interval=float(interval),
                                 jitter_min=float(jitter_min),
                                 jitter_max=float(jitter_max))
        self.policies[(level, owner, action)] = policy
        return policy

    def clear_policy(self, level, owner, action):
        return self.policies.pop((level, owner, action), None) is not None

    def policies_list(self):
        return []

    def stats(self):
        return {"floor": 1.5, "policies": []}

    def warnings(self):
        return []

    def effective(self, action, account=None, plugin=None):
        return {"action": action, "interval": 1.5, "jitter": [0.0, 0.0],
                "sources": []}

    def set_lane_enabled(self, enabled):
        self.lane["enabled"] = bool(enabled)

    def set_lane_prefixrate(self, seconds):
        self.lane["prefixrate"] = float(seconds)

    def lane_status(self):
        return dict(self.lane)

    def set_floor(self, seconds):
        return max(1.5, float(seconds))


class FakePoliciesRepo:
    def __init__(self) -> None:
        self.sets: list[tuple] = []
        self.deletes: list[tuple] = []

    async def set(self, level, owner, action, interval, jitter_min, jitter_max):
        self.sets.append((level, owner, action, interval, jitter_min, jitter_max))

    async def delete(self, level, owner, action):
        self.deletes.append((level, owner, action))
        return True


class FakeKvRepo:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def set(self, key, value):
        self.values[key] = value

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def delete(self, key):
        return self.values.pop(key, None) is not None


class SettingsStack:
    """ControlAPI over a REAL SettingsManager: plugin-setting clear (null)
    and framework account-setting facades (round-3 findings)."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cred_svc = CredentialService(
            None, key_path=Path(self.tmp.name) / "creds.key")
        self.accounts = FakeAccountsManager()
        self.bus = EventBus()
        self.plugin_repo = FakeDeletablePluginRepo()
        self.settings = SettingsManager(
            self.bus, plugin_repo=self.plugin_repo,
            global_layer={"exporter": {"trigger": "global-trigger"}})
        self.plugins = FakePluginManager(self.settings)
        self.limiter = FakeLimiter()
        self.database = FakeDatabase()
        self.audit = FakeAuditRecorder()
        self.api = ControlAPI(
            accounts=self.accounts, plugins=self.plugins,
            settings=self.settings,
            limiter=self.limiter, scheduler=FakeSyncCollaborators(),
            logs=FakeSyncCollaborators(), audit=self.audit,
            credentials=Credentials.from_values({}),
            database=self.database, credentials_svc=self.cred_svc,
        )

    def cleanup(self) -> None:
        self.tmp.cleanup()


class FakeDeletablePluginRepo:
    """Plugin repo with the round-3 delete_setting (F9's repo shape)."""

    def __init__(self) -> None:
        self.rows: dict[tuple, Any] = {}
        self.deletes: list = []

    async def all_account_settings(self):
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for (account, plugin, key), value in self.rows.items():
            out.setdefault(account, {}).setdefault(plugin, {})[key] = value
        return out

    async def set_setting(self, account, plugin, key, value):
        self.rows[(account, plugin, key)] = value

    async def delete_setting(self, account, plugin, key):
        self.deletes.append((account, plugin, key))
        self.rows.pop((account, plugin, key), None)


class FakePluginManager:
    """Plugin manager WITHOUT clear_setting: exercises the api fallback.
    set_setting delegates to the real SettingsManager (like PluginManager)."""

    def __init__(self, settings: SettingsManager) -> None:
        self._settings = settings
        self.live: dict[tuple, Any] = {}

    async def set_setting(self, account, plugin, key, value):
        validated = await self._settings.set_plugin_setting(
            account, plugin, key, value, _EXPORTER_SCHEMA)
        self.live[(account, plugin, key)] = validated
        return validated


class FakeDatabase:
    def __init__(self) -> None:
        self.policies = FakePoliciesRepo()
        self.kv = FakeKvRepo()

    async def close(self):
        return None


class FakeAuditRecorder:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, account, direction, action, channel_id=None, content="",
               wait_ms=0.0, jitter_ms=0.0, note=""):
        entry = {"account": account, "direction": direction, "action": action,
                 "channel_id": channel_id, "content": content,
                 "wait_ms": wait_ms, "jitter_ms": jitter_ms, "note": note}
        self.entries.append(entry)
        return dict(entry)

    def tail(self, n=50, account=None):
        return self.entries[-n:]

    def history(self, limit=200, account=None):
        return self.entries[-limit:]

    def subscribe(self, callback):
        return lambda: None


def actions(recorded: list[dict[str, Any]]) -> list[str]:
    return [e["action"] for e in recorded]


class ApiStack:
    """One fully wired ControlAPI over the fakes (+ real services)."""

    def __init__(self, env: dict[str, str] | None = None,
                 names: tuple = ("alpha", "beta")) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cred_repo = FakeCredentialsRepo()
        self.cred_svc = CredentialService(
            self.cred_repo, key_path=Path(self.tmp.name) / "creds.key")
        self.cd_repo = FakeCooldownRepo()
        self.cooldowns = CooldownService(self.cd_repo)
        self.accounts = FakeAccountsManager(names)
        self.shared = FakeSyncCollaborators()
        self.limiter = FakeLimiter()
        self.database = FakeDatabase()
        self.audit = FakeAuditRecorder()
        self.credentials = Credentials.from_values(env or {})
        self.api = ControlAPI(
            accounts=self.accounts, plugins=self.shared,
            settings=self.shared, limiter=self.limiter, scheduler=self.shared,
            logs=self.shared, audit=self.audit, credentials=self.credentials,
            database=self.database, credentials_svc=self.cred_svc,
            cooldowns=self.cooldowns,
        )

    def cleanup(self) -> None:
        self.tmp.cleanup()


class TestAccountsAddWithCredential(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stack = ApiStack()

    def tearDown(self) -> None:
        self.stack.cleanup()

    async def test_add_with_credential_stores_via_service(self) -> None:
        out = await self.stack.api.accounts_add(
            {"name": "gamma", "credential": SECRET})
        self.assertEqual(out["name"], "gamma")
        self.assertEqual(out["credential"], "db")
        # stored for the RIGHT account, decryptable with the service key
        self.assertIn("gamma", self.stack.cred_repo.rows)
        self.assertEqual(
            await self.stack.cred_svc.resolve("gamma"), SECRET)
        # audit: created + stored, names only
        self.assertIn("mgmt:account.created", actions(self.stack.audit.entries))
        self.assertIn("mgmt:credential.stored", actions(self.stack.audit.entries))
        stored_notes = [e for e in self.stack.audit.entries
                        if e["action"] == "mgmt:credential.stored"]
        self.assertEqual(stored_notes[0]["account"], "gamma")

    async def test_add_without_credential_leaves_service_alone(self) -> None:
        out = await self.stack.api.accounts_add({"name": "delta"})
        self.assertIsNone(out.get("credential"))
        self.assertNotIn("delta", self.stack.cred_repo.rows)
        self.assertNotIn("mgmt:credential.stored",
                         actions(self.stack.audit.entries))

    async def test_add_rejects_unknown_keys_and_bad_credential(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.stack.api.accounts_add({"name": "gamma", "cred": SECRET})
        self.assertIn("unknown account payload keys", str(ctx.exception))
        with self.assertRaises(ValueError):
            await self.stack.api.accounts_add({"name": "gamma", "credential": ""})
        with self.assertRaises(ValueError):
            await self.stack.api.accounts_add({"name": "gamma", "credential": 42})
        self.assertNotIn("gamma", self.stack.cred_repo.rows)

    async def test_no_secret_in_any_response_or_audit(self) -> None:
        import json

        out = await self.stack.api.accounts_add({"name": "gamma", "credential": SECRET})
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertNotIn(SECRET, json.dumps(self.stack.audit.entries))

    async def test_add_duplicate_maps_to_account_error(self) -> None:
        with self.assertRaises(AccountError):
            await self.stack.api.accounts_add({"name": "alpha"})


class TestCredentialStatusAndImport(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stack = ApiStack(env={
            "DISCORD_TOKEN_ALPHA": SECRET,
            "DISCORD_TOKEN_BETA": OTHER_SECRET,
        })

    def tearDown(self) -> None:
        self.stack.cleanup()

    async def test_credential_status_listing(self) -> None:
        await self.stack.cred_svc.store("alpha", SECRET)
        rows = await self.stack.api.accounts_credential_status()
        self.assertEqual(rows, [{"name": "alpha", "credential": "db"},
                                {"name": "beta", "credential": "missing"}])

    async def test_import_env_flow_names_only(self) -> None:
        await self.stack.cred_svc.store("beta", "pre-existing-token")
        report = await self.stack.api.credentials_import_env()
        # alpha imported from .env; beta SKIPPED (already has a db credential)
        self.assertEqual(report["imported"], ["alpha"])
        self.assertEqual(report["skipped"], ["beta"])
        self.assertEqual(report["failed"], {})
        self.assertEqual(await self.stack.cred_svc.resolve("alpha"), SECRET)
        # overwrite flips beta's skip into an import
        report = await self.stack.api.credentials_import_env(overwrite=True)
        self.assertIn("alpha", report["imported"])   # already db? no — alpha
        self.assertIn("beta", report["imported"])
        import json
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(OTHER_SECRET, json.dumps(report))
        self.assertIn("mgmt:credential.imported", actions(self.stack.audit.entries))

    async def test_import_env_strict_bool(self) -> None:
        with self.assertRaises(ValueError):
            await self.stack.api.credentials_import_env(overwrite="yes")

    async def test_set_credential_roundtrip_and_unknown_account(self) -> None:
        out = await self.stack.api.accounts_set_credential("alpha", SECRET)
        self.assertEqual(out, {"account": "alpha", "credential": "db"})
        self.assertEqual(await self.stack.cred_svc.resolve("alpha"), SECRET)
        with self.assertRaises(AccountError):
            await self.stack.api.accounts_set_credential("ghost", SECRET)
        with self.assertRaises(ValueError):
            await self.stack.api.accounts_set_credential("alpha", "  ")
        self.assertNotIn("ghost", self.stack.cred_repo.rows)

    async def test_set_credential_requires_service(self) -> None:
        stack = ApiStack()
        stack.api._credentials_svc = None
        try:
            with self.assertRaises(RuntimeError):
                await stack.api.accounts_set_credential("alpha", SECRET)
        finally:
            stack.cleanup()


class TestRemoveCascadeAndCooldowns(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stack = ApiStack()

    def tearDown(self) -> None:
        self.stack.cleanup()

    async def test_remove_cascades_credential_and_cooldowns(self) -> None:
        await self.stack.cred_svc.store("alpha", SECRET)
        await self.stack.cooldowns.set("alpha", "roll", "vote", 600)
        await self.stack.cooldowns.set("alpha", "roll", "daily", 300)
        out = await self.stack.api.accounts_remove("alpha")
        self.assertEqual(out["removed"], "alpha")
        self.assertNotIn("alpha", self.stack.cred_repo.rows)      # credential gone
        self.assertEqual(self.stack.cd_repo.deleted_accounts, ["alpha"])  # cooldowns gone
        self.assertEqual(await self.stack.cooldowns.active_for("alpha"), [])
        self.assertIn("mgmt:account.removed", actions(self.stack.audit.entries))

    async def test_remove_without_services_is_guarded(self) -> None:
        stack = ApiStack()
        stack.api._credentials_svc = None
        stack.api._cooldowns = None
        try:
            out = await stack.api.accounts_remove("alpha")
            self.assertEqual(out["removed"], "alpha")
        finally:
            stack.cleanup()

    async def test_cooldowns_list_per_account_and_all(self) -> None:
        await self.stack.cooldowns.set("alpha", "roll", "vote", 90)
        await self.stack.cooldowns.set("beta", "exporter", "export", 45)
        rows = await self.stack.api.cooldowns_list()
        self.assertEqual([r["account"] for r in rows], ["beta", "alpha"])  # by expiry
        self.assertEqual(rows[0]["plugin"], "exporter")
        self.assertEqual(rows[0]["cooldown_key"], "export")
        self.assertGreater(rows[0]["remaining_s"], 30)
        just_alpha = await self.stack.api.cooldowns_list("alpha")
        self.assertEqual([r["account"] for r in just_alpha], ["alpha"])
        self.assertEqual(await self.stack.api.cooldowns_list("ghost"), [])

    async def test_cooldowns_clear_one_key_and_whole_plugin(self) -> None:
        await self.stack.cooldowns.set("alpha", "roll", "vote", 600)
        await self.stack.cooldowns.set("alpha", "roll", "daily", 600)
        out = await self.stack.api.cooldowns_clear("alpha", "roll", "vote")
        self.assertEqual(out["cleared"], 1)
        self.assertEqual(out["cooldown_key"], "vote")
        remaining = await self.stack.api.cooldowns_list("alpha")
        self.assertEqual([r["cooldown_key"] for r in remaining], ["daily"])
        out = await self.stack.api.cooldowns_clear("alpha", "roll")
        self.assertIsNone(out["cooldown_key"])
        self.assertEqual(out["cleared"], 1)  # the one daily row left
        self.assertEqual(await self.stack.api.cooldowns_list("alpha"), [])
        cleared = [e for e in self.stack.audit.entries
                   if e["action"] == "mgmt:cooldown.cleared"]
        self.assertTrue(cleared)
        self.assertEqual(cleared[0]["account"], "alpha")

    async def test_cooldowns_validation(self) -> None:
        with self.assertRaises(ValueError):
            await self.stack.api.cooldowns_clear("", "roll", "vote")
        with self.assertRaises(ValueError):
            await self.stack.api.cooldowns_clear("alpha", "roll", "")
        stack = ApiStack()
        stack.api._cooldowns = None
        try:
            with self.assertRaises(RuntimeError):
                await stack.api.cooldowns_clear("alpha", "roll", "vote")
        finally:
            stack.cleanup()


class TestStatusEnrichmentAndAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stack = ApiStack()

    def tearDown(self) -> None:
        self.stack.cleanup()

    async def test_status_carries_credential_and_cooldown_count(self) -> None:
        await self.stack.cred_svc.store("alpha", SECRET)
        await self.stack.cooldowns.set("beta", "roll", "vote", 60)
        status = await self.stack.api.status()
        by_name = {a["name"]: a for a in status["accounts"]}
        self.assertEqual(by_name["alpha"]["credential"], "db")
        self.assertEqual(by_name["beta"]["credential"], "missing")
        self.assertEqual(by_name["beta"]["cooldowns"], 1)
        self.assertEqual(by_name["alpha"]["cooldowns"], 0)

    async def test_start_stop_estop_audited(self) -> None:
        await self.stack.api.accounts_start("alpha")
        await self.stack.api.accounts_stop("alpha")
        await self.stack.api.accounts_start()          # all -> system row
        await self.stack.api.accounts_emergency_stop("alpha")
        await self.stack.api.emergency_stop_all(surface="test")
        recorded = actions(self.stack.audit.entries)
        for expected in ("mgmt:account.started", "mgmt:account.stopped",
                         "mgmt:account.estop", "mgmt:estop"):
            self.assertIn(expected, recorded)
        started_all = [e for e in self.stack.audit.entries
                       if e["action"] == "mgmt:account.started" and
                       e["account"] == "system"]
        self.assertTrue(started_all)

    async def test_audit_notes_carry_the_surface(self) -> None:
        await self.stack.api.accounts_add({"name": "gamma"}, surface="cli")
        await self.stack.api.accounts_set_credential("gamma", SECRET,
                                                     surface="dashboard")
        notes = {e["action"]: e["note"] for e in self.stack.audit.entries}
        self.assertIn("surface=cli", notes["mgmt:account.created"])
        self.assertIn("surface=dashboard", notes["mgmt:credential.stored"])
        # and never the secret
        self.assertNotIn(SECRET, repr(self.stack.audit.entries))


class TestPluginSettingClear(unittest.IsolatedAsyncioTestCase):
    """Round-3: set-setting value null DELETES the account override."""

    def setUp(self) -> None:
        self.stack = SettingsStack()

    def tearDown(self) -> None:
        self.stack.cleanup()

    async def test_null_resets_by_deleting_the_override(self) -> None:
        api, stack = self.stack.api, self.stack
        await api.plugins_set_setting("alpha", "exporter", "trigger", "acct")
        self.assertEqual(
            stack.settings.resolve("exporter", _EXPORTER_SCHEMA,
                                   account="alpha")["trigger"], "acct")
        out = await api.plugins_set_setting(
            "alpha", "exporter", "trigger", None)
        self.assertTrue(out["cleared"])
        # fallback path has no schema: value is None (no override); the
        # manager-path (F8) returns the effective value instead
        self.assertIsNone(out["value"])
        self.assertEqual(
            stack.plugin_repo.deletes, [("alpha", "exporter", "trigger")])
        self.assertEqual(
            stack.settings.resolve("exporter", _EXPORTER_SCHEMA,
                                   account="alpha")["trigger"],
            "global-trigger")

    async def test_prefers_plugin_manager_clear_setting_when_present(self) -> None:
        calls: list = []

        class WithClear(FakePluginManager):
            async def clear_setting(self, account, plugin, key):
                calls.append((account, plugin, key))
                return "from-manager"

        stack = self.stack
        stack.api._plugins = WithClear(stack.settings)
        out = await stack.api.plugins_set_setting(
            "alpha", "exporter", "trigger", None)
        self.assertEqual(calls, [("alpha", "exporter", "trigger")])
        self.assertEqual(out["value"], "from-manager")
        self.assertEqual(stack.plugin_repo.deletes, [])  # manager owns it


_EXPORTER_SCHEMA = (Setting("trigger", "str", "default-trigger"),)


class TestAccountSettingFacades(unittest.IsolatedAsyncioTestCase):
    """Round-3: ControlAPI surface for framework account settings."""

    def setUp(self) -> None:
        self.stack = SettingsStack()

    def tearDown(self) -> None:
        self.stack.cleanup()

    async def test_get_returns_resolved_settings(self) -> None:
        out = await self.stack.api.account_setting_get("alpha")
        self.assertEqual(out["account"], "alpha")
        self.assertEqual(out["settings"]["reconnect_multiplier"], 2.0)
        self.assertEqual(out["settings"]["reconnect_enabled"], True)

    async def test_set_validates_and_audits(self) -> None:
        out = await self.stack.api.account_setting_set(
            "alpha", "typing_probability", 0.85, surface="cli")
        self.assertEqual(out["value"], 0.85)
        self.assertEqual(
            self.stack.settings.account_settings("alpha")["typing_probability"],
            0.85)
        rows = [e for e in self.stack.audit.entries
                if e["action"] == "mgmt:account.setting"]
        self.assertEqual(rows[0]["note"], "surface=cli key=typing_probability")

    async def test_set_rejects_unknown_and_out_of_bounds(self) -> None:
        with self.assertRaises(SettingsValidationError):
            await self.stack.api.account_setting_set("alpha", "bogus", 1)
        with self.assertRaises(SettingsValidationError):
            await self.stack.api.account_setting_set(
                "alpha", "reconnect_multiplier", 99.0)
        self.assertEqual(
            self.stack.settings.account_settings("alpha")["reconnect_multiplier"],
            2.0)


class TestCredentialKeyFileHardening(unittest.IsolatedAsyncioTestCase):
    """Round-3: empty-keyfile self-heal + loose-permission warning."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.key_path = Path(self.tmp.name) / "creds.key"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_empty_keyfile_regenerates_when_nothing_stored(self) -> None:
        repo = FakeCredentialsRepo()
        svc = CredentialService(repo, key_path=self.key_path)
        await svc.refresh()                       # known: zero stored
        self.key_path.write_text("")              # crash artifact
        await svc.store("alpha", "tok-1")         # must self-heal, not raise
        self.assertTrue(self.key_path.read_text().strip())
        self.assertEqual(await svc.resolve("alpha"), "tok-1")

    async def test_empty_keyfile_still_fails_when_credentials_stored(self) -> None:
        repo = FakeCredentialsRepo()
        svc = CredentialService(repo, key_path=self.key_path)
        await svc.store("alpha", "tok-1")         # creates the key file
        self.key_path.write_text("")              # disaster: key lost
        await svc.refresh()                       # DB still has a row
        svc._fernet = None                        # next use re-reads the file
        from core.errors import CredentialsError
        with self.assertRaises(CredentialsError):
            await svc.store("beta", "tok-2")

    @unittest.skipIf(os.name == "nt",
                     "Windows does not enforce POSIX permission bits - "
                     "the loose-keyfile warning is still verified at boot")
    async def test_loose_keyfile_permissions_warn_on_read(self) -> None:
        svc = CredentialService(FakeCredentialsRepo(), key_path=self.key_path)
        await svc.store("alpha", "tok-1")
        self.key_path.chmod(0o644)
        # merged semantics (upstream tighten + round-3 visibility): a
        # readable-but-loose key is TIGHTENED to 0600 on read; the warning
        # fires only when the tightening FAILED (still loose afterwards)
        svc._fernet = None                    # drop the cipher to re-read
        svc._memory.clear()
        await svc.resolve("alpha")
        self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)
        with self.assertNoLogs("libzent.credentials", level="WARNING"):
            svc._fernet = None
            svc._memory.clear()
            await svc.resolve("alpha")
        self.key_path.chmod(0o600)


class TestPluginStateSingleEncoding(unittest.IsolatedAsyncioTestCase):
    """Round-3: PluginStateService stores RAW values (kv encodes once);
    rows written via raw kv.set must read back, legacy double-encoded
    rows stay readable."""

    async def test_raw_kv_rows_read_back_instead_of_default(self) -> None:
        import json as _json

        from core.plugin_state import PluginStateService

        class MiniKv:
            """Mirrors KvRepository's contract: JSON-encode on set,
            tolerant-decode on get (raw string when not valid JSON)."""

            def __init__(self) -> None:
                self.rows: dict[str, str] = {}

            async def set(self, key, value):
                self.rows[key] = _json.dumps(value)

            async def get(self, key, default=None):
                raw = self.rows.get(key)
                if raw is None:
                    return default
                try:
                    return _json.loads(raw)
                except ValueError:
                    return raw

        kv = MiniKv()
        state = PluginStateService(kv)
        # a row written through RAW kv (what used to read back as default)
        await kv.set("pstate:global:counter", 41)
        self.assertEqual(await state.get_global("counter"), 41)
        # service writes are stored RAW (single-encoded) at the kv layer
        await state.set_global("flag", {"on": True})
        self.assertEqual(kv.rows["pstate:global:flag"], '{"on": true}')
        # ...and round-trip through the service
        self.assertEqual(await state.get_global("flag"), {"on": True})
        # plain strings survive too (JSON-looking ones mangle per K10 note)
        await state.set_global("note", "hello world")
        self.assertEqual(await state.get_global("note"), "hello world")
        # LEGACY double-encoded rows still decode
        await kv.set("pstate:global:old", _json.dumps({"legacy": [1, 2]}))
        self.assertEqual(
            await state.get_global("old"), {"legacy": [1, 2]})


class TestRateLimitPersistence(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stack = ApiStack()

    def tearDown(self) -> None:
        self.stack.cleanup()

    async def test_set_persists_policy_after_engine_mutation(self) -> None:
        out = await self.stack.api.ratelimit_set("message", 3.0, 0.1, 0.5,
                                                 "account", "alpha")
        self.assertEqual(out["action"], "message")   # engine mutated...
        self.assertEqual(                             # ...and persisted
            self.stack.database.policies.sets,
            [("account", "alpha", "message", 3.0, 0.1, 0.5)])

    async def test_clear_persists_delete(self) -> None:
        await self.stack.api.ratelimit_set("message", 3.0)
        await self.stack.api.ratelimit_clear("global", None, "message")
        self.assertEqual(self.stack.database.policies.deletes,
                         [("global", None, "message")])

    async def test_concurrent_clears_keep_both_tombstones(self) -> None:
        """Round-3 RMW race: two clears racing the kv read-modify-write
        lost tombstones — cleared defaults resurrected on the next boot."""
        import asyncio

        await self.stack.api.ratelimit_set("message", 3.0)
        await self.stack.api.ratelimit_set("dm", 3.0)
        await asyncio.gather(
            self.stack.api.ratelimit_clear("global", None, "message"),
            self.stack.api.ratelimit_clear("global", None, "dm"),
        )
        stored = self.stack.database.kv.values["ratelimit:removed"]
        self.assertIn("global||message", stored)
        self.assertIn("global||dm", stored)


if __name__ == "__main__":
    unittest.main()
