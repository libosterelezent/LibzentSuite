"""P4 — profile-attributed tasks and the runtime resolution seam.

P1's deferred dimension: a task carries the profile whose execution
context produced it, so estop/cancel can target ONE profile's work
without touching another profile's — attribution and scoping, while
pacing stays account-level (the server's limits do not care about
profiles; the safety floor is per account).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from libzentsuite.core.account_manager import DiscordCache
from libzentsuite.core.account_manager.services import AccountPluginServices
from libzentsuite.core.event_bus import EventBus
from libzentsuite.core.task_scheduler import Task, TaskScheduler
from libzentsuite.database.db import Database


class _SilentLogger:
    def debug(self, *a: Any, **k: Any) -> None:
        pass

    def info(self, *a: Any, **k: Any) -> None:
        pass

    def warning(self, *a: Any, **k: Any) -> None:
        pass

    def error(self, *a: Any, **k: Any) -> None:
        pass

    def exception(self, *a: Any, **k: Any) -> None:
        pass


class TaskAttribution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from libzentsuite.core.event_bus import EventBus

        async def parked_sender(task: Task) -> None:  # never completes: tasks stay live
            await asyncio.Event().wait()

        self.sched = TaskScheduler(EventBus(), _SilentLogger(), None, parked_sender, audit=None)
        self.sched.start()

    async def asyncTearDown(self) -> None:
        await self.sched.stop(drain=False)

    async def test_submit_carries_profile_and_tasks_list_shows_it(self) -> None:
        task = self.sched.submit("alpha", "exporter", "message", profile="main")
        self.assertEqual(task.profile, "main")
        rows = self.sched.tasks()
        self.assertEqual([r.get("profile") for r in rows], ["main"])

    async def test_default_profile_is_the_profile_less_scope(self) -> None:
        task = self.sched.submit("alpha", "exporter", "message")
        self.assertEqual(task.profile, "")

    async def test_cancel_matching_targets_one_profile_only(self) -> None:
        for _ in range(3):
            self.sched.submit("alpha", "exporter", "message", profile="main", timeout=None)
        for _ in range(2):
            self.sched.submit("alpha", "exporter", "message", profile="alt", timeout=None)
        cancelled = self.sched.cancel_matching(account="alpha", plugin="exporter", profile="alt")
        self.assertEqual(cancelled, 2)
        remaining = {t.profile for t in self.sched._tasks.values()}
        # the parked submits: exactly main survives, alt is gone
        self.assertEqual(remaining, {"main"})


class ResolutionSeam(unittest.IsolatedAsyncioTestCase):
    """profile_settings(): the ACTIVE profile's body for task start."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "r.db")
        await self.db.connect()
        # bus/cache are non-Optional on the pinned constructor; inert
        # real instances stand in — the profile-store paths under test
        # never touch them.
        self.svc = AccountPluginServices(
            "alpha",
            "exporter",
            bus=EventBus(),
            scheduler=None,
            credentials=None,
            cache=DiscordCache("alpha"),
            profile_store=self.db.account_plugin_profiles,
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_none_when_no_active_profile(self) -> None:
        self.assertIsNone(await self.svc.profile_settings())

    async def test_resolves_the_active_body(self) -> None:
        await self.svc.profile_create("main", settings={"chunk": 1800})
        await self.svc.profile_set_active("main")
        self.assertEqual(await self.svc.profile_settings(), {"chunk": 1800})
        # switching profiles switches the resolved body
        await self.svc.profile_create("alt", settings={"chunk": 900})
        await self.svc.profile_set_active("alt")
        self.assertEqual(await self.svc.profile_settings(), {"chunk": 900})

    async def test_clearing_active_returns_none(self) -> None:
        await self.svc.profile_create("main")
        await self.svc.profile_set_active("main")
        await self.svc.profile_delete("main")  # pointer clears with it
        self.assertIsNone(await self.svc.profile_settings())


if __name__ == "__main__":
    unittest.main()
