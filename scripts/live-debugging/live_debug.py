"""LiveDebug — the whole-suite LIVE reconnaissance harness.

A human-supervised pass over the REAL LibzentSuite stack (the exact
``core.bootstrap.build`` path of ``main.py``) with only the Discord
boundary replaced by the offline simulator: no network, no real
tokens, no second stack. Its job is to drive every feature area in
ordered PHASES while an anomaly-capture layer watches alongside:

  * LogService.subscribe — every structured row is classified
    (ERROR → major; contract-suspicious WARNING → minor/observation),
  * AuditRecorder.subscribe — a bounded audit tail provides evidence
    context for findings,
  * loop.set_exception_handler — "Task exception was never retrieved"
    wreckage becomes a CRITICAL finding instead of stderr noise,
  * phase expectations — every step states its expectation; a mismatch
    is a finding with expectation/actual/repro.

Output streams (one console, visually separated):
  * the PHASE feed — ``phase``/``step``/``PASS`` lines, and
  * the ANOMALY feed — ``[ANOMALY]``-prefixed lines, also appended to
    the findings file. ``--watch`` in a second terminal tails the
    findings file live.

Findings land in ``scripts/live-debugging/findings/<run id>.jsonl``
(one JSON object per anomaly, schema pinned by tests) plus a run
summary ``<run id>.summary.json`` (PASS lines live there, never in
the findings file). The id is collision-resistant and shared by both
files.

ISOLATION (AGENTS.md §7): the harness NEVER touches ``state/`` — it
boots inside a disposable ``tempfile.mkdtemp`` workdir (database,
credential key, staged plugins) removed at exit unless
``--keep-workdir``. Tokens are synthetic values only.

Usage:
  python3 scripts/live-debugging/live_debug.py             # full pass
  python3 scripts/live-debugging/live_debug.py --list      # phase table
  python3 scripts/live-debugging/live_debug.py --phase cog-commands
  python3 scripts/live-debugging/live_debug.py --watch     # 2nd terminal
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

# the repository root must be importable BEFORE the first repository
# import below (core.errors): direct invocation
# ``python scripts/live-debugging/live_debug.py`` from any working
# directory puts only this file's directory on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.errors import AccountError, UnsafePolicyError  # noqa: E402  # needs the sys.path setup above
from core.workspace.runtime import ChainRuntime  # noqa: E402  # definition validation before persist

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence

    from core.logging_service import LogEntry

TOOLKIT_DIR = Path(__file__).resolve().parent
TOOLKIT_PLUGINS = TOOLKIT_DIR / "plugins"
DEFAULT_FINDINGS_DIR = TOOLKIT_DIR / "findings"
SCENARIO_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "sim_plugins"

# --- synthetic world topology (mirrors the repo's sim conventions) ------
OWNER_ID = 1001
PRIMARY_BOT_ID = 999_999
ALPHA_ID = 2001
BETA_ID = 2002
GAMMA_ID = 2003
GUILD_CHANNEL = 500
OPS_CHANNEL = 501

#: first-boot config: message pacing at the 1.5s floor, near-zero jitter
#: (deterministic-ish timing for the pacing assertions)
FAST_POLICIES = {
    "global": {
        "message": {"interval": 1.5, "jitter": [0.0, 0.05]},
        "dm": {"interval": 1.5, "jitter": [0.0, 0.05]},
    },
    "accounts": {},
}
FAST_SETTINGS = {"ldp_worker": {"burst": 2}}

# --- bounded-wait budgets (every timing wait is explicit) ---------------
#: idle seconds that count as "the world went quiet"
QUIET_WINDOW_S = 2.2
#: hard cap for one world-quiet wait
QUIET_LIMIT_S = 30.0
#: the reconciler sweeps every 30s — the signal-delivery wait allows one
#: full period plus margin
RECONCILER_WAIT_S = 45.0
#: hard cap for one simulator scenario driven by the harness
SCENARIO_BUDGET_S = 120.0
#: poll cadence for predicate waits
POLL_S = 0.1

#: evidence lines kept per finding (log excerpt + audit-tail context)
EVIDENCE_MAX = 12
#: audit rows kept in the live tail (evidence context)
AUDIT_TAIL_MAX = 48
#: findings file naming (the --watch tail targets the newest match)
_RUN_FILE_PATTERN = re.compile(r"^run-.*\.jsonl$")

# ----------------------------------------------------------------- findings
#: severity ranking (worst first); the findings file stays anomalies-only
SEVERITIES = ("critical", "major", "minor", "observation")

#: WARNING rows matching one of these read like a violated contract (the
#: documented vocabulary of the framework's own failure paths) — they
#: become findings instead of console noise
_CONTRACT_WARNING_KEYWORDS = (
    "failed",
    "could not",
    "cannot",
    "error",
    "leak",
    "leaked",
    "orphan",
    "stale",
    "ignored",
    "skipping",
    "clamped",
    "insecure",
    "deprecated",
    "dropped",
    "dead",
    "unwired",
    "not wired",
    "mismatch",
    "abort",
    "thrash",
    "swallow",
    "re-created",
    "mid-",
    "never retrieved",
)


#: phases whose scenarios deliberately stop/estop accounts — the estop
#: disconnect echo is only "deliberate" while one of them is running
_DISCONNECT_PHASES = frozenset({"accounts", "shutdown"})


def _message_account(message: str) -> str | None:
    """The ``account='<name>'`` field of a lifecycle row, if present."""
    match = re.search(r"account='([^']+)'", message)
    return match.group(1) if match else None


def _deliberate_row(
    severity: str,
    source: str,
    message: str,
    event: str | None,
    phase: str | None = None,
    expected_disconnects: frozenset[str] = frozenset(),
    account: str | None = None,
) -> bool:
    """True for known-DELIBERATE loud rows — operator actions and staged
    failure paths that log WARNING/ERROR on purpose.

    The old substring lists matched the words ANYWHERE, so an unrelated
    error mentioning "emergency stop" sailed through as an exit-0
    observation. The deliberate rows are manager rows WITHOUT a bus
    event (the estop announcement, the scheduler's cancellation
    receipts, the plugins' flow-abort notices), so the discriminator is
    the exact source plus a specific message shape; ``event`` names the
    structured bus event when the row carries one, and estop-driven
    lifecycle events are matched by name + message instead of a bare
    substring.
    """
    lowered = message.lower()
    # the EXPECTED estop echo: the AccountDisconnected lifecycle record
    # from the events source — but ONLY for the ACCOUNT the harness is
    # deliberately stopping, while a disconnect-exercising phase runs
    # (accounts' estop/stop/remove, the ordered shutdown). A phase-wide
    # downgrade masked a disconnect of an account no harness action can
    # explain (PR review: scope to the expected account). The account
    # comes from the STRUCTURED LogEntry field first (production rows
    # carry it there), with the message field as the legacy fallback.
    # AccountFailed is NEVER expected (a real failure).
    row_account = account if account is not None else _message_account(message)
    if (
        event == "AccountDisconnected"
        and source == "events"
        and phase in _DISCONNECT_PHASES
        and row_account in expected_disconnects
    ):
        return True
    if severity == "ERROR":
        # the account manager's estop announcement is ERROR-loud BY
        # DESIGN: exact source + exact prefix, nothing broader
        return source == "accounts" and message.startswith("EMERGENCY STOP ")
    # WARNING: the scheduler's cancellation receipts (tasks the operator
    # cancelled on purpose) and the documented flow-abort notices for
    # vanished targets (one clean line, by the plugin contract)
    if source == "scheduler" and "was cancelled" in lowered:
        return True
    return "flow aborted" in lowered or "targetgone" in lowered.replace(" ", "")


def classify_log_entry(
    severity: str,
    source: str,
    message: str,
    event: str | None = None,
    phase: str | None = None,
    expected_disconnects: frozenset[str] = frozenset(),
    account: str | None = None,
) -> str | None:
    """Map one structured log row to a finding severity (None = not one).

    ERROR is always at least major unless the row is a KNOWN deliberate
    one (matched exactly — see _deliberate_row). WARNING rows become an
    observation when deliberate, minor when the message reads like a
    contract violation (the keyword list), and nothing at all when it is
    ordinary operational chatter. INFO/DEBUG rows are never findings.
    ``phase``, ``expected_disconnects`` and ``account`` scope the
    phase-dependent deliberate rules (the estop disconnect echo) to
    phases that actually disconnect, the accounts they named, and the
    row's structured account field.
    """
    sev = (severity or "").upper()
    if sev not in ("ERROR", "WARNING"):
        return None
    if _deliberate_row(sev, source, message, event, phase, expected_disconnects, account):
        return "observation"
    if sev == "ERROR":
        return "major"
    text = f"{source} {message}".lower()
    if any(word in text for word in _CONTRACT_WARNING_KEYWORDS):
        return "minor"
    return None


def format_log_row(severity: str, source: str, message: str) -> str:
    """One evidence line shaped like the simulator's spec-§9 rows."""
    return f"{(severity or 'INFO').upper():<7} {source} {message}".rstrip()


@dataclass(frozen=True)
class Finding:
    """One captured anomaly — the JSONL schema (pinned by unit tests)."""

    id: str
    ts: float
    phase: str
    area: str
    severity: str
    summary: str
    evidence: list[str]
    expectation: str
    actual: str
    repro: str

    def as_dict(self) -> dict[str, Any]:
        """The exact persisted shape — one JSON object per line."""
        return {
            "id": self.id,
            "ts": round(self.ts, 3),
            "phase": self.phase,
            "area": self.area,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "expectation": self.expectation,
            "actual": self.actual,
            "repro": self.repro,
        }


def make_finding(
    seq: int,
    stamp: str,
    *,
    phase: str,
    area: str,
    severity: str,
    summary: str,
    evidence: Sequence[str],
    expectation: str,
    actual: str,
    repro: str,
) -> Finding:
    """Validate-and-build (bad severities fail loudly, not silently)."""
    if severity not in SEVERITIES:
        raise ValueError(f"unknown finding severity {severity!r} (known: {SEVERITIES})")
    return Finding(
        id=f"{stamp}-{seq:04d}",
        ts=time.time(),
        phase=phase,
        area=area,
        severity=severity,
        summary=summary,
        evidence=[str(line) for line in evidence][:EVIDENCE_MAX],
        expectation=expectation,
        actual=actual,
        repro=repro,
    )


class FindingsWriter:
    """Append-only JSONL findings file + in-memory counters.

    The file is created EXCLUSIVELY (mode "x"): a run-id collision
    raises instead of silently truncating another run's evidence.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._handle = path.open("x", encoding="utf-8")
        self._counts = dict.fromkeys(SEVERITIES, 0)
        self._total = 0

    def write(self, finding: Finding) -> None:
        self._handle.write(json.dumps(finding.as_dict(), ensure_ascii=False) + "\n")
        self._handle.flush()
        self._counts[finding.severity] += 1
        self._total += 1

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def total(self) -> int:
        return self._total

    def close(self) -> None:
        self._handle.close()


@dataclass
class PhaseOutcome:
    """Run-summary entry for one phase (PASS lines live here, not in
    the findings file — that one stays anomalies-only)."""

    name: str
    area: str
    steps_passed: int = 0
    steps_failed: int = 0
    duration_s: float = 0.0


class Console:
    """The two visually separated output feeds (one stdout).

    Phase progress prints indented, quiet lines; anomalies print with
    the ``[ANOMALY]`` prefix so a supervising human can tell the
    streams apart at a glance. ``--watch`` mirrors the anomaly feed in
    a second terminal.
    """

    def __init__(self, quiet: bool = False) -> None:
        self._quiet = quiet

    def banner(self, text: str) -> None:
        print(f"\n=== {text} ===", flush=True)

    def phase(self, text: str) -> None:
        print(f"\n--- phase: {text} ---", flush=True)

    def line(self, text: str) -> None:
        if not self._quiet:
            print(f"    {text}", flush=True)

    def passed(self, text: str) -> None:
        print(f"  [PASS] {text}", flush=True)

    def failed(self, text: str) -> None:
        print(f"  [FAIL] {text}", flush=True)

    def anomaly(self, text: str) -> None:
        print(f"[ANOMALY] {text}", flush=True)


class Harness:
    """One live boot of the REAL stack over the simulator, wrapped in
    the anomaly-capture layer.

    The stack is composed by ``core.bootstrap.build`` — the same boot
    path ``main.py`` uses — with the Discord boundary swapped for
    ``SimClient`` over a shared ``SimWorld`` (the pattern the repo's
    own full-stack integration tests pin). Everything else (managers,
    scheduler, limiter, database, audit, credentials, workspace
    service, reconciler, chain runtime) is production code.
    """

    def __init__(
        self,
        findings: FindingsWriter,
        console: Console,
        *,
        run_id: str,
        keep_workdir: bool = False,
    ) -> None:
        self._findings = findings
        self._console = console
        self._keep_workdir = keep_workdir
        #: the run's collision-resistant id — shared by the findings
        #: file, the summary file, and every finding id
        self._stamp = run_id
        self._seq = 0
        self._phase = "pre-boot"
        self._area = "framework"
        self.steps_passed = 0
        self.steps_failed = 0
        self._audit_tail: deque[str] = deque(maxlen=AUDIT_TAIL_MAX)
        self._unsub_log: Callable[[], None] | None = None
        self._unsub_audit: Callable[[], None] | None = None
        self._prior_handler: Callable[[asyncio.AbstractEventLoop, dict[str, Any]], Any] | None = (
            None
        )
        # armed only by _arm_loop_handler — teardown restores the loop
        # handler iff this harness actually replaced it
        self._handler_installed = False
        # LZ_MASTER_KEY is cleared for the run and restored at teardown:
        # an ambient operator key would make CredentialService skip the
        # workdir key file entirely (breaking the key-sidecar
        # expectation) AND encrypt the synthetic tokens under the
        # operator's real key material (PR review 2026-11-21)
        self._prior_master_key: str | None = os.environ.pop("LZ_MASTER_KEY", None)
        # accounts a phase is DELIBERATELY stopping — the estop-echo
        # downgrade applies to these only, never the whole phase (PR
        # review: scope expected disconnects to the account being
        # stopped)
        self._expected_disconnects: set[str] = set()
        self._torn_down = False
        # framework handles (set by boot)
        self.workdir: Path | None = None
        self.world: Any = None
        self.framework: Any = None

    # ------------------------------------------------------------- property
    @property
    def api(self) -> Any:
        if self.framework is None:
            raise RuntimeError("harness has not booted (framework is None)")
        return self.framework.api

    # ----------------------------------------------------------------- boot
    async def boot(self) -> None:
        """Stage the disposable workdir and build the real stack."""
        from adapters.simulator.client import SimClient
        from adapters.simulator.world import SimWorld
        from core.bootstrap import build
        from core.credentials import Credentials
        from database.db import Database

        self.workdir = Path(tempfile.mkdtemp(prefix="libzent-live-debug-"))
        config_dir = self.workdir / "config"
        (config_dir / "accounts").mkdir(parents=True)
        (config_dir / "global_settings.json").write_text(json.dumps(FAST_SETTINGS))
        (config_dir / "rate_limits.json").write_text(json.dumps(FAST_POLICIES))
        env_file = self.workdir / "synthetic.env"
        env_file.write_text(
            f"DISCORD_TOKEN_ALPHA=sim-token-alpha\n"
            f"DISCORD_TOKEN_BETA=sim-token-beta\n"
            f"DISCORD_TOKEN_GAMMA=sim-token-gamma\n"
            f"OWNER_USER_IDS={OWNER_ID}\n"
            f"PRIMARY_BOT_ID={PRIMARY_BOT_ID}\n"
        )
        # 0600: the boot-time security audit warns on group/world-readable
        # credential files, and the backfill now SURFACES that warning
        env_file.chmod(0o600)
        self._stage_plugins(self.workdir / "plugins")

        self.world = SimWorld(responder_id=PRIMARY_BOT_ID, reply_delay=0.01)
        for user_id, name in (
            (OWNER_ID, "owner"),
            (PRIMARY_BOT_ID, "bot"),
            (ALPHA_ID, "alpha"),
            (BETA_ID, "beta"),
            (GAMMA_ID, "gamma"),
        ):
            self.world.add_user(user_id, name)
        self.world.add_channel(GUILD_CHANNEL, "grind-channel")
        self.world.add_channel(OPS_CHANNEL, "ops-channel")
        user_ids = {"alpha": ALPHA_ID, "beta": BETA_ID, "gamma": GAMMA_ID}

        def client_factory(name: str, token: str) -> SimClient:
            # deterministic per-account latency seed
            return SimClient(self.world, user_ids[name], seed=user_ids[name])

        credentials = Credentials.from_values(
            {
                "PRIMARY_BOT_ID": str(PRIMARY_BOT_ID),
                "OWNER_USER_IDS": str(OWNER_ID),
                "DISCORD_TOKEN_ALPHA": "sim-token-alpha",
                "DISCORD_TOKEN_BETA": "sim-token-beta",
                "DISCORD_TOKEN_GAMMA": "sim-token-gamma",
            }
        )
        database = Database(str(self.workdir / "libzent.db"))
        # ARMED BEFORE BUILD: the loop exception handler catches
        # unretrieved task exceptions raised while booting — even when
        # the boot still SUCCEEDS. The log/audit subscribers attach
        # after (the services must exist), and the rows bootstrap
        # already emitted are backfilled below.
        self._arm_loop_handler()
        self.framework = await build(
            config_dir=str(config_dir),
            plugin_dir=str(self.workdir / "plugins"),
            client_factory=client_factory,
            credentials=credentials,
            database=database,
            # db_path locates the SIDE-CARS (plugin-data.db,
            # credentials.key) next to the workdir database — passing
            # only ``database=`` left them on build()'s default
            # state/ path, breaking the harness's isolation promise
            # (PR review 2026-11-18)
            db_path=str(self.workdir / "libzent.db"),
            quiet=True,
            env_file=str(env_file),
        )
        self._install_capture()
        await self._replay_bootstrap_rows()

    @staticmethod
    def _stage_plugins(target: Path) -> None:
        """Copy the real plugin tree + the toolkit's fixtures into the
        workdir (discovery then sees ping + ldp_worker; production
        trees are only ever read)."""
        target.mkdir(parents=True, exist_ok=True)
        for source_root in (REPO_ROOT / "plugins", TOOLKIT_PLUGINS):
            for child in sorted(source_root.iterdir()):
                if child.name.startswith("_") or not child.is_dir():
                    continue
                shutil.copytree(child, target / child.name, dirs_exist_ok=True)

    # ------------------------------------------------------------- teardown
    async def teardown(self) -> None:
        """Framework.shutdown + capture detach + workdir removal.

        Idempotent, and CANCELLATION-PROOF: a CancelledError landing
        inside ``framework.shutdown`` still detaches the capture and
        removes the workdir (the sync cleanup runs in finally), then
        propagates — the cancellation is honored, never swallowed.
        """
        if self._torn_down:
            return
        self._torn_down = True
        try:
            if self.framework is not None:
                # standalone phases never reach phase_shutdown — when
                # THIS teardown performs the shutdown, its cleanup
                # failures must not escape as a green exit (PR #27
                # round 7 review). phase_shutdown already asserted on
                # its own call; the idempotent second shutdown here
                # re-records nothing, so skip when it already ran.
                performed_shutdown = not getattr(self.framework, "_shut_down", False)
                try:
                    await self.framework.shutdown()
                except asyncio.CancelledError:
                    # cleanup below runs in the shared finally; the
                    # cancellation itself re-raises after it
                    self._console.line("teardown cancelled mid-shutdown — cleanup continues")
                    raise
                except Exception as exc:  # teardown must still finish
                    self.report(
                        severity="major",
                        summary=f"framework.shutdown raised {type(exc).__name__}",
                        evidence=[repr(exc)],
                        expectation="graceful ordered teardown (AGENTS bootstrap contract)",
                        actual=f"{type(exc).__name__}: {exc}",
                        repro="shutdown",
                    )
                else:
                    failures = list(getattr(self.framework, "shutdown_failures", None) or [])
                    if performed_shutdown and failures:
                        self.report(
                            severity="major",
                            summary=f"implicit shutdown recorded {len(failures)} cleanup failure(s)",
                            evidence=failures[:3],
                            expectation="every shutdown cleanup step succeeds (phase_shutdown contract)",
                            actual=f"{len(failures)} failure(s): {failures[:3]}",
                            repro="teardown (phase reached no explicit shutdown)",
                        )
        finally:
            self._detach_capture()
            self._remove_workdir()
            # the operator's ambient master key returns with us (it was
            # never the harness's to consume)
            if self._prior_master_key is not None:
                os.environ["LZ_MASTER_KEY"] = self._prior_master_key

    def _remove_workdir(self) -> None:
        """Delete the disposable workdir (kept with --keep-workdir)."""
        if self.workdir is not None and not self._keep_workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)

    # ------------------------------------------------------ anomaly capture
    def mark_disconnect_expected(self, *accounts: str) -> None:
        """Name the accounts a phase is about to stop deliberately —
        their AccountDisconnected echo classifies as an observation;
        every other account's disconnect stays a finding."""
        self._expected_disconnects.update(accounts)

    def _arm_loop_handler(self) -> None:
        """Install the loop exception handler (unhandled task wreckage
        becomes a CRITICAL finding instead of a stderr print). Runs
        BEFORE build so bootstrap-time wreckage is captured too."""
        loop = asyncio.get_running_loop()
        self._prior_handler = loop.get_exception_handler()
        self._handler_installed = True

        def on_loop_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            message = str(context.get("message") or "unhandled loop exception")
            exc = context.get("exception")
            detail = f"{type(exc).__name__}: {exc}" if exc is not None else ""
            task = context.get("task") or context.get("future")
            task_name = getattr(task, "get_name", lambda: "?")()
            self.report(
                severity="critical",
                summary=f"unhandled asyncio exception ({message})",
                evidence=[line for line in (message, detail, f"task={task_name}") if line],
                expectation=(
                    "every framework task retrieves or handles its exception "
                    "(no 'Task exception was never retrieved' wreckage)"
                ),
                actual=(detail or message) + f" [task={task_name}]",
                repro=f"{self._phase}/loop-exception",
            )

        loop.set_exception_handler(on_loop_exception)

    async def _replay_bootstrap_rows(self) -> None:
        """Backfill: rows the bootstrap itself emitted BEFORE the
        subscribers could attach are still in the ring — classify them
        so a SUCCESSFUL boot's anomalies become findings too."""
        subscribed_at = time.time()
        for row in await self.api.logs_tail(n=400):
            if float(row.get("ts") or 0) >= subscribed_at:
                continue  # live capture owns these; never double-report
            verdict = classify_log_entry(
                str(row.get("severity") or ""),
                str(row.get("source") or ""),
                str(row.get("message") or ""),
                str(row.get("event") or "") or None,
                phase="boot",  # boot never disconnects: no echo here is expected
            )
            if verdict is not None:
                self.report(
                    severity=verdict,
                    summary=f"bootstrap row: {str(row.get('message'))[:160]}",
                    evidence=[
                        format_log_row(
                            str(row.get("severity") or ""),
                            str(row.get("source") or ""),
                            str(row.get("message") or ""),
                        )
                    ],
                    expectation="a successful bootstrap emits no ERROR rows or contract warnings",
                    actual=format_log_row(
                        str(row.get("severity") or ""),
                        str(row.get("source") or ""),
                        str(row.get("message") or ""),
                    ),
                    repro="boot/bootstrap-backfill",
                )
        # seed the audit evidence tail with what boot already recorded
        for row in await self.api.audit_tail(n=AUDIT_TAIL_MAX):
            self._audit_tail.append(
                " ".join(f"{key}={value}" for key, value in sorted(row.items()))
            )

    def _install_capture(self) -> None:
        """Wire the parallel anomaly window's subscribers: the log
        classifier and the audit evidence tail (the loop exception
        handler is armed separately, before build)."""

        def on_log(entry: LogEntry) -> None:
            severity = getattr(entry, "severity", "INFO")
            source = str(getattr(entry, "source", "?"))
            message = str(getattr(entry, "message", ""))
            event = str(getattr(entry, "event", "") or "") or None
            account = getattr(entry, "account", None)
            verdict = classify_log_entry(
                severity,
                source,
                message,
                event,
                phase=self._phase,
                expected_disconnects=frozenset(self._expected_disconnects),
                account=account if isinstance(account, str) else None,
            )
            if verdict is None:
                return
            self.report(
                severity=verdict,
                summary=f"{severity} log row from {source}: {message[:160]}",
                evidence=[format_log_row(severity, source, message)],
                expectation=(
                    "the feature contract holds with no ERROR rows and no "
                    "contract-violating warnings (docs/architecture/*.rst)"
                ),
                actual=format_log_row(severity, source, message),
                repro=f"{self._phase}/log-capture",
            )

        def on_audit(entry: Any) -> None:
            as_dict = getattr(entry, "as_dict", None)
            data: dict[str, Any] = {}
            if callable(as_dict):
                with contextlib.suppress(Exception):
                    data = dict(as_dict())
            elif isinstance(entry, dict):
                data = dict(entry)
            self._audit_tail.append(
                " ".join(f"{key}={value}" for key, value in sorted(data.items()))
            )

        self._unsub_log = self.framework.logs.subscribe(on_log)
        audit = self.framework.audit
        if audit is not None and hasattr(audit, "subscribe"):
            self._unsub_audit = audit.subscribe(on_audit)

    def _detach_capture(self) -> None:
        for unsub in (self._unsub_log, self._unsub_audit):
            if unsub is not None:
                with contextlib.suppress(Exception):
                    unsub()
        self._unsub_log = self._unsub_audit = None
        # restore the loop handler ONLY if this harness installed one —
        # a boot that failed before _arm_loop_handler leaves
        # _prior_handler unset, and clearing unconditionally destroyed
        # the CALLER's handler
        if self._handler_installed:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_event_loop_policy().get_event_loop()
                if self._prior_handler is not None:
                    loop.set_exception_handler(self._prior_handler)
                else:
                    loop.set_exception_handler(None)
            self._handler_installed = False

    # -------------------------------------------------------------- report
    def report(
        self,
        *,
        severity: str,
        summary: str,
        evidence: Sequence[str],
        expectation: str,
        actual: str,
        repro: str,
    ) -> Finding:
        """Record one finding: JSONL line + console anomaly line."""
        self._seq += 1
        finding = make_finding(
            self._seq,
            self._stamp,
            phase=self._phase,
            area=self._area,
            severity=severity,
            summary=summary,
            evidence=[*evidence, *list(self._audit_tail)[-3:]],
            expectation=expectation,
            actual=actual,
            repro=repro,
        )
        self._findings.write(finding)
        self._console.anomaly(f"{finding.id} [{severity}] {summary}")
        return finding

    def observation(self, summary: str, *, expectation: str, actual: str, repro: str) -> None:
        """A non-defect behavior worth keeping (contract gap, cosmetic
        drift, known limitation) — severity 'observation'."""
        self.report(
            severity="observation",
            summary=summary,
            evidence=[],
            expectation=expectation,
            actual=actual,
            repro=repro,
        )

    # ------------------------------------------------------- step plumbing
    def set_phase(self, name: str, area: str) -> None:
        self._phase = name
        self._area = area

    async def step(self, name: str, action: Callable[[], Awaitable[Any]]) -> Any:
        """Run one announced step; exceptions become findings (the phase
        keeps going — the human watches the anomaly feed)."""
        self._console.line(f"step: {name}")
        try:
            return await action()
        except Exception as exc:
            self.steps_failed += 1
            self._console.failed(f"{name} raised {type(exc).__name__}: {exc}")
            tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
            self.report(
                severity="major",
                summary=f"step {name!r} raised {type(exc).__name__}: {exc}",
                evidence=[line.rstrip() for line in tb[-4:]],
                expectation="the step completes against the documented contract",
                actual=f"{type(exc).__name__}: {exc}",
                repro=f"{self._phase}/{name}",
            )

    async def step_raises(
        self, name: str, expected: tuple[type[Exception], ...], action: Callable[[], Awaitable[Any]]
    ) -> bool:
        """Run one step EXPECTING the operation's DECLARED guard exception
        (the typo traps and safety guards): PASS only when THAT exception
        raises — an unrelated failure (say, a persistence RuntimeError
        during a below-floor check) stays on the failure path."""
        self._console.line(f"step: {name} (expects {expected[0].__name__})")
        try:
            result = await action()
        except expected as exc:
            self.steps_passed += 1
            self._console.passed(f"{name} raised {type(exc).__name__} as designed")
            return True
        except Exception as exc:
            self.steps_failed += 1
            self._console.failed(f"{name} raised unexpected {type(exc).__name__}: {exc}")
            self.report(
                severity="major",
                summary=f"step {name!r} raised {type(exc).__name__} (expected {expected})",
                evidence=[repr(exc)],
                expectation=f"one of {expected}",
                actual=f"{type(exc).__name__}: {exc}",
                repro=f"{self._phase}/{name}",
            )
            return False
        self.steps_failed += 1
        self._console.failed(f"{name} returned {result!r} instead of raising")
        self.report(
            severity="major",
            summary=f"step {name!r} failed to raise (guard did not fire)",
            evidence=[],
            expectation=f"one of {expected}",
            actual=f"returned {result!r}",
            repro=f"{self._phase}/{name}",
        )
        return False

    def expect(self, ok: bool, *, what: str, expectation: str, actual: str) -> bool:
        """Record one PASS/FAIL expectation check (FAIL → finding)."""
        if ok:
            self.steps_passed += 1
            self._console.passed(f"{what} — {actual}")
            return True
        self.steps_failed += 1
        self._console.failed(f"{what} — expected {expectation!r}, saw {actual!r}")
        self.report(
            severity="major",
            summary=f"expectation not met: {what}",
            evidence=[],
            expectation=expectation,
            actual=actual,
            repro=f"{self._phase}/{what}",
        )
        return False

    # ------------------------------------------------------------- helpers
    async def wait_until(
        self,
        predicate: Callable[[], Awaitable[bool]],
        *,
        timeout: float,
        description: str,
    ) -> bool:
        """Bounded poll (every wait in the harness is explicit)."""
        deadline = time.monotonic() + timeout
        while True:
            if await predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL_S)

    async def scan_for_secret(self, tokens: Sequence[str]) -> list[str]:
        """Where the given secret values surface: EVERY field of every
        structured log row and every audit row (the message alone was
        not enough — structured context and audit content could carry a
        leak the old probes missed). Returns location pointers like
        ``log[12].account`` / ``audit[3].content`` — never the token
        itself, so a finding cannot become the leak it reports."""
        hits: list[str] = []
        for index, row in enumerate(await self.api.logs_tail(n=400)):
            for field, value in row.items():
                if any(token in str(value) for token in tokens):
                    hits.append(f"log[{index}].{field}")
        for index, row in enumerate(await self.api.audit_tail(n=400)):
            for field, value in row.items():
                if any(token in str(value) for token in tokens):
                    hits.append(f"audit[{index}].{field}")
        return hits

    async def wait_quiet(
        self, channel: int = GUILD_CHANNEL, *, limit: float = QUIET_LIMIT_S
    ) -> None:
        """Wait for the world to go quiet (bounded hard cap)."""
        await self.world.wait_for_quiet(channel, quiet=QUIET_WINDOW_S, limit=limit)

    def owner_says(self, content: str, *, channel: int = GUILD_CHANNEL) -> None:
        """The owner speaks in a guild channel (drives the event path)."""
        self.world.deliver(channel, OWNER_ID, content)

    async def audit_out_rows(self, *, action: str = "message") -> list[dict[str, Any]]:
        rows = await self.api.audit_tail(n=200)
        return [r for r in rows if r.get("direction") == "out" and r.get("action") == action]

    async def account_state(self, name: str) -> str:
        """The account lifecycle state (accounts_info status is a dict
        when connected, a string otherwise — both shapes parsed)."""
        info = await self.api.accounts_info(name)
        if not isinstance(info, dict):
            return ""
        status = info.get("status")
        if isinstance(status, dict):
            return str(status.get("state") or "")
        return str(status or "")

    async def ensure_account(self, name: str = "alpha", plugins: list[str] | None = None) -> None:
        """Idempotent: the account exists, has the plugins, and runs
        (standalone ``--phase`` runs need their prerequisites)."""
        known = {a.get("name") for a in await self.api.accounts_list()}
        if name not in known:
            await self.api.accounts_add(
                {"name": name, "credential": f"sim-token-{name}", "plugins": plugins or []}
            )
        assigned = set(await self.api.accounts_plugins_list(name))
        for plugin in plugins or []:
            if plugin not in assigned:
                await self.api.accounts_plugin_assign(name, plugin)
        if await self.account_state(name) != "running":
            await self.api.accounts_start(name)


