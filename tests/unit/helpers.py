"""Shared unit-test fixtures (any agent may extend — see docs/architecture/integration.md).

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
import atexit
import copy
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from adapters.base import DiscordMessage
from core.account_manager import AccountManager
from core.credentials import Credentials
from core.errors import DatabaseError, SchedulerError
from core.event_bus import EventBus
from database.models import AccountRecord

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable, Iterable

LOG = logging.getLogger("tests.helpers")


# ------------------------------------------------------------ repositories
class FakeAccountsRepo:
    """Dict-backed AccountsRepository with injectable failures (async)."""

    def __init__(self) -> None:
        self.records: dict[str, AccountRecord] = {}
        self.next_id = 1
        self.fail_on: str | None = None  # method name that raises

    def _maybe_fail(self, what: str) -> None:
        if self.fail_on == what:
            raise RuntimeError(f"injected repository failure: {what}")

    async def add(
        self,
        name: str,
        *,
        token_ref: str | None = None,
        enabled: bool = True,
        workspace: str | None = None,
    ) -> AccountRecord:
        self._maybe_fail("add")
        # parity with the real AccountsRepository (database/repositories/
        # accounts.py): empty/whitespace and duplicate names are
        # DatabaseErrors, not RuntimeErrors
        if not isinstance(name, str) or not name.strip():
            raise DatabaseError("account name must be a non-empty string")
        if name in self.records:
            raise DatabaseError(f"account {name!r} already exists")
        record = AccountRecord(
            id=self.next_id,
            name=name,
            token_ref=token_ref,
            enabled=enabled,
            workspace=workspace,
        )
        self.next_id += 1
        self.records[name] = record
        return self._copy(record)

    async def get(self, name: str) -> AccountRecord | None:
        self._maybe_fail("get")
        record = self.records.get(name)
        return self._copy(record) if record is not None else None

    async def list(self) -> builtins.list[AccountRecord]:
        return [self._copy(r) for _, r in sorted(self.records.items())]

    async def set_enabled(self, name: str, enabled: bool) -> None:
        self._maybe_fail("set_enabled")
        record = self.records.get(name)
        if record is None:
            raise RuntimeError(f"unknown account {name!r}")
        record.enabled = bool(enabled)

    async def set_settings(self, name: str, settings: dict[str, Any]) -> None:
        record = self.records.get(name)
        if record is None:
            raise RuntimeError(f"unknown account {name!r}")
        record.settings = dict(settings)

    async def delete(self, name: str) -> bool:
        self._maybe_fail("delete")
        return self.records.pop(name, None) is not None

    @staticmethod
    def _copy(record: AccountRecord) -> AccountRecord:
        return AccountRecord(**record.to_dict())


class FakeAccountPluginsRepo:
    """Dict-backed AccountPluginsRepository (async methods, like the real one
    in database/repositories/account_plugins.py).

    Mirrors the persisted contract: sorted/deduped lists, idempotent
    assign/unassign, replace-all set_plugins, and all_assignments() that
    OMITS accounts with zero plugins (consumers use .get(name, [])).
    Profile bindings follow the real v7 column: one OPTIONAL profile per
    assignment row (None = plugin defaults), wiped by replacement, never
    created without an assignment.
    """

    def __init__(self) -> None:
        self.rows: dict[str, list[str]] = {}
        #: (account, plugin) -> bound profile name or None
        self.profile_of: dict[tuple[str, str], str | None] = {}
        self.fail_on: str | None = None  # method name that raises

    def _maybe_fail(self, what: str) -> None:
        if self.fail_on == what:
            raise RuntimeError(f"injected repository failure: {what}")

    async def assign_plugin(self, account: str, plugin: str) -> None:
        self._maybe_fail("assign_plugin")
        plugins = set(self.rows.get(account, []))
        plugins.add(plugin)
        self.rows[account] = sorted(plugins)

    async def unassign_plugin(self, account: str, plugin: str) -> None:
        self._maybe_fail("unassign_plugin")
        plugins = set(self.rows.get(account, []))
        plugins.discard(plugin)
        self.rows[account] = sorted(plugins)
        self.profile_of.pop((account, plugin), None)

    async def plugins_of(self, account: str) -> list[str]:
        self._maybe_fail("plugins_of")
        return list(self.rows.get(account, []))

    async def set_plugins(self, account: str, plugins: Iterable[str]) -> list[str]:
        self._maybe_fail("set_plugins")
        cleaned = sorted(set(plugins or []))
        kept = set(cleaned)
        for plugin in list(self.rows.get(account, [])):
            if plugin not in kept:  # delete+insert drops the binding column
                self.profile_of.pop((account, plugin), None)
        self.rows[account] = cleaned
        return list(cleaned)

    async def all_assignments(self) -> dict[str, list[str]]:
        self._maybe_fail("all_assignments")
        return {a: list(ps) for a, ps in sorted(self.rows.items()) if ps}

    async def set_profile(self, account: str, plugin: str, profile: str | None) -> bool:
        self._maybe_fail("set_profile")
        if plugin not in set(self.rows.get(account, [])):
            return False  # no assignment row: binding never creates one
        self.profile_of[(account, plugin)] = profile
        return True

    async def bindings(self, account: str) -> dict[str, str | None]:
        self._maybe_fail("bindings")
        return {
            plugin: self.profile_of.get((account, plugin)) for plugin in self.rows.get(account, [])
        }

    async def all_bindings(self) -> dict[str, dict[str, str | None]]:
        self._maybe_fail("all_bindings")
        return {
            account: {plugin: self.profile_of.get((account, plugin)) for plugin in plugins}
            for account, plugins in sorted(self.rows.items())
            if plugins
        }


# ------------------------------------------------------------- collaborators
class FakePluginManager:
    """Records attach/detach/activate/deactivate; serves canned states().

    ``attach_account`` is async like the real plugin manager (it reads
    persisted enabled state); the account manager also tolerates a sync
    return for duck-typed doubles. ``known_names`` feeds the optional
    ``names()`` surface the ControlAPI validates plugin names against.
    """

    def __init__(self, known_names: list[str] | None = None) -> None:
        self.attached: list[Any] = []  # (account, allowed)
        self.detached: list[str] = []
        self.activated: list[Any] = []  # (account, services_factory)
        self.deactivated: list[str] = []
        self.states_map: dict[str, list[dict[str, Any]]] = {}
        self.fail_states = False
        self.known_names: list[str] = list(known_names or [])
        # cog-hosting surface (roadmap #4 reconcile tests): None / empty
        # until a test wires a real PluginCogHost + runtimes in
        self.cog_host: Any = None
        self._accounts: dict[str, dict[str, Any]] = {}

    async def host_cog(self, account: str, plugin: str, bot: Any, paced: Any = None) -> Any:
        """Cog-hosting seam (roadmap #4), mirroring the real manager:
        resolve COG from the runtimes view, host it when a cog_host is
        wired — None when there is nothing to host."""
        if self.cog_host is None:
            return None
        runtime = self._accounts.get(account, {}).get(plugin)
        cog_class = getattr(getattr(runtime, "instance", None), "COG", None)
        if cog_class is None:
            return None
        return await self.cog_host.attach(bot, account, plugin, cog_class, paced=paced)

    async def unhost_cog(self, account: str, plugin: str) -> bool:
        """The detach-side seam — delegates to the wired cog_host."""
        if self.cog_host is None:
            return False
        return bool(await self.cog_host.detach(account, plugin))

    def names(self) -> list[str]:
        """Discovered-plugin surface (ControlAPI typo trap)."""
        return list(self.known_names)

    async def attach_account(self, account: str, allowed: list[str]) -> None:
        self.attached.append((account, allowed))

    def detach_account(self, account: str) -> None:
        self.detached.append(account)

    async def activate_account(self, account: str, services_factory: Any) -> None:
        self.activated.append((account, services_factory))

    async def deactivate_account(self, account: str) -> None:
        self.deactivated.append(account)

    def states(self, account: str) -> list[dict[str, Any]]:
        if self.fail_states:
            raise RuntimeError("states() boom")
        return [dict(s) for s in self.states_map.get(account, [])]

    def live_settings(self, account: str, plugin: str) -> dict[str, Any] | None:
        for state in self.states_map.get(account, []):
            if state.get("name") == plugin:
                return state.get("settings") or {}
        return None


class FakeScheduler:
    """Records submits + cancel_matching calls; returns fake task objects."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self.next_id = 1
        self.fail_submit = False

    def submit(
        self,
        account: str,
        plugin: str,
        action: str,
        *,
        channel_id: int | None = None,
        user_id: int | None = None,
        content: str | None = None,
        message_id: int | None = None,
        priority: Any = None,
        name: str | None = None,
        timeout: float | None = None,
        retries: int = 0,
    ) -> SimpleNamespace:
        # ``message_id`` mirrors the real scheduler signature
        # (core/task_scheduler/scheduler.py submit) — reaction/button
        # sends pass it positionally-by-keyword and a fake without it
        # dies with a TypeError that blames the caller.
        if self.fail_submit:
            raise SchedulerError("scheduler is stopped")
        task = SimpleNamespace(
            id=self.next_id,
            name=name or f"{action}-{self.next_id}",
            account=account,
            plugin=plugin,
            action=action,
            channel_id=channel_id,
            user_id=user_id,
            content=content,
            message_id=message_id,
            priority=priority,
            status="queued",
            grant_wait_ms=12.0,
            grant_jitter_ms=5.0,
            # parity with core.task_scheduler.Task attributes consumed by
            # the account manager's send/audit paths
            attempts=0,
            cancelled=False,
            sender_ran=False,
        )
        task.is_terminal = task.status in ("completed", "failed", "cancelled", "timed_out")
        self.next_id += 1
        self.submitted.append(task)
        return task

    def cancel_matching(
        self, *, account: str | None = None, plugin: str | None = None, action: str | None = None
    ) -> int:
        self.cancel_calls.append({"account": account, "plugin": plugin, "action": action})
        return len(self.submitted)


class FakeSettings:
    """account_settings() with per-account overrides."""

    def __init__(self, **defaults: Any) -> None:
        self.defaults: dict[str, Any] = {
            "reconnect_enabled": True,
            "reconnect_initial_delay": 1.0,
            "reconnect_max_delay": 60.0,
            "reconnect_multiplier": 2.0,
        }
        self.defaults.update(defaults)
        self.overrides: dict[str, dict[str, Any]] = {}

    def account_settings(self, account: str) -> dict[str, Any]:
        merged = dict(self.defaults)
        merged.update(self.overrides.get(account, {}))
        return merged


class FakeAudit:
    """Captures every recorded interaction entry."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> dict[str, Any]:
        self.entries.append(kwargs)
        return kwargs


class FakeDetailsLimiter:
    """policies()/effective() shaped for AccountManager.details()."""

    def __init__(self) -> None:
        self.policies_list: list[dict[str, Any]] = []

    def policies(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self.policies_list]

    def effective(
        self, action: str, account: str | None = None, plugin: str | None = None
    ) -> dict[str, Any]:
        applicable = [
            p
            for p in self.policies_list
            if p["action"] == action and (p["level"] == "global" or p["owner"] == account)
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
def default_snapshot() -> dict[str, Any]:
    """Canned READY snapshot: 1 guild, 2 channels, 1 DM, user 555."""
    return {
        "available": True,
        "user": {"id": 555, "name": "AlphaSelf"},
        "latency_ms": 42.0,
        "guilds": [
            {
                "id": 10,
                "name": "Guildy",
                "channels": [
                    {"id": 100, "name": "general"},
                    {"id": 101, "name": "bot-spam"},
                ],
            }
        ],
        "dm_channels": [{"id": 9001, "recipient": 777}],
    }


class FakeClient:
    """In-memory DiscordClient: connect/send/open_dm/drop/receive."""

    def __init__(
        self,
        *,
        fail_connect: bool = False,
        snapshot: dict[str, Any] | None = None,
        user_id: int = 555,
        latency_ms: float = 42.0,
        fail_message: str = "login failed: invalid token",
    ) -> None:
        self.connected = False
        self.fail_connect = fail_connect
        self.fail_message = fail_message  # G8: default matches NONE of the
        # auth markers; pass an auth-shaped message to exercise the
        # INVALID_CREDENTIAL classification (see _AUTH_ERROR_MARKERS)
        self.user_id: int | None = None
        self._user_id = user_id
        self.latency_ms = latency_ms
        self.on_message: Callable[[DiscordMessage], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None
        self.sent: list[Any] = []  # (kind, channel_id, content)
        self.open_dm_calls: list[int] = []
        self.dm_channels: dict[int, int] = {}
        self._snapshot = snapshot if snapshot is not None else default_snapshot()
        self._next_msg_id = 1000

    async def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError(self.fail_message)
        self.connected = True
        self.user_id = self._user_id

    async def disconnect(self) -> None:
        self.connected = False  # deliberate stop: on_disconnect NOT fired

    def _message(self, channel_id: int, content: str, dm: bool) -> DiscordMessage:
        self._next_msg_id += 1
        return DiscordMessage(
            id=self._next_msg_id,
            channel_id=channel_id,
            author_id=self.user_id or 1,
            content=content,
            dm=dm,
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

    def cached_snapshot(self) -> dict[str, Any]:
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

    def __init__(self, **client_kwargs: Any) -> None:
        self.clients: list[FakeClient] = []
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
        self.events: list[Any] = []
        self._unsubs: list[Any] = []

    def subscribe(self, *event_types: type[Any]) -> None:
        for event_type in event_types:
            self._unsubs.append(self.bus.subscribe(event_type, self._append))

    def _append(self, event: Any) -> None:
        self.events.append(event)

    def of(self, event_type: type[Any]) -> list[Any]:
        return [e for e in self.events if isinstance(e, event_type)]

    async def wait_for(
        self, event_type: type[Any], count: int = 1, timeout: float = 2.0
    ) -> list[Any]:
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
    plugins: FakePluginManager
    scheduler: FakeScheduler
    settings: FakeSettings
    audit: FakeAudit
    factory: FakeClientFactory
    credentials: Credentials
    account_plugins: FakeAccountPluginsRepo
    collector: EventCollector = field(default_factory=lambda: EventCollector(EventBus()))


#: Disposable per-process home for harness credential keys. Never a fixed
#: shared path: the previous /tmp/opencode/... literal was a Linux-session
#: artifact — unresolvable on Windows and shared across runs everywhere,
#: against the disposable-storage rule. One dir per process keeps xdist
#: workers isolated; removed at interpreter exit.
_CRED_KEY_DIR = Path(tempfile.mkdtemp(prefix="lz-test-cred-"))
atexit.register(shutil.rmtree, _CRED_KEY_DIR, ignore_errors=True)


def make_manager(
    *,
    token_values: dict[str, str] | None = None,
    settings: FakeSettings | None = None,
    limiter: Any = None,
    client_kwargs: dict[str, Any] | None = None,
    supervisor_tick: float = 0.02,
    account_plugins: FakeAccountPluginsRepo | None = None,
) -> ManagerHarness:
    """Build an AccountManager wired to fakes (fresh bus/repos per call)."""
    bus = EventBus()
    repo = FakeAccountsRepo()
    plugins = FakePluginManager()
    scheduler = FakeScheduler()
    settings = settings or FakeSettings()
    # AccountManager annotates this collaborator as SettingsManager but
    # only duck-types it (like every other collaborator); the fake here
    # implements exactly that surface, so the boundary stays dynamic.
    settings_for_manager: Any = settings
    audit = FakeAudit()
    factory = FakeClientFactory(**(client_kwargs or {}))
    credentials = Credentials.from_values(
        token_values
        if token_values is not None
        else {
            "DISCORD_TOKEN_ALPHA": "tok-alpha",
            "DISCORD_TOKEN_BETA": "tok-beta",
            "DISCORD_TOKEN_GAMMA": "tok-gamma",
        }
    )
    # Wire a CredentialService backed by an in-memory store so the
    # database-only resolution works in tests (no .env fallback exists)
    from core.credential_service import CredentialService

    class _MemCredRepo:
        def __init__(self) -> None:
            self.rows: dict[str, str] = {}

        async def store(self, account: str, ciphertext: str) -> None:
            self.rows[account] = ciphertext

        async def load(self, account: str) -> str | None:
            return self.rows.get(account)

        async def delete(self, account: str) -> bool:
            return self.rows.pop(account, None) is not None

        async def accounts_with_credential(self) -> list[str]:
            return sorted(self.rows)

    cred_repo = _MemCredRepo()
    credential_service = CredentialService(cred_repo, key_path=_CRED_KEY_DIR / "test-cred.key")
    seed = (
        token_values
        if token_values is not None
        else {
            "DISCORD_TOKEN_ALPHA": "tok-alpha",
            "DISCORD_TOKEN_BETA": "tok-beta",
            "DISCORD_TOKEN_GAMMA": "tok-gamma",
        }
    )
    for acct, tok in seed.items():
        name = acct.replace("DISCORD_TOKEN_", "").lower()
        credential_service._memory[name] = tok
        credential_service._db_accounts.add(name)
        credential_service._runtime_sources[name] = "db"

    manager = AccountManager(
        bus,
        LOG,
        repo,
        plugins,
        scheduler,
        settings_for_manager,
        audit,
        factory,
        credentials,
        limiter=limiter,
        supervisor_tick=supervisor_tick,
        credential_service=credential_service,
        account_plugins=account_plugins
        if account_plugins is not None
        else FakeAccountPluginsRepo(),
    )
    collector = EventCollector(bus)
    from core.event_bus import (
        AccountConnected,
        AccountDisconnected,
        AccountFailed,
        MessageReceived,
    )

    collector.subscribe(
        AccountConnected,
        AccountDisconnected,
        AccountFailed,
        MessageReceived,
    )
    return ManagerHarness(
        manager=manager,
        bus=bus,
        repo=repo,
        plugins=plugins,
        scheduler=scheduler,
        settings=settings,
        audit=audit,
        factory=factory,
        credentials=credentials,
        collector=collector,
        account_plugins=manager._account_plugins,
    )


def make_task(**overrides: Any) -> SimpleNamespace:
    """A scheduler-task lookalike for send_hook tests."""
    task = SimpleNamespace(
        id=1,
        name="t",
        account="alpha",
        plugin="exporter",
        action="message",
        channel_id=100,
        user_id=None,
        content="hi",
        grant_wait_ms=12.0,
        grant_jitter_ms=5.0,
    )
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


__all__ = [
    "EventCollector",
    "FakeAccountPluginsRepo",
    "FakeAccountsRepo",
    "FakeAudit",
    "FakeClient",
    "FakeClientFactory",
    "FakeDetailsLimiter",
    "FakePluginManager",
    "FakeScheduler",
    "FakeSettings",
    "ManagerHarness",
    "default_snapshot",
    "make_manager",
    "make_task",
]


def fake_discord_sysmodules(lib_cls: Any) -> dict[str, Any]:
    """The fake discord world for sys.modules patching — Bot included.

    The adapter ALWAYS builds ext.commands.Bot now (the construction
    fork is gone), so every stub world needs a minimal
    ``discord.ext.commands``: a Bot stand-in riding the lib-client stub
    (READY/park lifecycle inherited) plus the cog/check/before_invoke
    seams the command layer installs, the prefix callables the dynamic
    resolver reads, and the error types on_command_error filters on.
    All three keys must ride the SAME patch.dict — from-imports resolve
    through sys.modules and patch.dict restores the whole set.
    """
    import types

    def _make_bot_stand_in(base: Any) -> Any:
        class BotStandIn(base):  # type: ignore[misc]  # per-test Any base
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.cogs: dict[str, Any] = {}
                self._checks: list[Any] = []

            def add_check(self, func: Any) -> None:
                self._checks.append(func)

            _before_invoke: Any = None

            def before_invoke(self, coro: Any) -> Any:
                self._before_invoke = coro
                return coro

            async def add_cog(self, cog: Any) -> None:
                self.cogs[type(cog).__name__] = cog

            async def remove_cog(self, name: str) -> None:
                self.cogs.pop(name, None)

            async def process_commands(self, message: Any) -> None:
                return None

            async def get_context(self, message: Any) -> Any:
                # the adapter's command-traffic check: context with no
                # command means ordinary traffic (the DTO path continues)
                return SimpleNamespace(command=None)

            async def invoke(self, ctx: Any) -> None:
                return None

        return BotStandIn

    commands_mod = types.ModuleType("discord.ext.commands")
    commands_mod.Bot = _make_bot_stand_in(lib_cls)  # type: ignore[attr-defined]
    commands_mod.when_mentioned = (  # type: ignore[attr-defined]
        lambda bot, message: []
    )
    commands_mod.when_mentioned_or = (  # type: ignore[attr-defined]
        lambda *prefixes: lambda bot, message: []
    )
    commands_mod.CommandNotFound = (  # type: ignore[attr-defined]
        type("CommandNotFound", (Exception,), {})
    )
    commands_mod.CheckFailure = (  # type: ignore[attr-defined]
        type("CheckFailure", (Exception,), {})
    )
    ext_mod = types.ModuleType("discord.ext")
    ext_mod.commands = commands_mod  # type: ignore[attr-defined]
    discord_mod = types.ModuleType("discord")
    discord_mod.Client = lib_cls  # type: ignore[attr-defined]
    discord_mod.DMChannel = type("DMChannel", (), {})  # type: ignore[attr-defined]
    return {
        "discord": discord_mod,
        "discord.ext": ext_mod,
        "discord.ext.commands": commands_mod,
    }
