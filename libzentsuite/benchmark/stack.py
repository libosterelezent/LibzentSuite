"""TemporaryWorkspace + BenchStack: isolated stacks for benchmark scenarios.

NEVER touches state/libzent.db or state/credentials.key: every run gets
a tempfile workspace; the bootstrap's ``credential_key_path`` kwarg keeps
a concurrent live process safe. Fakes/simulation only — no real Discord.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any

from libzentsuite.core.bootstrap import build
from libzentsuite.core.credentials import Credentials

if TYPE_CHECKING:
    from libzentsuite.benchmark.collector import PerfCollector


class TemporaryWorkspace:
    """Context manager: fresh state dir (db, credential key, config dir)."""

    def __enter__(self) -> TemporaryWorkspace:
        self._tmp = tempfile.TemporaryDirectory(prefix="sm-bench-")
        self.root = Path(self._tmp.name)
        self.db_path = str(self.root / "bench.db")
        self.cred_key_path = self.root / "credentials.key"
        self.config_dir = str(self.root / "config")
        Path(self.config_dir).mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._tmp.cleanup()  # removes WAL/-shm residue with the directory


class BenchFakeClient:
    """Minimal DiscordClient double: connects instantly, records sends."""

    def __init__(self, *, fail_connect: bool = False) -> None:
        self.connected = False
        self.fail_connect = fail_connect
        self.user_id: int | None = 111
        self.on_message = None
        self.on_disconnect = None
        self.sent: list[Any] = []
        self._next_id = 1000

    async def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError("login failed (bench)")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send_message(self, channel_id: int, content: str) -> NS:
        self.sent.append((channel_id, content))
        self._next_id += 1
        return NS(id=self._next_id)

    async def send_dm(self, user_id: int, content: str) -> NS:
        return await self.send_message(9000 + user_id, content)

    async def open_dm(self, user_id: int) -> int:
        return 9000 + user_id

    async def fetch_user(self, user_id: int) -> NS:
        return NS(id=user_id)

    def cached_snapshot(self) -> dict[str, Any]:
        return {
            "user": {"id": self.user_id},
            "guilds": [
                {
                    "id": 10,
                    "name": "bench",
                    "channels": [{"id": 100, "name": "grind", "dm": False}],
                }
            ],
            "dm_channels": [],
        }


class BenchClientFactory:
    """ClientFactory protocol double — records every client (leak counter)."""

    def __init__(self) -> None:
        self.clients: list[BenchFakeClient] = []

    def __call__(self, name: str, token: str) -> BenchFakeClient:
        client = BenchFakeClient()
        client.token = token  # type: ignore[attr-defined]
        self.clients.append(client)
        return client


class BenchStack:
    """A real-bootstrap framework over a temp workspace + fake clients."""

    def __init__(
        self, framework: Any, factory: BenchClientFactory, workspace: TemporaryWorkspace
    ) -> None:
        self.framework = framework
        self.factory = factory
        self.workspace = workspace

    @classmethod
    async def build(
        cls, ws: TemporaryWorkspace, *, perf: PerfCollector | None = None
    ) -> BenchStack:
        factory = BenchClientFactory()
        if perf is not None:
            with perf.stage("bootstrap"):
                framework = await build(
                    env_file=".env",  # read-only config read; state stays in ws
                    config_dir=ws.config_dir,
                    db_path=ws.db_path,
                    credential_key_path=ws.cred_key_path,
                    client_factory=factory,
                    credentials=Credentials.from_values({}),
                    quiet=True,
                )
        else:
            framework = await build(
                env_file=".env",
                config_dir=ws.config_dir,
                db_path=ws.db_path,
                credential_key_path=ws.cred_key_path,
                client_factory=factory,
                credentials=Credentials.from_values({}),
                quiet=True,
            )
        return cls(framework, factory, ws)

    async def add_account(self, name: str, token: str = "bench-tok") -> None:
        await self.framework.api.accounts_add({"name": name})
        await self.framework.credential_service.store(name, token)
        # make resolution instant for lifecycle benchmarks (no decrypt)
        self.framework.credential_service._memory[name] = token

    async def shutdown(self, *, perf: PerfCollector | None = None) -> None:
        if perf is not None:
            with perf.stage("shutdown"):
                await self.framework.shutdown()
        else:
            await self.framework.shutdown()
