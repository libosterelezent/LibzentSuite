"""Soulcopy flow safety: TargetGone aborts the whole flow with one clean line.

Covers the FlowBlocked contract from core/errors.py as seen from the plugin:
  * a trigger channel missing from the account's cache aborts the flow
    BEFORE anything is sent (TargetGone, "not in the account's cache"),
  * the plugin logs exactly one WARNING (no container-recorded crash),
  * flow_block_reason rendering (pure helper).
"""
from __future__ import annotations

import re
import unittest

from core.errors import AccessDenied, FlowBlocked, TargetGone
from core.events import MessageEvent
from plugins.soulcopy.plugin import flow_block_reason

from .helpers import TEST_CHANNEL, Harness

BOGUS_CHANNEL = 987654  # absent from the harness world AND the account cache
FAST_SETTINGS = {"soulcopy": {"dm_read_timeout": 0.5, "inter_chunk_delay": 0.01, "max_chunk_length": 60}}


class TestFlowBlockReason(unittest.TestCase):
    """Pure helper: reason extraction for the one-line abort log."""

    def test_flow_blocked_subclasses_yield_their_reason(self) -> None:
        self.assertEqual(flow_block_reason(TargetGone("alpha", "channel 7 gone")), "channel 7 gone")
        self.assertEqual(flow_block_reason(AccessDenied("beta", "access denied: perms")), "access denied: perms")

    def test_generic_exception_gets_type_and_message(self) -> None:
        self.assertEqual(flow_block_reason(KeyError("channel gone")), "KeyError: 'channel gone'")
        self.assertEqual(flow_block_reason(ValueError("bad setting")), "ValueError: bad setting")


class TestTargetGoneAbortsFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = Harness("plugins", ["alpha"], global_settings=FAST_SETTINGS)
        await self.harness.container.start_account("alpha")  # attaches + ingests the cache

    async def asyncTearDown(self) -> None:
        await self.harness.container.stop_all()

    # ------------------------------------------------------------- internals
    def _soulcopy_runtime(self):
        runtime = self.harness.container._accounts["alpha"]  # noqa: SLF001 (test accessor)
        return runtime, runtime.plugins["soulcopy"]

    async def test_run_copy_raises_target_gone_for_uncached_channel(self) -> None:
        runtime, prt = self._soulcopy_runtime()
        ctx = self.harness.container._context(runtime, prt)  # noqa: SLF001
        with self.assertRaises(FlowBlocked) as cm:
            await prt.instance._run_copy(ctx, BOGUS_CHANNEL)
        self.assertIsInstance(cm.exception, TargetGone)
        self.assertIn("not in the account's cache", cm.exception.reason)
        # aborted BEFORE any send: not one message anywhere in the world
        self.assertEqual(sum(len(h) for h in self.harness.world.history.values()), 0)

    async def test_published_trigger_aborts_with_single_warning_line(self) -> None:
        _, prt = self._soulcopy_runtime()
        with self.assertLogs("slaughtermudae.alpha.soulcopy", level="WARNING") as captured:
            await self.harness.container.bus.publish(
                MessageEvent(
                    account="alpha", message_id=1, channel_id=BOGUS_CHANNEL,
                    author_id=self.harness.owner_id, content="slcopy",
                )
            )
        joined = "\n".join(captured.output)
        self.assertIn("flow aborted for alpha", joined)
        self.assertIn("not in the account's cache", joined)
        self.assertEqual(len(captured.output), 1)  # exactly ONE line, no spam
        # the plugin handled it itself: the container saw NO crash
        self.assertEqual(prt.error_count, 0)
        self.assertIsNone(prt.last_error)
        # ...and nothing reached the real trigger channel either
        self.assertEqual(self.harness.world.messages_of(TEST_CHANNEL), [])

    async def test_non_owner_warning_names_the_account(self) -> None:
        with self.assertLogs("slaughtermudae.alpha.soulcopy", level="WARNING") as captured:
            await self.harness.container.bus.publish(
                MessageEvent(
                    account="alpha", message_id=2, channel_id=TEST_CHANNEL,
                    author_id=4242, content="slcopy",  # not in OWNER_USER_IDS
                )
            )
        joined = "\n".join(captured.output)
        self.assertIn("non-owner 4242", joined)
        self.assertIn("alpha", joined)
        self.assertEqual(self.harness.world.messages_of(TEST_CHANNEL), [])

    async def test_happy_path_logs_target_via_cache_describe(self) -> None:
        """The normal flow still works and names '#test-channel in sim-guild'."""
        with self.assertLogs("slaughtermudae.alpha.soulcopy", level="INFO") as captured:
            await self.harness.container.bus.publish(
                MessageEvent(
                    account="alpha", message_id=3, channel_id=TEST_CHANNEL,
                    author_id=self.harness.owner_id, content="slcopy",
                )
            )
            await self.harness.world.wait_for_quiet(TEST_CHANNEL, quiet=1.5, limit=20.0)
        joined = "\n".join(captured.output)
        self.assertRegex(joined, r"sent \d+ chunk\(s\) back to #test-channel in sim-guild")
        transcript = self.harness.world.messages_of(TEST_CHANNEL)
        self.assertEqual(transcript[0], "$slud-s")  # export command went out
        self.assertTrue(transcript[1].startswith("$soulcopy "))


if __name__ == "__main__":
    unittest.main()
