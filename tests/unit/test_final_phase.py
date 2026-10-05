"""FINAL-PHASE regression tests: caches, memory-first status, lifecycle
scheduling, burst dispatch integration, security hardening."""
from __future__ import annotations

import asyncio
import os
import stat
import unittest
from types import SimpleNamespace as NS

from core.account_manager import AccountStatus
from core.account_manager.services import MessageWaiterRegistry
from core.cooldowns import CooldownService
from core.errors import AccountError
from core.event_bus import AccountConnected, EventBus, MessageReceived
from core.rate_limit_manager.engine import RateLimitManager
from core.rate_limit_manager.policies import RatePolicy

from tests.unit.helpers import make_manager
from tests.unit.test_account_manager import _fast_reconnect_settings
from tests.unit.test_rate_engine import StubLogger


def _msg(account: str, i: int) -> MessageReceived:
    return MessageReceived(account=account, message_id=i, channel_id=500,
                           author_id=1, content=f"m{i}")


class WaiterRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_invoked_only_for_own_account(self) -> None:
        """Isolation-by-COUNT: the handler must never even RUN for another
        account's events (stronger than result-correctness)."""
        bus = EventBus()
        registry = MessageWaiterRegistry(bus)
        calls = {"alpha": 0, "beta": 0}
        def alpha_handler(event):
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
        seen = []
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
        from core.account_manager.services import AccountPluginServices

        facade = AccountPluginServices(
            "alpha", "roll", bus=bus, scheduler=NS(submit=lambda *a, **k: None),
            credentials=h.credentials, cache=NS(
                channels={}, guilds={}), waiter_registry=registry)

        async def deliver():
            await asyncio.sleep(0.05)
            bus.publish_nowait(_msg("beta", 1))   # wrong account first
            bus.publish_nowait(_msg("alpha", 2))  # then the match

        task = asyncio.create_task(deliver())
        result = await facade.wait_for_message(
            lambda e: True, timeout=2.0)
        await task
        self.assertIsNotNone(result)
        self.assertEqual(result.account, "alpha")
        registry.close()

    async def test_manager_removal_forgets_waiters(self) -> None:
        h = make_manager()
        h.manager._waiter_registry.add(
            "alpha", lambda e: None)
        await h.manager.add_account("alpha")
        await h.manager.remove_account("alpha")
        self.assertEqual(h.manager._waiter_registry.pending(), {})


class EffectiveCacheTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self) -> RateLimitManager:
        return RateLimitManager(EventBus(), StubLogger(), None)

    async def test_hit_equivalence_and_fresh_dicts(self) -> None:
        m = self._engine()
        m.set_policy_values("message", 3.0, 0.1, 0.5)
        m.set_policy_values("message", 2.0, 0.2, 0.6,
                            "account", "alpha")
        a = m.effective("message", "alpha", "roll")
        b = m.effective("message", "alpha", "roll")
        self.assertEqual(a, b)
        self.assertIsNot(a, b)              # fresh dict per call
        a["sources"].append("junk")          # caller mutation cannot
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
        self.assertEqual(m.effective("message", "alpha")["interval"], 1.5)
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
            self.assertEqual(m.effective("act19", "alpha", "roll")["action"],
                             "act19")
        finally:
            eng._EFFECTIVE_CACHE_CAP = 4096

    async def test_account_isolation_in_the_key(self) -> None:
        m = self._engine()
        m.set_policy_values("message", 3.0, 0.1, 0.5, "account", "a/b")
        # the delimiter trap: ("a/b", "c") must NOT collide with ("a", "b/c")
        self.assertEqual(m.effective("message", "a/b", "c")["interval"], 3.0)
        self.assertEqual(m.effective("message", "a", "b/c")["interval"], 1.5)


class MemoryFirstStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_status_within_ttl_does_no_repo_reads(self) -> None:
        h = make_manager()
        await h.manager.add_account("alpha")
        reads = {"n": 0}
        original_get = h.repo.get

        async def counting_get(name):
            reads["n"] += 1
            return await original_get(name)

        h.repo.get = counting_get
        out1 = await h.manager.accounts()   # memory-first: NO reads
        self.assertEqual(reads["n"], 0)
        out2 = await h.manager.accounts(refresh_records=True)  # explicit
        self.assertEqual(reads["n"], 1)
        self.assertEqual(out1[0]["name"], out2[0]["name"])

    async def test_cooldown_tombstones_purged_on_expiry(self) -> None:
        now = {"t": 1000.0}
        class FakeRepo:
            async def upsert(self, **kw): pass
            async def get(self, *a, now=None, **k): return None
            async def clear_expired_for(self, *a, **k): return 0
            async def active_for(self, *a, **k): return []
            async def active_all(self, now=None): return []
        service = CooldownService(FakeRepo(), time_fn=lambda: now["t"])
        for i in range(50):
            await service.set("acct", "plug", f"k{i}", 10.0)
        self.assertEqual(len(service._active), 50)
        now["t"] += 11.0                       # everything expires
        for i in range(50):
            await service.get("acct", "plug", f"k{i}")
        self.assertEqual(len(service._active), 0)  # tombstones purged

    async def test_resync_replaces_mirror_from_db(self) -> None:
        class FakeRepo:
            async def upsert(self, **kw): pass
            async def get(self, *a, **k): return None
            async def clear_expired_for(self, *a, **k): return 0
            async def active_for(self, *a, **k): return []
            async def active_all(self, now=None):
                return [{"account": "alpha", "plugin": "roll",
                         "cooldown_key": "roulette",
                         "started_at": 1.0, "expires_at": 9e9,
                         "source": "", "reason": "",
                         "metadata": {}}]
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
        alpha_client = None
        original_factory = h.manager._client_factory

        def parking_factory(name, token):
            client = original_factory(name, token)
            if name == "alpha":
                orig_connect = client.connect

                async def parked_connect():
                    await park.wait()
                    await orig_connect()
                client.connect = parked_connect
            return client

        h.manager._client_factory = parking_factory
        task = asyncio.create_task(h.manager.start_all(stagger=0))
        await asyncio.sleep(0.3)  # beta/gamma must complete while alpha parks
        self.assertEqual(
            h.manager.get("beta").runtime.status, AccountStatus.RUNNING)
        self.assertEqual(
            h.manager.get("gamma").runtime.status, AccountStatus.RUNNING)
        park.set()
        out = await asyncio.wait_for(task, timeout=10)
        self.assertEqual(sorted(out["started"]),
                         ["alpha", "beta", "gamma"])
        await h.manager.stop_all()

    async def test_second_start_all_joins(self) -> None:
        h = make_manager()
        for name in ("alpha", "beta"):
            await h.manager.add_account(name)
        park = asyncio.Event()
        original_factory = h.manager._client_factory

        def parking_factory(name, token):
            client = original_factory(name, token)
            orig_connect = client.connect

            async def parked_connect():
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
        self.assertNotEqual(
            h.manager.get("gamma").runtime.status, AccountStatus.RUNNING)
        self.assertIn("gamma", out["overtaken"])
        await h.manager.stop_all()

    async def test_stop_while_parked_on_lane_is_not_undone(self) -> None:
        """Round-4 (probe3_sema_overtake/probe3b): with a fleet wider than
        _START_ALL_WIDTH, an operator stop of an account whose sweep-start
        is still parked on the lane SEMAPHORE must win — the start is
        re-checked after acquiring the lane and reported overtaken, never
        blindly executed against the stopped account. (Which accounts park
        on the semaphore vs grab a lane is scheduler-dependent, so the
        victim is chosen at runtime: one with NO client built yet.)"""
        h = make_manager(token_values={
            f"DISCORD_TOKEN_{n.upper()}": f"tok-{n}" for n in "abcdef"})
        gate = asyncio.Event()
        original_factory = h.manager._client_factory
        built: set = set()

        def gated_factory(name, token):
            client = original_factory(name, token)
            built.add(name)  # this account is IN a lane now
            orig_connect = client.connect

            async def gated_connect():
                await gate.wait()  # all lane occupants park here
                await orig_connect()
            client.connect = gated_connect
            return client

        h.manager._client_factory = gated_factory
        for name in "abcdef":
            await h.manager.add_account(name)

        task = asyncio.create_task(h.manager.start_all(stagger=0.01))
        await asyncio.sleep(0.1)  # 4 lanes parked in connect; 2 on the sema
        parked = [n for n in "abcdef" if n not in built]
        self.assertEqual(len(parked), 2)  # fleet 6 > width 4
        victim = parked[0]  # still parked on the lane semaphore
        await h.manager.stop(victim)  # operator stops the PARKED account
        rt = h.manager.get(victim).runtime
        self.assertEqual(rt.status, AccountStatus.STOPPED)

        gate.set()  # lanes free; the victim's queued start hits the re-check
        out = await asyncio.wait_for(task, timeout=10)
        self.assertIn(victim, out["overtaken"])
        self.assertNotIn(victim, out["started"])
        rt = h.manager.get(victim).runtime
        self.assertEqual(rt.status, AccountStatus.STOPPED)  # not undone
        self.assertIsNone(rt.client)
        self.assertEqual(h.manager.get(parked[1]).runtime.status,
                         AccountStatus.RUNNING)  # untouched sibling ran
        await h.manager.stop_all()

    async def test_joiner_returns_when_owner_cancelled(self) -> None:
        """Round-4 (probe2_joiner_hang): the sweep owner dying (cancelled)
        must complete the flight — a joiner parked on shield(flight) gets
        the aborted partial report instead of hanging forever."""
        h = make_manager()
        for name in ("alpha", "beta"):
            await h.manager.add_account(name)
        park = asyncio.Event()
        original_factory = h.manager._client_factory

        def parking_factory(name, token):
            client = original_factory(name, token)
            orig_connect = client.connect

            async def parked_connect():
                await park.wait()
                await orig_connect()
            client.connect = parked_connect
            return client

        h.manager._client_factory = parking_factory
        owner = asyncio.create_task(h.manager.start_all(stagger=0))
        await asyncio.sleep(0.05)  # owner mid-sweep (parked in connect)
        joiner = asyncio.create_task(h.manager.start_all(stagger=0))
        await asyncio.sleep(0.05)  # joiner parked on the flight
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner

        out = await asyncio.wait_for(asyncio.shield(joiner), timeout=3)
        self.assertTrue(out.get("joined"))
        self.assertTrue(out.get("aborted"))  # labelled, not a clean report
        self.assertEqual(out["started"], [])
        # the flight slot is free again: a fresh sweep works end-to-end
        park.set()
        await h.manager.stop_all()
        retry = await h.manager.start_all(stagger=0)
        self.assertEqual(retry["started"], ["alpha", "beta"])
        self.assertNotIn("aborted", retry)
        await h.manager.stop_all()