# ------------------------------------------------------------------- phases
async def phase_boot(h: Harness) -> None:
    """The real bootstrap over the simulator: discovery + status."""
    manifests = await h.step("plugin manifests", h.api.plugins_manifests)
    names = sorted(m.get("name") for m in manifests or [])
    h.expect(
        names == ["ldp_follower", "ldp_worker", "ping"],
        what="discovered plugins (production ping + toolkit fixtures)",
        expectation="['ldp_follower', 'ldp_worker', 'ping']",
        actual=str(names),
    )
    # side-car isolation (PR review 2026-11-18): plugin-data.db and
    # credentials.key resolve from db_path's parent — with only
    # ``database=`` passed they landed on the DEFAULT state/ path. The
    # store's resolved path is the live proof the workdir owns them.
    plugin_data_path = str(Path(h.framework.plugin_data.path).resolve())
    workdir_path = str(h.workdir.resolve())
    h.expect(
        plugin_data_path.startswith(workdir_path + os.sep),
        what="plugin-data side-car lives inside the harness workdir",
        expectation=f"{workdir_path}{os.sep}plugin-data.db",
        actual=plugin_data_path,
    )
    status = await h.step("status snapshot", h.api.status)
    ok = isinstance(status, dict) and status.get("uptime_s", -1) >= 0
    h.expect(
        ok,
        what="status() returns a live snapshot",
        expectation="dict with uptime",
        actual=str(type(status)),
    )

    import_status = await h.step(
        "synthetic env import (migration verb, nothing registered yet)",
        h.api.credentials_import_env,
    )
    h.expect(
        isinstance(import_status, dict),
        what="credentials_import_env returns the migration report",
        expectation="EnvImportReport dict",
        actual=str(import_status),
    )
    # the synthetic tokens must never surface anywhere structured: every
    # field of every log row AND every audit row. `== []` (not `not
    # leaks`): a FAILED scan step returns None, and `not None` printed
    # PASS on the run that never verified anything.
    synthetic_tokens = ("sim-token-alpha", "sim-token-beta", "sim-token-gamma")
    leaks = await h.step(
        "full-field secret scan (logs + audit)",
        lambda: h.scan_for_secret(synthetic_tokens),
    )
    h.expect(
        leaks == [],
        what="no credential material in any structured field",
        expectation="tokens never appear in any log/audit field",
        actual=f"leaking fields: {leaks or None}",
    )


