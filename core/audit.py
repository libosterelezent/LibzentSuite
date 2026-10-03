"""Interaction audit trail: every Discord touch the framework makes.

Purpose (user spec): a detailed, in-depth view of how the selfbots interact
so behavior can be reviewed and fixed BEFORE it triggers Discord's
anti-abuse — plus a persistent record in the database.

Recorded per interaction: ``direction='out'`` rows carry every
rate-limited send (message/DM/reaction/button) with a content preview,
how long the limiter made us WAIT (``wait_ms``) and the human-like jitter
applied after the grant (``jitter_ms``); ``direction='in'`` rows carry
every message event dispatched to plugins.

Ported from the audited ``git show HEAD:core/audit.py`` with the rewrite's
renames: ``db_repo`` instead of ``database`` (duck-typed repository with
async ``add(entry)`` / ``recent(limit, account)``), ``jitter_ms`` instead
of the old ``delay_ms``, and a dict boundary — ``record()``/``tail()``/
``history()`` all speak JSON-safe dicts so the CLI, dashboard and
simulations share one shape. Kept from the original: the best-effort DB
write (now QUEUED — record() never touches SQLite, spec §29; one async
drain worker per recorder), per-subscriber error isolation, the ring
capacity bound, and the n<=0 tail guard. ``tail()`` stays synchronous
(ring-only); ``history()`` is async (awaits the repository, falls back to
the ring).

Consumers: the dashboard Audit page (live via WS + history via REST), the
CLI 'audit' command, scripts, and the simulation reports.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from ._persist_queue import PersistQueue

LOG = logging.getLogger("slaughtermudae.audit")

#: FINAL-PHASE (F8 gap 3): per-entry content cap (manager paths truncate
#: to 80 chars; the scheduler's task content was landing untruncated —
#: Discord caps messages at 2000, plugins may not)
_CONTENT_CAP = 2000

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
        return {  # explicit: dataclasses.asdict() is ~3x slower (hot path)
            "ts": round(self.ts, 3), "account": self.account,
            "direction": self.direction, "action": self.action,
            "channel_id": self.channel_id, "content": self.content,
            "wait_ms": round(self.wait_ms, 3),
            "jitter_ms": round(self.jitter_ms, 3), "note": self.note,
        }


def _field(entry: Any, key: str, default: Any = None) -> Any:
    """Read one field from a dict OR an AuditEntry-like object."""
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def format_audit(entries: Iterable[Any]) -> List[str]:
    """Render entries (dicts or AuditEntry) as compact human-readable lines.

    Line shape (inbound rows use ``<-``; a missing channel renders as
    ``ch=-``; newlines in the preview are escaped and long content
    truncated)::

        HH:MM:SS -> account action ch=... preview (wait Xms, jitter Yms) note

    An empty iterable
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


def _as_float(value: Any) -> float:
    """float() that never raises (record() promises not to)."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class AuditRecorder:
    """Ring buffer + queued-async-DB sink + live subscribers for audit entries.

    Parameters:
        db_repo: duck-typed AuditRepository (async ``add(entry)`` /
            ``recent(limit, account)``); CONFIGURATION ONLY — nothing is
            written until ``await start_persistence(repo)`` spawns the
            drain worker (spec §29: record() never touches SQLite).
            ``None`` keeps everything in memory (simulator/headless boots:
            the queue stays unused, record() does zero extra work).
        capacity: ring size (older entries are dropped first).

    Lifecycle:
        recorder = AuditRecorder()                # or AuditRecorder(repo)
        await recorder.start_persistence(repo)    # BEFORE traffic
        ...
        await recorder.stop_persistence()         # BEFORE db.close()
    """

    def __init__(self, db_repo: Any = None, capacity: int = 2000) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"audit capacity must be a positive int, got {capacity!r}")
        self._db = db_repo
        self._ring: Deque[AuditEntry] = deque(maxlen=capacity)
        self._subscribers: List[Callable[[AuditEntry], None]] = []
        self._lock = threading.Lock()
        # QUEUED PERSISTENCE: the shared engine also used by LogService
        self._persist = PersistQueue("audit")

    # ------------------------------------------------- queued persistence
    async def start_persistence(
        self, repo: Any, *, batch: int = 50, interval: float = 0.25
    ) -> Optional["asyncio.Task"]:
        """Spawn the async drain worker (call BEFORE traffic flows).

        Batched writes (``repo.add_many``) when available; failures
        increment the ``db_failures`` counter and never raise (one raw
        stderr one-liner per failure burst). Idempotent: a live worker is
        returned as-is.
        """
        if repo is None:
            return self._persist._worker
        self._db = repo
        return await self._persist.start(repo, batch=batch, interval=interval)

    async def stop_persistence(self, timeout: float = 5.0) -> None:
        """Stop the drain worker (idempotent); call BEFORE ``db.close()``."""
        await self._persist.stop(timeout)

    def persistence_stats(self) -> Dict[str, int]:
        """Queued-persistence counters (db_failures / dropped)."""
        return self._persist.stats()

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
        """Store one interaction (ring + queued best-effort DB) and notify
        subscribers.

        Returns the JSON-safe dict form of the stored entry. Never raises
        and never awaits (spec §29): the DB write is ENQUEUED for the drain
        worker when one is live; every subscriber callback is individually
        isolated — auditing must never break the send path.
        """
        if channel_id is not None:  # coerce defensively: record() never raises
            try:
                channel_id = int(channel_id)
            except (TypeError, ValueError):
                channel_id = None
        content = str(content or "")
        if len(content) > _CONTENT_CAP:  # FINAL-PHASE (F8 gap 3): LogService
            # caps entries at 16k — audit didn't, and the scheduler writes
            # task.content untruncated at terminal; a buggy plugin could
            # land full-size rows in the ring AND the DB.
            content = content[:_CONTENT_CAP] + "…[truncated]"
        entry = AuditEntry(
            ts=time.time(),
            account=str(account),
            direction=str(direction),
            action=str(action),
            channel_id=channel_id,
            content=content,
            wait_ms=_as_float(wait_ms),
            jitter_ms=_as_float(jitter_ms),
            note=str(note or ""),
        )
        with self._lock:
            self._ring.append(entry)
        payload = entry.as_dict()  # built ONCE (was 2x per record — the
        # exact bug already fixed in LogService; PHASE-4 S7-C5)
        if self._persist.active:
            self._persist.put(payload)
        for callback in list(self._subscribers):
            try:
                callback(entry)
            except Exception:
                LOG.debug("audit subscriber failed", exc_info=True)
        return payload

    def tail(self, n: int = 50, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """Most recent entries as dicts (oldest first), optionally per account.

        n <= 0 returns [] — never the whole ring.
        """
        if n <= 0:
            return []
        with self._lock:
            ring = list(self._ring)
        if not account:
            return [e.as_dict() for e in ring[-n:]]
        out: List[AuditEntry] = []
        for entry in reversed(ring):  # newest first, stop once n found
            if entry.account == account:
                out.append(entry)
                if len(out) >= n:
                    break
        out.reverse()
        return [e.as_dict() for e in out]

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

    async def history(self, limit: int = 200, account: Optional[str] = None) -> List[Dict[str, Any]]:
        """DB-backed history (awaits the repository; falls back to the ring).

        Rows coming from the DB drop their internal 'id' and get 'ts'
        rounded so both sources look identical to consumers. Reads are the
        one place audit touches SQLite directly — they run in async
        contexts (dashboard REST / CLI) and never on the record() path.
        """
        if self._db is not None:
            try:
                rows = await self._db.recent(limit=limit, account=account)
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
