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

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import time
from typing import TYPE_CHECKING

from .connection import DatabaseConnection, DatabaseError
from .repositories.account_channels import AccountChannelsRepository
from .repositories.account_plugin_profiles import AccountPluginProfilesRepository
from .repositories.account_plugins import AccountPluginsRepository
from .repositories.accounts import AccountsRepository
from .repositories.cache import CacheRepository
from .repositories.cooldowns import CooldownRepository
from .repositories.credentials import CredentialsRepository
from .repositories.kv import KvRepository
from .repositories.logs import AuditRepository, LogRepository
from .repositories.plugin_profiles import PluginProfilesRepository
from .repositories.plugins import PluginRepository
from .repositories.presets import PresetsRepository
from .repositories.rate_policies import RatePolicyRepository
from .repositories.settings import AccountSettingsRepository
from .repositories.signals import SignalsRepository
from .repositories.tasks import TaskHistoryRepository
from .repositories.workspaces import WorkspaceRepository
from .schema import MIGRATIONS, latest_version

LOG = logging.getLogger("libzent.database")


def _split_statements(script: str) -> Iterator[str]:
    """Split a SQL script on complete statements (the sqlite3
    `complete_statement` rule) — executescript would COMMIT the
    surrounding transaction, and the migration pass now owns it."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                # a statement may open with a -- comment line before its
                # SQL; sqlite3.execute accepts that. Comment-ONLY
                # statements (complete on their own line) are no-ops —
                # executing them is harmless, so nothing is filtered.
                yield statement
    tail = buffer.strip()
    if tail:
        yield tail


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class Database:
    """Async persistence service owning ONE aiosqlite connection."""

    def __init__(
        self,
        path: str | Path = "state/libzent.db",
        *,
        audit_keep: int = 10_000,
        log_keep: int = 10_000,
    ) -> None:
        self._path = str(path)
        self._audit_keep = int(audit_keep)
        self._log_keep = int(log_keep)
        self.connection = DatabaseConnection(self._path)
        self._migrate_lock = asyncio.Lock()  # concurrent connect() -> one migrate
        # stateless repositories, sharing the owned connection
        self.accounts = AccountsRepository(self)
        self.account_plugins = AccountPluginsRepository(self)
        self.account_settings = AccountSettingsRepository(self)
        self.plugins = PluginRepository(self)
        self.plugin_profiles = PluginProfilesRepository(self)
        self.account_plugin_profiles = AccountPluginProfilesRepository(self)
        self.policies = RatePolicyRepository(self)
        self.audit = AuditRepository(self, keep=self._audit_keep)
        self.logs = LogRepository(self, keep=self._log_keep)
        self.tasks = TaskHistoryRepository(self, keep=10_000)
        self.kv = KvRepository(self)
        self.cache = CacheRepository(self)
        self.credentials = CredentialsRepository(self)
        self.cooldowns = CooldownRepository(self)
        # workspace unification (W2): the new projection + template +
        # channel layers, wired alongside their v9 predecessors until
        # W3 swaps the surfaces and v11 drops the old tables
        self.workspaces = WorkspaceRepository(self)
        self.presets = PresetsRepository(self)
        self.account_channels = AccountChannelsRepository(self)
        self.signals = SignalsRepository(self)

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
            with contextlib.suppress(OSError):
                os.chmod(self._path, 0o600)
            # FINAL-PHASE (F7): tighten the WAL/-shm sidecars too — SQLite
            # creates them with umask-default perms (typically 0644) and
            # they carry the same pages. Restart case: they exist by now.
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(OSError):
                    os.chmod(self._path + suffix, 0o600)
        try:
            async with self._migrate_lock:
                return await self.migrate()
        except BaseException:
            await self.connection.close()  # never leak the worker thread
            raise

    async def migrate(self) -> int:
        """Apply pending migrations in order (idempotent, recorded).

        Each migration runs as ONE serialized transaction on the RAW
        connection (the wrapper's execute() auto-commits outermost
        callers — savepoints and multi-statement atomicity need raw).
        The stamp is re-checked INSIDE the transaction's write lock: a
        racing connect either wins the version or observes it stamped
        and skips — interleaving between a winner's versions (a v10
        fold reading v9 tables a racing v11 already dropped) is
        impossible because the loser never applies a stamped version.
        """
        versions = [version for version, _, _ in MIGRATIONS]
        if versions != sorted(versions) or len(set(versions)) != len(versions):
            raise DatabaseError(
                f"database.schema MIGRATIONS versions must be unique and "
                f"strictly increasing, got {versions!r}"
            )
        conn = self.connection
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at REAL NOT NULL)"
        )
        done = {
            row["version"] for row in await conn.fetch_all("SELECT version FROM schema_migrations")
        }
        if done and max(done) > latest_version():
            LOG.warning(
                "database schema v%d is NEWER than this code (v%d): "
                "running a downgrade is unsupported and may misbehave",
                max(done),
                latest_version(),
            )
        applied = 0
        for version, name, sql in MIGRATIONS:
            if version in done:
                continue
            if await self._apply_guarded(version, name, sql):
                applied += 1
        return applied

    async def _apply_guarded(self, version: int, name: str, sql: str) -> bool:
        """One migration as a self-contained serialized transaction on
        the raw connection. Returns False when a racing connect already
        applied it (the in-lock stamp check). The two tolerated-loser
        classes roll back to a SAVEPOINT and re-run without the ALTERs:
        duplicate-column ALTER races, and DROP COLUMN finding the
        column already dropped."""
        safe_name = name.replace("'", "''")
        # Bandit B608: version/name come from the in-repo migration
        # registry, not user input; name is quote-escaped above and the
        # version is int-coerced.
        stamp = (
            f"INSERT INTO schema_migrations (version, name, applied_at) "  # nosec B608
            f"VALUES ({int(version)}, '{safe_name}', {time.time()!r})"
        )
        savepoint = f"mig_{int(version)}"
        async with self.connection.serialized() as raw:
            await raw.execute("BEGIN IMMEDIATE")
            try:
                cursor = await raw.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (int(version),),
                )
                if await cursor.fetchone() is not None:
                    await raw.execute("COMMIT")
                    return False  # the racing winner stamped it
                await raw.execute(f"SAVEPOINT {savepoint}")
                try:
                    for statement in _split_statements(sql):
                        await raw.execute(statement)
                    await raw.execute(stamp)
                except sqlite3.OperationalError as exc:
                    sql_upper = sql.upper()
                    message = str(exc).lower()
                    tolerated = (
                        "duplicate column name" in message and "ALTER TABLE" in sql_upper
                    ) or ("no such column" in message and "DROP COLUMN" in sql_upper)
                    if not tolerated:
                        raise DatabaseError(f"migration {version} ({name}) failed: {exc}") from exc
                    await raw.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    rest = re.sub(r"ALTER\s+TABLE\b[^;]*;", "", sql, flags=re.IGNORECASE)
                    for statement in _split_statements(rest):
                        await raw.execute(statement)
                    await raw.execute(stamp)
                finally:
                    with contextlib.suppress(Exception):
                        await raw.execute(f"RELEASE SAVEPOINT {savepoint}")
                await raw.execute("COMMIT")
                return True
            except BaseException:
                with contextlib.suppress(Exception):
                    await raw.execute("ROLLBACK")
                raise

    async def close(self) -> None:
        """Close the connection (idempotent; part of graceful shutdown).

        The migrate lock is reborn with the connection: both are
        loop-bound once awaited, and a cleanly closed Database must be
        reusable on a NEW event loop (see DatabaseConnection.close).
        """
        await self.connection.close()
        self._migrate_lock = asyncio.Lock()


__all__ = ["Database", "DatabaseError"]