async def phase_accounts(h: Harness) -> None:
    """Accounts add/credential/start/estop/stop/remove end to end."""
    out = await h.step(
        "add alpha (ping + ldp_worker, Fernet-stored credential)",
        lambda: h.api.accounts_add(
            {
                "name": "alpha",
                "credential": "sim-token-alpha",
                "plugins": ["ping", "ldp_worker"],
            }
        ),
    )
    h.expect(
        isinstance(out, dict) and out.get("name") == "alpha",
        what="accounts_add alpha",
        expectation="echoed account row",
        actual=str(out),
    )
    cred_state = (out or {}).get("credential")
    h.expect(
        cred_state in ("db", "env", "configured", "memory"),
        what="credential stored through the encrypted service",
        expectation="accounts_add echoes a credential state",
        actual=f"credential={cred_state!r}",
    )

    await h.step_raises(
        "duplicate add is refused",
        (AccountError,),
        lambda: h.api.accounts_add({"name": "alpha"}),
    )
    started = await h.step("start alpha", lambda: h.api.accounts_start("alpha"))
    alpha_state = await h.account_state("alpha")
    h.expect(
        alpha_state == "running",
        what="alpha reaches running",
        expectation="status.state == running",
        actual=f"state={alpha_state!r}",
    )
    h.expect(
        isinstance(started, dict),
        what="accounts_start returns a report",
        expectation="dict report",
        actual=str(started),
    )

    await h.step(
        "add gamma (scratch)",
        lambda: h.api.accounts_add({"name": "gamma", "credential": "sim-token-gamma"}),
    )
    await h.step("start gamma", lambda: h.api.accounts_start("gamma"))
    # mid-estop bookkeeping first: gamma's disconnect echo is the
    # EXPECTED one (an observation); any other account's stays major
    h.mark_disconnect_expected("gamma")
    estop = await h.step("emergency-stop gamma", lambda: h.api.accounts_emergency_stop("gamma"))
    h.expect(
        isinstance(estop, dict) and estop.get("stop_initiated") is True,
        what="account-scoped estop initiates the stop",
        expectation="stop_initiated=True (disconnect is async by contract)",
        actual=str(estop),
    )
    # the estop stop is deliberately fire-and-forget: wait (bounded) for
    # the teardown to land before removing the account
    gamma_down = await h.wait_until(
        lambda: _state_is_not(h, "gamma", "running"),
        timeout=10.0,
        description="gamma teardown lands after estop",
    )
    gamma_state = await h.account_state("gamma")
    h.expect(
        gamma_down,
        what="gamma is not running after estop",
        expectation="state leaves running within 10s (async disconnect)",
        actual=f"state={gamma_state!r}",
    )
    removed = await h.step("remove gamma (cascade)", lambda: h.api.accounts_remove("gamma"))
    h.expect(
        bool(isinstance(removed, dict) and removed.get("removed")),
        what="remove cascades (credential + cooldowns + access)",
        expectation="removed=True",
        actual=str(removed),
    )
    names_after = [a.get("name") for a in await h.step("accounts_list", h.api.accounts_list)]
    h.expect(
        "gamma" not in names_after,
        what="gamma gone from accounts_list",
        expectation="no gamma",
        actual=str(names_after),
    )


