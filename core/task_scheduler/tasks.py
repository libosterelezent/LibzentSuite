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
    the account worker aborts at the next checkpoint (after the rate-limit
    grant wait, or right after the sender returns).
  * ``as_dict()`` is the JSON-safe snapshot used for task listings, the
    history repository row, and stats — floats are rounded.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

from core.priority import Priority

#: All valid task statuses (contract: "queued|running|completed|failed|cancelled|timed_out").
TaskStatus = Literal[
    "queued", "running", "completed", "failed", "cancelled", "timed_out"
]

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
TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMED_OUT}
)


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
        submitted_at / started_at / finished_at: wall-clock timestamps.
        attempts: retries used so far (0 until a retry is scheduled).
        max_retries: how many retries are allowed after the first failure.
        timeout_s: per-attempt asyncio.wait_for budget for the sender.
        status: current TaskStatus.
        error: human-readable failure reason (None unless failed/timed_out).
        result_summary: short (< 80 chars) description of a success.
        grant_wait_ms / grant_jitter_ms: pacing wait + jitter the rate
            limiter applied for the (latest) grant, in milliseconds.
        cancelled: cooperative cancel flag, checked between phases.
    """

    id: int
    name: str
    account: str
    plugin: str
    action: str
    channel_id: Optional[int] = None
    user_id: Optional[int] = None
    content: Optional[str] = None
    priority: int = Priority.NORMAL
    submitted_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    attempts: int = 0
    max_retries: int = 0
    timeout_s: float = 30.0
    status: TaskStatus = STATUS_QUEUED
    error: Optional[str] = None
    result_summary: Optional[str] = None
    grant_wait_ms: float = 0.0
    grant_jitter_ms: float = 0.0
    cancelled: bool = False

    @property
    def is_terminal(self) -> bool:
        """True once the task reached a final status (no further transitions)."""
        return self.status in TERMINAL_STATUSES

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe snapshot of the task (all floats rounded, no objects)."""
        return {
            "id": self.id,
            "name": self.name,
            "account": self.account,
            "plugin": self.plugin,
            "action": self.action,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "content": self.content,
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
    "Task",
    "TaskStatus",
    "TASK_STATUSES",
    "TERMINAL_STATUSES",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "STATUS_TIMED_OUT",
]
