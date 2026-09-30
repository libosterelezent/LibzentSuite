"""SQLite persistence: state, accounts registry, roles, settings, audit log.

Replaces the old JSON StateStore (which it migrates automatically). The DB
file is created with 0600 permissions — persistency AND secrecy:

  * account TOKEN VALUES never enter the database — only the .env key that
    references them (tokens stay in .env, gitignored, chmod 600)
  * everything else (accounts, roles, plugin states, settings overrides,
    limit/delay overrides, audit trail) lives here and survives restarts
  * the audit table is pruned to a bounded size so the file can't grow forever
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("slaughtermudae.database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    name         TEXT PRIMARY KEY,
    token_ref    TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    sync_group   TEXT,
    plugins_json TEXT,
    roles_json   TEXT,
    settings_json TEXT,
    rate_rules_json TEXT,
    reconnect_json TEXT,
    added_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plugin_states (
    account TEXT NOT NULL,
    plugin  TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (account, plugin)
);
CREATE TABLE IF NOT EXISTS limit_overrides (
    account TEXT NOT NULL,
    action  TEXT NOT NULL,
    rule    TEXT NOT NULL,
    PRIMARY KEY (account, action)
);
CREATE TABLE IF NOT EXISTS delay_settings (
    account  TEXT NOT NULL DEFAULT '',   -- '' = global default
    action   TEXT NOT NULL,
    minimum  REAL NOT NULL,
    maximum  REAL NOT NULL,
    PRIMARY KEY (account, action)
);
CREATE TABLE IF NOT EXISTS setting_overrides (
    account TEXT NOT NULL,
    plugin  TEXT NOT NULL,
    key     TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (account, plugin, key)
);
CREATE TABLE IF NOT EXISTS roles (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    plugins_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    account    TEXT NOT NULL,
    direction  TEXT NOT NULL,           -- 'out' | 'in'
    action     TEXT NOT NULL,
    channel_id INTEGER,
    content    TEXT,
    wait_ms    REAL NOT NULL DEFAULT 0,
    delay_ms   REAL NOT NULL DEFAULT 0,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit_log (ts DESC);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

#: audit rows kept in the DB (older rows pruned on write)
AUDIT_KEEP_ROWS = 10_000


class DatabaseError(RuntimeError):
    """Raised for invalid database operations."""


class Database:
    """Thread-safe sqlite persistence for the whole framework."""

    def __init__(self, path: str | Path = "state/slaughter.db", audit_keep: int = AUDIT_KEEP_ROWS) -> None:
        self._path = Path(path)
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
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        self._audit_keep = audit_keep
        LOG.info("database ready at %s", self._path)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- accounts
    def upsert_account(self, config) -> None:
        """Persist an AccountConfig (token VALUES are never stored)."""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO accounts
                   (name, token_ref, enabled, sync_group, plugins_json, roles_json,
                    settings_json, rate_rules_json, reconnect_json, added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     token_ref=excluded.token_ref, enabled=excluded.enabled,
                     sync_group=excluded.sync_group, plugins_json=excluded.plugins_json,
                     roles_json=excluded.roles_json, settings_json=excluded.settings_json,
                     rate_rules_json=excluded.rate_rules_json,
                     reconnect_json=excluded.reconnect_json""",
                (
                    config.name, config.token_ref, int(config.enabled), config.sync_group,
                    json.dumps(config.plugins), json.dumps(config.roles),
                    json.dumps(config.settings), json.dumps(config.rate_rules),
                    json.dumps(config.reconnect), time.time(),
                ),
            )

    def delete_account(self, name: str) -> bool:
        """Remove an account and its dependent rows."""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM accounts WHERE name=?", (name,))
            self._conn.execute("DELETE FROM plugin_states WHERE account=?", (name,))
            self._conn.execute("DELETE FROM setting_overrides WHERE account=?", (name,))
            self._conn.execute("DELETE FROM limit_overrides WHERE account=?", (name,))
            self._conn.execute("DELETE FROM delay_settings WHERE account=?", (name,))
            return cur.rowcount > 0

    def load_accounts(self) -> List:
        """All persisted accounts as AccountConfig objects."""
        from .account import AccountConfig

        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        configs = []
        for row in rows:
            configs.append(
                AccountConfig(
                    name=row["name"],
                    token_ref=row["token_ref"],
                    enabled=bool(row["enabled"]),
                    sync_group=row["sync_group"],
                    plugins=json.loads(row["plugins_json"] or "null"),
                    roles=json.loads(row["roles_json"] or "[]"),
                    settings=json.loads(row["settings_json"] or "{}"),
                    rate_rules=json.loads(row["rate_rules_json"] or "{}"),
                    reconnect=json.loads(row["reconnect_json"] or "null"),
                )
            )
        return configs

    # -------------------------------------------------------- plugin states
    def plugin_enabled(self, account: str, plugin: str, default: bool = True) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT enabled FROM plugin_states WHERE account=? AND plugin=?",
                (account, plugin),
            ).fetchone()
        return default if row is None else bool(row["enabled"])

    def set_plugin_enabled(self, account: str, plugin: str, enabled: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO plugin_states (account, plugin, enabled) VALUES (?,?,?)
                   ON CONFLICT(account, plugin) DO UPDATE SET enabled=excluded.enabled""",
                (account, plugin, int(enabled)),
            )

    # ------------------------------------------------------- limit overrides
    def limit_overrides(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            rows = self._conn.execute("SELECT account, action, rule FROM limit_overrides").fetchall()
        out: Dict[str, Dict[str, str]] = {}
        for row in rows:
            out.setdefault(row["account"], {})[row["action"]] = row["rule"]
        return out

    def set_limit_override(self, account: str, action: str, rule: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO limit_overrides (account, action, rule) VALUES (?,?,?)
                   ON CONFLICT(account, action) DO UPDATE SET rule=excluded.rule""",
                (account, action, rule),
            )

    def clear_limit_override(self, account: str, action: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM limit_overrides WHERE account=? AND action=?", (account, action)
            )
            return cur.rowcount > 0

    # ---------------------------------------------------------- delay ranges
    def set_delay(self, action: str, minimum: float, maximum: float, account: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO delay_settings (account, action, minimum, maximum) VALUES (?,?,?,?)
                   ON CONFLICT(account, action) DO UPDATE SET minimum=excluded.minimum, maximum=excluded.maximum""",
                (account or "", action, float(minimum), float(maximum)),
            )

    def clear_delay(self, action: str, account: Optional[str] = None) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM delay_settings WHERE account=? AND action=?", (account or "", action)
            )
            return cur.rowcount > 0

    def delays(self) -> Dict[str, Any]:
        """{'global': {action: [min,max]}, 'accounts': {acct: {action: [min,max]}}}."""
        with self._lock:
            rows = self._conn.execute("SELECT account, action, minimum, maximum FROM delay_settings").fetchall()
        out: Dict[str, Any] = {"global": {}, "accounts": {}}
        for row in rows:
            pair = [row["minimum"], row["maximum"]]
            if row["account"]:
                out["accounts"].setdefault(row["account"], {})[row["action"]] = pair
            else:
                out["global"][row["action"]] = pair
        return out

    # ---------------------------------------------------- setting overrides
    def set_setting_override(self, account: str, plugin: str, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO setting_overrides (account, plugin, key, value_json) VALUES (?,?,?,?)
                   ON CONFLICT(account, plugin, key) DO UPDATE SET value_json=excluded.value_json""",
                (account, plugin, key, json.dumps(value)),
            )

    def setting_overrides(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """account -> plugin -> key -> value (SettingsStore account layer)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT account, plugin, key, value_json FROM setting_overrides"
            ).fetchall()
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["account"], {}).setdefault(row["plugin"], {})[row["key"]] = json.loads(
                row["value_json"]
            )
        return out

    # ----------------------------------------------------------------- roles
    def upsert_role(self, name: str, plugins: List[str], description: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO roles (name, description, plugins_json) VALUES (?,?,?)
                   ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                   plugins_json=excluded.plugins_json""",
                (name, description, json.dumps(sorted(set(plugins)))),
            )

    def delete_role(self, name: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM roles WHERE name=?", (name,))
            return cur.rowcount > 0

    def roles(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT name, description, plugins_json FROM roles ORDER BY name").fetchall()
        return {
            row["name"]: {
                "description": row["description"],
                "plugins": json.loads(row["plugins_json"]),
            }
            for row in rows
        }

    # ------------------------------------------------------------- audit log
    def add_audit(self, entry: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO audit_log
                   (ts, account, direction, action, channel_id, content, wait_ms, delay_ms, note)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    entry.get("ts", time.time()), entry.get("account", "?"),
                    entry.get("direction", "out"), entry.get("action", "?"),
                    entry.get("channel_id"), (entry.get("content") or "")[:500],
                    entry.get("wait_ms", 0.0), entry.get("delay_ms", 0.0),
                    entry.get("note"),
                ),
            )
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
            if cur["n"] > self._audit_keep:
                self._conn.execute(
                    "DELETE FROM audit_log WHERE id <= (SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (self._audit_keep,),
                )

    def audit_log(self, limit: int = 200, account: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if account:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log WHERE account=? ORDER BY id DESC LIMIT ?",
                    (account, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # -------------------------------------------------------------------- kv
    def kv_set(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else row["value"]

    def kv_prefix(self, prefix: str) -> Dict[str, str]:
        """All kv entries whose key starts with prefix (key -> value)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    # -------------------------------------------------------------- migration
    def migrate_legacy_state(self, path: str | Path = "state/state.json") -> int:
        """One-time import from the old JSON StateStore; returns rows imported."""
        legacy = Path(path)
        if not legacy.is_file():
            return 0
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("legacy state unreadable (%s); skipping", exc)
            return 0
        count = 0
        for account, plugins in (data.get("plugin_states") or {}).items():
            for plugin, enabled in plugins.items():
                self.set_plugin_enabled(account, plugin, bool(enabled))
                count += 1
        for account, rules in (data.get("limit_overrides") or {}).items():
            for action, rule in rules.items():
                self.set_limit_override(account, action, rule)
                count += 1
        if count:
            legacy.rename(legacy.with_suffix(".json.imported"))
            LOG.info("migrated %d legacy state entrie(s) from %s", count, legacy)
        return count
