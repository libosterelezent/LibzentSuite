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
from typing import Any, Callable, Deque, Dict, List, Optional

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR")
_LEVEL_BY_SEVERITY = {
    "DEBUG": logging.DEBUG, "INFO": logging.INFO,
    "WARNING": logging.WARNING, "ERROR": logging.ERROR,
}
_STDERR_CONFIGURED = False


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
        logging.getLogger(f"slaughtermudae.{source}").log(
            _LEVEL_BY_SEVERITY[severity], "%s", entry.message
        )
        if self._db is not None:
            try:
                self._db.add(entry.as_dict())
            except Exception:  # pragma: no cover - DB must never break logging
                pass
        for callback in list(self._subscribers):
            try:
                callback(entry)
            except Exception:  # pragma: no cover - subscriber isolation
                pass
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
        """Counters by severity for the dashboard overview."""
        with self._lock:
            counts = dict(self._counts)
        return {"entries": sum(counts.values()), "by_severity": counts}


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
        severity = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR"}.get(level, "INFO")
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
            name = record.name or "stdlib"
            if name.startswith("slaughtermudae"):
                source = name[len("slaughtermudae"):].lstrip(".") or "core"
                source = source.replace(".", ":")
            else:
                source = f"ext:{name}"
            severity = record.levelname
            if severity not in SEVERITIES:
                severity = "INFO"
            self._service.record(severity, source, record.getMessage())
        except Exception:  # a logging bridge must never raise
            pass


def attach_stdlib_bridge(
    service: "LogService", logger_name: str = "slaughtermudae"
) -> StdlibBridge:
    """Attach (idempotently) the stdlib->structured bridge; returns the handler."""
    root = logging.getLogger(logger_name)
    for handler in root.handlers:
        if isinstance(handler, StdlibBridge):
            return handler
    bridge = StdlibBridge(service)
    root.addHandler(bridge)
    return bridge
