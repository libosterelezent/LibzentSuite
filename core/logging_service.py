"""Centralized logging service (spec §10).

Every component (core, account, plugin, scheduler, ratelimiter, adapter)
records STRUCTURED entries through LogService instead of scattering print()
calls or raw logger text. Each entry carries:

    timestamp, severity, source, account, plugin, task_id, event, message

Consumers:
  * stderr mirror (human-readable, one line per record),
  * the dashboard Logs page (live tail over WebSocket — no text parsing),
  * the database `logs` table (bounded, best-effort).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR")
_LEVEL_BY_SEVERITY = {
    "DEBUG": logging.DEBUG, "INFO": logging.INFO,
    "WARNING": logging.WARNING, "ERROR": logging.ERROR,
}
_STDERR_CONFIGURED = False

#: stdlib level name -> canonical severity (CRITICAL/FATAL collapse to
#: ERROR — the structured store has no crash level, and INFO would hide it)
_SEVERITY_BY_LEVELNAME = {
    "DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING",
    "ERROR": "ERROR", "CRITICAL": "ERROR", "FATAL": "ERROR",
    "WARN": "WARNING",
}


@dataclass
class LogEntry:
    """One structured log record (JSON-safe via as_dict)."""

    ts: float
    severity: str
    source: str            # component: "core", "account:alpha", "scheduler"...
    message: str
    account: Optional[str] = None
    plugin: Optional[str] = None
    task_id: Optional[int] = None
    event: Optional[str] = None   # event name when this records a bus event

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["ts"] = round(out["ts"], 3)
        return out


def configure_stderr(level: int = logging.INFO) -> None:
    """Attach one human-readable stderr handler to the logger tree (idempotent)."""
    global _STDERR_CONFIGURED
    if _STDERR_CONFIGURED:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
    ))
    root = logging.getLogger("slaughtermudae")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _STDERR_CONFIGURED = True


class LogService:
    """Ring buffer + live subscribers + optional DB sink for structured logs."""

    def __init__(self, capacity: int = 4000) -> None:
        self._ring: Deque[LogEntry] = deque(maxlen=capacity)
        self._subscribers: List[Callable[[LogEntry], None]] = []
        self._lock = threading.Lock()
        self._db = None  # LogRepository (duck-typed, attached later)
        self._counts: Dict[str, int] = {sev: 0 for sev in SEVERITIES}
        # OBSERVABILITY (dead sinks must not be silent): drop/failure counters
        self._db_failures = 0
        self._subscriber_failures = 0
        # INBOUND/OUTBOUND SEPARATION: while a StdlibBridge is ingesting a
        # raw stdlib record ON THIS THREAD, the mirror is suppressed — the
        # record came FROM stdlib (stderr already printed it once), so
        # mirroring it back would double-print and re-enter the tree
        self._ingest = threading.local()

    @contextmanager
    def ingest_context(self):
        """Mark this thread as inside a bridge ingestion (mirror suppressed).

        Used by StdlibBridge around its ``record()`` call: the record is
        INBOUND (stdlib -> service); its outbound mirror is redundant by
        construction. Thread-local: other threads keep their mirrors.
        """
        self._ingest.active = True
        try:
            yield
        finally:
            self._ingest.active = False

    # ---------------------------------------------------------------- sink
    def attach_db(self, repository: Any) -> None:
        """Persist every record to a LogRepository (best-effort, never raises)."""
        self._db = repository

    def record(
        self,
        severity: str,
        source: str,
        message: str,
        *,
        account: Optional[str] = None,
        plugin: Optional[str] = None,
        task_id: Optional[int] = None,
        event: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store one structured entry; returns its JSON-safe dict.

        Mirrors to the stdlib logger (stderr) and notifies live subscribers.
        Never raises — logging must not break the caller's flow.
        """
        severity = severity.upper()
        if severity not in SEVERITIES:
            severity = "INFO"
        entry = LogEntry(
            ts=time.time(), severity=severity, source=source, message=str(message),
            account=account, plugin=plugin, task_id=task_id, event=event,
        )
        with self._lock:
            self._ring.append(entry)
            self._counts[severity] = self._counts.get(severity, 0) + 1
        # OUTBOUND mirror — only for records NOT ingested from stdlib on
        # this thread (a bridge-ingested record already printed to stderr
        # via the original stdlib call; mirroring it back double-prints).
        # The _sm_from_service marker below is defense-in-depth: even if the
        # mirror somehow reaches a bridge, it is skipped (the original loop
        # echoed every line ~recursion-limit times into ring AND DB).
        if not getattr(self._ingest, "active", False):
            logging.getLogger(f"slaughtermudae.{source}").log(
                _LEVEL_BY_SEVERITY[severity], "%s", entry.message,
                extra={"_sm_from_service": True},
            )
        if self._db is not None:
            try:
                self._db.add(entry.as_dict())
            except Exception:
                # OBSERVABLE, never recursive: no logging here (a log call
                # inside the write path is what fed the original loop)
                self._db_failures += 1
        for callback in list(self._subscribers):
            try:
                callback(entry)
            except Exception:
                self._subscriber_failures += 1  # observable, never recursive
        return entry.as_dict()

    def info(self, source: str, message: str, **ctx: Any) -> Dict[str, Any]:
        return self.record("INFO", source, message, **ctx)

    def warning(self, source: str, message: str, **ctx: Any) -> Dict[str, Any]:
        return self.record("WARNING", source, message, **ctx)

    def error(self, source: str, message: str, **ctx: Any) -> Dict[str, Any]:
        return self.record("ERROR", source, message, **ctx)

    # ------------------------------------------------------------- reading
    def tail(
        self,
        n: int = 100,
        *,
        account: Optional[str] = None,
        plugin: Optional[str] = None,
        severity: Optional[str] = None,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Most recent entries (oldest first) after applying the filters."""
        with self._lock:
            items = list(self._ring)
        if severity:
            severity = severity.upper()
            items = [e for e in items if e.severity == severity]
        if account:
            items = [e for e in items if e.account == account]
        if plugin:
            items = [e for e in items if e.plugin == plugin]
        if source:
            items = [e for e in items if e.source == source]
        return [e.as_dict() for e in items[-max(0, n):]] if n > 0 else []

    def subscribe(self, callback: Callable[[LogEntry], None]) -> Callable[[], None]:
        """Live-tail subscription (dashboard WS); returns unsubscribe."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def stats(self) -> Dict[str, Any]:
        """Counters by severity + drop/failure counters for the dashboard."""
        with self._lock:
            counts = dict(self._counts)
            db_failures = self._db_failures
            subscriber_failures = self._subscriber_failures
        return {
            "entries": sum(counts.values()),
            "by_severity": counts,
            "db_failures": db_failures,
            "subscriber_failures": subscriber_failures,
        }


__all__ = ["LogService", "LogEntry", "ServiceLogger", "StdlibBridge", "attach_stdlib_bridge", "SEVERITIES", "configure_stderr"]


class ServiceLogger:
    """Stdlib-logger-shaped adapter over LogService (shared convention shim).

    Managers historically mixed two logger conventions: stdlib style
    (``info("fmt %s", arg)``, ``exception(...)``) and LogService style
    (``info(source, message)``). This adapter accepts the stdlib call shape
    while ALSO surviving LogService-style calls; every method is
    exception-proof (logging must never break a manager). Written by agent A7
    for the simulation runner and promoted to the spine at integration.
    """

    def __init__(self, service: "LogService", source: str) -> None:
        self._service = service
        self._source = source
        self.name = f"slaughtermudae.{source}"

    def _record(self, severity: str, msg: Any, args: tuple) -> None:
        try:
            message = self._format(msg, args)
            if severity == "error" and sys.exc_info()[0] is not None:
                # stdlib .exception() contract: append the active exception
                message = f"{message} [{self._exc_one_liner()}]"
            self._service.record(severity.upper(), self._source, message)
        except Exception:
            pass  # logging must never break the caller

    @staticmethod
    def _format(msg: Any, args: tuple) -> str:
        """Render (msg, args) for either logger convention, never raising."""
        text = str(msg)
        if not args:
            return text
        try:
            return text % args
        except (TypeError, ValueError):
            # LogService-style call (source, message) or mismatched args:
            # keep every piece of information in one readable line
            return f"{text} {' '.join(str(a) for a in args)}"

    @staticmethod
    def _exc_one_liner() -> str:
        exc_type, exc_value, _ = sys.exc_info()
        if exc_type is None:
            return ""
        return f"{exc_type.__name__}: {exc_value}"

    # ------------------------------------------------- stdlib logger surface
    def debug(self, msg: Any, *args: Any) -> None:
        self._record("debug", msg, args)

    def info(self, msg: Any, *args: Any) -> None:
        self._record("info", msg, args)

    def warning(self, msg: Any, *args: Any) -> None:
        self._record("warning", msg, args)

    def warn(self, msg: Any, *args: Any) -> None:  # legacy alias
        self._record("warning", msg, args)

    def error(self, msg: Any, *args: Any) -> None:
        self._record("error", msg, args)

    def exception(self, msg: Any, *args: Any) -> None:
        self._record("error", msg, args)

    def log(self, level: int, msg: Any, *args: Any) -> None:
        severity = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR",
                    50: "ERROR"}.get(level, "INFO")
        self._record(severity.lower(), msg, args)

    # ---------------------------------------------- LogService-style surface
    def record(self, severity: str, source: str, message: str, **ctx: Any) -> dict:
        """LogService-style call passes through tagged with THIS source."""
        return self._service.record(severity, self._source, message, **ctx)



