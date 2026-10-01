"""Ordered SQL migrations. Append-only: NEVER edit an applied migration.

Each entry: (version, name, sql). The runner (database/db.py) records applied
versions in `schema_migrations` and applies pending ones in order inside a
transaction per migration.
"""
from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            token_ref   TEXT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            sync_group  TEXT,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_settings (
            account    TEXT NOT NULL,
            key        TEXT NOT NULL,
            value_json TEXT NOT NULL,
            PRIMARY KEY (account, key)
        );
        CREATE TABLE IF NOT EXISTS plugins (
            name            TEXT PRIMARY KEY,
            version         TEXT NOT NULL DEFAULT '0.0.0',
            description     TEXT NOT NULL DEFAULT '',
            settings_json   TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS plugin_settings (
            account    TEXT NOT NULL,
            plugin     TEXT NOT NULL,
            key        TEXT NOT NULL,
            value_json TEXT NOT NULL,
            PRIMARY KEY (account, plugin, key)
        );
        CREATE TABLE IF NOT EXISTS plugin_states (
            account TEXT NOT NULL,
            plugin  TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (account, plugin)
        );
        CREATE TABLE IF NOT EXISTS profiles (
            name        TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS profile_plugins (
            profile TEXT NOT NULL,
            plugin  TEXT NOT NULL,
            PRIMARY KEY (profile, plugin)
        );
        CREATE TABLE IF NOT EXISTS account_profiles (
            account TEXT NOT NULL,
            profile TEXT NOT NULL,
            PRIMARY KEY (account, profile)
        );
        CREATE TABLE IF NOT EXISTS rate_limit_policies (
            level      TEXT NOT NULL,
            owner      TEXT NOT NULL DEFAULT '',
            action     TEXT NOT NULL,
            interval   REAL NOT NULL,
            jitter_min REAL NOT NULL DEFAULT 0.0,
            jitter_max REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (level, owner, action)
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            account    TEXT NOT NULL,
            direction  TEXT NOT NULL,
            action     TEXT NOT NULL,
            channel_id INTEGER,
            content    TEXT,
            wait_ms    REAL NOT NULL DEFAULT 0,
            jitter_ms  REAL NOT NULL DEFAULT 0,
            note       TEXT
        );
        CREATE INDEX IF NOT EXISTS audit_ts ON audit_logs (ts DESC);
        CREATE TABLE IF NOT EXISTS logs (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       REAL NOT NULL,
            severity TEXT NOT NULL,
            source   TEXT NOT NULL,
            account  TEXT,
            plugin   TEXT,
            task_id  INTEGER,
            event    TEXT,
            message  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS logs_ts ON logs (ts DESC);
        CREATE TABLE IF NOT EXISTS task_history (
            id           INTEGER PRIMARY KEY,
            name         TEXT,
            account      TEXT NOT NULL,
            plugin       TEXT,
            action       TEXT NOT NULL,
            status       TEXT NOT NULL,
            priority     INTEGER NOT NULL DEFAULT 1,
            submitted_at REAL NOT NULL,
            started_at   REAL,
            finished_at  REAL,
            attempts     INTEGER NOT NULL DEFAULT 0,
            duration_s   REAL,
            error        TEXT,
            summary      TEXT
        );
        CREATE INDEX IF NOT EXISTS task_history_account ON task_history (account, id DESC);
        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
]


def latest_version() -> int:
    """Highest migration version defined (0 when none)."""
    return MIGRATIONS[-1][0] if MIGRATIONS else 0


__all__ = ["MIGRATIONS", "latest_version"]
