"""Persistence: plugin toggles + limit overrides survive restarts."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.api import ControlAPI
from core.persistence import StateStore
from core.rate_limiter import RateLimiterService

from .helpers import Harness


class TestStateStore(unittest.TestCase):
    def test_roundtrip_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            store = StateStore(path)
            self.assertTrue(store.plugin_enabled("alpha", "spy"))  # default
            store.set_plugin_enabled("alpha", "spy", False)
            store.set_limit_override("alpha", "message", "1/30s")
            reloaded = StateStore(path)
            self.assertFalse(reloaded.plugin_enabled("alpha", "spy"))
            self.assertTrue(reloaded.plugin_enabled("alpha", "other"))
            self.assertEqual(reloaded.limit_overrides(), {"alpha": {"message": "1/30s"}})

    def test_clear_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            store.set_limit_override("alpha", "message", "1/30s")
            store.set_limit_override("alpha", "dm", "2/30s")
            store.clear_limit_override("alpha", "message")
            self.assertEqual(store.limit_overrides(), {"alpha": {"dm": "2/30s"}})
            store.clear_limit_override("alpha", "dm")
            self.assertEqual(store.limit_overrides(), {})

    def test_corrupt_file_backs_up_and_starts_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            store = StateStore(path)
            self.assertEqual(store.limit_overrides(), {})
            self.assertTrue(path.with_suffix(".json.bak").exists())
            store.set_plugin_enabled("a", "p", True)  # writable again
            self.assertTrue(StateStore(path).plugin_enabled("a", "p"))

    def test_snapshot_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "s.json")
            store.set_plugin_enabled("a", "p", False)
            snap = store.snapshot()
            self.assertEqual(snap["plugin_states"], {"a": {"p": False}})
            json.dumps(snap)  # JSON-safe


class TestPersistenceIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_toggle_survives_new_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            first = Harness("tests/fixtures/plugins", ["alpha"], state=StateStore(state_path))
            api = ControlAPI(first.container, first.limiter, first.loader, first.credentials)
            await api.set_plugin_enabled("alpha", "spy", enabled=False)

            second = Harness("tests/fixtures/plugins", ["alpha"], state=StateStore(state_path))
            runtime = second.container._accounts["alpha"]
            self.assertFalse(runtime.plugins["spy"].enabled)

    async def test_limit_override_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            harness = Harness("tests/fixtures/plugins", ["alpha"])
            api = ControlAPI(
                harness.container, harness.limiter, harness.loader, harness.credentials,
                state=StateStore(state_path),
            )
            await api.set_limit("message", "2/30s", account="alpha")

            restored = StateStore(state_path)
            limiter = RateLimiterService(default_rules={"message": "8/12s"})
            for account, rules in restored.limit_overrides().items():
                for action, rule in rules.items():
                    limiter.set_account_rule(account, action, rule)
            self.assertEqual(limiter.rule_for("message", "alpha").actions, 2)
            self.assertEqual(limiter.rule_for("message", "beta").actions, 8)


if __name__ == "__main__":
    unittest.main()
