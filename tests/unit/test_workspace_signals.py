"""Signal outbox tests (W4) — decision 12's delivery contract.

Real SignalsRepository over a temp database, the real SignalOutbox
semantics on top: durable acceptance BEFORE acknowledgement,
at-least-once delivery with attempt counting, dead-lettering after
bounded retries, idempotent acceptance, retention purging, and the
plugin-facing emit_signal seam (unwired stacks fail honestly).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from core.workspace.models import InvalidDefinition, SignalIdentity
from core.workspace.signals import DEAD_LETTER_MAX_ATTEMPTS, SignalOutbox
from database.db import Database


class SignalOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.delivered: list[str] = []
        self.fail_next = 0
        self.outbox = SignalOutbox(self.db.signals, deliver=self._deliver)

    async def _deliver(self, signal: SignalIdentity) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("consumer down")
        self.delivered.append(signal.signal_id)

    async def test_accept_persists_before_ack(self) -> None:
        signal_id = await self.outbox.accept("A", "grinder", "claim_done", {"n": 1})
        # the row EXISTS the moment accept returned (durable-before-ack)
        row = await self.db.signals.signal(signal_id)
        assert row is not None
        self.assertEqual(row.account, "A")
        self.assertEqual(row.namespaced, "grinder:claim_done")
        pending = await self.db.signals.pending(10)
        self.assertEqual([s.signal_id for s in pending], [signal_id])

    async def test_delivery_acks_and_retries(self) -> None:
        signal_id = await self.outbox.accept("A", "grinder", "claim_done")
        self.fail_next = 1  # first sweep fails, second delivers
        first = await self.outbox.deliver_pending()
        self.assertEqual((first["failed"], first["delivered"]), (1, 0))
        second = await self.outbox.deliver_pending()
        self.assertEqual((second["delivered"], second["failed"]), (1, 0))
        self.assertEqual(self.delivered, [signal_id])
        # acked: not pending anymore
        self.assertEqual(await self.db.signals.pending(10), [])

    async def test_dead_letter_after_bounded_retries(self) -> None:
        signal_id = await self.outbox.accept("A", "grinder", "boom")
        for _ in range(DEAD_LETTER_MAX_ATTEMPTS):
            self.fail_next = 1
            await self.outbox.deliver_pending()
        # the NEXT failing sweep dead-letters... actually the sweep that
        # OBSERVES attempts >= max dead-letters immediately: the final
        # failing sweep above already classified it
        row = await self.db.connection.fetch_one(
            "SELECT delivery_state, last_error FROM signals WHERE signal_id=?", (signal_id,)
        )
        assert row is not None
        self.assertEqual(row["delivery_state"], "dead")
        self.assertIn("consumer down", row["last_error"])
        # dead letters are never re-delivered
        self.fail_next = 0
        again = await self.outbox.deliver_pending()
        self.assertEqual(again["delivered"], 0)

    async def test_invalid_emission_rejected_before_persistence(self) -> None:
        with self.assertRaises(InvalidDefinition):
            await self.outbox.accept("A", "grinder", "grinder:spoof")  # namespaced
        with self.assertRaises(InvalidDefinition):
            await self.outbox.accept("A", "grinder", "")  # bare-empty
        self.assertEqual(await self.db.signals.pending(10), [])  # nothing stored

    async def test_accepted_id_dedupe_companion(self) -> None:
        await self.outbox.accept("A", "grinder", "claim_done")
        found = await self.outbox.accepted_id("A", "grinder", "claim_done")
        self.assertIsNotNone(found)
        self.assertIsNone(await self.outbox.accepted_id("A", "other", "claim_done"))

    async def test_purge_retention(self) -> None:
        await self.outbox.accept("A", "grinder", "done")
        self.fail_next = 0
        await self.outbox.deliver_pending()
        removed = await self.outbox.purge(older_than_s=0.0)  # everything old
        self.assertEqual(removed, 1)
        self.assertEqual(await self.db.signals.pending(10), [])


class EmitSignalFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unwired_stack_fails_honestly(self) -> None:
        from core.account_manager.cache import DiscordCache
        from core.account_manager.services import AccountPluginServices
        from core.event_bus import EventBus

        services = AccountPluginServices(
            "A",
            "cleaner",
            bus=EventBus(),
            scheduler=None,
            credentials=None,
            cache=DiscordCache("A"),
        )
        with self.assertRaises(RuntimeError) as ctx:
            await services.emit_signal("claim_done", {})
        self.assertIn("not configured", str(ctx.exception))

    async def test_wired_facade_routes_to_outbox(self) -> None:
        from core.account_manager.cache import DiscordCache
        from core.account_manager.services import AccountPluginServices
        from core.event_bus import EventBus

        class StubOutbox:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str, dict[str, Any]]] = []

            async def accept(
                self,
                account: str,
                plugin: str,
                name: str,
                payload: dict[str, Any] | None = None,
                **_: Any,
            ) -> str:
                self.calls.append((account, plugin, name, payload or {}))
                return "sig-1"

        stub = StubOutbox()
        # AccountPluginServices types the collaborator nominally
        # (SignalOutbox); the stub implements the same accept contract.
        signals_seam: Any = stub
        services = AccountPluginServices(
            "A",
            "cleaner",
            bus=EventBus(),
            scheduler=None,
            credentials=None,
            cache=DiscordCache("A"),
            signals=signals_seam,
        )
        out = await services.emit_signal("claim_done", {"n": 1})
        self.assertEqual(out, "sig-1")
        self.assertEqual(stub.calls, [("A", "cleaner", "claim_done", {"n": 1})])


if __name__ == "__main__":
    unittest.main()
