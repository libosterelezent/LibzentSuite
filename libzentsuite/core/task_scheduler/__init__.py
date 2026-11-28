"""Centralized task scheduler package (A5).

Public surface (INTEGRATION 'core/task_scheduler'):
    TaskScheduler — queue/priority/cancel/retry/timeout/history
    Task          — the task dataclass
    TaskStatus    — status literal type (see tasks.STATUS_* constants)
    SchedulerError — re-exported from core.errors for caller convenience
"""

from libzentsuite.core.errors import SchedulerError

from .scheduler import TaskScheduler
from .tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_TIMED_OUT,
    TASK_STATUSES,
    TERMINAL_STATUSES,
    Task,
    TaskStatus,
)

__all__ = [
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_TIMED_OUT",
    "TASK_STATUSES",
    "TERMINAL_STATUSES",
    "SchedulerError",
    "Task",
    "TaskScheduler",
    "TaskStatus",
]
