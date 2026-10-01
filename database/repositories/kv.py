"""KvRepository — tiny generic key/value store (misc persistent state).

Used for one-off persistent facts (migration markers, scenario reports,
last-run bookkeeping...). Values are stored JSON-encoded so any
JSON-serializable type round-trips; a row that is NOT valid JSON (written
by hand or by an older version) is returned as its raw string instead of
crashing the read — the audited old code stored raw strings, so this
tolerance keeps old files readable.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from core.errors import DatabaseError

from .base import BaseRepository, loads_tolerant

__all__ = ["KvRepository"]


class KvRepository(BaseRepository):
    """get/set/delete/prefix over the ``kv`` table."""

    def _check_key(self, key: str) -> None:
        """Reject keys that cannot address a row (clear, human message)."""
        if not isinstance(key, str) or not key:
            raise DatabaseError("kv key must be a non-empty string")

    def get(self, key: str, default: Any = None) -> Any:
        """Value stored under key (JSON-decoded), or ``default`` when absent.

        Corrupt JSON falls back to the raw string (see module docstring).
        """
        row = self._read_one(
            "SELECT value FROM kv WHERE key = ?", (key,), what="read kv"
        )
        if row is None:
            return default
        return loads_tolerant(row["value"], row["value"], f"kv[{key!r}]")

    def set(self, key: str, value: Any) -> None:
        """Store value under key (upsert; JSON-encoded so types round-trip).

        Raises DatabaseError for a bad key or a non-serializable value.
        """
        self._check_key(key)
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise DatabaseError(f"kv value for {key!r} is not JSON-serializable: {exc}") from exc
        with self._tx(f"kv set {key!r}") as conn:
            conn.execute(
                """INSERT INTO kv (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, payload),
            )

    def delete(self, key: str) -> bool:
        """Remove one entry; True when a row was deleted."""
        with self._tx(f"kv delete {key!r}") as conn:
            cur = conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            return cur.rowcount > 0

    def prefix(self, p: str) -> Dict[str, Any]:
        """All entries whose key starts with ``p`` (key -> decoded value).

        LIKE wildcards inside the prefix (%, _) are escaped so a literal
        'a%b' key matches exactly; an empty prefix returns everything.
        Raises DatabaseError when p is not a string.
        """
        if not isinstance(p, str):
            raise DatabaseError("kv prefix must be a string")
        pattern = (
            p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        rows = self._read(
            "SELECT key, value FROM kv WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (pattern,),
            what="read kv prefix",
        )
        return {
            row["key"]: loads_tolerant(row["value"], row["value"], f"kv[{row['key']!r}]")
            for row in rows
        }
