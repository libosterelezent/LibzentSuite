"""FINAL-PHASE regression tests: caches, memory-first status, lifecycle
scheduling, burst dispatch integration, security hardening."""

from __future__ import annotations

import asyncio
import os
import stat
import unittest
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any

from core.account_manager import AccountStatus
from core.account_manager.services import MessageWaiterRegistry
from core.cooldowns import CooldownService
from core.event_bus import EventBus, MessageReceived
from core.rate_limit_manager.engine import RateLimitManager
from tests.unit.helpers import enforces_file_modes, make_manager
from tests.unit.test_rate_engine import StubLogger

if TYPE_CHECKING:
    from database.models import AccountRecord


def _msg(account: str, i: int) -> MessageReceived:
    return MessageReceived(
        account=account, message_id=i, channel_id=500, author_id=1, content=f"m{i}"
    )


def _shadow_repo_get(repo: Any, getter: Any) -> None:
    """Instance-attribute shadow of a repository method (mypy forbids
    the method-slot store; the runtime behaviour is identical)."""
    repo.get = getter


class WaiterRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_invoked_only_for_own_account(self) -> None:
        """Isolation-by-COUNT: the handler must never even RUN for another
        account's events (stronger than result-correctness)."""
        bus = EventBus()
        registry = MessageWaiterRegistry(bus)
        calls = {"alpha": 0, "beta": 0}

        def alpha_handler(event: MessageReceived) -> None:
            calls["alpha"] += 1

        registry.add("alpha", alpha_handler)
        for i in range(100):
            bus.publish_nowait(_msg("alpha" if i % 2 else "beta", i))
        await bus.drain(timeout=10)
        self.assertEqual(calls["alpha"], 50)  # ONLY alpha's events
        self.assertEqual(calls["beta"], 0)
        registry.close()

    async def test_forget_account_drops_only_that_account(self) -> None:
        bus = EventBus()
        registry = MessageWaiterRegistry(bus)
        seen: list[str] = []
        registry.add("alpha", lambda e: seen.append("a"))
        registry.add("beta", lambda e: seen.append("b"))
        registry.forget_account("alpha")
        bus.publish_nowait(_msg("alpha", 1))
        bus.publish_nowait(_msg("beta", 1))
        await bus.drain(timeout=5)
        self.assertEqual(seen, ["b"])
        registry.close()

    async def test_wait_for_message_through_registry(self) -> None:
        """The facade path: account-filtered delivery end to end."""
        h = make_manager()
        bus = h.bus
        registry = MessageWaiterRegistry(bus)
        from core.account_manager.cache import DiscordCache
        from core.account_manager.services import AccountPluginServices

        # a fresh DiscordCache IS the empty channels/guilds view the NS
        # double used to fake (both empty dicts at construction)
        facade = AccountPluginServices(
            "alpha",
            "roll",
            bus=bus,
            scheduler=NS(submit=lambda *a, **k: None),
            credentials=h.credentials,
            cache=DiscordCache("alpha"),
            waiter_registry=registry,
        )

        async def deliver() -> None:
            await asyncio.sleep(0.05)
            bus.publish_nowait(_msg("beta", 1))  # wrong account first
            bus.publish_nowait(_msg("alpha", 2))  # then the match

        task = asyncio.create_task(deliver())
        result = await facade.wait_for_message(lambda e: True, timeout=2.0)
        await task
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.account, "alpha")
        registry.close()

    async def test_manager_removal_forgets_waiters(self) -> None:
        h = make_manager()
        h.manager._waiter_registry.add("alpha", lambda e: None)
        await h.manager.add_account("alpha")
        await h.manager.remove_account("alpha")
        self.assertEqual(h.manager._waiter_registry.pending(), {})


class EffectiveCacheTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self) -> RateLimitManager:
        return RateLimitManager(EventBus(), StubLogger(), None)

    async def test_hit_equivalence_and_fresh_dicts(self) -> None:
        m = self._engine()
        m.set_policy_values("message", 3.0, 0.1, 0.5)
        m.set_policy_values("message", 2.0, 0.2, 0.6, "account", "alpha")
        a = m.effective("message", "alpha", "roll")
        b = m.effective("message", "alpha", "roll")
        self.assertEqual(a, b)
        self.assertIsNot(a, b)  # fresh dict per call
        a["sources"].append("junk")  # caller mutation cannot
        c = m.effective("message", "alpha", "roll")  # poison the cache
        self.assertNotIn("junk", c["sources"])
        # cache actually populated + version-consistent
        self.assertEqual(len(m._effective_cache), 1)
        self.assertEqual(m._config_version, 2)  # 2 policy writes bumped

    async def test_invalidation_on_every_mutation(self) -> None:
        m = self._engine()
        m.set_policy_values("message", 3.0, 0.1, 0.5, "account", "alpha")
        self.assertEqual(m.effective("message", "alpha")["interval"], 3.0)
        m.set_policy_values("message", 5.0, 0.1, 0.5, "account", "alpha")
        self.assertEqual(m.effective("message", "alpha")["interval"], 5.0)
        m.clear_policy("account", "alpha", "message")
        # cleared → LEARNED default for message applies (rate study)
        self.assertEqual(m.effective("message", "alpha")["interval"], 2.6)
        m.set_floor(4.0)
        self.assertEqual(m.effective("message", "alpha")["interval"], 4.0)
        await m.prune_account("alpha")
        self.assertEqual(len(m._effective_cache), 0)  # prune invalidated

    async def test_group_change_does_not_invalidate(self) -> None:
        m = self._engine()
        m.set_policy_values("message", 3.0, 0.1, 0.5)
        before = m.effective("message", "alpha")
        m.set_account_group("alpha", "g1")
        self.assertEqual(m.effective("message", "alpha"), before)

    async def test_cap_bounds_the_cache(self) -> None:
        m = self._engine()
        import core.rate_limit_manager.engine as eng

        eng._EFFECTIVE_CACHE_CAP = 8
        try:
            for i in range(20):
                m.effective(f"act{i}", "alpha", "roll")
            self.assertLessEqual(len(m._effective_cache), 8)
            # post-flush reads still correct
            self.assertEqual(m.effective("act19", "alpha", "roll")["action"], "act19")
        finally:
            eng._EFFECTIVE_CACHE_CAP = 4096

    async def test_account_isolation_in_the_key(self) -> None:
        m = self._engine()
        m.set_policy_values("message", 3.0, 0.1, 0.5, "account", "a/b")
        # the delimiter trap: ("a/b", "c") must NOT collide with ("a", "b/c")
        self.assertEqual(m.effective("message", "a/b", "c")["interval"], 3.0)
        # no stored policy matches → the learned message default (2.6)
        self.assertEqual(m.effective("message", "a", "b/c")["interval"], 2.6)


class MemoryFirstStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_status_within_ttl_does_no_repo_reads(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        reads = {"n": 0}
        original_get = h.repo.get

        async def counting_get(name: str) -> AccountRecord | None:
            reads["n"] += 1
            return await original_get(name)

        # instance-attribute shadowing of the repo method
        _shadow_repo_get(h.repo, counting_get)
        out1 = await h.manager.accounts()  # memory-first: NO reads
        self.assertEqual(reads["n"], 0)
        out2 = await h.manager.accounts(refresh_records=True)  # explicit
        self.assertEqual(reads["n"], 1)
        self.assertEqual(out1[0]["name"], out2[0]["name"])

    async def test_cooldown_tombstones_purged_on_expiry(self) -> None:
        now = {"t": 1000.0}

        class FakeRepo:
            # coherent double: rows stored by upsert are visible to get
            # (a repo that "forgets" rows it just wrote reads as a
            # concurrent delete to the set() race guard)
            def __init__(self) -> None:
                self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}

            async def upsert(self, **kw: Any) -> None:
                self.rows[
                    (kw["account"], kw["plugin"], kw.get("profile", ""), kw["cooldown_key"])
                ] = dict(kw)

            async def get(
                self, *a: Any, now: float | None = None, profile: str = "", **k: Any
            ) -> dict[str, Any] | None:
                row = self.rows.get((a[0], a[1], profile, a[2]))
                if row is None or (now is not None and row["expires_at"] <= now):
                    return None
                return dict(row)

            async def clear_expired_for(self, *a: Any, **k: Any) -> int:
                return 0

            async def active_for(self, *a: Any, **k: Any) -> list[Any]:
                return []

            async def active_all(self, now: float | None = None) -> list[Any]:
                return []

        service = CooldownService(FakeRepo(), time_fn=lambda: now["t"])
        for i in range(50):
            await service.set("acct", "plug", f"k{i}", 10.0)
        self.assertEqual(len(service._active), 50)
        now["t"] += 11.0  # everything expires
        for i in range(50):
            await service.get("acct", "plug", f"k{i}")
        self.assertEqual(len(service._active), 0)  # tombstones purged

    async def test_resync_replaces_mirror_from_db(self) -> None:
        class FakeRepo:
            async def upsert(self, **kw: Any) -> None:
                pass

            async def get(self, *a: Any, **k: Any) -> None:
                return None

            async def clear_expired_for(self, *a: Any, **k: Any) -> int:
                return 0

            async def active_for(self, *a: Any, **k: Any) -> list[Any]:
                return []

            async def active_all(self, now: float | None = None) -> list[dict[str, Any]]:
                return [
                    {
                        "account": "alpha",
                        "plugin": "roll",
                        "cooldown_key": "roulette",
                        "started_at": 1.0,
                        "expires_at": 9e9,
                        "source": "",
                        "reason": "",
                        "metadata": {},
                    }
                ]

        service = CooldownService(FakeRepo(), time_fn=lambda: 1000.0)
        await service.set("alpha", "roll", "old", 10.0)
        count = await service.resync()
        self.assertEqual(count, 1)
        self.assertEqual(service.active_count_for("alpha"), 1)
        self.assertNotIn(("alpha", "roll", "old"), service._active)


class StartAllSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_connect_does_not_block_the_fleet(self) -> None:
        h = make_manager()
        for name in ("alpha", "beta", "gamma"):
            await h.manager.add_account(name)
        park = asyncio.Event()
        original_factory = h.manager._client_factory

        def parking_factory(name: str, token: str) -> Any:
            client = original_factory(name, token)
            if name == "alpha":
                orig_connect = client.connect

                async def parked_connect() -> None:
                    await park.wait()
                    await orig_connect()

                client.connect = parked_connect
            return client

        h.manager._client_factory = parking_factory
        task = asyncio.create_task(h.manager.start_all(stagger=0))
        await asyncio.sleep(0.3)  # beta/gamma must complete while alpha parks
        self.assertEqual(h.manager.get("beta").runtime.status, AccountStatus.RUNNING)
        self.assertEqual(h.manager.get("gamma").runtime.status, AccountStatus.RUNNING)
        park.set()
        out = await asyncio.wait_for(task, timeout=10)
        self.assertEqual(sorted(out["started"]), ["alpha", "beta", "gamma"])
        await h.manager.stop_all()

    async def test_second_start_all_joins(self) -> None:
        h = make_manager()
        for name in ("alpha", "beta"):
            await h.manager.add_account(name)
        park = asyncio.Event()
        original_factory = h.manager._client_factory

        def parking_factory(name: str, token: str) -> Any:
            client = original_factory(name, token)
            orig_connect = client.connect

            async def parked_connect() -> None:
                await park.wait()
                await orig_connect()

            client.connect = parked_connect
            return client

        h.manager._client_factory = parking_factory
        first = asyncio.create_task(h.manager.start_all(stagger=0))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(h.manager.start_all(stagger=0))
        park.set()
        out1 = await first
        out2 = await asyncio.wait_for(second, timeout=10)
        self.assertTrue(out2.get("joined"))
        self.assertEqual(out1["started"], out2["started"])
        self.assertEqual(len(h.factory.clients), 2)  # ONE client each
        await h.manager.stop_all()

    async def test_estop_overtakes_pending_start(self) -> None:
        """A queued start whose account is estopped mid-sweep is cancelled:
        emergency_stop bumps the generation synchronously, so the snapshot
        check marks it overtaken instead of starting it. (A plain stop of
        an ALREADY-stopped account is an idempotent no-op and correctly
        bumps nothing — disable is the operator's 'never start' tool.)"""
        h = make_manager()
        for name in ("alpha", "beta", "gamma"):
            await h.manager.add_account(name)
        await h.manager.start_all(stagger=0)
        await h.manager.stop_all()
        task = asyncio.create_task(h.manager.start_all(stagger=0.25))
        await asyncio.sleep(0.05)  # alpha admitted; beta/gamma queued
        h.manager.emergency_stop("gamma")  # synchronous generation bump
        out = await asyncio.wait_for(task, timeout=15)
        self.assertNotEqual(h.manager.get("gamma").runtime.status, AccountStatus.RUNNING)
        self.assertIn("gamma", out["overtaken"])
        await h.manager.stop_all()


