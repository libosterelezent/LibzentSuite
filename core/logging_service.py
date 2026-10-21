"""Centralized logging service (spec §10).

Every component (core, account, plugin, scheduler, ratelimiter, adapter)
records STRUCTURED entries through LogService instead of scattering print()
calls or raw logger text. Each entry carries:

    timestamp, severity, source, account, plugin, task_id, event, message

Consumers:
  * stderr mirror (human-readable, one line per record),
  * the dashboard Logs page (live tail over WebSocket — no text parsing),
  * the database `logs` table (bounded, best-effort — via an ASYNC drain
    worker fed by a thread-safe queue, spec §29: the record path NEVER
    touches SQLite synchronously).

Persistence model (async DB layer): ``record()`` only appends to the ring,
bumps counters, mirrors to stdlib, notifies subscribers and ENQUEUES a dict
on a ``queue.SimpleQueue`` (thread-safe; callable from any thread, never
awaits, never blocks). ``await start_persistence(repo)`` spawns one asyncio
drain worker that awaits ``repo.add(entry)`` per dequeued item; failures are
counted in ``stats()['db_failures']`` and never raise (one raw stderr
one-liner per failure burst at most). The queue is capped at
``_MAX_INFLIGHT`` in-flight items — beyond that records are DROPPED from
persistence (ring/counters unaffected) and counted in ``stats()['dropped']``.

STARTUP ORDER: ``await start_persistence(...)`` BEFORE heavy logging begins
(early records otherwise never persist). SHUTDOWN ORDER: ``await
stop_persistence(...)`` BEFORE ``await database.close()`` so the drain
finishes against a live connection. With no persistence started, record()
does zero extra work and never warns (simulator/headless boots).
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import logging.handlers
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Any, Callable, Deque, Dict, List, Optional

from ._persist_queue import PersistQueue

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR")
#: per-entry message cap (ring memory + DB row size stay bounded)
_MESSAGE_CAP = 16_000
_LEVEL_BY_SEVERITY = {
    "DEBUG": logging.DEBUG, "INFO": logging.INFO,
    "WARNING": logging.WARNING, "ERROR": logging.ERROR,
}
_STDERR_CONFIGURED = False

#: NON-BLOCKING stderr pipeline (configure_stderr): the record() mirror
#: and every raw stdlib emit under the tree hand records to a QueueHandler
#: (put_nowait only) and ONE listener thread owns the real StreamHandler —
#: a StreamHandler.emit writes AND FLUSHES synchronously, so attached
#: directly it let a backed-up stderr pipe stall the event loop thread
#: that called record()/logger.log().
_STDERR_QUEUE: "queue.SimpleQueue[logging.LogRecord]" = queue.SimpleQueue()
_STDERR_PIPELINE_LOCK = threading.Lock()
#: live framework CLAIMS on the process-global pipeline (review pass-3
#: #1): exclusive ownership let the FIRST build's shutdown stop the
#: pipeline while a second, live framework was still using it — the
#: listener now stops only when the LAST claim releases.
_STDERR_USERS = 0
_STDERR_USERS_LOCK = threading.Lock()
_STDERR_LISTENER: Optional[logging.handlers.QueueListener] = None
_STDERR_QUEUE_HANDLER: Optional[logging.handlers.QueueHandler] = None
_STDERR_ATEXIT_ARMED = False


def _start_stderr_pipeline() -> None:
    """Create + start the one listener thread and attach its queue handler
    to the ``libzent`` logger (idempotent; lock-guarded)."""
    global _STDERR_LISTENER, _STDERR_QUEUE_HANDLER, _STDERR_ATEXIT_ARMED
    with _STDERR_PIPELINE_LOCK:
        if _STDERR_QUEUE_HANDLER is not None:
            return
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        _STDERR_QUEUE_HANDLER = logging.handlers.QueueHandler(_STDERR_QUEUE)
        _STDERR_LISTENER = logging.handlers.QueueListener(
            _STDERR_QUEUE, handler, respect_handler_level=True)
        _STDERR_LISTENER.start()
        if not _STDERR_ATEXIT_ARMED:
            # abnormal exits (no stop_persistence pass) must not hang on
            # the listener's non-daemon thread — registered ONCE
            _STDERR_ATEXIT_ARMED = True
            atexit.register(stop_stderr_pipeline)


def stderr_pipeline_active() -> bool:
    """True while the process-global stderr listener is running."""
    return _STDERR_LISTENER is not None


def claim_stderr_pipeline() -> bool:
    """Register one framework user of the process-global pipeline.

    Returns True when the CALLER created the pipeline (atomic with the
    configuration). Every successful build claims — borrower or creator
    — and must release exactly once (shutdown or build rollback)."""
    global _STDERR_USERS
    with _STDERR_USERS_LOCK:
        created = not stderr_pipeline_active()
        if created:
            configure_stderr()
        _STDERR_USERS += 1
        return created


def release_stderr_pipeline() -> None:
    """Release one user's claim; the LAST release stops the pipeline.

    Idempotent per caller by contract (a framework releases once); a
    stray extra release is a no-op so a failed build's rollback can
    never silence a live framework."""
    global _STDERR_USERS
    with _STDERR_USERS_LOCK:
        if _STDERR_USERS > 0:
            _STDERR_USERS -= 1
        if _STDERR_USERS == 0 and stderr_pipeline_active():
            stop_stderr_pipeline()


def stop_stderr_pipeline() -> None:
    """Stop the stderr listener thread and detach its queue handler.

    Idempotent and leak-free across repeated start/stop: the handler is
    removed from the logger (post-stop records stop queueing) and the
    listener is drained + joined. Called from :meth:`LogService.stop_persistence`
    (the service shutdown seam) and at process exit.
    """
    global _STDERR_LISTENER, _STDERR_QUEUE_HANDLER, _STDERR_CONFIGURED
    with _STDERR_PIPELINE_LOCK:
        listener, qhandler = _STDERR_LISTENER, _STDERR_QUEUE_HANDLER
        _STDERR_LISTENER = _STDERR_QUEUE_HANDLER = None
        # allow configure_stderr() to re-arm later (boot -> sim -> boot
        # cycles left the flag latched and the second arm was a no-op)
        _STDERR_CONFIGURED = False
    if qhandler is not None:
        logging.getLogger("libzent").removeHandler(qhandler)
    if listener is not None:
        listener.stop()  # flushes what already queued, joins the thread

#: logger name -> the propagate value observed when the FIRST bridge was
#: attached (detach restores it once the last bridge handler is removed —
#: attach used to flip propagate=False permanently)
_BRIDGE_PRIOR_PROPAGATE: Dict[str, bool] = {}

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
        # explicit dict: dataclasses.asdict() deep-copies recursively and is
        # ~3x slower on this hot path (every record + every queued row)
        return {
            "ts": round(self.ts, 3), "severity": self.severity,
            "source": self.source, "message": self.message,
            "account": self.account, "plugin": self.plugin,
            "task_id": self.task_id, "event": self.event,
        }


def configure_stderr(level: int = logging.INFO) -> None:
    """Attach one human-readable stderr handler to the logger tree (idempotent).

    The StreamHandler runs on a QueueListener thread behind a
    QueueHandler: callers (including the record() mirror on the event
    loop) only enqueue — a blocked stderr pipe can never stall them.
    """
    global _STDERR_CONFIGURED
    if _STDERR_CONFIGURED:
        return
    root = logging.getLogger("libzent")
    root.setLevel(level)
    _start_stderr_pipeline()
    root.addHandler(_STDERR_QUEUE_HANDLER)
    root.propagate = False
    _STDERR_CONFIGURED = True


class LogService:
    """Ring buffer + live subscribers + queued-async-DB sink for logs.

    Lifecycle (spec §29 — application/event paths never touch SQLite):

        service = LogService()
        service.attach_db(repo)                  # configuration only
        await service.start_persistence(repo)    # BEFORE heavy logging
        ...
        await service.stop_persistence()         # BEFORE db.close()
    """

    def __init__(self, capacity: int = 4000) -> None:
        self._ring: Deque[LogEntry] = deque(maxlen=capacity)
        self._subscribers: List[Callable[[LogEntry], None]] = []
        self._lock = threading.Lock()
        self._db = None  # LogRepository (duck-typed) — CONFIG ONLY until start_persistence
        self._counts: Dict[str, int] = {sev: 0 for sev in SEVERITIES}
        # OBSERVABILITY (dead sinks must not be silent): drop/failure counters
        self._subscriber_failures = 0
        # QUEUED PERSISTENCE: producers (any thread) put; ONE async drain
        # worker (event loop) consumes — shared engine, see _persist_queue
        self._persist = PersistQueue("log")
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
        """Configure the persistence repository — CONFIGURATION ONLY.

        No write happens until ``await start_persistence(...)`` spawns the
        drain worker; ``attach_db(None)`` just clears the configuration
        (stop_persistence() is the runtime off-switch).
        """
        self._db = repository

    # ------------------------------------------------- queued persistence
    async def start_persistence(
        self, repo: Any, *, batch: int = 50, interval: float = 0.25
    ) -> Optional["asyncio.Task"]:
        """Spawn the async drain worker (call BEFORE heavy logging).

        The worker drains the queue in batches (``repo.add_many`` when the
        repository has it — one transaction per batch — else ``repo.add``
        per item) and sleeps ``interval`` seconds when idle. Failures are
        counted in ``stats()['db_failures']`` — never raised, never logged
        through LogService (one raw stderr one-liner per failure burst).

        Idempotent: when a worker is already alive it is returned as-is
        (stop_persistence() first to restart with a different repo/batch).
        """
        if repo is None:
            return self._persist._worker
        self._db = repo
        return await self._persist.start(repo, batch=batch, interval=interval)

    async def stop_persistence(self, timeout: float = 5.0) -> None:
        """Stop the drain worker (idempotent); call BEFORE ``db.close()``.

        Records stop being enqueued, the in-flight batch finishes (bounded
        join), a stuck worker is cancelled after the grace window and the
        remaining queue is drained inline in order (bounded by ``timeout``
        — a hung repo cannot wedge shutdown). This is also the service's
        shutdown seam for the stderr listener thread (Framework.shutdown
        calls it before database.close), so it stops cleanly here too.
        """
        await self._persist.stop(timeout)
        # NOTE: stop_stderr_pipeline is deliberately NOT called here —
        # the pipeline is process-global (one module-level listener for
        # every LogService in the process); stopping it per-service let
        # simulator teardown silence the PRODUCTION console. Framework
        # .shutdown owns the one true stop.

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

        FULLY non-blocking (spec §29): ring append + counters + guarded
        stdlib mirror + subscriber dispatch + queue ENQUEUE when a drain
        worker is live. No DB call, no await, never raises — logging must
        not break the caller's flow. Without start_persistence() the queue
        is simply unused (zero extra work, no warnings).
        """
        try:  # never raises (contract): a broken __str__/severity degrades
            severity = str(severity or "INFO").upper()
            message = str(message)
        except Exception:
            try:
                source_text = str(source)
            except Exception:
                source_text = "<unloggable source>"
            return {"ts": time.time(), "severity": "INFO", "source": source_text,
                    "message": "<unloggable message>", "account": account,
                    "plugin": plugin, "task_id": task_id, "event": event}
        if severity not in SEVERITIES:
            # fail LOUD: a crash-level report ("CRITICAL", "FATAL", a
            # typo'd severity from a caller) downgraded to INFO was
            # invisible to ERROR-filtered tails
            severity = "ERROR"
        if len(message) > _MESSAGE_CAP:  # ring + DB rows bounded per entry
            message = message[:_MESSAGE_CAP] + f"…(+{len(message) - _MESSAGE_CAP} chars)"
        with self._lock:
            # ts captured INSIDE the lock: ring position and timestamp are
            # serialized together, so concurrent record() calls can never
            # land out-of-order timestamps in the ring (dashboard tail order)
            entry = LogEntry(
                ts=time.time(), severity=severity, source=source,
                message=message, account=account, plugin=plugin,
                task_id=task_id, event=event,
            )
            self._ring.append(entry)
            self._counts[severity] = self._counts.get(severity, 0) + 1
        # OUTBOUND mirror — only for records NOT ingested from stdlib on
        # this thread (a bridge-ingested record already printed to stderr
        # via the original stdlib call; mirroring it back double-prints).
        # The _sm_from_service marker below is defense-in-depth: even if the
        # mirror somehow reaches a bridge, it is skipped (the original loop
        # echoed every line ~recursion-limit times into ring AND DB).
        if not getattr(self._ingest, "active", False):
            logging.getLogger(f"libzent.{source}").log(
                _LEVEL_BY_SEVERITY[severity], "%s", entry.message,
                extra={"_sm_from_service": True},
            )
        payload = entry.as_dict()  # built ONCE (was 2x per record)
        if self._persist.active or self._persist.stopped:
            # active: enqueue for the worker. stopped (was started, then
            # stopped): still put — the queue counts the drop so post-stop
            # losses are visible in stats()['dropped'], not silent. A sink
            # that was never started stays fully inert (sim/headless boots).
            self._persist.put(payload)
        if self._subscribers:  # no per-record copy when nobody listens
            for callback in list(self._subscribers):
                try:
                    callback(entry)
                except Exception:
                    with self._lock:  # record() runs on many threads
                        self._subscriber_failures += 1  # observable, never recursive
        return payload

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
        if n <= 0:
            return []
        severity = severity.upper() if severity else None
        with self._lock:
            ring = list(self._ring)  # cheap snapshot; filtering runs unlocked
        out: List[LogEntry] = []
        for entry in reversed(ring):  # newest first: stop once n are found
            if severity and entry.severity != severity:
                continue
            if account and entry.account != account:
                continue
            if plugin and entry.plugin != plugin:
                continue
            if source and entry.source != source:
                continue
            out.append(entry)
            if len(out) >= n:
                break
        out.reverse()
        return [e.as_dict() for e in out]

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
            subscriber_failures = self._subscriber_failures
        pstats = self._persist.stats()
        return {
            "entries": sum(counts.values()),
            "by_severity": counts,
            "db_failures": pstats["db_failures"],
            "subscriber_failures": subscriber_failures,
            "dropped": pstats["dropped"],
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
        self.name = f"libzent.{source}"

    def _record(self, severity: str, msg: Any, args: tuple,
                with_location: bool = False, **ctx: Any) -> None:
        try:
            message = self._format(msg, args)
            if severity == "error" and sys.exc_info()[0] is not None:
                # stdlib .exception() contract: append the active exception
                message = f"{message} [{self._exc_one_liner(with_location)}]"
            self._service.record(severity.upper(), self._source, message, **ctx)
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
    def _exc_one_liner(with_location: bool = False) -> str:
        """``Type: message`` (+ ``@ file.py:LINE in func`` of the innermost
        frame for .exception(): the one-liner alone hides WHERE it broke)."""
        exc_type, exc_value, tb = sys.exc_info()
        if exc_type is None:
            return ""
        text = f"{exc_type.__name__}: {exc_value}"
        if with_location and tb is not None:
            while tb.tb_next is not None:
                tb = tb.tb_next
            code = tb.tb_frame.f_code
            text += (f" @ {code.co_filename.rsplit('/', 1)[-1]}"
                     f":{tb.tb_lineno} in {code.co_name}")
        return text

    # ---------------------------------------------- stdlib logger surface
    # K12 (S6-C5/S9-C3): **ctx is accepted on the stdlib-shaped surface and
    # forwarded — RateLimitManager._log passes account= context this way, and
    # a TypeError here was silently swallowed, dropping the safety-critical
    # per-account 429 cooldown warnings from ring/dashboard/DB entirely.
    def debug(self, msg: Any, *args: Any, **ctx: Any) -> None:
        self._record("debug", msg, args, **ctx)

    def info(self, msg: Any, *args: Any, **ctx: Any) -> None:
        self._record("info", msg, args, **ctx)

    def warning(self, msg: Any, *args: Any, **ctx: Any) -> None:
        self._record("warning", msg, args, **ctx)

    def warn(self, msg: Any, *args: Any, **ctx: Any) -> None:  # legacy alias
        self._record("warning", msg, args, **ctx)

    def error(self, msg: Any, *args: Any, **ctx: Any) -> None:
        self._record("error", msg, args, **ctx)

    def exception(self, msg: Any, *args: Any, **ctx: Any) -> None:
        self._record("error", msg, args, **ctx, with_location=True)

    def log(self, level: int, msg: Any, *args: Any, **ctx: Any) -> None:
        severity = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR",
                    50: "ERROR"}.get(level, "INFO")
        self._record(severity.lower(), msg, args, **ctx)

    # ---------------------------------------------- LogService-style surface
    def record(self, severity: str, source: str, message: str, **ctx: Any) -> dict:
        """LogService-style call passes through tagged with THIS source."""
        return self._service.record(severity, self._source, message, **ctx)