async def phase_credentials(h: Harness) -> None:
    """Credential lifecycle: Fernet store, status transitions, cascade."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    rows = await h.step("credential status", h.api.accounts_credential_status)
    alpha_row = next((r for r in rows or [] if r.get("name") == "alpha"), None)
    h.expect(
        alpha_row is not None and alpha_row.get("credential") == "db",
        what="alpha credential resolves through the encrypted store",
        expectation="credential state db (env no longer exists at runtime)",
        actual=str(alpha_row),
    )
    # the Fernet key is a side-car too (PR review 2026-11-18): it must
    # have been minted next to the workdir database — a key in the
    # live state/ would mean every harness run encrypted its synthetic
    # tokens under the owner's real key material
    h.expect(
        (h.workdir / "credentials.key").is_file(),
        what="credential key lives inside the harness workdir",
        expectation=f"{h.workdir}{os.sep}credentials.key exists",
        actual=f"exists={(h.workdir / 'credentials.key').is_file()}",
    )
    set_again = await h.step(
        "re-store alpha credential (rotation)",
        lambda: h.api.accounts_set_credential("alpha", "sim-token-alpha-2"),
    )
    h.expect(
        isinstance(set_again, dict),
        what="credential rotation accepted",
        expectation="dict echo",
        actual=str(set_again),
    )
    env_import = await h.step(
        "migration import over the env view (overwrite)",
        lambda: h.api.credentials_import_env(overwrite=True),
    )
    h.expect(
        isinstance(env_import, dict) and "alpha" in (env_import.get("imported") or []),
        what="import-env re-imports the synthetic alpha token",
        expectation="alpha listed in imported",
        actual=str(env_import),
    )
    # queued persistence drains asynchronously (§29) — bounded wait for
    # the mgmt rows to land before reading history back
    await h.wait_until(
        lambda: _has_mgmt_credential_rows(h), timeout=5.0, description="mgmt:credential rows drain"
    )
    audit = await h.api.audit_tail(n=200, account="alpha")
    mgmt = [r for r in audit if str(r.get("action", "")).startswith("mgmt:credential")]
    h.expect(
        bool(mgmt),
        what="credential operations leave management audit rows",
        expectation="mgmt:credential.* rows present",
        actual=f"{len(mgmt)} row(s)",
    )
    # the rotated token must never surface in any structured field
    rotated_leaks = await h.step(
        "full-field secret scan (rotated token)",
        lambda: h.scan_for_secret(("sim-token-alpha-2",)),
    )
    h.expect(
        rotated_leaks == [],  # None (failed scan) must not read as clean
        what="rotated token absent from every log/audit field",
        expectation="no leak",
        actual=f"leaking fields: {rotated_leaks or None}",
    )


async def phase_plugins(h: Harness) -> None:
    """Plugin assignment + enable/disable on a running account."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    assigned = await h.step("accounts_plugins_list", lambda: h.api.accounts_plugins_list("alpha"))
    h.expect(
        sorted(assigned or []) == ["ldp_worker", "ping"],
        what="assignment set readback",
        expectation="[ldp_worker, ping]",
        actual=str(assigned),
    )
    await h.step_raises(
        "unknown plugin assignment is refused",
        (ValueError,),
        lambda: h.api.accounts_plugin_assign("alpha", "no-such-plugin"),
    )
    state = await h.step("plugins_state", lambda: h.api.plugins_state("alpha"))
    enabled_by_name = {row.get("name"): row.get("enabled") for row in state or []}
    h.expect(
        enabled_by_name.get("ldp_worker") is True and enabled_by_name.get("ping") is True,
        what="both plugins enabled by default",
        expectation="enabled=True for both",
        actual=str(enabled_by_name),
    )
    await h.step(
        "disable ldp_worker", lambda: h.api.plugins_set_enabled("alpha", "ldp_worker", False)
    )
    state_after = await h.step("plugins_state after disable", lambda: h.api.plugins_state("alpha"))
    disabled = {row.get("name"): row.get("enabled") for row in state_after or []}
    h.expect(
        disabled.get("ldp_worker") is False,
        what="disable takes effect",
        expectation="enabled=False",
        actual=str(disabled),
    )
    await h.step(
        "re-enable ldp_worker", lambda: h.api.plugins_set_enabled("alpha", "ldp_worker", True)
    )
    unassigned = await h.step(
        "unassign + reassign ldp_worker live",
        lambda: h.api.accounts_plugin_unassign("alpha", "ldp_worker"),
    )
    h.expect(
        isinstance(unassigned, dict) and unassigned.get("existed") is True,
        what="unassign reports pre-state",
        expectation="existed=True",
        actual=str(unassigned),
    )
    await h.step("reassign ldp_worker", lambda: h.api.accounts_plugin_assign("alpha", "ldp_worker"))
    final = await h.step("final assignment set", lambda: h.api.accounts_plugins_list("alpha"))
    h.expect(
        sorted(final or []) == ["ldp_worker", "ping"],
        what="assignment restored",
        expectation="both assigned",
        actual=str(final),
    )


