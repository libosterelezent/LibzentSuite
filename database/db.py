"""Async SQLite persistence service (aiosqlite) — the app's single DB owner.

Lifecycle (spec §6): the constructor does NO I/O; ``await connect()``
opens the one long-lived aiosqlite connection, applies pragmas and runs
pending migrations; ``await close()`` shuts it down. Repositories are
stateless views over that connection (see database/connection.py for the
pragma rationale and the honest aiosqlite limits).

Migration strategy: ordered ``database.schema.MIGRATIONS`` applied inside
the write lock, each recorded in ``schema_migrations`` — repeatable
(already-applied versions are skipped), transactional per migration.

Secrets policy (unchanged, now stronger): the database stores Fernet
CIPHERTEXT for credentials (key outside the DB); it never stores
plaintext tokens.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from .connection import DatabaseConnection, DatabaseError  # noqa: F401 re-export
from .schema import MIGRATIONS, latest_version

LOG = logging.getLogger("slaughtermudae.database")

from .repositories.accounts import AccountsRepository
from .repositories.cache import CacheRepository
from .repositories.cooldowns import CooldownRepository
from .repositories.credentials import CredentialsRepository
from .repositories.kv import KvRepository
from .repositories.logs import AuditRepository, LogRepository
from .repositories.plugins import PluginRepository
from .repositories.profiles import ProfilesRepository
from .repositories.rate_policies import RatePolicyRepository
from .repositories.settings import AccountSettingsRepository
from .repositories.tasks import TaskHistoryRepository


class Database:
    """Async persistence service owning ONE aiosqlite connection."""

    def __init__(self, path: str | Path = "state/slaughter.db", *,
                 audit_keep: int = 10_000, log_keep: int = 10_000) -> None:
        self._path = str(path)
        self._audit_keep = int(audit_keep)
        self._log_keep = int(log_keep)
        self.connection = DatabaseConnection(self._path)
        # stateless repositories, sharing the owned connection
        self.accounts = AccountsRepository(self)
        self.account_settings = AccountSettingsRepository(self)
        self.profiles = ProfilesRepository(self)
        self.plugins = PluginRepository(self)
        self.policies = RatePolicyRepository(self)
        self.audit = AuditRepository(self, keep=self._audit_keep)
        self.logs = LogRepository(self, keep=self._log_keep)
        self.tasks = TaskHistoryRepository(self, keep=10_000)
        self.kv = KvRepository(self)
        self.cache = CacheRepository(self)
        self.credentials = CredentialsRepository(self)
        self.cooldowns = CooldownRepository(self)

    @property
    def path(self) -> str:
        return self._path

    async def connect(self) -> int:
        """Open the connection, apply pragmas, run pending migrations.

        Returns how many migrations were applied (0 when current).
        The file is created 0600 up front (connection layer); the chmod
        here also tightens a pre-existing, looser file (best-effort).
        """
        await self.connection.connect()
        if self._path != ":memory:":
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        try:
            return await self.migrate()
        except BaseException:
            await self.connection.close()  # never leak the worker thread
            raise

    async def migrate(self) -> int:
        """Apply pending migrations in order (idempotent, recorded)."""
        conn = self.connection
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at REAL NOT NULL)")
        done = {row["version"] for row in await conn.fetch_all(
            "SELECT version FROM schema_migrations")}
        if done and max(done) > latest_version():
            LOG.warning(
                "database schema v%d is NEWER than this code (v%d): "
                "running a downgrade is unsupported and may misbehave",
                max(done), latest_version())
        applied = 0
        for version, name, sql in MIGRATIONS:
            if version in done:
                continue
            await self._apply_migration(version, name, sql)
            applied += 1
        return applied

    async def _apply_migration(self, version: int, name: str, sql: str) -> None:
        """Run ONE migration and its bookkeeping row as a single unit.

        ``executescript`` auto-commits before it runs and gives no
        rollback of its own, so the script is wrapped in an explicit
        BEGIN..COMMIT together with the schema_migrations insert: a failing
        statement leaves NOTHING half-applied and no bookkeeping row.
        """
        safe_name = name.replace("'", "''")
        script = (f"BEGIN IMMEDIATE;\n{sql}\n"
                  f"INSERT INTO schema_migrations (version, name, applied_at) "
                  f"VALUES ({int(version)}, '{safe_name}', {time.time()!r});\n"
                  f"COMMIT;")
        async with self.connection.write() as db:
            await db.rollback()  # no stray open transaction before BEGIN
            try:
                await db.executescript(script)
            except BaseException as exc:
                try:
                    await db.rollback()
                except Exception:
                    pass
                if isinstance(exc, Exception):
                    raise DatabaseError(
                        f"migration {version} ({name}) failed: {exc}") from exc
                raise

    async def close(self) -> None:
        """Close the connection (idempotent; part of graceful shutdown)."""
        await self.connection.close()


__all__ = ["Database", "DatabaseError"]