class StdlibBridge(logging.Handler):
    """Bridges stdlib logger records into the structured LogService.

    Plugin contexts (and any third-party code) log through ordinary stdlib
    loggers under the ``slaughtermudae.*`` tree; this handler forwards those
    records into LogService so the dashboard Logs page sees EVERYTHING from
    one place (spec §10) without changing plugin call sites.
    """

    def __init__(self, service: "LogService") -> None:
        super().__init__()
        self._service = service

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "_sm_from_service", False):
                # this record IS LogService's own stdlib mirror — the entry
                # already exists; re-ingesting it would re-mirror it and
                # recurse (found live: every boot log line echoed ~143x,
                # bounded only by the recursion limit)
                return
            name = record.name or "stdlib"
            if name.startswith("slaughtermudae"):
                source = name[len("slaughtermudae"):].lstrip(".") or "core"
                source = source.replace(".", ":")
            else:
                source = f"ext:{name}"
            severity = _SEVERITY_BY_LEVELNAME.get(
                record.levelname,
                "ERROR" if record.levelno >= logging.ERROR
                else "INFO" if record.levelno >= logging.INFO else "DEBUG",
            )
            # INBOUND path: suppress the outbound mirror for this record on
            # this thread (its stderr print already happened at the original
            # stdlib call; re-mirroring double-prints and re-enters the tree)
            with self._service.ingest_context():
                self._service.record(severity, source, record.getMessage())
        except Exception:  # a logging bridge must never raise
            pass


