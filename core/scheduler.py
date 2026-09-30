"""Task Scheduler: interval jobs with jitter, persisted in the database.

A job = "send this command from this account into this channel every N
seconds (+jitter)". The scheduler only fires; all sends still flow through
the account's serialized lane and the rate limiter — scheduling NEVER
bypasses safety. Jobs persist in the DB (kv 'jobs'), are toggled at runtime
(CLI `jobs/job-add/job-del/job-on/job-off`, dashboard panel) and re-arm on
boot. Failures are contained per job: a failing job is logged and skipped,
never killing the loop or other jobs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("slaughtermudae.scheduler")

MIN_INTERVAL = 5.0  # seconds; anything faster is below the safety floor anyway


class SchedulerError(ValueError):
    """Raised for invalid job definitions."""


@dataclass
class Job:
    name: str
    account: str
    channel_id: int
    command: str
    interval: float
    jitter: float = 0.0          # extra random 0..jitter seconds per fire
    enabled: bool = True
    last_run: float = 0.0
    run_count: int = 0
    last_error: Optional[str] = None

    def __post_init__(self) -> None:
        # NOTE: interval floor is enforced by TaskScheduler.add_job against
        # its configurable min_interval (tests relax it; production = 5s)
        if float(self.jitter) < 0:
            raise SchedulerError(f"job {self.name!r}: jitter must be >= 0")
        if not self.command or not self.account:
            raise SchedulerError(f"job {self.name!r}: account and command are required")

    def next_delay(self, rng: random.Random) -> float:
        return self.interval + (rng.uniform(0, self.jitter) if self.jitter else 0.0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "account": self.account, "channel_id": self.channel_id,
            "command": self.command, "interval": self.interval, "jitter": self.jitter,
            "enabled": self.enabled, "last_run": round(self.last_run, 2),
            "run_count": self.run_count, "last_error": self.last_error,
        }


class TaskScheduler:
    """Interval job runner over the serialized send lanes."""

    def __init__(
        self,
        database: Optional[Any] = None,
        *,
        send: Optional[Callable[[str, int, str], Any]] = None,
        tick: float = 1.0,
        seed: Optional[int] = None,
        min_interval: float = MIN_INTERVAL,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._db = database
        self._send = send              # async (account, channel_id, command) -> result
        self._tick = tick
        self._min_interval = float(min_interval)  # tests may relax; production keeps 5s
        self._rng = random.Random(seed)
        self._jobs: Dict[str, Job] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._logger = logger or LOG
        self._load()

    # ------------------------------------------------------------- lifecycle
    def _load(self) -> None:
        """Restore persisted jobs from the DB (kv 'jobs')."""
        if self._db is None:
            return
        raw = self._db.kv_get("jobs")
        if not raw:
            return
        try:
            for spec in json.loads(raw):
                try:
                    job = Job(
                        name=spec["name"], account=spec["account"],
                        channel_id=int(spec["channel_id"]), command=spec["command"],
                        interval=float(spec["interval"]),
                        jitter=float(spec.get("jitter", 0.0)),
                        enabled=bool(spec.get("enabled", True)),
                    )
                    job.run_count = int(spec.get("run_count", 0))
                    self._jobs[job.name] = job
                except (KeyError, SchedulerError, ValueError) as exc:
                    LOG.warning("skipping invalid stored job %r: %s", spec.get("name"), exc)
        except json.JSONDecodeError:
            LOG.warning("stored jobs unreadable; starting empty")
        LOG.info("scheduler restored %d job(s)", len(self._jobs))

    def _persist(self) -> None:
        if self._db is not None:
            self._db.kv_set("jobs", json.dumps([j.as_dict() for j in self._jobs.values()]))

    def start(self) -> "TaskScheduler":
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.get_running_loop().create_task(
                self._run(), name="task-scheduler"
            )
        return self

    async def stop(self) -> None:
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------- jobs
    def add_job(self, name: str, account: str, channel_id: int, command: str,
                interval: float, jitter: float = 0.0, enabled: bool = True) -> Job:
        if name in self._jobs:
            raise SchedulerError(f"job {name!r} already exists (delete it first)")
        if float(interval) < self._min_interval:
            raise SchedulerError(
                f"job {name!r}: interval {interval}s is below the "
                f"{self._min_interval}s minimum (use the rate limiter for finer pacing)"
            )
        job = Job(name=name, account=account, channel_id=int(channel_id),
                  command=command, interval=float(interval), jitter=float(jitter),
                  enabled=enabled)
        self._jobs[name] = job
        self._persist()
        LOG.info("job added: %s (%s every %.0fs)", name, account, interval)
        return job

    def delete_job(self, name: str) -> bool:
        existed = self._jobs.pop(name, None) is not None
        if existed:
            self._persist()
        return existed

    def set_enabled(self, name: str, enabled: bool) -> bool:
        job = self._jobs.get(name)
        if job is None:
            raise SchedulerError(f"unknown job {name!r}")
        job.enabled = enabled
        self._persist()
        return True

    def jobs(self) -> List[Dict[str, Any]]:
        return [j.as_dict() for j in sorted(self._jobs.values(), key=lambda j: j.name)]

    # ------------------------------------------------------------------ loop
    async def _run(self) -> None:
        try:
            while self._loop_task is not None:
                now = time.time()
                for job in list(self._jobs.values()):
                    if not job.enabled:
                        continue
                    if now - (job.last_run or 0) >= job.interval + self._rng.uniform(
                        0, job.jitter or 0.0
                    ):
                        asyncio.create_task(self._fire(job))
                await asyncio.sleep(self._tick)
        except asyncio.CancelledError:
            return

    async def _fire(self, job: Job) -> None:
        """Run one job (contained: failures are logged + recorded, never fatal)."""
        if self._send is None:
            job.last_error = "no send function wired"
            return
        try:
            await self._send(job.account, job.channel_id, job.command)
            job.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.last_error = f"{type(exc).__name__}: {exc}"
            LOG.warning("job %s failed: %s", job.name, job.last_error)
        job.last_run = time.time()
        job.run_count += 1
        self._persist()