async def phase_profiles(h: Harness) -> None:
    """Account plugin profiles: create/update/activate/clear + settings."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    created = await h.step(
        "create profile 'slow' for ldp_worker",
        lambda: h.api.account_plugin_profile_create(
            "alpha", "ldp_worker", "slow", settings={"burst": 1}
        ),
    )
    h.expect(
        isinstance(created, dict) and created.get("name") == "slow",
        what="profile created",
        expectation="echoed profile row",
        actual=str(created),
    )
    updated = await h.step(
        "update profile settings",
        lambda: h.api.account_plugin_profile_update(
            "alpha", "ldp_worker", "slow", settings={"burst": 3}
        ),
    )
    h.expect(
        isinstance(updated, dict),
        what="profile updated",
        expectation="echoed row",
        actual=str(updated),
    )
    await h.step(
        "set profile active",
        lambda: h.api.account_plugin_profile_set_active("alpha", "ldp_worker", "slow"),
    )
    active = await h.step(
        "active profile readback",
        lambda: h.api.account_plugin_profile_active("alpha", "ldp_worker"),
    )
    h.expect(
        isinstance(active, dict) and active.get("active") == "slow",
        what="active profile resolves",
        expectation="active == slow",
        actual=str(active),
    )
    # the active pointer feeds the RUNTIME seam (P4: plugins resolve
    # `await ctx.services.profile_settings()` before their work), not the
    # merged effective view
    services = h.framework.accounts.services_for("alpha", "ldp_worker")
    body = await h.step(
        "plugin-side profile_settings() resolution",
        lambda: services.profile_settings(),
    )
    h.expect(
        isinstance(body, dict) and body.get("burst") == 3,
        what="the active profile's body resolves through the services seam",
        expectation="burst == 3 (the active profile's settings)",
        actual=str(body),
    )
    h.observation(
        "accounts_effective_settings does not reflect the ACTIVE profile "
        "pointer — the merged view reads the assignment-row binding only; "
        "the active pointer reaches plugins solely through "
        "ctx.services.profile_settings() (the P4 two-level seam)",
        expectation="one profile mechanism visible from every settings surface",
        actual="binding (effective view + refresh_settings) vs active pointer (services seam)",
        repro="profiles/two-mechanism-seam",
    )
    await h.step(
        "clear active profile",
        lambda: h.api.account_plugin_profile_clear_active("alpha", "ldp_worker"),
    )
    body_after_clear = await h.step(
        "plugin-side profile_settings() after clear",
        lambda: services.profile_settings(),
    )
    h.expect(
        body_after_clear is None,
        what="cleared profile stops overriding",
        expectation="profile_settings() returns None (no active profile)",
        actual=str(body_after_clear),
    )
    deleted = await h.step(
        "delete profile",
        lambda: h.api.account_plugin_profile_delete("alpha", "ldp_worker", "slow"),
    )
    h.expect(
        isinstance(deleted, dict),
        what="profile deleted",
        expectation="echoed row",
        actual=str(deleted),
    )


async def phase_cog_commands(h: Harness) -> None:
    """The COG command face: real library dispatch for ``<prefix>ping``.

    The sim boundary has no library Bot (the live adapter's client IS
    one), so the harness hosts the ping cog the way the account
    manager's reconcile does — ``plugins.host_cog`` with the REAL
    per-(account, plugin) paced facade — then drives the command
    through ``Command.invoke`` with a typed context (the repo's
    established cog-driving pattern; tests/unit/test_cog_host.py).
    The ``ctx.services`` lift mirrors the adapter's before_invoke.
    """
    from types import SimpleNamespace as NS

    from discord.ext import commands
    from discord.ext.commands.view import StringView

    await h.ensure_account("alpha", ["ping"])
    bot = commands.Bot(command_prefix="!", user_bot=True, owner_ids={OWNER_ID}, help_command=None)
    paced = h.framework.accounts.services_for("alpha", "ping")
    cog = await h.step(
        "host ping cog on a library bot",
        lambda: h.framework.plugins.host_cog("alpha", "ping", bot, paced=paced),
    )
    command = bot.get_command("ping")
    h.expect(
        cog is not None and command is not None,
        what="cog hosted + command registered",
        expectation="PingCog on the bot with command 'ping'",
        actual=f"cog={cog!r} command={command!r}",
    )
    assert command is not None  # narrowed for the typed closures below

    replies: list[str] = []

    async def send(text: str, **kwargs: Any) -> None:
        replies.append(str(text))

    def make_ctx(content: str) -> Any:
        view = StringView(content)
        view.skip_string("!ping")
        ctx = NS(
            bot=bot,
            prefix="!",
            invoked_with="ping",
            command=command,
            message=NS(attachments=[], author=NS(id=ALPHA_ID, bot=False)),
            view=view,
            send=send,
        )
        ctx.services = paced  # the adapter's before_invoke lift, mirrored
        return ctx

    async def invoke(content: str) -> None:
        await command.invoke(make_ctx(content))

    before = len(h.world.messages_of(GUILD_CHANNEL))
    await h.step(
        "invoke '!ping 500 hello live-debug'", lambda: invoke("!ping 500 hello live-debug")
    )
    await h.wait_quiet()
    landed = [m for m in h.world.messages_of(GUILD_CHANNEL) if "hello live-debug" in m]
    h.expect(
        len(landed) == 1,
        what="paced send reached the world channel",
        expectation="exactly one delivered 'hello live-debug' message",
        actual=f"{len(landed)} message(s) (channel had {before} before)",
    )
    h.expect(
        bool(replies) and replies[-1].startswith("sent to"),
        what="command replies through ctx.send",
        expectation="reply 'sent to <channel label>'",
        actual=str(replies[-1] if replies else None),
    )
    outs = await h.audit_out_rows()
    h.expect(
        bool(outs),
        what="paced send left an outbound audit row",
        expectation="direction=out action=message row",
        actual=f"{len(outs)} row(s)",
    )

    await h.step(
        "invoke '!ping' against a vanished channel (FlowBlocked path)",
        lambda: invoke("!ping 424242 into the void"),
    )
    h.expect(
        any("unusable" in r or "blocked" in r for r in replies),
        what="gone channel aborts with one clean reply",
        expectation="channel unusable / send blocked reply",
        actual=str(replies),
    )


async def phase_tasks_scheduler(h: Harness) -> None:
    """Task pipeline: queued/running/history + mid-flight cancellation."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    h.owner_says("ldp-go 3")
    await h.wait_quiet(limit=25.0)
    ticks = [m for m in h.world.messages_of(GUILD_CHANNEL) if m.startswith("ldp tick")]
    h.expect(
        len(ticks) == 3,
        what="owner trigger produced the full paced burst",
        expectation="3 'ldp tick' messages through the pipeline",
        actual=f"{len(ticks)} message(s)",
    )
    history = await h.step("tasks_history", lambda: h.api.tasks_history(limit=20))
    completed = [row for row in history or [] if row.get("status") == "completed"]
    h.expect(
        len(completed) >= 3,
        what="completed tasks persisted in history",
        expectation=">= 3 completed rows",
        actual=f"{len(completed)} completed of {len(history or [])}",
    )
    listed = await h.step("tasks_list", lambda: h.api.tasks_list())
    h.expect(
        isinstance(listed, list),
        what="tasks_list readable while idle",
        expectation="list (may be empty)",
        actual=f"{len(listed)} row(s)",
    )

    # mid-flight cancellation: start a burst, cancel the tail
    h.owner_says("ldp-go 5")
    await asyncio.sleep(1.8)  # the first paced send lands; the rest queue
    cancelled = await h.step(
        "cancel_matching while the burst is queued",
        lambda: h.api.tasks_cancel_matching(account="alpha", plugin="ldp_worker"),
    )
    n_cancelled = (cancelled or {}).get("cancelled", 0)
    await h.wait_quiet(limit=15.0)
    history_after = await h.step(
        "tasks_history after cancel", lambda: h.api.tasks_history(limit=30)
    )
    cancelled_rows = [row for row in history_after or [] if row.get("status") == "cancelled"]
    h.expect(
        n_cancelled >= 1 and n_cancelled == len(cancelled_rows),
        what="cancelled tasks are counted and persisted",
        expectation="verb count EQUALS the persisted cancelled rows (a count or persistence regression breaks the equality)",
        actual=f"verb={n_cancelled} rows={len(cancelled_rows)}",
    )
    final_ticks = [m for m in h.world.messages_of(GUILD_CHANNEL) if m.startswith("ldp tick")]
    h.expect(
        len(final_ticks) < 3 + 5,
        what="cancelled tail never reached the world",
        expectation="fewer than 8 delivered ticks (burst was cut short)",
        actual=f"{len(final_ticks)} tick message(s) total",
    )


async def phase_rate_limits(h: Harness) -> None:
    """Pacing: policies, effective views, the 1.5s floor, cooldowns."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    effective = await h.step(
        "effective policy for message", lambda: h.api.ratelimit_effective("message", "alpha")
    )
    interval = float((effective or {}).get("interval") or 0)
    h.expect(
        interval >= 1.5,
        what="message pacing sits at or above the 1.5s floor",
        expectation="interval >= 1.5 (safety floor)",
        actual=f"interval={interval}",
    )
    # measured spacing on the audit trail (the wire truth)
    before = await h.audit_out_rows()
    h.owner_says("ldp-go 2")
    await h.wait_quiet(limit=15.0)
    rows = await h.audit_out_rows()
    fresh = rows[len(before) :] if len(rows) > len(before) else []
    h.expect(
        len(fresh) >= 2,
        what="burst left outbound audit rows for the timing check",
        expectation=">= 2 rows",
        actual=f"{len(fresh)} row(s)",
    )
    if len(fresh) >= 2:
        gaps = [
            round(float(b.get("ts", 0)) - float(a.get("ts", 0)), 3)
            for a, b in itertools.pairwise(fresh)
        ]
        h.expect(
            min(gaps) >= 1.45,
            what="consecutive paced sends keep the floor (audit timestamps)",
            expectation="every gap >= 1.45s (1.5s floor, 50ms max jitter)",
            actual=f"gaps={gaps}",
        )
    account_policy = await h.step(
        "set account-level message policy 2.0s",
        lambda: h.api.ratelimit_set("message", 2.0, 0.0, 0.0, level="account", owner="alpha"),
    )
    effective_after = await h.step(
        "effective policy after the account override",
        lambda: h.api.ratelimit_effective("message", "alpha"),
    )
    h.expect(
        isinstance(account_policy, dict)
        and float((effective_after or {}).get("interval") or 0) == 2.0,
        what="account-level override wins the effective view",
        expectation="effective interval == 2.0",
        actual=f"set={account_policy} effective={effective_after}",
    )
    cleared = await h.step(
        "clear the account override",
        lambda: h.api.ratelimit_clear("account", "alpha", "message"),
    )
    h.expect(
        (cleared or {}).get("cleared") is True,
        what="policy clear removes the override",
        expectation="cleared=True",
        actual=str(cleared),
    )
    await h.step_raises(
        "below-floor policy is rejected",
        (UnsafePolicyError,),
        lambda: h.api.ratelimit_set("message", 0.2, level="global"),
    )
    warnings = await h.step("ratelimit_warnings", h.api.ratelimit_warnings)
    h.expect(
        isinstance(warnings, list),
        what="safety review readable",
        expectation="list",
        actual=f"{len(warnings)} warning(s)",
    )
    stats = await h.step("ratelimit_stats", h.api.ratelimit_stats)
    h.expect(
        isinstance(stats, dict),
        what="rate stats readable",
        expectation="dict",
        actual=str(type(stats)),
    )
    cooldowns = await h.step("cooldowns list", lambda: h.api.cooldowns_list(account="alpha"))
    h.expect(
        isinstance(cooldowns, list),
        what="cooldown list readable",
        expectation="list",
        actual=f"{len(cooldowns)} row(s)",
    )


async def phase_workspaces(h: Harness) -> None:
    """Workspaces: create/join/pause/resume/eligibility/cooldowns/delete."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    created = await h.step(
        "create workspace 'grind'",
        lambda: h.api.workspaces_create({"name": "grind", "description": "live-debug"}),
    )
    h.expect(
        isinstance(created, dict),
        what="workspace created",
        expectation="echoed row",
        actual=str(created),
    )
    joined = await h.step(
        "alpha joins grind", lambda: h.api.workspaces_add_account("alpha", "grind")
    )
    h.expect(
        isinstance(joined, dict),
        what="membership recorded",
        expectation="join report",
        actual=str(joined),
    )
    info = await h.step("workspaces_get grind", lambda: h.api.workspaces_get("grind"))
    members = (info or {}).get("members") or []
    h.expect(
        "alpha" in members,
        what="membership visible in the workspace view",
        expectation="alpha among the member names",
        actual=str(members),
    )
    paused = await h.step("pause grind", lambda: h.api.workspaces_pause("grind"))
    resumed = await h.step("resume grind", lambda: h.api.workspaces_resume("grind"))
    h.expect(
        bool(
            isinstance(paused, dict)
            and isinstance(resumed, dict)
            and paused.get("paused") is True
            and resumed.get("paused") is False
        ),
        what="pause bumps the generation; resume restores flow",
        expectation="paused=True then paused=False (generation bumps)",
        actual=f"pause={paused} resume={resumed}",
    )
    await h.step(
        "set member eligibility paused",
        lambda: h.api.workspaces_op("eligibility", account="alpha", eligibility="paused"),
    )
    cooldown = await h.step(
        "start a member cooldown",
        lambda: h.api.workspaces_op(
            "cooldown", scope_type="member", scope_id="alpha", duration_s=30
        ),
    )
    active = await h.step(
        "cooldowns active_only",
        lambda: h.api.workspaces_cooldowns(active_only=True),
    )
    h.expect(
        isinstance(cooldown, dict) and len(active or []) >= 1,
        what="cooldown started and listed",
        expectation="active cooldown row for alpha",
        actual=f"start={cooldown} active={len(active or [])}",
    )
    cooldown_id = int((active or [{}])[0].get("id") or 0)
    cleared = await h.step(
        "clear the cooldown", lambda: h.api.workspaces_op("cooldown_clear", id=cooldown_id)
    )
    h.expect(
        isinstance(cleared, dict),
        what="cooldown cleared",
        expectation="clear report",
        actual=str(cleared),
    )
    await h.step(
        "restore eligibility",
        lambda: h.api.workspaces_op("eligibility", account="alpha", eligibility="eligible"),
    )
    await h.step_raises(
        "delete with a member is refused",
        (ValueError,),
        lambda: h.api.workspaces_delete("grind"),
    )
    gone = await h.step("leave workspace", lambda: h.api.workspaces_remove_account("alpha"))
    h.expect(isinstance(gone, dict), what="leave report", expectation="dict", actual=str(gone))
    # rejoin for the presets/chains phases (standalone-runnable)
    await h.api.workspaces_add_account("alpha", "grind")
    h.observation(
        "workspace events publish only to a debug logger (bootstrap wires "
        "publisher=ROOT_LOGGER.debug) — membership changes are invisible to "
        "the structured log tail",
        expectation="workspace membership/pause transitions surface as structured rows",
        actual="WorkspaceService events land on libzent root logger at DEBUG only",
        repro="workspaces/publisher-visibility",
    )