class GateDepthTests(unittest.IsolatedAsyncioTestCase):
    async def test_projected_depth_fast_fail(self) -> None:
        from adapters.discord.client import IdentifyGate

        # 12 waiters queue: those projected beyond the wait budget
        # fast-fail. max_wait 0.20 (not 0.45): a loaded loop staggers
        # the waiters' arrival, and with 0.45 the queue never reached
        # projection depth — zero failed (2026-11-27). At depth 5
        # (5 x 0.05s spacing) the projection crosses 0.20 under any
        # staggering 12 gathered waiters can produce.
        gate = IdentifyGate(interval=0.05, max_wait=0.20)
        results = []

        async def attempt(n: int) -> None:
            try:
                await gate.acquire()
                results.append(("ok", n))
            except ConnectionError:
                results.append(("fail", n))

        await asyncio.gather(*[attempt(n) for n in range(12)])
        ok = [n for status, n in results if status == "ok"]
        failed = [n for status, n in results if status == "fail"]
        self.assertEqual(len(ok) + len(failed), 12)
        self.assertGreater(len(failed), 0)  # depth projection fired
        self.assertLess(len(ok), 12)

    async def test_spacing_still_honored(self) -> None:
        from adapters.discord.client import IdentifyGate

        gate = IdentifyGate(interval=0.05, max_wait=45.0)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.gather(gate.acquire(), gate.acquire())
        # a loaded loop can wake the timer slightly early (Windows
        # resolution ~15ms): 10ms of slack absorbs it - the same medicine
        # its sibling test received in 151ea08
        self.assertGreaterEqual(loop.time() - t0, 0.05 - 0.01)


class SecurityHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_repr_is_token_free(self) -> None:
        client = RealClientShim("SUPERSECRET-TOKEN-VALUE")
        rendered = repr(client)
        self.assertNotIn("SUPERSECRET-TOKEN-VALUE", rendered)
        self.assertIn("RealDiscordClient", rendered)

    @unittest.skipUnless(enforces_file_modes(), "POSIX permission bits are not enforced here")
    async def test_key_file_tightened_on_read(self) -> None:
        import tempfile
        from pathlib import Path

        from core.credential_service import CredentialService

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "k.key"
            from cryptography.fernet import Fernet

            key.write_text(Fernet.generate_key().decode())
            os.chmod(key, 0o644)  # deliberately loose
            service = CredentialService(_MemRepo(), key_path=key)
            service._read_key_file()
            mode = stat.S_IMODE(os.stat(key).st_mode)
            self.assertEqual(mode, 0o600)

    async def test_audit_content_capped(self) -> None:
        from core.audit import AuditRecorder

        recorder = AuditRecorder(db_repo=None)
        entry = recorder.record("alpha", "out", "message", content="x" * 50_000)
        self.assertLess(len(entry["content"]), 2_100)


class RealClientShim:
    """Uses the real repr against a real instance shape."""

    def __init__(self, token: str) -> None:
        from adapters.discord.client import RealDiscordClient

        self._inner = RealDiscordClient.__new__(RealDiscordClient)
        self._inner._token = token
        self._inner._account = "alpha"
        self._inner.user_id = None
        self._inner._client = None

    def __repr__(self) -> str:
        return repr(self._inner)


class _MemRepo:
    async def store(self, account: str, ciphertext: str) -> None:
        pass

    async def load(self, account: str) -> None:
        return None

    async def delete(self, account: str) -> bool:
        return True

    async def accounts_with_credential(self) -> list[str]:
        return []


class BurstIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_stack_burst_through_real_stack(self) -> None:
        """The benchmarks' shape against a real make_manager stack."""
        h = make_manager()
        await h.manager.add_account("alpha")
        await h.manager.start("alpha")
        # the collector from make_manager already subscribes; just publish
        for i in range(2000):
            h.bus.publish_nowait(_msg("alpha", i))
        await h.bus.drain(timeout=30.0)
        messages = h.collector.of(MessageReceived)
        self.assertEqual(len(messages), 2000)
        ids = [m.message_id for m in messages]
        self.assertEqual(ids, sorted(ids))  # FIFO preserved
        await h.manager.stop("alpha")


if __name__ == "__main__":
    unittest.main()