def attach_stdlib_bridge(
    service: "LogService", logger_name: str = "slaughtermudae"
) -> StdlibBridge:
    """Attach (idempotently) the stdlib->structured bridge; returns the handler.

    Also ensures the tree accepts INFO records even when configure_stderr()
    was never called (headless/sim boots) — otherwise raw plugin-context
    INFO logs are filtered at the logger level before reaching the bridge.
    """
    root = logging.getLogger(logger_name)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # quiet-mode hole: configure_stderr() sets propagate=False, but builds
    # with quiet=True never call it — without this, mirrors and raw WARNING+
    # records leak to the global root (lastResort prints them raw)
    root.propagate = False
    for handler in root.handlers:
        if isinstance(handler, StdlibBridge):
            return handler
    bridge = StdlibBridge(service)
    root.addHandler(bridge)
    return bridge


def detach_stdlib_bridge(logger_name: str = "slaughtermudae") -> int:
    """Remove every StdlibBridge from the tree (shutdown/teardown).

    Returns how many were removed. After this, raw stdlib records under the
    tree print only (no structured ingestion) — exactly the pre-attach
    behavior. Idempotent; safe to call from Framework.shutdown BEFORE
    database.close() so the bridge stops feeding a dying sink.
    """
    root = logging.getLogger(logger_name)
    bridges = [h for h in root.handlers if isinstance(h, StdlibBridge)]
    for bridge in bridges:
        root.removeHandler(bridge)
    return len(bridges)