async def phase_presets(h: Harness) -> None:
    """Presets: settings-template apply path + P8 definitions/executions."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    await _ensure_workspace(h, "grind")  # preset definitions reference it
    repos = h.framework.database
    template = await h.step(
        "create preset template (scope account/alpha)",
        lambda: repos.presets.create(
            "account", "alpha", "ldp_worker", "fast", {"burst": 4}, description="live-debug"
        ),
    )
    preset_id = int(getattr(template, "preset_id", 0) or 0)
    h.expect(
        preset_id > 0,
        what="template identity created",
        expectation="preset_id > 0",
        actual=f"preset_id={preset_id}",
    )
    preview = await h.step(
        "preset preview (revision fingerprint)",
        lambda: h.api.workspaces_preset_preview("alpha", "ldp_worker", preset_id),
    )
    revision = str((preview or {}).get("revision") or "")
    h.expect(
        bool(revision),
        what="preview returns the revision guard",
        expectation="revision fingerprint",
        actual=str(preview),
    )
    applied = await h.step(
        "preset apply (copy-on-write + provenance)",
        lambda: h.api.workspaces_preset_apply(
            "alpha", "ldp_worker", preset_id, preview_revision=revision
        ),
    )
    h.expect(
        (applied or {}).get("applied") is True,
        what="apply copies settings + records provenance",
        expectation="applied=True",
        actual=str(applied),
    )
    effective = await h.step(
        "effective settings after apply",
        lambda: h.api.accounts_effective_settings("alpha", "ldp_worker"),
    )
    h.expect(
        (effective or {}).get("burst") == 4,
        what="applied settings are live",
        expectation="burst == 4",
        actual=str(effective),
    )
    detached = await h.step(
        "preset detach (values stay, link drops)",
        lambda: h.api.workspaces_preset_detach("alpha", "ldp_worker"),
    )
    h.expect(
        isinstance(detached, dict), what="detach report", expectation="dict", actual=str(detached)
    )
    stale = await h.step(
        "apply with a stale preview revision is refused",
        lambda: h.api.workspaces_preset_apply(
            "alpha", "ldp_worker", preset_id, preview_revision=revision
        ),
    )
    h.expect(
        (stale or {}).get("applied") is False,
        what="stale-preview guard fires (decision 16)",
        expectation="applied=False with a conflict",
        actual=str(stale),
    )

    # P8: reference-style definitions + executions (service-level)
    await h.step(
        "profile for the preset to reference",
        lambda: h.api.account_plugin_profile_create(
            "alpha", "ldp_worker", "preset-profile", settings={"burst": 2}
        ),
    )
    defined = await h.step(
        "preset_define (references, never copies)",
        lambda: h.framework.workspaces.preset_define(
            "grind",
            "grind-standard",
            participants=[
                {"account": "alpha", "plugin": "ldp_worker", "profile": "preset-profile"}
            ],
        ),
    )
    h.expect(
        (defined or {}).get("preset_id", 0) > 0,
        what="preset definition stored",
        expectation="preset_id > 0",
        actual=str(defined),
    )
    exec_started = await h.step(
        "execution_start", lambda: h.framework.workspaces.execution_start(defined["preset_id"])
    )
    execution_id = str((exec_started or {}).get("execution_id") or "")
    checkpointed = await h.step(
        "execution_checkpoint (alpha done)",
        lambda: h.framework.workspaces.execution_checkpoint(execution_id, "alpha", "done"),
    )
    resume_view = await h.step(
        "execution_resume (completed not replayed)",
        lambda: h.framework.workspaces.execution_resume(execution_id),
    )
    h.expect(
        execution_id.startswith("exec-")
        and "alpha" in (checkpointed or {}).get("completed", [])
        and (resume_view or {}).get("completed") == ["alpha"]
        and (resume_view or {}).get("pending") == [],
        what="execution lifecycle: start → checkpoint → resume",
        expectation="completed=[alpha] and pending=[]",
        actual=f"start={exec_started} checkpoint={checkpointed} resume={resume_view}",
    )
    h.observation(
        "preset_define / preset_apply_references / execution_* have no "
        "ControlAPI verb — they are reachable only through the workspace "
        "service object",
        expectation="every feature area is drivable through the ONE facade",
        actual="P8 preset definitions + executions are service-level only",
        repro="presets/facade-coverage",
    )


async def _ensure_workspace(h: Harness, name: str = "grind") -> None:
    """Idempotent: the workspace exists and alpha is an eligible member
    (standalone ``--phase`` runs for presets/chains need it)."""
    known = {row.get("name") for row in await h.api.workspaces_list()}
    if name not in known:
        await h.api.workspaces_create({"name": name, "description": "live-debug"})
    info = await h.api.workspaces_get(name)
    if "alpha" not in ((info or {}).get("members") or []):
        await h.api.workspaces_add_account("alpha", name)
    # eligible -> eligible is not a legal move: already there
    with contextlib.suppress(ValueError):
        await h.api.workspaces_op("eligibility", account="alpha", eligibility="eligible")


async def phase_chains_signals(h: Harness) -> None:
    """Chains + durable signals: terminal trigger, outbox, reconciler —
    with a SECOND account (beta) as the eligible follower of alpha's
    burst, so a completed run proves A-acts-B-follows, not self-follow."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    await _ensure_workspace(h, "grind")
    # beta joins as the FOLLOWER carrying ldp_follower — the fixture
    # SPLIT from ldp_worker (PR review 2026-11-18): when one plugin
    # played both roles, the follower's own sends re-matched the
    # ldp_worker terminal trigger (same plugin + task action) and its
    # handler re-emitted chain-done, so the probe self-perpetuated
    # runs. The follower completes as plugin=ldp_follower (no
    # re-match) and has NO chat surface (alpha's burst word can never
    # make it react — structural, not a settings workaround). Both
    # members stay eligible — pausing alpha would fence ALPHA's own
    # sends at the dispatch guard — so the chain steps below route
    # EXPLICITLY to beta: a completed run with target beta proves
    # A-acts-B-follows, never self-follow
    await h.ensure_account("beta", ["ldp_follower"])
    await h.step("beta joins grind", lambda: h.api.workspaces_add_account("beta", "grind"))
    repo = h.framework.database.workspaces
    definition = {
        # the name is REQUIRED (PR review 2026-11-18): the runtime
        # reads it back from definition_json — a nameless row can
        # never match a trigger, and because intake parses every
        # enabled row first it also blocks signal intake workspace-wide
        "name": "follow-burst",
        "steps": [
            {
                "position": 1,
                "plugin": "ldp_follower",
                "routing": "specific_account",
                "target": "beta",
                "on": {"task_action": "message", "plugin": "ldp_worker", "event": "task_completed"},
                "gate": {},
                "emit": "",
            }
        ],
        "on_skip": "continue",
    }
    # validate BEFORE persisting (PR review 2026-11-18): insert_chain
    # is a raw repo write with no gate of its own
    ChainRuntime.parse_definition(definition)
    chain_id = await h.step(
        "insert chain definition (follow-burst)",
        lambda: repo.insert_chain("grind", "follow-burst", json.dumps(definition)),
    )
    h.expect(bool(chain_id), what="chain stored", expectation="chain id", actual=f"id={chain_id}")
    baseline_runs = {str(row.get("id")) for row in await repo.chain_runs()}
    h.owner_says("ldp-go 1")
    ok = await h.wait_until(
        lambda: _new_completed_run(repo, baseline_runs, chain_id=int(chain_id or 0)),
        timeout=20.0,
        description="a terminal-triggered chain run completes",
    )
    runs_after = await repo.chain_runs()
    fresh_runs = [row for row in runs_after if str(row.get("id")) not in baseline_runs]
    h.expect(
        ok,
        what="TaskCompleted fired the chain (A-acts, B-follows)",
        expectation="a run id ABSENT from the baseline reaches status 'completed'",
        actual=f"new runs: {[(r.get('id'), r.get('status')) for r in fresh_runs] or 'none'}",
    )
    # the follow-up must have executed on BETA — a completed run that
    # alpha routed back to itself proves nothing about following
    follow_run = next(
        (
            row
            for row in fresh_runs
            if row.get("chain_id") == chain_id and str(row.get("status")) == "completed"
        ),
        None,
    )
    follower = ""
    if follow_run is not None:
        steps = await repo.step_executions(str(follow_run.get("id")))
        follower = next((str(step.get("target") or "") for step in steps), "")
    h.expect(
        follower == "beta",
        what="the follow-up executed on the SECOND account (beta)",
        expectation="completed follow-burst run routed to beta (alpha initiated the burst)",
        actual=f"run={follow_run.get('id') if follow_run else None} target={follower!r}",
    )
    # and the run must trace back to ALPHA's act, not beta's own sends
    # — the runaway the shared trigger key used to produce (PR review
    # 2026-11-18)
    triggered_by = str(follow_run.get("triggered_by") or "") if follow_run is not None else ""
    h.expect(
        triggered_by == "task:alpha:ldp_worker",
        what="the follow run was triggered by ALPHA's burst",
        expectation="triggered_by == 'task:alpha:ldp_worker'",
        actual=f"triggered_by={triggered_by!r}",
    )
    signal_chain = {
        "name": "signal-followup",
        "steps": [
            {
                "position": 1,
                "plugin": "ldp_follower",
                "routing": "specific_account",
                "target": "beta",
                "on": {"signal": "ldp_worker:chain-done"},
                "gate": {},
                "emit": "",
            }
        ],
        "on_skip": "continue",
    }
    ChainRuntime.parse_definition(signal_chain)
    signal_chain_id = await h.step(
        "insert signal-triggered chain (signal-followup)",
        lambda: repo.insert_chain("grind", "signal-followup", json.dumps(signal_chain)),
    )
    h.expect(
        bool(signal_chain_id),
        what="signal chain stored",
        expectation="chain id",
        actual=f"id={signal_chain_id}",
    )
    signal_baseline = {str(row.get("id")) for row in await repo.chain_runs()}
    emitted = await h.step(
        "emit a durable signal through the plugin facade",
        lambda: h.framework.accounts.services_for("alpha", "ldp_worker").emit_signal(
            "chain-done", {"source": "live-debug"}
        ),
    )
    h.expect(
        bool(emitted),
        what="outbox acknowledges with a signal id",
        expectation="signal id string",
        actual=f"signal_id={emitted!r}",
    )
    signal_id = str(emitted or "")
    print("    waiting for the reconciler sweep (<=45s, 30s cadence)...")
    delivered = await h.wait_until(
        lambda: _signal_delivered_and_followup_ran(
            h, signal_id, signal_baseline, int(signal_chain_id or 0)
        ),
        timeout=RECONCILER_WAIT_S,
        description="the reconciler delivers the signal and fires the followup chain",
    )
    still_pending = signal_id in {
        str(getattr(row, "signal_id", "")) for row in await h.framework.database.signals.pending(50)
    }
    followup_runs = [
        row
        for row in await repo.chain_runs()
        if str(row.get("id")) not in signal_baseline and row.get("chain_id") == signal_chain_id
    ]
    h.expect(
        delivered,
        what="reconciler delivered the signal and fired the chain",
        expectation=(
            "the EMITTED signal leaves the pending set AND a run id absent "
            "from the baseline reaches status 'completed'"
        ),
        actual=(
            f"signal {signal_id[:12]}… pending={still_pending}; new runs: "
            f"{[(r.get('id'), r.get('status')) for r in followup_runs] or 'none'}"
        ),
    )
    await h.step("disable both chains", lambda: _disable_chains(repo, [chain_id, signal_chain_id]))
    h.observation(
        "chain definitions have no ControlAPI verb (create/update/list) — "
        "the runtime's create_chain is bootstrap-local; the harness writes "
        "rows through the workspaces repository",
        expectation="chains are manageable from the interfaces",
        actual="repository-level access required",
        repro="chains-signals/facade-coverage",
    )


