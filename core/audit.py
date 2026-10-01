"""Interaction audit trail: every Discord touch the framework makes.

Purpose (user spec): a detailed, in-depth view of how the selfbots interact
so behavior can be reviewed and fixed BEFORE it triggers Discord's
anti-abuse — plus a persistent record in the database.

Recorded per interaction:
  direction 'out' — every rate-limited send (message/DM): content preview,
                    how long the limiter made us WAIT (wait_ms), and the
                    human-like jitter applied after the grant (jitter_ms)
  direction 'in'  — every message event dispatched to plugins

Ported from the audited ``git show HEAD:core/audit.py`` with the rewrite's
renames: ``db_repo`` instead of ``database`` (duck-typed repository with
``add(entry)`` / ``recent(limit, account)``), ``jitter_ms`` instead of the
old ``delay_ms``, and a dict boundary — ``record()``/``tail()``/``history()``
all speak JSON-safe dicts so the CLI, dashboard and simulations share one
shape. Kept from the original: the best-effort DB write, per-subscriber
error isolation, the ring capacity bound, and the n<=0 tail guard.

Consumers: the dashboard Audit page (live via WS + history via REST), the
CLI 'audit' command, scripts, and the simulation reports.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

LOG = logging.getLogger("slaughtermudae.audit")

#: content previews are truncated to this length in format_audit lines
_PREVIEW_LIMIT = 60


@dataclass
class AuditEntry:
    """One observed interaction (internal unit of the audit trail)."""

    ts: float
    account: str
    direction: str            # 'out' | 'in'
    action: str               # 'message' | 'dm' | 'event:message' | ...
    channel_id: Optional[int] = None
    content: str = ""
    wait_ms: float = 0.0      # time spent waiting for the rate limiter
    jitter_ms: float = 0.0    # human-like jitter applied after the grant
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe dict form (floats rounded to milliseconds precision)."""
        out = asdict(self)
        out["ts"] = round(out["ts"], 3)
        out["wait_ms"] = round(out["wait_ms"], 3)
        out["jitter_ms"] = round(out["jitter_ms"], 3)
        return out


def _field(entry: Any, key: str, default: Any = None) -> Any:
    """Read one field from a dict OR an AuditEntry-like object."""
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def format_audit(entries: Iterable[Any]) -> List[str]:
    """Render entries (dicts or AuditEntry) as compact human-readable lines.

    Line shape:
        HH:MM:SS -> account action ch=... preview (wait Xms, jitter Yms) note
    ('<-' for inbound; missing channel renders as 'ch=-'; newlines in the
    preview are escaped and long content truncated). An empty iterable
    renders as the '(no interactions recorded)' placeholder.
    """
    lines: List[str] = []
    for entry in entries:
        ts = float(_field(entry, "ts", 0.0) or 0.0)
        clock = time.strftime("%H:%M:%S", time.localtime(ts))
        arrow = "->" if _field(entry, "direction", "") == "out" else "<-"
        account = str(_field(entry, "account", "") or "")
        action = str(_field(entry, "action", "") or "")
        channel = _field(entry, "channel_id", None)
        channel_text = f"ch={channel}" if channel is not None else "ch=-"
        preview = str(_field(entry, "content", "") or "").replace("\n", "\\n")
        if len(preview) > _PREVIEW_LIMIT:
            preview = preview[: _PREVIEW_LIMIT - 3] + "..."
        waits = []
        wait_ms = float(_field(entry, "wait_ms", 0.0) or 0.0)
        jitter_ms = float(_field(entry, "jitter_ms", 0.0) or 0.0)
        if wait_ms:
            waits.append(f"wait {wait_ms:.0f}ms")
        if jitter_ms:
            waits.append(f"jitter {jitter_ms:.0f}ms")
        extra = f" ({', '.join(waits)})" if waits else ""
        note = str(_field(entry, "note", "") or "")
        note_text = f" {note}" if note else ""
        lines.append(
            f"{clock} {arrow} {account:<12}{action:<14}{channel_text:<24}{preview}{extra}{note_text}"
        )
    return lines or ["(no interactions recorded)"]


class AuditRecorder:
    """Ring buffer + optional DB sink + live subscribers for audit entries.

    Parameters:
        db_repo: duck-typed AuditRepository (``add(entry)`` /
            ``recent(limit, account)``); None keeps everything in memory.
        capacity: ring size (older entries are dropped first).
    """

    def __init__(self, db_repo: Any = None, capacity: int = 2000) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"audit capacity must be a positive int, got {capacity!r}")
        self._db = db_repo
        self._ring: Deque[AuditEntry] = deque(maxlen=capacity)
        self._subscribers: List[Callable[[AuditEntry], None]] = []
        self._lock = threading.Lock()

    def record(
        self,
        account: str,
        direction: str,
        action: str,
        channel_id: Optional[int] = None,
        content: str = "",
        wait_ms: float = 0.0,
        jitter_ms: float = 0.0,
        note: str = "",
    ) -> Dict[str, Any]:
        """Store one interaction (ring + best-effort DB) and notify subscribers.

        Returns the JSON-safe dict form of the stored entry. Never raises:
        the DB write and every subscriber callback are individually isolated
        — auditing must never break the send path.
        """
        entry = AuditEntry(
            ts=time.time(),
            account=str(account),
            direction=str(direction),
            action=str(action),
            channel_id=channel_id,
            content=str(content or ""),
            wait_ms=float(wait_ms or 0.0),
            jitter_ms=float(jitter_ms or 0.0),
            note=str(note or ""),
        )
        with self._lock:
            self._ring.append(entry)
        if self._db is not None:
            try:
                self._db.add(entry.as_dict())
            except Exception:  # auditing must never break the send path
                LOG.exception("audit DB write failed")
        for callback in list(self._subscribers):
            try:
                callback(entry)
            except Exception:
                LOG.debug("audit subscriber failed", exc_info=True)
        return entry.as_dict()

    def tail(self, n: int = 50, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """Most recent entries as dicts (oldest first), optionally per account.

        n <= 0 returns [] — never the whole ring.
        """
        with self._lock:
            items = list(self._ring)
        if account:
            items = [e for e in items if e.account == account]
        return [e.as_dict() for e in (items[-n:] if n > 0 else [])]

    def subscribe(self, callback: Callable[[AuditEntry], None]) -> Callable[[], None]:
        """Live-tail subscription (dashboard WS); returns an unsubscribe callable."""
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass  # already unsubscribed (idempotent)

        return _unsubscribe

    def history(self, limit: int = 200, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """DB-backed history (falls back to the ring when no DB / DB failure).

        Rows coming from the DB drop their internal 'id' and get 'ts' rounded
        so both sources look identical to consumers.
        """
        if self._db is not None:
            try:
                rows = self._db.recent(limit=limit, account=account)
                normalized: List[Dict[str, Any]] = []
                for row in rows:
                    row = dict(row)
                    row.pop("id", None)
                    row["ts"] = round(float(row.get("ts") or 0.0), 3)
                    normalized.append(row)
                return normalized
            except Exception:
                LOG.exception("audit history read failed; falling back to ring")
        return self.tail(limit, account)


__all__ = ["AuditRecorder", "AuditEntry", "format_audit"]