class StdlibBridge(logging.Handler):
    """Bridges stdlib logger records into the structured LogService.

    Plugin contexts (and any third-party code) log through ordinary stdlib
    loggers under the ``libzent.*`` tree; this handler forwards those
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
            if name.startswith("libzent"):
                source = name[len("libzent"):].lstrip(".") or "core"
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
            # FINAL-PHASE (G3): structured context passthrough — extra=
            # account/plugin fields ride the LogRecord; today no bridge
            # caller sets them (attribution flows via the logger NAME),
            # but future context-based loggers get correct entry fields
            # without another bridge change.
            with self._service.ingest_context():
                message = record.getMessage()
                if record.exc_info:
                    # exception tracebacks must reach the ring/DB/dashboard
                    # too — the message alone hides WHERE it broke
                    try:
                        message = (message + "\n"
                                   + logging.Formatter().formatException(
                                       record.exc_info))
                    except Exception:
                        pass  # a logging bridge must never raise
                self._service.record(
                    severity, source, message,
                    account=getattr(record, "account", None),
                    plugin=getattr(record, "plugin", None))
        except Exception:  # a logging bridge must never raise
            pass


def attach_stdlib_bridge(
    service: "LogService", logger_name: str = "libzent"
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
    if logger_name not in _BRIDGE_PRIOR_PROPAGATE:
        # remember the pre-attach value ONCE (a second attach of a stale
        # bridge must not overwrite it); detach restores it when the last
        # bridge goes away
        _BRIDGE_PRIOR_PROPAGATE[logger_name] = root.propagate
    root.propagate = False
    for handler in root.handlers:
        if isinstance(handler, StdlibBridge):
            return handler
    bridge = StdlibBridge(service)
    root.addHandler(bridge)
    return bridge


def detach_stdlib_bridge(logger_name: str = "libzent") -> int:
    """Remove every StdlibBridge from the tree (shutdown/teardown).

    Returns how many were removed. After this, raw stdlib records under the
    tree print only (no structured ingestion) — exactly the pre-attach
    behavior, INCLUDING the ``propagate`` flag attach had pinned to False
    (restored from the value observed at first attach). Idempotent; safe to
    call from Framework.shutdown BEFORE database.close() so the bridge
    stops feeding a dying sink.
    """
    root = logging.getLogger(logger_name)
    bridges = [h for h in root.handlers if isinstance(h, StdlibBridge)]
    for bridge in bridges:
        root.removeHandler(bridge)
    if bridges and not any(
        isinstance(h, StdlibBridge) for h in root.handlers
    ):
        prior = _BRIDGE_PRIOR_PROPAGATE.pop(logger_name, None)
        if prior is not None:
            root.propagate = prior
    return len(bridges)
