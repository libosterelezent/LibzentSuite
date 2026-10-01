"""Database layer tests (agent A2 deliverable, finished by the lead).

Covers: migration idempotence, every repository round-trip, cascade deletes,
pruning, filters, and the in-memory mode used by simulations.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database.db import Database
from database.models import AccountRecord, ProfileRecord, RatePolicyRecord


class DatabaseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, **kwargs) -> Database:
        return Database(self.path, **kwargs)


class TestMigrations(DatabaseTestBase):
    def test_migrate_then_reopen_is_noop(self) -> None:
        db = self.make()
        # constructor already migrated; explicit second call applies nothing
        self.assertEqual(db.migrate(), 0)
        db.close()
        db2 = self.make()
        self.assertEqual(db2.migrate(), 0)
        db2.close()

    def test_memory_mode(self) -> None:
        db = Database(":memory:")
        try:
            self.assertIsNone(db.path)
            db.accounts.add("alpha")
            self.assertEqual(db.accounts.get("alpha").name, "alpha")
        finally:
            db.close()


class TestAccounts(DatabaseTestBase):
    def test_add_get_list_roundtrip(self) -> None:
        db = self.make()
        db.accounts.add("alpha", token_ref="DISCORD_TOKEN_ALPHA", sync_group="g1")
        db.accounts.add("beta")
        names = [r.name for r in db.accounts.list()]
        self.assertEqual(names, ["alpha", "beta"])
        rec = db.accounts.get("alpha")
        self.assertIsInstance(rec, AccountRecord)
        self.assertEqual(rec.token_ref, "DISCORD_TOKEN_ALPHA")
        self.assertEqual(rec.sync_group, "g1")
        self.assertTrue(rec.enabled)
        self.assertIsNone(db.accounts.get("nope"))

    def test_duplicate_name_raises(self) -> None:
        from core.errors import DatabaseError

        db = self.make()
        db.accounts.add("alpha")
        with self.assertRaises(DatabaseError):
            db.accounts.add("alpha")

    def test_enable_and_settings(self) -> None:
        db = self.make()
        db.accounts.add("alpha")
        db.accounts.set_enabled("alpha", False)
        self.assertFalse(db.accounts.get("alpha").enabled)
        db.accounts.set_settings("alpha", {"theme": "dark"})
        self.assertEqual(db.accounts.get("alpha").settings, {"theme": "dark"})

    def test_profiles_assign_unassign(self) -> None:
        db = self.make()
        db.accounts.add("alpha")
        db.accounts.assign_profile("alpha", "roller")
        db.accounts.assign_profile("alpha", "sniper")
        self.assertEqual(db.accounts.profiles_of("alpha"), ["roller", "sniper"])
        db.accounts.unassign_profile("alpha", "roller")
        self.assertEqual(db.accounts.profiles_of("alpha"), ["sniper"])

    def test_delete_cascades(self) -> None:
        db = self.make()
        db.accounts.add("alpha")
        db.accounts.assign_profile("alpha", "roller")
        db.plugins.set_state("alpha", "soulcopy", False)
        db.plugins.set_setting("alpha", "soulcopy", "trigger", "x")
        db.account_settings.set("alpha", "reconnect_enabled", False)
        self.assertTrue(db.accounts.delete("alpha"))
        self.assertIsNone(db.accounts.get("alpha"))
        self.assertEqual(db.plugins.settings("alpha", "soulcopy"), {})
        self.assertIsNone(db.plugins.state("alpha", "soulcopy"))
        self.assertEqual(db.account_settings.get("alpha"), {})
        self.assertEqual(db.accounts.profiles_of("alpha"), [])
        self.assertFalse(db.accounts.delete("alpha"))


class TestAccountSettings(DatabaseTestBase):
    def test_set_get_delete(self) -> None:
        db = self.make()
        db.accounts.add("alpha")
        self.assertEqual(db.account_settings.get("alpha"), {})
        db.account_settings.set("alpha", "reconnect_max_delay", 30)
        db.account_settings.set("alpha", "reconnect_enabled", True)
        got = db.account_settings.get("alpha")
        self.assertEqual(got["reconnect_max_delay"], 30)
        self.assertTrue(got["reconnect_enabled"])
        db.account_settings.delete("alpha", "reconnect_enabled")
        self.assertNotIn("reconnect_enabled", db.account_settings.get("alpha"))


class TestProfiles(DatabaseTestBase):
    def test_upsert_get_list_delete(self) -> None:
        db = self.make()
        db.profiles.upsert("roller", ["roll"], "rolls")
        rec = db.profiles.get("roller")
        self.assertIsInstance(rec, ProfileRecord)
        self.assertEqual(rec.plugins, ["roll"])
        self.assertEqual(rec.description, "rolls")
        db.profiles.upsert("roller", ["roll", "snipe"], "rolls+snipes")
        self.assertEqual(db.profiles.get("roller").plugins, ["roll", "snipe"])
        self.assertEqual(len(db.profiles.list()), 1)
        self.assertTrue(db.profiles.delete("roller"))
        self.assertFalse(db.profiles.delete("roller"))
        self.assertIsNone(db.profiles.get("roller"))


class TestPlugins(DatabaseTestBase):
    def test_metadata(self) -> None:
        db = self.make()
        db.plugins.upsert_metadata("soulcopy", "1.2.0", "desc", [{"name": "trigger"}])
        meta = db.plugins.metadata_for("soulcopy")
        self.assertEqual(meta["version"], "1.2.0")
        self.assertEqual(meta["settings"], [{"name": "trigger"}])
        db.plugins.upsert_metadata("soulcopy", "1.3.0", "desc2", [])
        self.assertEqual(db.plugins.metadata_for("soulcopy")["version"], "1.3.0")
        self.assertEqual(len(db.plugins.metadata()), 1)

    def test_state_defaults_none_then_set(self) -> None:
        db = self.make()
        self.assertIsNone(db.plugins.state("alpha", "soulcopy"))
        db.plugins.set_state("alpha", "soulcopy", False)
        self.assertFalse(db.plugins.state("alpha", "soulcopy"))
        db.plugins.set_state("alpha", "soulcopy", True)
        self.assertTrue(db.plugins.state("alpha", "soulcopy"))

    def test_settings_and_all_account_settings(self) -> None:
        db = self.make()
        db.plugins.set_setting("alpha", "soulcopy", "trigger", "go")
        db.plugins.set_setting("beta", "soulcopy", "trigger", "go2")
        db.plugins.set_setting("beta", "roll", "command", "$m")
        self.assertEqual(db.plugins.setting("alpha", "soulcopy", "trigger"), "go")
        self.assertEqual(db.plugins.settings("beta", "roll"), {"command": "$m"})
        all_layers = db.plugins.all_account_settings()
        self.assertEqual(all_layers["beta"]["soulcopy"]["trigger"], "go2")


class TestRatePolicies(DatabaseTestBase):
    def test_roundtrip_all_levels(self) -> None:
        db = self.make()
        db.policies.set("global", "", "message", 2.0, 0.1, 0.5)
        db.policies.set("account", "alpha", "message", 3.0, 0.0, 1.0)
        db.policies.set("plugin", "soulcopy", "dm", 4.0, 0.2, 0.6)
        db.policies.set("account_plugin", "alpha/soulcopy", "dm", 5.0, 0.0, 0.0)
        rec = db.policies.get("global", "", "message")
        self.assertIsInstance(rec, RatePolicyRecord)
        self.assertEqual(rec.interval, 2.0)
        self.assertEqual(rec.jitter_min, 0.1)
        self.assertEqual(db.policies.get("account", "alpha", "message").interval, 3.0)
        self.assertEqual(len(db.policies.list()), 4)
        self.assertTrue(db.policies.delete("account", "alpha", "message"))
        self.assertFalse(db.policies.delete("account", "alpha", "message"))
        self.assertIsNone(db.policies.get("account", "alpha", "message"))


class TestAuditAndLogs(DatabaseTestBase):
    def test_audit_recent_oldest_first_and_prune(self) -> None:
        db = self.make(audit_keep=5)
        for i in range(10):
            db.audit.add({"ts": 1700000000.0 + i, "account": "alpha",
                          "direction": "out", "action": "message",
                          "channel_id": 1, "content": f"m{i}"})
        rows = db.audit.recent(limit=100)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["content"], "m5")  # oldest of the kept, first
        self.assertEqual(rows[-1]["content"], "m9")
        rows_alpha = db.audit.recent(limit=3, account="alpha")
        self.assertEqual(len(rows_alpha), 3)

    def test_audit_content_truncated(self) -> None:
        db = self.make()
        db.audit.add({"ts": 1.0, "account": "a", "direction": "out",
                      "action": "message", "content": "x" * 900})
        self.assertEqual(len(db.audit.recent(1)[0]["content"]), 500)

    def test_logs_filters_and_prune(self) -> None:
        db = self.make(log_keep=4)
        for i in range(8):
            db.logs.add({"ts": float(i), "severity": "INFO" if i % 2 else "ERROR",
                         "source": "scheduler" if i < 4 else "core",
                         "account": "alpha", "message": f"line{i}"})
        kept = db.logs.recent(limit=100)
        self.assertEqual(len(kept), 4)
        errs = db.logs.recent(limit=100, severity="ERROR")
        self.assertTrue(all(r["severity"] == "ERROR" for r in errs))
        srcs = db.logs.recent(limit=100, source="core")
        self.assertTrue(all(r["source"] == "core" for r in srcs))
        acct = db.logs.recent(limit=100, account="alpha")
        self.assertEqual(len(acct), 4)


class TestTaskHistory(DatabaseTestBase):
    def test_add_update_recent(self) -> None:
        db = self.make()
        tid = db.tasks.add({"name": "t", "account": "alpha", "plugin": "soulcopy",
                            "action": "message", "status": "completed",
                            "submitted_at": 1.0, "duration_s": 0.5})
        self.assertIsInstance(tid, int)
        db.tasks.update(tid, status="failed", error="boom")
        rows = db.tasks.recent(limit=10)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["error"], "boom")
        only = db.tasks.recent(limit=10, account="beta")
        self.assertEqual(only, [])


class TestKv(DatabaseTestBase):
    def test_get_set_delete_prefix(self) -> None:
        db = self.make()
        self.assertIsNone(db.kv.get("missing"))
        db.kv.set("jobs", "[]")
        db.kv.set("jobs:1", "a")
        self.assertEqual(db.kv.get("jobs"), "[]")
        self.assertEqual(db.kv.prefix("jobs:"), {"jobs:1": "a"})
        db.kv.delete("jobs")
        self.assertIsNone(db.kv.get("jobs"))


if __name__ == "__main__":
    unittest.main()
