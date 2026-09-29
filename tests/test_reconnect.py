"""Auto-reconnect supervisor: backoff, reattach, deliberate-stop behavior."""
from __future__ import annotations

import asyncio
import unittest

from core.account import AccountStatus, ReconnectPolicy

from .helpers import TEST_CHANNEL, Harness


def fast_policy() -> ReconnectPolicy:
    return ReconnectPolicy(enabled=True, initial_delay=0.05, max_delay=0.2, multiplier=1.5)


async def wait_for_status(session, *statuses, timeout: float = 3.0) -> str:
    """Poll until the session reaches one of the given statuses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if session.status in statuses:
            return session.status
        await asyncio.sleep(0.02)
    return session.status


class TestReconnectPolicy(unittest.TestCase):
    def test_delay_growth_and_cap(self) -> None:
        policy = ReconnectPolicy(initial_delay=0.5, max_delay=4.0, multiplier=2.0)
        self.assertAlmostEqual(policy.delay_for(0), 0.5)
        self.assertAlmostEqual(policy.delay_for(1), 1.0)
        self.assertAlmostEqual(policy.delay_for(2), 2.0)
        self.assertAlmostEqual(policy.delay_for(3), 4.0)   # capped
        self.assertAlmostEqual(policy.delay_for(9), 4.0)

    def test_from_dict_defaults(self) -> None:
        policy = ReconnectPolicy.from_dict(None)
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.to_dict()["multiplier"], 2.0)
        tuned = ReconnectPolicy.from_dict({"enabled": False, "initial": 0.25})
        self.assertFalse(tuned.enabled)
        self.assertAlmostEqual(tuned.initial_delay, 0.25)


class TestSupervisor(unittest.IsolatedAsyncioTestCase):
    async def test_reconnects_after_drop(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], reconnect_policy=fast_policy())
        spy = harness.fixture_module("spy")
        spy.reset()
        await harness.container.start_account("alpha")
        session = harness.session("alpha")
        old_client = session._client
        self.assertEqual(session.status, AccountStatus.RUNNING)

        old_client.drop("simulated network blip")
        self.assertEqual(await wait_for_status(session, AccountStatus.RUNNING), AccountStatus.RUNNING)
        self.assertIsNot(session._client, old_client)  # a FRESH client was attached
        self.assertFalse(session.lost.is_set())

        # reconnected session still receives and dispatches events
        harness.world.deliver(TEST_CHANNEL, harness.owner_id, "after-drop")
        await asyncio.sleep(0.1)
        self.assertEqual(spy.EVENTS, [("alpha", harness.owner_id, "after-drop")])
        await harness.container.stop_account("alpha")

    async def test_no_reconnect_when_disabled(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], reconnect_policy=ReconnectPolicy(enabled=False))
        await harness.container.start_account("alpha")
        session = harness.session("alpha")
        session._client.drop()
        await asyncio.sleep(0.3)
        self.assertEqual(session.status, AccountStatus.RECONNECTING)  # stuck, by choice
        await harness.container.stop_account("alpha")

    async def test_deliberate_stop_does_not_reconnect(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], reconnect_policy=fast_policy())
        await harness.container.start_account("alpha")
        await harness.container.stop_account("alpha")
        session = harness.session("alpha")
        self.assertFalse(session.lost.is_set())
        await asyncio.sleep(0.3)
        self.assertEqual(session.status, AccountStatus.STOPPED)

    async def test_restart_after_stop_starts_clean(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], reconnect_policy=fast_policy())
        container = harness.container
        await container.start_account("alpha")
        await container.stop_account("alpha")
        await container.start_account("alpha")  # stop_requested reset
        session = harness.session("alpha")
        self.assertEqual(session.status, AccountStatus.RUNNING)
        await container.stop_account("alpha")

    async def test_transient_factory_failure_retries(self) -> None:
        harness = Harness("tests/fixtures/plugins", ["alpha"], reconnect_policy=fast_policy())
        attempts = {"count": 0}
        real_factory = harness.container._client_factory

        def flaky_factory(config, credentials):
            attempts["count"] += 1
            if attempts["count"] == 2:  # fail the first RECONNECT attempt only
                raise RuntimeError("factory hiccup")
            return real_factory(config, credentials)

        harness.container._client_factory = flaky_factory
        await harness.container.start_account("alpha")
        session = harness.session("alpha")
        session._client.drop()
        self.assertEqual(await wait_for_status(session, AccountStatus.RUNNING), AccountStatus.RUNNING)
        self.assertGreaterEqual(attempts["count"], 3)  # start + failed + successful retry
        await harness.container.stop_account("alpha")


if __name__ == "__main__":
    unittest.main()
