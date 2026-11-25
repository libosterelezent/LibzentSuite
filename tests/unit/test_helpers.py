"""Contract tests for the shared helpers themselves (tests/unit/helpers.py)."""

import unittest

from tests.unit.helpers import wait_until_async


class WaitUntilAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_true_when_predicate_already_true(self) -> None:
        self.assertTrue(await wait_until_async(lambda: True, timeout=0.5))

    async def test_true_on_the_third_poll(self) -> None:
        polls = 0

        def ready() -> bool:
            nonlocal polls
            polls += 1
            return polls >= 3

        self.assertTrue(await wait_until_async(ready, timeout=5.0, interval=0.01))
        self.assertEqual(polls, 3)

    async def test_false_when_predicate_never_true(self) -> None:
        self.assertFalse(await wait_until_async(lambda: False, timeout=0.1, interval=0.02))

    async def test_zero_timeout_still_checks_once(self) -> None:
        # timeout=0 skips the polling loop, but the predicate is still
        # evaluated by the final return — a predicate already true at the
        # deadline wins rather than losing on a technicality.
        checks = 0

        def flag() -> bool:
            nonlocal checks
            checks += 1
            return True

        self.assertTrue(await wait_until_async(flag, timeout=0.0))
        self.assertEqual(checks, 1)