async def _has_mgmt_credential_rows(h: Harness) -> bool:
    """Wait-predicate: mgmt:credential rows drained into readable history
    (queued persistence is asynchronous by contract, §29)."""
    rows = await h.api.audit_tail(n=200, account="alpha")
    return bool([r for r in rows if str(r.get("action", "")).startswith("mgmt:credential")])


async def _state_is_not(h: Harness, name: str, state: str) -> bool:
    """Wait-predicate: the account left the given lifecycle state."""
    return await h.account_state(name) != state


async def _new_completed_run(
    repo: Any,
    baseline: frozenset[str] | set[str],
    *,
    chain_id: int | None = None,
    triggered_by: str | None = None,
) -> bool:
    """True only when a run id ABSENT from the baseline has status
    'completed' — a failed/partial new run, or an old terminal run
    joined by a fresh pending one, must NOT satisfy the wait.

    ``chain_id`` / ``triggered_by`` narrow the match to ONE chain (and,
    for signal runs, to the exact ``signal:<id>`` trigger recorded at
    run creation) — a completed run from an UNRELATED chain can never
    satisfy the wait."""
    for row in await repo.chain_runs():
        if str(row.get("id")) in baseline:
            continue
        if str(row.get("status")) != "completed":
            continue
        if chain_id is not None and row.get("chain_id") != chain_id:
            continue
        if triggered_by is not None and str(row.get("triggered_by") or "") != triggered_by:
            continue
        return True
    return False


async def _signal_delivered_and_followup_ran(
    h: Harness, signal_id: str, baseline: frozenset[str] | set[str], chain_id: int
) -> bool:
    """True only when the EMITTED signal is no longer pending AND a run
    of the SIGNAL-FOLLOWUP chain — triggered by exactly that signal id,
    absent from the baseline — completed. An empty outbox alone proves
    nothing (the emission may never have landed), and an unrelated
    chain's completion proves the wrong thing."""
    if not signal_id:
        return False
    pending_ids = {
        str(getattr(row, "signal_id", "")) for row in await h.framework.database.signals.pending(50)
    }
    if signal_id in pending_ids:
        return False
    return await _new_completed_run(
        h.framework.database.workspaces,
        baseline,
        chain_id=chain_id,
        triggered_by=f"signal:{signal_id}",
    )


async def _disable_chains(repo: Any, chain_ids: Sequence[int]) -> None:
    for chain_id in chain_ids:
        if chain_id:
            await repo.set_chain_enabled(int(chain_id), False)


async def phase_observability(h: Harness) -> None:
    """The observability verbs: filtered log tails + audit history."""
    await h.ensure_account("alpha", ["ping", "ldp_worker"])
    h.owner_says("ldp-go 1")
    await h.wait_quiet(limit=15.0)
    account_rows = await h.step(
        "log tail (account filter)", lambda: h.api.logs_tail(n=50, account="alpha")
    )
    h.expect(
        bool(account_rows) and all(r.get("account") == "alpha" for r in account_rows),
        what="account-filtered tail returns only that account's rows",
        expectation="non-empty, all account=alpha",
        actual=f"{len(account_rows)} row(s)",
    )
    global_rows = await h.step(
        "global-log view (framework rows)", lambda: h.api.logs_tail(n=50, without_account=True)
    )
    h.expect(
        bool(global_rows) and all(not r.get("account") for r in global_rows),
        what="global-log view carries no account context",
        expectation="non-empty, no account field",
        actual=f"{len(global_rows)} row(s)",
    )
    error_rows = await h.step(
        "severity-filtered tail", lambda: h.api.logs_tail(n=50, severity="ERROR")
    )
    h.expect(
        all(r.get("severity") == "ERROR" for r in error_rows),
        what="severity filter is exact",
        expectation="only ERROR rows",
        actual=f"{len(error_rows)} row(s)",
    )
    stats = h.framework.logs.stats()
    h.expect(
        stats.get("entries", 0) > 0,
        what="log stats counters live",
        expectation="entries > 0",
        actual=str(stats),
    )
    audit_all = await h.step("audit tail", lambda: h.api.audit_tail(n=100))
    mgmt = [r for r in audit_all or [] if str(r.get("action", "")).startswith("mgmt:")]
    h.expect(
        bool(mgmt),
        what="management operations left audit rows",
        expectation="mgmt:* rows from the earlier phases",
        actual=f"{len(mgmt)} mgmt row(s) of {len(audit_all or [])}",
    )
    lifecycle = [
        r
        for r in await h.api.logs_tail(n=200)
        if r.get("event") in ("AccountConnected", "TaskCompleted", "PluginLoaded")
    ]
    h.expect(
        bool(lifecycle),
        what="bus lifecycle events reached the structured log",
        expectation="AccountConnected/TaskCompleted/PluginLoaded rows",
        actual=f"{len(lifecycle)} event row(s)",
    )