class GateDepthTests(unittest.IsolatedAsyncioTestCase):
    async def test_projected_depth_fast_fail(self) -> None:
        from adapters.discord.client import IdentifyGate
        gate = IdentifyGate(interval=0.05, max_wait=0.45)
        # 12 waiters queue: those projected beyond 0.45s fast-fail
        results = []
        async def attempt(n):
            try:
                await gate.acquire()
                results.append(("ok", n))
            except ConnectionError as exc:
                results.append(("fail", n))
        await asyncio.gather(*[attempt(n) for n in range(12)])
        ok = [n for status, n in results if status == "ok"]
        failed = [n for status, n in results if status == "fail"]
        self.assertEqual(len(ok) + len(failed), 12)
        self.assertGreater(len(failed), 0)   # depth projection fired
        self.assertLess(len(ok), 12)

    async def test_spacing_still_honored(self) -> None:
        from adapters.discord.client import IdentifyGate
        gate = IdentifyGate(interval=0.05, max_wait=45.0)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.gather(gate.acquire(), gate.acquire())
        self.assertGreaterEqual(loop.time() - t0, 0.05)


class SecurityHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_repr_is_token_free(self) -> None:
        client = RealClientShim("SUPERSECRET-TOKEN-VALUE")
        rendered = repr(client)
        self.assertNotIn("SUPERSECRET-TOKEN-VALUE", rendered)
        self.assertIn("RealDiscordClient", rendered)

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
        entry = recorder.record("alpha", "out", "message",
                                content="x" * 50_000)
        self.assertLess(len(entry["content"]), 2_100)


class RealClientShim:
    """Uses the real repr against a real instance shape."""
    def __init__(self, token):
        from adapters.discord.client import RealDiscordClient
        self._inner = RealDiscordClient.__new__(RealDiscordClient)
        self._inner._token = token
        self._inner._account = "alpha"
        self._inner.user_id = None
        self._inner._client = None

    def __repr__(self):
        return repr(self._inner)


class _MemRepo:
    async def store(self, account, ciphertext): pass
    async def load(self, account): return None
    async def delete(self, account): return True
    async def accounts_with_credential(self): return []


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
