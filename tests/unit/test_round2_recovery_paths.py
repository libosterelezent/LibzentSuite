"""Round-2 audit pins: silent-recovery paths in core/ that were untested.

``except: pass`` sites that swallow a collaborator failure and KEEP GOING
are only safe if the surrounding contract still holds. These pins cover
the three riskiest untested ones (plus the rate-engine logger swallow):

1. credential_service.py:156-160 — resolve() survives a refresh() that
   explodes and still loads + decrypts the stored token (boot path for
   every account connect).
2. account_manager/services.py:158-169 — a waiter timing out with a
   scheduler whose cancel() RAISES still gets the SchedulerError (not
   the cancel exception) and leaves no future behind.
3. plugin_manager/manager.py:335-343 — a plugin whose on_disable raises
   during deactivate_account is still unloaded (on_unload + event);
   set_enabled(False) likewise survives a raising on_disable.
4. rate_limit_manager/engine.py:1020-1029 — a logger whose methods
   raise must never break policy edits or acquire (pacing fast path).
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from core.account_manager.services import TaskAckRegistry
from core.credential_service import CredentialService
from core.errors import SchedulerError
from core.event_bus import EventBus, PluginEnabledChanged, PluginUnloaded
from core.plugin_manager import PluginManager
from core.rate_limit_manager.engine import ManualClock, RateLimitManager
from core.settings_manager import SettingsManager

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
LOG = logging.getLogger("tests.round2.recovery")


class ExplodingListCredentialRepo:
    """accounts_with_credential() always explodes; load/store work."""

    def __init__(self) -> None:
        self.rows: dict[str, bytes] = {}

    async def accounts_with_credential(self) -> None:
        raise RuntimeError("credentials listing exploded")

    async def load(self, account: str) -> bytes | None:
        return self.rows.get(account)

    async def store(self, account: str, ciphertext: bytes) -> None:
        self.rows[account] = ciphertext

    async def delete(self, account: str) -> bool:
        return self.rows.pop(account, None) is not None


class TestCredentialResolveSurvivesRefreshExplosion(unittest.IsolatedAsyncioTestCase):
    def _service(self, repo: ExplodingListCredentialRepo) -> CredentialService:
        """Over a DISPOSABLE key file (tempfile) — never a fixed shared path."""
        tmp = tempfile.TemporaryDirectory(prefix="lz-round2-cred-")
        self.addCleanup(tmp.cleanup)
        return CredentialService(repo, key_path=Path(tmp.name) / "test.key")

    async def test_resolve_loads_token_despite_broken_listing(self) -> None:
        repo = ExplodingListCredentialRepo()
        svc = self._service(repo)
        await repo.store("alpha", svc.fernet.encrypt(b"tok-alpha"))

        self.assertEqual(await svc.resolve("alpha"), "tok-alpha")
        self.assertEqual(await svc.resolve("alpha"), "tok-alpha")  # cached

    async def test_resolve_of_truly_missing_account_stays_none(self) -> None:
        repo = ExplodingListCredentialRepo()
        svc = self._service(repo)
        self.assertIsNone(await svc.resolve("ghost"))


class CancelBoomScheduler:
    """Duck-typed scheduler whose cancel() always raises."""

    def __init__(self) -> None:
        self.cancelled: list[int] = []

    def cancel(self, task_id: int) -> NoReturn:
        self.cancelled.append(task_id)
        raise RuntimeError("cancel exploded")


class TestTaskAckTimeoutSurvivesCancelExplosion(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_raises_scheduler_error_not_cancel_boom(self) -> None:
        scheduler = CancelBoomScheduler()
        registry = TaskAckRegistry(EventBus(), scheduler=scheduler)
        with self.assertRaises(SchedulerError) as caught:
            await registry.wait_for_task(7, timeout=0.02)
        self.assertIn("did not complete", str(caught.exception))
        self.assertEqual(scheduler.cancelled, [7])  # cancel was attempted
        self.assertEqual(registry.pending(), [])  # no future leaked


def make_plugin_manager() -> tuple[PluginManager, EventBus]:
    bus = EventBus()
    settings = SettingsManager(bus, LOG)
    manager = PluginManager(bus, LOG, settings, plugin_dir=str(FIXTURES))
    return manager, bus


def raising_on_disable(
    fired: list[str],
) -> Callable[[Any], Coroutine[Any, Any, None]]:
    async def _boom(ctx: Any) -> None:
        fired.append("on_disable")
        raise RuntimeError("on_disable exploded")

    return _boom


class TestDeactivateSurvivesRaisingOnDisable(unittest.IsolatedAsyncioTestCase):
    async def test_raising_on_disable_still_unloads_and_publishes(self) -> None:
        manager, bus = make_plugin_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", lambda name: None)
        spy = manager._accounts["alpha"]["spy"]
        fired: list[str] = []
        spy.instance.on_disable = raising_on_disable(fired)
        unloaded: list[PluginUnloaded] = []
        bus.subscribe(PluginUnloaded, unloaded.append)

        await manager.deactivate_account("alpha")
        await bus.drain()  # publish_nowait defers dispatch to loop tasks

        self.assertEqual(fired, ["on_disable"])
        self.assertIn("on_unload", spy.instance.calls)
        self.assertFalse(spy.active)
        self.assertFalse(spy.loaded)
        self.assertTrue(
            any(e.plugin == "spy" for e in unloaded),
            "on_unload event must still fire after on_disable raised",
        )

    async def test_set_enabled_false_survives_raising_on_disable(self) -> None:
        manager, bus = make_plugin_manager()
        await manager.attach_account("alpha")
        await manager.activate_account("alpha", lambda name: None)
        spy = manager._accounts["alpha"]["spy"]
        fired: list[str] = []
        spy.instance.on_disable = raising_on_disable(fired)
        events: list[PluginEnabledChanged] = []
        bus.subscribe(PluginEnabledChanged, events.append)

        await manager.set_enabled("alpha", "spy", False)
        await bus.drain()  # publish_nowait defers dispatch to loop tasks

        self.assertEqual(fired, ["on_disable"])
        self.assertFalse(spy.enabled)
        self.assertTrue(any(e.plugin == "spy" and e.enabled is False for e in events))


class HostileLogger:
    """Every method raises — logging must never break pacing."""

    def info(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("log info exploded")

    def warning(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("log warning exploded")

    def error(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("log error exploded")

    def debug(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("log debug exploded")


class TestRateEngineSurvivesHostileLogger(unittest.IsolatedAsyncioTestCase):
    async def test_policy_edit_and_acquire_with_raising_logger(self) -> None:
        manager = RateLimitManager(
            EventBus(),
            HostileLogger(),
            None,
            clock=ManualClock(0.0),
            jitter_seed=7,
        )
        policy = manager.set_policy_values("message", 1.5, 0.0, 0.0)
        self.assertEqual(policy.interval, 1.5)
        grant = await manager.acquire("message")
        self.assertIsNotNone(grant)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
