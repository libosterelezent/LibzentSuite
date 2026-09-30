"""SQLite database: accounts registry, states, overrides, roles, audit, migration."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from core.account import AccountConfig
from core.database import Database


class TestDatabaseBasics(unittest.TestCase):
    def test_creates_file_with_restricted_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "slaughter.db"
            db = Database(path)
            self.assertTrue(path.is_file())
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            db.close()

    def test_account_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            db.upsert_account(AccountConfig(
                name="alpha", sync_group="grinders", roles=["roll", "snipe"],
            ))
            loaded = db.load_accounts()
            self.assertEqual([c.name for c in loaded], ["alpha"])
            self.assertEqual(loaded[0].roles, ["roll", "snipe"])
            self.assertEqual(loaded[0].sync_group, "grinders")
            # token VALUES are never stored
            raw = json.dumps([dict(r) for r in db._conn.execute("SELECT * FROM accounts")])
            self.assertNotIn("DISCORD_TOKEN", raw.replace("token_ref", ""))
            self.assertTrue(db.delete_account("alpha"))
            self.assertEqual(db.load_accounts(), [])
            db.close()

    def test_plugin_states_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            self.assertTrue(db.plugin_enabled("a", "spy"))
            db.set_plugin_enabled("a", "spy", False)
            self.assertFalse(db.plugin_enabled("a", "spy"))
            db.set_limit_override("a", "message", "1/30s")
            self.assertEqual(db.limit_overrides(), {"a": {"message": "1/30s"}})
            self.assertTrue(db.clear_limit_override("a", "message"))
            self.assertEqual(db.limit_overrides(), {})
            db.close()

    def test_delays_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            db.set_delay("message", 0.5, 1.5)
            db.set_delay("message", 0.8, 2.0, account="alpha")
            self.assertEqual(db.delays(), {
                "global": {"message": [0.5, 1.5]},
                "accounts": {"alpha": {"message": [0.8, 2.0]}},
            })
            self.assertTrue(db.clear_delay("message", "alpha"))
            self.assertTrue(db.clear_delay("message"))
            self.assertEqual(db.delays(), {"global": {}, "accounts": {}})
            db.set_setting_override("alpha", "soulcopy", "trigger", "sc")
            self.assertEqual(db.setting_overrides(), {"alpha": {"soulcopy": {"trigger": "sc"}}})
            db.close()

    def test_roles_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            db.upsert_role("roll", ["roll"], "rolling duty")
            db.upsert_role("snipe", ["snipe"])
            roles = db.roles()
            self.assertEqual(sorted(roles), ["roll", "snipe"])
            self.assertEqual(roles["roll"]["plugins"], ["roll"])
            db.upsert_role("roll", ["roll", "claim"])
            self.assertEqual(db.roles()["roll"]["plugins"], ["claim", "roll"])
            self.assertTrue(db.delete_role("roll"))
            self.assertEqual(sorted(db.roles()), ["snipe"])
            db.close()

    def test_audit_log_and_prune(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db", audit_keep=50)
            for i in range(80):
                db.add_audit({"account": "alpha", "direction": "out", "action": "message",
                              "channel_id": 500, "content": f"m{i}", "wait_ms": 10.0})
            rows = db.audit_log(limit=100)
            self.assertEqual(len(rows), 50)  # pruned to the cap
            self.assertEqual(rows[-1]["content"], "m79")
            filtered = db.audit_log(limit=10, account="beta")
            self.assertEqual(filtered, [])
            db.close()

    def test_kv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "s.db")
            self.assertIsNone(db.kv_get("x"))
            db.kv_set("x", "1")
            self.assertEqual(db.kv_get("x"), "1")
            db.close()

    def test_legacy_state_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "state.json"
            legacy.write_text(json.dumps({
                "plugin_states": {"alpha": {"spy": False}},
                "limit_overrides": {"alpha": {"message": "2/30s"}},
            }), encoding="utf-8")
            db = Database(Path(tmp) / "s.db")
            imported = db.migrate_legacy_state(legacy)
            self.assertEqual(imported, 2)
            self.assertFalse(db.plugin_enabled("alpha", "spy"))
            self.assertEqual(db.limit_overrides(), {"alpha": {"message": "2/30s"}})
            self.assertTrue(legacy.with_suffix(".json.imported").exists())
            # second run is a no-op (file renamed away)
            self.assertEqual(db.migrate_legacy_state(legacy), 0)
            db.close()


if __name__ == "__main__":
    unittest.main()
