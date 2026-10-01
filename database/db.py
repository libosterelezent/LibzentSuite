"""SQLite persistence root: connection, migrations, repository factory.

One ``Database`` object owns the single sqlite3 connection plus the RLock every
repository shares (thread-safe by construction — the audited old
implementation used the same single-lock discipline). Responsibilities:

  * open/create the file with 0600 permissions BEFORE the first write
    (best-effort on non-POSIX), parent dirs auto-created;
  * run pending migrations from ``database.schema.MIGRATIONS`` in order, each
    inside its own transaction, recorded in ``schema_migrations``;
  * expose the nine repositories as lazily-created properties sharing the
    connection + lock;
  * ``Database(":memory:")`` works fully (simulations/scenarios).

Secrets policy (spec §7): token VALUES never enter this database — accounts
store only the .env key that references them.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from core.errors import DatabaseError
from database.schema import MIGRATIONS

from .repositories.accounts import AccountsRepository
from .repositories.cache import CacheRepository
from .repositories.kv import KvRepository
from .repositories.logs import AuditRepository, LogRepository
from .repositories.plugins import PluginRepository
from .repositories.profiles import ProfilesRepository
from .repositories.rate_policies import RatePolicyRepository
from .repositories.settings import AccountSettingsRepository
from .repositories.tasks import TaskHistoryRepository

LOG = logging.getLogger("slaughtermudae.database")


class Database:
    """Thread-safe sqlite persistence for the whole framework."""

    def __init__(
        self,
        path: str | Path = "state/slaughter.db",
        *,
        audit_keep: int = 10_000,
        log_keep: int = 10_000,
    ) -> None:
        """Open (or create) the database file and prepare migrations.

        ``path=":memory:"`` yields a private in-memory database (simulations).
        """
        self._memory = str(path) == ":memory:"
        self._path = Path(path) if not self._memory else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # secrecy: restrict before first write (best-effort on non-POSIX)
            try:
                if not self._path.exists():
                    os.close(os.open(self._path, os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                pass
            except OSError:
                pass
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                ":memory:" if self._memory else str(self._path),
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"cannot open database {path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        if not self._memory:
            try:  # WAL keeps readers/writers from blocking each other
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
        if self._path is not None:
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        self._audit_keep = int(audit_keep)
        self._log_keep = int(log_keep)
        self._closed = False
        self._repos: dict = {}
        self.migrate()
        LOG.info(
            "database ready at %s", ":memory:" if self._memory else self._path
        )

    # ------------------------------------------------------------- lifecycle
    @property
    def path(self) -> Optional[Path]:
        """File path (None for in-memory databases)."""
        return self._path

    def migrate(self) -> int:
        """Apply pending migrations in order; returns how many were applied.

        Each migration runs inside its own transaction and is recorded in
        ``schema_migrations``; re-running is a no-op (returns 0).
        """
        applied = 0
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
            )
            self._conn.commit()
            done = {
                row["version"]
                for row in self._conn.execute("SELECT version FROM schema_migrations")
            }
            import time as _time

            for version, name, sql in MIGRATIONS:
                if version in done:
                    continue
                try:
                    self._conn.executescript("BEGIN;" + sql + ";COMMIT;")
                    self._conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
                        (version, name, _time.time()),
                    )
                    self._conn.commit()
                except sqlite3.Error as exc:
                    self._conn.rollback()
                    raise DatabaseError(
                        f"migration {version} ({name}) failed: {exc}"
                    ) from exc
                applied += 1
                LOG.info("applied migration %03d: %s", version, name)
        return applied

    def close(self) -> None:
        """Close the connection (idempotent)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ---------------------------------------------------------- repositories
    def _repo(self, key: str, factory):
        repo = self._repos.get(key)
        if repo is None:
            repo = factory()
            self._repos[key] = repo
        return repo

    @property
    def accounts(self) -> AccountsRepository:
        """Accounts + their profile assignments + framework settings JSON."""
        return self._repo("accounts", lambda: AccountsRepository(self._conn, self._lock))

    @property
    def account_settings(self) -> AccountSettingsRepository:
        """Per-account framework settings (reconnect policy...)."""
        return self._repo(
            "account_settings", lambda: AccountSettingsRepository(self._conn, self._lock)
        )

    @property
    def profiles(self) -> ProfilesRepository:
        """Profiles + the plugins each permits."""
        return self._repo("profiles", lambda: ProfilesRepository(self._conn, self._lock))

    @property
    def plugins(self) -> PluginRepository:
        """Plugin metadata cache + per-account states + plugin settings."""
        return self._repo("plugins", lambda: PluginRepository(self._conn, self._lock))

    @property
    def policies(self) -> RatePolicyRepository:
        """Rate-limit policies at every level (global/account/plugin/pair)."""
        return self._repo("policies", lambda: RatePolicyRepository(self._conn, self._lock))

    @property
    def audit(self) -> AuditRepository:
        """Interaction audit trail (pruned to the newest audit_keep rows)."""
        return self._repo(
            "audit", lambda: AuditRepository(self._conn, self._lock, keep=self._audit_keep)
        )

    @property
    def logs(self) -> LogRepository:
        """Structured log sink (pruned to the newest log_keep rows)."""
        return self._repo(
            "logs", lambda: LogRepository(self._conn, self._lock, keep=self._log_keep)
        )

    @property
    def tasks(self) -> TaskHistoryRepository:
        """Finished-task history for the Tasks page."""
        return self._repo("tasks", lambda: TaskHistoryRepository(self._conn, self._lock))

    @property
    def cache(self) -> CacheRepository:
        """Per-account persistent cache of resolved Discord entities."""
        return self._repo("cache", lambda: CacheRepository(self._conn, self._lock))

    @property
    def kv(self) -> KvRepository:
        """Miscellaneous key/value storage."""
        return self._repo("kv", lambda: KvRepository(self._conn, self._lock))


__all__ = ["Database"]
