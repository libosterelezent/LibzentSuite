"""Task model for the centralized scheduler (spec §6, INTEGRATION 'core/task_scheduler').

A Task is ONE outbound action for ONE account: plugin X wants to send action
Y (usually 'message' or 'dm'). The scheduler owns the Task's lifecycle and
mutates its status/timing fields as the task moves through the pipeline:

    queued -> running -> completed | failed | cancelled | timed_out

Design notes:
  * ``attempts`` counts RETRIES USED (incremented only when a retry is
    scheduled, not on the final failure). First-try success therefore leaves
    ``attempts == 0`` and a task that succeeded on its second try has
    ``attempts == 1`` (one retry used, two sends total). This makes the
    audit-note contract ``attempts={attempts + 1}`` render the total number
    of sends, and keeps the retry budget at ``1 + max_retries`` tries.
  * ``cancelled`` is the cooperative cancel flag. ``cancel()`` sets it and
    the account worker aborts at its next checkpoint; a lane parked inside
    ``limiter.acquire`` is preempted via ``cancel_event`` (the acquire is
    raced against it and aborted), so a cancel never waits out a pacing
    cooldown.
  * ``as_dict()`` is the JSON-safe snapshot used for task listings, the
    history repository row, and stats — floats are rounded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from core.priority import Priority

#: All valid task statuses (contract: "queued|running|completed|failed|cancelled|timed_out").
TaskStatus = Literal["queued", "running", "completed", "failed", "cancelled", "timed_out"]

STATUS_QUEUED: TaskStatus = "queued"
STATUS_RUNNING: TaskStatus = "running"
STATUS_COMPLETED: TaskStatus = "completed"
STATUS_FAILED: TaskStatus = "failed"
STATUS_CANCELLED: TaskStatus = "cancelled"
STATUS_TIMED_OUT: TaskStatus = "timed_out"

#: Every status a task can hold.
TASK_STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_TIMED_OUT,
)

#: Statuses from which a task never transitions again.
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMED_OUT})


@dataclass
class Task:
    """One scheduled outbound action for one account.

    Attributes:
        id: scheduler sequence id (stable, preserved in the history repo).
        name: human label; defaults to ``plugin:action`` at submit time.
        account / plugin / action: who wants what done; action is passed
            through opaquely to the sender (not only 'message'/'dm').
        channel_id / user_id / content: action payload (message targets a
            channel, dm targets a user; other actions may carry either).
        priority: queue priority (core.priority.Priority value).
        submitted_at / started_at / finished_at: wall-clock timestamps
            (display/history only — never used for elapsed-age math: a
            backward NTP step would latch derived signals high).
        submitted_mono: monotonic twin of submitted_at; every AGE
            computation (pressure, queue-wait samples, LOW aging) must
            read this field, not the wall clock.
        attempts: retries used so far (0 until a retry is scheduled).
        max_retries: how many retries are allowed after the first failure.
        timeout_s: per-attempt asyncio.wait_for budget for the sender.
        status: current TaskStatus.
        error: human-readable failure reason (None unless failed/timed_out).
        result_summary: short (< 80 chars) description of a success.
        grant_wait_ms / grant_jitter_ms: pacing wait + jitter the rate
            limiter applied for the (latest) grant, in milliseconds.
        cancelled: cooperative cancel flag, checked between phases.
        cancel_event: per-task asyncio.Event created by the scheduler when
            the lane starts driving the task; cancel() sets it so a lane
            parked inside limiter.acquire (429 cooldowns park for up to
            MAX_INTERVAL=86400s) is preempted instead of wedging the
            account for a day.
    """

    id: int
    name: str
    account: str
    plugin: str
    action: str
    #: the PROFILE whose execution context produced this task (P4
    #: attribution; '' = the profile-less scope). Pacing stays
    #: account-level — the server's limits do not care about profiles.
    profile: str = ""
    channel_id: int | None = None
    user_id: int | None = None
    content: str | None = None
    #: target message for reaction/button actions (emoji / custom_id rides
    #: in ``content``; the scheduler treats both opaquely)
    message_id: int | None = None
    priority: int = Priority.NORMAL
    submitted_at: float = field(default_factory=time.time)
    #: monotonic twin of submitted_at — the ONLY clock age math may use
    submitted_mono: float = field(default_factory=time.monotonic, repr=False)
    started_at: float | None = None
    finished_at: float | None = None
    attempts: int = 0
    max_retries: int = 0
    timeout_s: float = 30.0
    status: TaskStatus = STATUS_QUEUED
    error: str | None = None
    result_summary: str | None = None
    grant_wait_ms: float = 0.0
    grant_jitter_ms: float = 0.0
    cancelled: bool = False
    #: preemption channel for cancel-while-parked-in-acquire (see class
    #: docstring); plain attribute, never persisted
    cancel_event: Any = field(default=None, repr=False, compare=False)
    #: True once the sender was actually invoked (any attempt) — only
    #: tasks that touched the API may produce an outbound audit row: a
    #: cancel while queued/parked on the grant AND a failure before the
    #: send (limiter error) never sent, so both must stay unaudited.
    sender_ran: bool = False
    #: Tri-state wire classification for the audit gate. None = unknown
    #: (default: the sender started but its outcome does not prove
    #: whether a request reached the wire — budget-expiry timeout, cancel
    #: mid-send, ambiguous failures; audited, the SAFE direction for
    #: pacing). True = a wire send completed (sender returned a result).
    #: False = PROVABLY pre-wire (the terminal failure's error kind is in
    #: the scheduler's pre-wire set: account lookup / run-state check /
    #: cache validation / DM resolution all failed before any client
    #: call) — no audit row may exist for a send that never happened.
    wire_sent: bool | None = None

    @property
    def is_terminal(self) -> bool:
        """True once the task reached a final status (no further transitions)."""
        return self.status in TERMINAL_STATUSES

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot of the task (all floats rounded, no objects)."""
        return {
            "id": self.id,
            "name": self.name,
            "account": self.account,
            "plugin": self.plugin,
            "action": self.action,
            "profile": self.profile,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "content": self.content,
            "message_id": self.message_id,
            "priority": int(self.priority),
            "submitted_at": round(self.submitted_at, 3),
            "started_at": round(self.started_at, 3) if self.started_at is not None else None,
            "finished_at": round(self.finished_at, 3) if self.finished_at is not None else None,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "timeout_s": round(self.timeout_s, 3),
            "status": self.status,
            "error": self.error,
            "result_summary": self.result_summary,
            "grant_wait_ms": round(self.grant_wait_ms, 3),
            "grant_jitter_ms": round(self.grant_jitter_ms, 3),
            "cancelled": bool(self.cancelled),
            "duration_s": (
                round(self.finished_at - self.started_at, 3)
                if (self.started_at is not None and self.finished_at is not None)
                else 0.0
            ),
        }


__all__ = [
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_TIMED_OUT",
    "TASK_STATUSES",
    "TERMINAL_STATUSES",
    "Task",
    "TaskStatus",
]
