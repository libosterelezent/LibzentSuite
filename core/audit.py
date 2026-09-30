"""Interaction audit trail: every Discord touch the framework makes.

Purpose (user spec): a detailed, in-depth view of how the selfbots interact
so behavior can be reviewed and fixed BEFORE it triggers Discord's
anti-abuse — plus a persistent record in the database.

Recorded per interaction:
  direction 'out' — every rate-limited send (message/DM): content preview,
                    how long the limiter made us WAIT, and the human-like
                    DELAY applied after the grant
  direction 'in'  — every message event dispatched to plugins: author,
                    channel, dm flag

Consumers: the dashboard Audit page (live via WS + history via REST),
the CLI 'audit' command, and scripts/sim_inspect.py (local simulation suite).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

LOG = logging.getLogger("slaughtermudae.audit")


@dataclass
class AuditEntry:
    """One observed interaction."""

    ts: float
    account: str
    direction: str            # 'out' | 'in'
    action: str               # 'message' | 'dm' | 'event:message' | ...
    channel_id: Optional[int] = None
    content: str = ""
    wait_ms: float = 0.0      # time spent waiting for the rate limiter
    delay_ms: float = 0.0     # human-like delay applied after the grant
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["ts"] = round(out["ts"], 3)
        return out


def format_audit(entries: List[AuditEntry]) -> List[str]:
    """Entries -> compact human-readable lines (CLI/TUI/inspector)."""
    lines = []
    for e in entries:
        clock = time.strftime("%H:%M:%S", time.localtime(e.ts))
        arrow = "->" if e.direction == "out" else "<-"
        waits = []
        if e.wait_ms:
            waits.append(f"wait {e.wait_ms:.0f}ms")
        if e.delay_ms:
            waits.append(f"delay {e.delay_ms:.0f}ms")
        extra = f" ({', '.join(waits)})" if waits else ""
        note = f" {e.note}" if e.note else ""
        preview = (e.content or "").replace("\n", "\\n")
        if len(preview) > 60:
            preview = preview[:57] + "..."
        lines.append(
            f"  {clock} {arrow} {e.account:<8}{e.action:<16}"
            f"ch={e.channel_id if e.channel_id is not None else '-':<22}{preview}{extra}{note}"
        )
    return lines or ["  (no interactions recorded)"]


class AuditRecorder:
    """Ring buffer + optional DB sink + live subscribers for audit entries."""

    def __init__(self, database: Optional[Any] = None, capacity: int = 2000) -> None:
        self._db = database
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
        delay_ms: float = 0.0,
        note: str = "",
    ) -> AuditEntry:
        """Store one interaction (ring + DB) and notify live subscribers."""
        entry = AuditEntry(
            ts=time.time(), account=account, direction=direction, action=action,
            channel_id=channel_id, content=content, wait_ms=wait_ms,
            delay_ms=delay_ms, note=note,
        )
        with self._lock:
            self._ring.append(entry)
        if self._db is not None:
            try:
                self._db.add_audit(entry.as_dict())
            except Exception:  # auditing must never break the send path
                LOG.exception("audit DB write failed")
        for callback in list(self._subscribers):
            try:
                callback(entry)
            except Exception:
                LOG.debug("audit subscriber failed", exc_info=True)  # ERROR AUDIT: no more silent pass
        return entry

    def tail(self, n: int = 50, account: Optional[str] = None) -> List[AuditEntry]:
        """Most recent entries (oldest first), optionally filtered per account."""
        with self._lock:
            items = list(self._ring)
        if account:
            items = [e for e in items if e.account == account]
        # AUDIT FIX: n <= 0 returns nothing (previously returned the whole ring)
        return items[-n:] if n > 0 else []

    def subscribe(self, callback: Callable[[AuditEntry], None]) -> Callable[[], None]:
        """Live-tail subscription (dashboard WS); returns unsubscribe."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def history(self, limit: int = 200, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """DB-backed history (falls back to the ring when no DB is attached).

        AUDIT FIX: normalized shape — DB rows drop the internal `id` and round
        `ts` so both sources look identical to consumers.
        """
        if self._db is not None:
            try:
                rows = self._db.audit_log(limit=limit, account=account)
                for row in rows:
                    row.pop("id", None)
                    row["ts"] = round(float(row.get("ts") or 0.0), 3)
                return rows
            except Exception:
                LOG.exception("audit history read failed")
        return [e.as_dict() for e in self.tail(limit, account)]
