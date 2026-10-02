"""Shared unit-test fixtures (any agent may extend — see INTEGRATION.md).

Currently provides the A6 account-manager test doubles:

  * dict-backed fake repositories (pinned repository method names),
  * a fake plugin manager / scheduler / settings / audit / limiter,
  * in-memory Discord clients (FakeClient) + factory + canned snapshots,
  * an event collector and a one-call manager harness.

They implement EXACTLY the duck-typed surfaces pinned in INTEGRATION.md, so
tests never import the parallel-built real packages.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from adapters.base import DiscordMessage
from core.account_manager import AccountManager
from core.credentials import Credentials
from core.errors import SchedulerError
from core.event_bus import EventBus
from core.profile_manager import ProfileManager
from database.models import AccountRecord, ProfileRecord

LOG = logging.getLogger("tests.helpers")


# ------------------------------------------------------------ repositories
class FakeProfilesRepo:
    """Dict-backed ProfilesRepository (async methods, like the real one)."""

    def __init__(self) -> None:
        self.data: Dict[str, ProfileRecord] = {}

    async def upsert(self, name, plugins, description="") -> None:
        self.data[name] = ProfileRecord(
            name=name, plugins=sorted(set(plugins)), description=description
        )

    async def delete(self, name) -> bool:
        existed = name in self.data
        self.data.pop(name, None)
        return existed

    async def get(self, name) -> Optional[ProfileRecord]:
        return self.data.get(name)

    async def list(self) -> List[ProfileRecord]:
        return [self.data[k] for k in sorted(self.data)]


class FakeAccountsRepo:
    """Dict-backed AccountsRepository with injectable failures (async)."""

    def __init__(self) -> None:
        self.records: Dict[str, AccountRecord] = {}
        self.next_id = 1
        self.fail_on: Optional[str] = None  # method name that raises

    def _maybe_fail(self, what: str) -> None:
        if self.fail_on == what:
            raise RuntimeError(f"injected repository failure: {what}")

    async def add(self, name, *, token_ref=None, enabled=True, sync_group=None):
        self._maybe_fail("add")
        record = AccountRecord(
            id=self.next_id, name=name, token_ref=token_ref,
            enabled=enabled, sync_group=sync_group,
        )
        self.next_id += 1
        self.records[name] = record
        return self._copy(record)

    async def get(self, name) -> Optional[AccountRecord]:
        self._maybe_fail("get")
        record = self.records.get(name)
        return self._copy(record) if record is not None else None

    async def list(self) -> List[AccountRecord]:
        return [self._copy(r) for _, r in sorted(self.records.items())]

    async def set_enabled(self, name, enabled) -> None:
        self._maybe_fail("set_enabled")
        record = self.records.get(name)
        if record is None:
            raise RuntimeError(f"unknown account {name!r}")
        record.enabled = bool(enabled)

    async def set_settings(self, name, settings: Dict[str, Any]) -> None:
        record = self.records.get(name)
        if record is None:
            raise RuntimeError(f"unknown account {name!r}")
        record.settings = dict(settings)

    async def delete(self, name) -> bool:
        self._maybe_fail("delete")
        return self.records.pop(name, None) is not None

    async def assign_profile(self, name, profile) -> None:
        record = self.records.get(name)
        if record is None:
            raise RuntimeError(f"unknown account {name!r}")
        if profile not in record.profiles:
            record.profiles.append(profile)

    async def unassign_profile(self, name, profile) -> None:
        record = self.records.get(name)
        if record is not None and profile in record.profiles:
            record.profiles.remove(profile)

    async def profiles_of(self, name) -> List[str]:
        record = self.records.get(name)
        return sorted(record.profiles) if record is not None else []

    @staticmethod
    def _copy(record: AccountRecord) -> AccountRecord:
        return AccountRecord(**record.to_dict())


# ------------------------------------------------------------- collaborators
class FakePluginManager:
    """Records attach/detach/activate/deactivate; serves canned states().

    ``attach_account`` is async like the real plugin manager (it reads
    persisted enabled state); the account manager also tolerates a sync
    return for duck-typed doubles.
    """

    def __init__(self) -> None:
        self.attached: List[Any] = []       # (account, allowed)
        self.detached: List[str] = []
        self.activated: List[Any] = []      # (account, services_factory)
        self.deactivated: List[str] = []
        self.states_map: Dict[str, List[Dict[str, Any]]] = {}
        self.fail_states = False

    async def attach_account(self, account, allowed) -> None:
        self.attached.append((account, allowed))

    def detach_account(self, account) -> None:
        self.detached.append(account)

    async def activate_account(self, account, services_factory) -> None:
        self.activated.append((account, services_factory))

    async def deactivate_account(self, account) -> None:
        self.deactivated.append(account)

    def states(self, account) -> List[Dict[str, Any]]:
        if self.fail_states:
            raise RuntimeError("states() boom")
        return [dict(s) for s in self.states_map.get(account, [])]

    def live_settings(self, account, plugin):
        for state in self.states_map.get(account, []):
            if state.get("name") == plugin:
                return state.get("settings") or {}
        return None


class FakeScheduler:
    """Records submits + cancel_matching calls; returns fake task objects."""

    def __init__(self) -> None:
        self.submitted: List[Any] = []
        self.cancel_calls: List[Dict[str, Any]] = []
        self.next_id = 1
        self.fail_submit = False

    def submit(self, account, plugin, action, *, channel_id=None, user_id=None,
               content=None, priority=None, name=None, timeout=None, retries=0):
        if self.fail_submit:
            raise SchedulerError("scheduler is stopped")
        task = SimpleNamespace(
            id=self.next_id, name=name or f"{action}-{self.next_id}",
            account=account, plugin=plugin, action=action,
            channel_id=channel_id, user_id=user_id, content=content,
            priority=priority, status="queued",
            grant_wait_ms=12.0, grant_jitter_ms=5.0,
        )
        self.next_id += 1
        self.submitted.append(task)
        return task

    def cancel_matching(self, *, account=None, plugin=None, action=None) -> int:
        self.cancel_calls.append(
            {"account": account, "plugin": plugin, "action": action}
        )
        return len(self.submitted)


class FakeSettings:
    """account_settings() with per-account overrides."""

    def __init__(self, **defaults) -> None:
        self.defaults: Dict[str, Any] = {
            "reconnect_enabled": True,
            "reconnect_initial_delay": 1.0,
            "reconnect_max_delay": 60.0,
            "reconnect_multiplier": 2.0,
        }
        self.defaults.update(defaults)
        self.overrides: Dict[str, Dict[str, Any]] = {}

    def account_settings(self, account) -> Dict[str, Any]:
        merged = dict(self.defaults)
        merged.update(self.overrides.get(account, {}))
        return merged


class FakeAudit:
    """Captures every recorded interaction entry."""

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []

    def record(self, **kwargs) -> Dict[str, Any]:
        self.entries.append(kwargs)
        return kwargs


class FakeDetailsLimiter:
    """policies()/effective() shaped for AccountManager.details()."""

    def __init__(self) -> None:
        self.policies_list: List[Dict[str, Any]] = []

    def policies(self) -> List[Dict[str, Any]]:
        return [dict(p) for p in self.policies_list]

    def effective(self, action, account=None, plugin=None) -> Dict[str, Any]:
        applicable = [
            p for p in self.policies_list
            if p["action"] == action
            and (p["level"] == "global" or p["owner"] == account)
        ]
        sources = [
            {"level": p["level"], "owner": p["owner"], "interval": p["interval"]}
            for p in applicable
        ]
        winner = max(applicable, key=lambda p: p["interval"], default=None)
        return {
            "action": action,
            "interval": winner["interval"] if winner else 0.0,
            "jitter": list(winner["jitter"]) if winner else [0.0, 0.0],
            "sources": sources,
        }


# ------------------------------------------------------------------ clients
def default_snapshot() -> Dict[str, Any]:
    """Canned READY snapshot: 1 guild, 2 channels, 1 DM, user 555."""
    return {
        "available": True,
        "user": {"id": 555, "name": "AlphaSelf"},
        "latency_ms": 42.0,
        "guilds": [{
            "id": 10, "name": "Guildy",
            "channels": [
                {"id": 100, "name": "general"},
                {"id": 101, "name": "bot-spam"},
            ],
        }],
        "dm_channels": [{"id": 9001, "recipient": 777}],
    }


class FakeClient:
    """In-memory DiscordClient: connect/send/open_dm/drop/receive."""

    def __init__(self, *, fail_connect: bool = False, snapshot=None,
                 user_id: int = 555, latency_ms: float = 42.0) -> None:
        self.connected = False
        self.fail_connect = fail_connect
        self.user_id: Optional[int] = None
        self._user_id = user_id
        self.latency_ms = latency_ms
        self.on_message = None
        self.on_disconnect = None
        self.sent: List[Any] = []          # (kind, channel_id, content)
        self.open_dm_calls: List[int] = []
        self.dm_channels: Dict[int, int] = {}
        self._snapshot = snapshot if snapshot is not None else default_snapshot()
        self._next_msg_id = 1000

    async def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError("login failed: invalid token")
        self.connected = True
        self.user_id = self._user_id

    async def disconnect(self) -> None:
        self.connected = False  # deliberate stop: on_disconnect NOT fired

    def _message(self, channel_id: int, content: str, dm: bool) -> DiscordMessage:
        self._next_msg_id += 1
        return DiscordMessage(
            id=self._next_msg_id, channel_id=channel_id,
            author_id=self.user_id or 1, content=content, dm=dm,
        )

    async def send_message(self, channel_id: int, content: str) -> DiscordMessage:
        self.sent.append(("message", int(channel_id), content))
        # DM sends arrive through send_message with an already-resolved DM
        # channel id (unified path): mark dm when the id is a known DM channel
        dm = int(channel_id) in set(self.dm_channels.values()) or int(channel_id) >= 9000
        return self._message(int(channel_id), content, dm=dm)

    async def send_dm(self, user_id: int, content: str) -> DiscordMessage:
        channel_id = self.dm_channels.get(int(user_id), 9000 + int(user_id))
        self.sent.append(("dm", channel_id, content))
        return self._message(channel_id, content, dm=True)

    async def open_dm(self, user_id: int) -> int:
        self.open_dm_calls.append(int(user_id))
        channel_id = 9000 + int(user_id)
        self.dm_channels[int(user_id)] = channel_id
        return channel_id

    async def fetch_user(self, user_id: int) -> Any:
        return {"id": int(user_id)}

    def cached_snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self._snapshot)

    # test helpers ---------------------------------------------------------
    def drop(self, reason: str = "connection lost") -> None:
        """Simulate an unexpected disconnect (fires on_disconnect)."""
        self.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(reason)

    def receive(self, message: DiscordMessage) -> None:
        """Simulate an inbound message (fires on_message -> bus)."""
        if self.on_message is not None:
            self.on_message(message)


class FakeClientFactory:
    """Builds FakeClients and records every client it handed out."""

    def __init__(self, **client_kwargs) -> None:
        self.clients: List[FakeClient] = []
        self.client_kwargs = client_kwargs

    def __call__(self, name: str, token: str) -> FakeClient:
        client = FakeClient(**self.client_kwargs)
        client.token = token  # type: ignore[attr-defined]
        self.clients.append(client)
        return client


# ------------------------------------------------------------------ harness
class EventCollector:
    """Bus subscription helper with a bounded wait-for-N assertions API."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.events: List[Any] = []
        self._unsubs: List[Any] = []

    def subscribe(self, *event_types) -> None:
        for event_type in event_types:
            self._unsubs.append(self.bus.subscribe(event_type, self._append))

    def _append(self, event: Any) -> None:
        self.events.append(event)

    def of(self, event_type) -> List[Any]:
        return [e for e in self.events if isinstance(e, event_type)]

    async def wait_for(self, event_type, count: int = 1,
                       timeout: float = 2.0) -> List[Any]:
        """Poll until `count` events of the type arrived (bounded)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.of(event_type)) < count:
            if loop.time() > deadline:
                raise AssertionError(
                    f"timeout waiting for {event_type.__name__} x{count}; "
                    f"saw {[type(e).__name__ for e in self.events]}"
                )
            await asyncio.sleep(0.01)
        return self.of(event_type)

    def close(self) -> None:
        for unsub in self._unsubs:
            unsub()


@dataclass
class ManagerHarness:
    """Everything a manager test needs, pre-wired."""

    manager: AccountManager
    bus: EventBus
    repo: FakeAccountsRepo
    profiles_repo: FakeProfilesRepo
    profile_manager: ProfileManager
    plugins: FakePluginManager
    scheduler: FakeScheduler
    settings: FakeSettings
    audit: FakeAudit
    factory: FakeClientFactory
    credentials: Credentials
    collector: EventCollector = field(default_factory=lambda: EventCollector(EventBus()))

    def add_profile(self, name: str, *plugin_names: str) -> Any:
        """Create a profile through the async profile manager (AWAIT this)."""
        return self.profile_manager.set_profile(name, list(plugin_names))


def make_manager(
    *,
    token_values: Optional[Dict[str, str]] = None,
    settings: Optional[FakeSettings] = None,
    limiter: Any = None,
    client_kwargs: Optional[Dict[str, Any]] = None,
    supervisor_tick: float = 0.02,
) -> ManagerHarness:
    """Build an AccountManager wired to fakes (fresh bus/repos per call)."""
    bus = EventBus()
    repo = FakeAccountsRepo()
    profiles_repo = FakeProfilesRepo()
    profile_manager = ProfileManager(profiles_repo, LOG, repo)
    plugins = FakePluginManager()
    scheduler = FakeScheduler()
    settings = settings or FakeSettings()
    audit = FakeAudit()
    factory = FakeClientFactory(**(client_kwargs or {}))
    credentials = Credentials.from_values(
        token_values if token_values is not None else {
            "DISCORD_TOKEN_ALPHA": "tok-alpha",
            "DISCORD_TOKEN_BETA": "tok-beta",
            "DISCORD_TOKEN_GAMMA": "tok-gamma",
        }
    )
    # Wire a CredentialService backed by an in-memory store so the
    # database-only resolution works in tests (no .env fallback exists)
    from core.credential_service import CredentialService

    class _MemCredRepo:
        def __init__(self):
            self.rows = {}

        async def store(self, account, ciphertext):
            self.rows[account] = ciphertext

        async def load(self, account):
            return self.rows.get(account)

        async def delete(self, account):
            return self.rows.pop(account, None) is not None

        async def accounts_with_credential(self):
            return sorted(self.rows)

    cred_repo = _MemCredRepo()
    credential_service = CredentialService(
        cred_repo, key_path="/tmp/opencode/test-cred.key")
    seed = token_values if token_values is not None else {
        "DISCORD_TOKEN_ALPHA": "tok-alpha",
        "DISCORD_TOKEN_BETA": "tok-beta",
        "DISCORD_TOKEN_GAMMA": "tok-gamma",
    }
    for acct, tok in seed.items():
        name = acct.replace("DISCORD_TOKEN_", "").lower()
        credential_service._memory[name] = tok
        credential_service._db_accounts.add(name)
        credential_service._runtime_sources[name] = "db"

    manager = AccountManager(
        bus, LOG, repo, plugins, profile_manager, scheduler, settings, audit,
        factory, credentials, limiter=limiter, supervisor_tick=supervisor_tick,
        credential_service=credential_service,
    )
    collector = EventCollector(bus)
    from core.event_bus import (
        AccountConnected, AccountDisconnected, AccountFailed, MessageReceived,
    )
    collector.subscribe(
        AccountConnected, AccountDisconnected, AccountFailed, MessageReceived,
    )
    return ManagerHarness(
        manager=manager, bus=bus, repo=repo, profiles_repo=profiles_repo,
        profile_manager=profile_manager, plugins=plugins, scheduler=scheduler,
        settings=settings, audit=audit, factory=factory,
        credentials=credentials, collector=collector,
    )


def make_task(**overrides: Any) -> SimpleNamespace:
    """A scheduler-task lookalike for send_hook tests."""
    task = SimpleNamespace(
        id=1, name="t", account="alpha", plugin="soulcopy", action="message",
        channel_id=100, user_id=None, content="hi",
        grant_wait_ms=12.0, grant_jitter_ms=5.0,
    )
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


__all__ = [
    "FakeProfilesRepo", "FakeAccountsRepo", "FakePluginManager",
    "FakeScheduler", "FakeSettings", "FakeAudit", "FakeDetailsLimiter",
    "FakeClient", "FakeClientFactory", "EventCollector", "ManagerHarness",
    "make_manager", "make_task", "default_snapshot",
]