async def phase_simulation(h: Harness) -> None:
    """The simulator surface: the facade verb, the CURRENT --sim default
    positively, and the production-dir override failing honestly."""
    from adapters.simulator import SimulationRunner

    scenarios = await h.step("simulation_list", h.api.simulation_list)
    names = sorted(s.get("name") for s in scenarios or [])
    h.expect(
        "export_basic" in names,
        what="the facade lists the registered scenarios",
        expectation="export_basic among the six",
        actual=str(names),
    )

    # POSITIVE check at the CURRENT `--sim` default: main.py points the
    # runner at the workload fixtures (tests/fixtures/sim_plugins)
    # POSITIVE check through the CLI ENTRYPOINT — the exact command the
    # operator types (``python main.py --sim export_basic``), so the
    # default plugin dir, exit code, and summary line are all exercised
    # as shipped rather than through the internal runner
    async def run_cli_sim() -> tuple[int | None, str]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(REPO_ROOT / "main.py"),
            "--sim",
            "export_basic",
            cwd=str(REPO_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=SCENARIO_BUDGET_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # timeout OR operator interruption: either way the child is
            # killed and reaped before the exception propagates — an
            # unreaped simulator process outlives the cancelled phase
            proc.kill()
            with contextlib.suppress(asyncio.CancelledError, ProcessLookupError):
                await proc.communicate()
            raise
        lines = out.decode("utf-8", errors="replace").strip().splitlines()
        summary = next(
            (line for line in lines if line.startswith("scenario")), lines[0] if lines else ""
        )
        return proc.returncode, summary

    try:
        returncode, summary_line = await run_cli_sim()
        h.expect(
            returncode == 0,
            what="main.py --sim export_basic exits clean (the operator's command)",
            expectation="exit 0 with a passing summary",
            actual=f"exit={returncode} last line={summary_line!r}",
        )
    except asyncio.TimeoutError:
        h.expect(
            False,
            what="main.py --sim export_basic respects its budget",
            expectation="returns in <=120s",
            actual="timed out",
        )
    # SEPARATE override case: an EXPLICIT --plugins override to the
    # production dir finds no exporter workload there — the scenario
    # must fail loudly, never pass vacuously
    prod_runner = SimulationRunner(plugin_dir=str(REPO_ROOT / "plugins"))
    try:
        override = await asyncio.wait_for(
            prod_runner.run("export_basic"), timeout=SCENARIO_BUDGET_S
        )
        override_failures = list(override.get("failures") or [])
        h.expect(
            bool(override_failures),
            what="explicit production-dir override fails honestly (no workload)",
            expectation="scenario reports its send failures, not a vacuous pass",
            actual=f"{len(override_failures)} failure(s): {override_failures[:2]}",
        )
    except asyncio.TimeoutError:
        h.expect(
            False,
            what="production-dir override respects its budget",
            expectation="scenario returns in <=120s",
            actual="timed out",
        )


async def phase_shutdown(h: Harness) -> None:
    """Graceful teardown of the whole stack (the ordered contract)."""
    # stop_all disconnects EVERY account: each one's echo is expected
    # here (and only here) — marked before the rows can arrive
    for row in await h.api.accounts_list():
        name = row.get("name")
        if isinstance(name, str):
            h.mark_disconnect_expected(name)
    counts_before = h._findings.counts()
    errors_before = counts_before.get("major", 0) + counts_before.get("critical", 0)
    await h.step("framework.shutdown", h.framework.shutdown)
    await asyncio.sleep(0.3)  # let straggler warnings surface into the capture
    counts_after = h._findings.counts()
    new_errors = counts_after.get("major", 0) + counts_after.get("critical", 0) - errors_before
    h.expect(
        new_errors == 0,
        what="teardown raised no new ERROR-class anomalies",
        expectation="ordered shutdown completes clean",
        actual=f"{new_errors} new major+ finding(s) during teardown",
    )
    # cleanup failures are WARNING-class at the root logger, and the
    # structured bridge detaches mid-teardown — the count above cannot
    # see them. Framework.shutdown_failures is the durable witness
    # (PR review 2026-11-18): a failed database.close() used to still
    # read as a graceful shutdown and exit 0
    h.expect(
        not h.framework.shutdown_failures,
        what="every shutdown cleanup step succeeded",
        expectation="shutdown_failures is empty",
        actual=f"{len(h.framework.shutdown_failures)} failure(s): "
        f"{h.framework.shutdown_failures[:3] or None}",
    )


# ------------------------------------------------------------------ registry
@dataclass(frozen=True)
class Phase:
    """One named, announced pass over a feature area."""

    name: str
    area: str
    description: str
    run: Callable[[Harness], Coroutine[Any, Any, None]]


PHASES: tuple[Phase, ...] = (
    Phase("boot", "framework", "bootstrap over the simulator + discovery + leak scan", phase_boot),
    Phase(
        "accounts", "accounts", "add/credential/start/estop/stop/remove + cascade", phase_accounts
    ),
    Phase(
        "credentials",
        "credentials",
        "Fernet store, status, rotation, env migration",
        phase_credentials,
    ),
    Phase(
        "plugins",
        "plugin-assignment",
        "assign/unassign/enable/disable on a live account",
        phase_plugins,
    ),
    Phase(
        "profiles",
        "account-plugin-profiles",
        "create/update/activate/clear + effective view",
        phase_profiles,
    ),
    Phase(
        "cog-commands",
        "cog-dispatch",
        "library dispatch of <prefix>ping over the paced pipeline",
        phase_cog_commands,
    ),
    Phase(
        "tasks-scheduler",
        "scheduler",
        "queued/running/history + mid-flight cancellation",
        phase_tasks_scheduler,
    ),
    Phase(
        "rate-limits",
        "pacing",
        "policies, effective views, the 1.5s floor, cooldowns",
        phase_rate_limits,
    ),
    Phase(
        "workspaces",
        "workspaces",
        "membership/pause/resume/eligibility/cooldowns",
        phase_workspaces,
    ),
    Phase(
        "presets",
        "workspace-presets",
        "template apply + P8 definitions and executions",
        phase_presets,
    ),
    Phase(
        "chains-signals",
        "chains-signals",
        "terminal-triggered chains, durable outbox, reconciler",
        phase_chains_signals,
    ),
    Phase(
        "observability",
        "audit-observability",
        "filtered log tails, stats, audit history",
        phase_observability,
    ),
    Phase(
        "simulation",
        "simulator-scenarios",
        "the facade verb + scenario baseline runs",
        phase_simulation,
    ),
    Phase("shutdown", "framework", "graceful ordered teardown, no new anomalies", phase_shutdown),
)

_PHASE_INDEX: dict[str, Phase] = {phase.name: phase for phase in PHASES}


def select_phases(selected: Sequence[str]) -> list[Phase]:
    """Validate EVERY name first (a typo must be rejected before any
    findings file is created), then return the matching phases in
    REGISTRY order — argument order never reorders the pass, so
    ``--phase shutdown --phase accounts`` still runs accounts first.
    A single name runs alone; an empty selection means every phase."""
    unknown = [name for name in selected if name not in _PHASE_INDEX]
    if unknown:
        known = ", ".join(_PHASE_INDEX)
        raise KeyError(f"unknown phase(s) {unknown} (known: {known})")
    chosen = set(selected)
    return [phase for phase in PHASES if phase.name in chosen] or list(PHASES)


def phase_by_name(name: str) -> Phase:
    """Resolve one phase (KeyError names the known set — typo trap)."""
    try:
        return _PHASE_INDEX[name]
    except KeyError:
        known = ", ".join(phase.name for phase in PHASES)
        raise KeyError(f"unknown phase {name!r} (known: {known})") from None


async def run_pass(
    selected: Sequence[str],
    *,
    findings_dir: Path = DEFAULT_FINDINGS_DIR,
    keep_workdir: bool = False,
    console: Console | None = None,
) -> dict[str, Any]:
    """Boot once, run the selected phases in registry order, tear down,
    and write the run summary. Returns the summary dict (also written
    to ``findings_dir/<run id>.summary.json``)."""
    # COLLISION-RESISTANT, CREATION-ORDERED run id: the timestamp carries
    # NANOSECOND resolution (fixed-width, so lexical order == creation
    # order even for two runs inside one second — the --watch tail picks
    # the newest by name), the uuid suffix stays for uniqueness, and the
    # writer's exclusive "x" mode turns any residual collision into a
    # loud FileExistsError instead of a silent truncation.
    now_ns = time.time_ns()
    run_id = (
        f"run-{time.strftime('%Y%m%dT%H%M%S', time.localtime(now_ns / 1_000_000_000))}"
        f"{now_ns % 1_000_000_000:09d}-{uuid4().hex[:8]}"
    )
    phases = select_phases(selected)  # validates BEFORE the findings file opens
    findings = FindingsWriter(findings_dir / f"{run_id}.jsonl")
    console = console or Console()
    summary: dict[str, Any] = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "phases": [],
        "exit_code": 0,
    }
    harness = Harness(findings, console, run_id=run_id, keep_workdir=keep_workdir)
    started = time.monotonic()
    completed = False
    try:
        console.banner(
            f"live-debug pass {run_id} — {len(phases)} phase(s), findings dir {findings_dir}"
        )
        try:
            await harness.boot()
        except Exception as exc:
            # the summary must never claim success for a stack that never
            # came up — record the failure, then let it propagate
            harness.report(
                severity="critical",
                summary=f"harness boot failed: {type(exc).__name__}: {exc}",
                evidence=[
                    line.rstrip()
                    for line in traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:]
                ],
                expectation="the real stack boots over the simulator",
                actual=f"{type(exc).__name__}: {exc}",
                repro="boot",
            )
            raise
        if keep_workdir:
            console.line(f"workdir (kept): {harness.workdir}")
        for position, phase in enumerate(phases, start=1):
            console.phase(
                f"{position}/{len(phases)} {phase.name} ({phase.area}) — {phase.description}"
            )
            harness.set_phase(phase.name, phase.area)
            passed_before = harness.steps_passed
            failed_before = harness.steps_failed
            phase_start = time.monotonic()
            try:
                await asyncio.wait_for(phase.run(harness), timeout=300.0)
            except asyncio.TimeoutError:
                harness.report(
                    severity="critical",
                    summary=f"phase {phase.name!r} exceeded the 300s budget",
                    evidence=[],
                    expectation="each phase completes inside its budget",
                    actual="phase abandoned after 300s",
                    repro=f"{phase.name}/budget",
                )
            except Exception as exc:
                harness.report(
                    severity="critical",
                    summary=f"phase {phase.name!r} crashed: {type(exc).__name__}: {exc}",
                    evidence=[
                        line.rstrip()
                        for line in traceback.format_exception(type(exc), exc, exc.__traceback__)[
                            -6:
                        ]
                    ],
                    expectation="phase completes",
                    actual=f"{type(exc).__name__}: {exc}",
                    repro=phase.name,
                )
            summary["phases"].append(
                PhaseOutcome(
                    name=phase.name,
                    area=phase.area,
                    steps_passed=harness.steps_passed - passed_before,
                    steps_failed=harness.steps_failed - failed_before,
                    duration_s=round(time.monotonic() - phase_start, 2),
                ).__dict__
            )
        completed = True
    finally:
        await harness.teardown()
        summary["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        summary["duration_s"] = round(time.monotonic() - started, 1)
        summary["findings_by_severity"] = findings.counts()
        summary["steps_passed"], summary["steps_failed"] = (
            harness.steps_passed,
            harness.steps_failed,
        )
        # completed=False (boot failure, mid-run propagation) is an
        # unsuccessful pass regardless of finding counts
        summary["completed"] = completed
        summary["exit_code"] = (
            0
            if (
                completed
                and not (findings.counts().get("critical") or findings.counts().get("major"))
            )
            else 1
        )
        findings.close()
        (findings_dir / f"{run_id}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        console.banner(
            f"run finished in {summary['duration_s']}s — findings "
            f"{dict(summary['findings_by_severity'])}, steps "
            f"{summary['steps_passed']} passed / {summary['steps_failed']} failed "
            f"(exit {summary['exit_code']})"
        )
    return summary


# --------------------------------------------------------------------- main
def _watch(findings_dir: Path) -> int:
    """Tail the newest findings file live (second terminal window).

    Prints the existing findings, then polls for appended lines until
    interrupted. A newer run file takes over automatically.
    """
    current: Path | None = None
    handled = 0
    while True:
        # lexical order == creation order: run ids carry a fixed-width
        # nanosecond component (see run_pass), so the LAST name is the
        # newest run even when two started inside the same second
        runs = sorted(
            p for p in findings_dir.glob("run-*.jsonl") if _RUN_FILE_PATTERN.match(p.name)
        )
        newest = runs[-1] if runs else None
        if newest is None:
            print(f"[watch] no findings yet under {findings_dir} — waiting...", flush=True)
            time.sleep(1.0)
            continue
        if newest != current:
            current, handled = newest, 0
            print(f"[watch] tailing {newest.name}", flush=True)
        # Only COMPLETE newline-terminated records advance the cursor:
        # the writer flushes per record, but the tail read can race a
        # half-written final line — an unterminated record is retried on
        # the next poll instead of being skipped forever. A complete but
        # malformed line is announced, never silently dropped.
        complete, _, _ = current.read_text(encoding="utf-8").rpartition("\n")
        records = [record for record in complete.split("\n") if record]
        for record in records[handled:]:
            handled += 1
            try:
                row = json.loads(record)
            except json.JSONDecodeError:
                print(f"[watch] malformed record in {current.name}: {record[:120]!r}", flush=True)
                continue
            print(
                f"[ANOMALY] {row.get('id')} [{row.get('severity')}] "
                f"{row.get('phase')}/{row.get('area')}: {row.get('summary')}",
                flush=True,
            )
        time.sleep(0.4)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="live-debug",
        description="Live supervised debugging pass over the whole suite (simulator, no network)",
    )
    parser.add_argument("--list", action="store_true", help="list phases and exit")
    parser.add_argument(
        "--phase",
        metavar="NAME",
        action="append",
        default=None,
        help="run only these phase(s) (repeatable); the stack still boots first",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="tail the newest findings file live (run in a second terminal)",
    )
    parser.add_argument(
        "--findings-dir",
        default=str(DEFAULT_FINDINGS_DIR),
        help=f"findings output directory (default: {DEFAULT_FINDINGS_DIR})",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="keep the disposable boot workdir for forensics (path printed at boot)",
    )
    args = parser.parse_args(argv)

    if args.list:
        print("live-debug phases (registry order):")
        for position, phase in enumerate(PHASES, start=1):
            print(f"  {position:2d}. {phase.name:<18} {phase.area:<22} {phase.description}")
        return 0

    findings_dir = Path(args.findings_dir)

    if args.watch:
        try:
            return _watch(findings_dir)
        except KeyboardInterrupt:
            return 0

    selected = list(args.phase) if args.phase else [phase.name for phase in PHASES]
    for name in selected:
        try:
            phase_by_name(name)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.keep_workdir:
        print("note: --keep-workdir set — the workdir prints in the boot banner path")
    try:
        summary = asyncio.run(
            run_pass(selected, findings_dir=findings_dir, keep_workdir=args.keep_workdir)
        )
    except KeyboardInterrupt:
        return 130
    return int(summary.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
