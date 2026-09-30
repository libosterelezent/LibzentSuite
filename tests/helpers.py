"""Shared test fixtures: deterministic clock, fake world, container builder."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.account import AccountConfig, ReconnectPolicy
from core.container import AccountContainer  # noqa: E402
from core.credentials import Credentials  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.plugin_loader import PluginLoader  # noqa: E402
from core.rate_limiter import ManualClock, RateLimiterService  # noqa: E402
from core.settings import SettingsStore  # noqa: E402
from core.simulator import FakeClient, FakeWorld  # noqa: E402

TEST_CHANNEL = 500
FIXTURE_MUDAE_ID = 999_999
FIXTURE_OWNER_ID = 1001
#: supervisors off by default so tests that never stop accounts stay clean
_NO_RECONNECT = ReconnectPolicy(enabled=False)


class Harness:
    """Bundle of a container wired over a FakeWorld, for container-level tests."""

    def __init__(
        self,
        plugin_dir: str,
        accounts: List[str],
        global_settings: Optional[Dict] = None,
        rate_rules: Optional[Dict[str, str]] = None,
        mudae_id: int = FIXTURE_MUDAE_ID,
        owner_id: int = FIXTURE_OWNER_ID,
        reconnect_policy: Optional = None,
        state: Optional = None,
        roles: Optional = None,
        audit: Optional = None,
    ) -> None:
        self.mudae_id = mudae_id
        self.owner_id = owner_id
        self.credentials = Credentials.from_values(
            {
                "MUDAE_BOT_ID": str(mudae_id),
                "OWNER_USER_IDS": str(owner_id),
                **{f"DISCORD_TOKEN_{name.upper()}": f"sim-token-{name}" for name in accounts},
            }
        )
        self.world = FakeWorld(mudae_user_id=mudae_id)
        self.world.add_user(owner_id, "owner")
        self.world.add_user(mudae_id, "mudae")
        self.user_ids = {name: 2000 + i for i, name in enumerate(accounts)}
        for name, uid in self.user_ids.items():
            self.world.add_user(uid, name)
        self.world.add_channel(TEST_CHANNEL, "test-channel")
        self.limiter = RateLimiterService(
            default_rules=rate_rules or {"message": "2000/0.05s", "dm": "2000/0.05s"}
        )
        loader = PluginLoader(ROOT / plugin_dir)
        loader.discover()
        self.loader = loader
        self.container = AccountContainer(
            credentials=self.credentials,
            settings=SettingsStore(global_settings=global_settings or {}),
            limiter=self.limiter,
            loader=loader,
            bus=EventBus(),
            client_factory=lambda cfg, _creds: FakeClient(self.world, self.user_ids[cfg.name]),
            reconnect_policy=reconnect_policy if reconnect_policy is not None else _NO_RECONNECT,
            state=state,
            roles=roles,
            audit=audit,
        )
        for name in accounts:
            self.container.add_account(AccountConfig(name=name, sync_group="grp"))

    def session(self, name: str):
        return self.container._accounts[name].session  # noqa: SLF001

    def fixture_module(self, name: str):
        """Live module object of a loaded fixture plugin (single source of truth).

        The loader imports plugins under synthetic module names, so tests MUST
        read fixture state (CALLS/EVENTS) through this accessor instead of
        importing the fixture file directly (which would create a second,
        disconnected copy of the module).
        """
        import sys

        cls = self.loader.record(name).cls
        return sys.modules[cls.__module__]


def fixture_path(relative: str) -> Path:
    """Absolute path under tests/fixtures."""
    return Path(__file__).resolve().parent / relative


__all__ = [
    "Harness",
    "ManualClock",
    "TEST_CHANNEL",
    "FIXTURE_MUDAE_ID",
    "FIXTURE_OWNER_ID",
    "fixture_path",
    "ROOT",
]
