"""Regression tests for the audited adapters/discord/client.py fixes.

Everything here runs WITHOUT a network: the discord module is faked
through sys.modules exactly like test_rich_model.py's harnesses (the
pinned discord.py-self stays a dev-only dependency of the Bot tests).

Coverage:
  (a) rich edit/delete/reaction filters — OTHERS' cached events publish,
      self-authored never do, and the raw-first dispatch suppresses the
      rich copy through the (now live) _mark_event_key dedup;
  (b) a click timeout maps to the transient ConnectionError vocabulary
      (asyncio.TimeoutError — Python 3.10 aliasing);
  (c) disconnect debounce — a transient 'disconnect' is NOT forwarded
      inside the window, on_resumed cancels the pending forward, the
      window elapsing forwards, close code 4004 forwards IMMEDIATELY
      with the fatal marker, and a dispatch while not connected never
      forwards;
  (d) connect() idempotency — a second invocation raises AccountError;
  (e) message-dedup capacity eviction keeps the NEWEST half (no more
      clear() re-publication after a >512-message RESUME burst);
  (f) IdentifyGate — expected=50 holds the 5s per-IP floor, release()
      restores the budget (and the adapter releases its slot exactly
      once), and a loop change resets _last without a spurious
      fast-fail;
  (g) the Bot-constructor TypeError is contained as AccountError (no
      blind retry).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import unittest
from types import SimpleNamespace as NS
from typing import Any
from unittest import mock

from libzentsuite.adapters.discord import client as client_module
from libzentsuite.adapters.discord.client import IdentifyGate, RealDiscordClient
from libzentsuite.core.errors import AccountError


# --------------------------------------------------------------- harness
class _ReadyThenParkLibClient:
    """discord.Client stand-in that reaches READY (user 777) then parks."""

    #: the library fills this via @client.event before start() runs
    on_ready: Any

    def __init__(self, **kwargs: Any) -> None:
        self.constructor_kwargs: dict[str, Any] = kwargs
        self.closed = False
        self.user: Any = NS(id=777)
        self.ws: Any = None
        self._park = asyncio.Event()

    def event(self, coro: Any) -> Any:
        setattr(self, coro.__name__, coro)
        return coro

    async def start(self, token: str) -> None:
        await self.on_ready()
        await self._park.wait()

    async def close(self) -> None:
        self.closed = True
        self._park.set()


class _NeverReadyLibClient(_ReadyThenParkLibClient):
    """Never reaches READY — connect() stays parked in the ready wait."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = None

    async def start(self, token: str) -> None:
        await self._park.wait()


def _patch_discord(lib_cls: Any) -> Any:
    """The fake discord world via the shared helper (Bot included)."""
    from tests.unit.helpers import fake_discord_sysmodules

    return mock.patch.dict(sys.modules, fake_discord_sysmodules(lib_cls))


# ------------------------------------------------- (a) rich event polarity
class RichEventPolarityTests(unittest.IsolatedAsyncioTestCase):
    """AUDIT FIX: the rich edit/delete/reaction handlers were INVERTED —
    they published ONLY self-authored cached events while the raw handlers
    (and the simulator) publish only OTHERS'. Live parity = others-only."""

    async def _connected(self) -> tuple[Any, Any]:
        with _patch_discord(_ReadyThenParkLibClient):
            client = RealDiscordClient("tok")
            await client.connect()
        self.assertTrue(client.connected)
        self.assertEqual(client.user_id, 777)
        self.addAsyncCleanup(client.disconnect)
        return client, client._client

    @staticmethod
    def _editable(mid: int = 5, author_id: int = 999999) -> NS:
        return NS(
            id=mid,
            content="edited payload",
            author=NS(id=author_id, name="the_bot"),
            channel=NS(id=500, name="grind", type=NS(name="text"), guild=NS(id=42)),
            guild=NS(id=42),
            embeds=[],
            components=[],
            attachments=[],
            reactions=[],
            reference=None,
            created_at=None,
        )

    async def test_others_cached_edit_publishes(self) -> None:
        client, lib = await self._connected()
        edits: list[Any] = []
        client.on_message_edit = edits.append
        await lib.on_message_edit(NS(id=1), self._editable(author_id=999999))
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].id, 5)
        self.assertEqual(edits[0].content, "edited payload")

    async def test_self_cached_edit_never_publishes(self) -> None:
        client, lib = await self._connected()
        edits: list[Any] = []
        client.on_message_edit = edits.append
        await lib.on_message_edit(NS(id=1), self._editable(author_id=777))  # the account itself
        self.assertEqual(edits, [])

    async def test_others_cached_delete_publishes(self) -> None:
        client, lib = await self._connected()
        deletes: list[Any] = []
        client.on_message_delete = lambda *a: deletes.append(a)
        cached = NS(id=7, channel=NS(id=500), guild=NS(id=42), author=NS(id=999999))
        await lib.on_message_delete(cached)
        self.assertEqual(deletes, [(7, 500, 42, cached)])

    async def test_self_cached_delete_never_publishes(self) -> None:
        client, lib = await self._connected()
        deletes: list[Any] = []
        client.on_message_delete = lambda *a: deletes.append(a)
        own = NS(id=8, channel=NS(id=500), guild=NS(id=42), author=NS(id=777))
        await lib.on_message_delete(own)
        self.assertEqual(deletes, [])

    async def test_others_cached_reactions_publish(self) -> None:
        client, lib = await self._connected()
        reactions: list[Any] = []
        client.on_reaction = lambda *a: reactions.append(a)
        message = NS(id=9, channel=NS(id=5, guild=None), guild=None)
        await lib.on_reaction_add(NS(message=message, emoji=NS(name="❤", id=None)), NS(id=999999))
        await lib.on_reaction_remove(
            NS(message=message, emoji=NS(name="kakera", id=77)), NS(id=999999)
        )
        self.assertEqual(
            [r[:6] for r in reactions],
            [
                ("add", 9, 5, None, 999999, "❤"),
                ("remove", 9, 5, None, 999999, "<:kakera:77>"),
            ],
        )

    async def test_self_cached_reaction_never_publishes(self) -> None:
        client, lib = await self._connected()
        reactions: list[Any] = []
        client.on_reaction = lambda *a: reactions.append(a)
        message = NS(id=9, channel=NS(id=5, guild=None), guild=None)
        await lib.on_reaction_add(NS(message=message, emoji=NS(name="❤", id=None)), NS(id=777))
        await lib.on_reaction_remove(NS(message=message, emoji=NS(name="❤", id=None)), NS(id=777))
        self.assertEqual(reactions, [])

    async def test_raw_first_suppresses_rich_edit_copy(self) -> None:
        """The dedup guard is live now: raw publishes, the rich copy of
        the SAME cached dispatch must not double-publish."""
        client, lib = await self._connected()
        edits: list[Any] = []
        client.on_message_edit = edits.append
        await lib.on_raw_message_edit(
            NS(
                message_id=5,
                channel_id=500,
                guild_id=42,
                data={},
                cached_message=None,
                message=self._editable(),
            )
        )
        self.assertEqual(len(edits), 1)
        await lib.on_message_edit(NS(id=1), self._editable())
        self.assertEqual(len(edits), 1)  # rich copy suppressed

    async def test_raw_first_suppresses_rich_reaction_copy(self) -> None:
        client, lib = await self._connected()
        reactions: list[Any] = []
        client.on_reaction = lambda *a: reactions.append(a)
        payload = NS(
            message_id=9,
            channel_id=5,
            guild_id=None,
            user_id=999999,
            emoji=NS(name="kakera", id=77, animated=False),
            member=None,
        )
        await lib.on_raw_reaction_add(payload)
        self.assertEqual(len(reactions), 1)
        await lib.on_reaction_add(
            NS(
                message=NS(id=9, channel=NS(id=5, guild=None), guild=None),
                emoji=NS(name="kakera", id=77),
            ),
            NS(id=999999),
        )
        self.assertEqual(len(reactions), 1)  # rich copy suppressed


# ------------------------------------------------- (b) click timeout mapping
class ClickTimeoutMappingTests(unittest.IsolatedAsyncioTestCase):
    """The click ceiling maps asyncio.TimeoutError to the transient
    ConnectionError vocabulary (bare `except TimeoutError` missed
    asyncio.TimeoutError on Python 3.10, where the alias is 3.11+)."""

    def _clickable(self, click: Any) -> RealDiscordClient:
        client = RealDiscordClient("tok")
        client.connected = True
        component = NS(custom_id="kakera:claim", click=click)
        message = NS(id=11, components=[NS(children=[component])])
        channel = NS(id=500, get_message=lambda mid: message)
        # SimpleNamespace doubles stand in for the library Client object
        lib_client: Any = NS(get_channel=lambda cid: channel)
        client._client = lib_client
        client._discord = NS(
            DMChannel=type("DMChannel", (), {}),
            HTTPException=type("HTTPException", (Exception,), {}),
            Forbidden=type("Forbidden", (Exception,), {}),
            NotFound=type("NotFound", (Exception,), {}),
        )
        return client

    async def test_wait_for_timeout_maps_to_transient_connection_error(self) -> None:
        async def stalled_click() -> str:
            await asyncio.sleep(30.0)  # the uncapped 202-retry shape
            return "never"

        client = self._clickable(stalled_click)
        with (
            mock.patch.object(client_module, "CLICK_TIMEOUT_S", 0.05),
            self.assertRaises(ConnectionError) as ctx,
        ):
            await client.click_button(500, 11, "kakera:claim")
        self.assertIn("did not settle", str(ctx.exception))

    async def test_sender_raising_asyncio_timeout_error_is_mapped(self) -> None:
        """The seam the click path awaits raising asyncio.TimeoutError
        directly (the exception class the bare except used to miss)."""

        async def timing_out_click() -> None:
            raise asyncio.TimeoutError()

        client = self._clickable(timing_out_click)
        with self.assertRaises(ConnectionError) as ctx:
            await client.click_button(500, 11, "kakera:claim")
        self.assertIn("did not settle", str(ctx.exception))


# ------------------------------------------------- (c) disconnect debounce
class DisconnectDebounceTests(unittest.IsolatedAsyncioTestCase):
    """The fork dispatches transient 'disconnect's BEFORE its own
    backoff+RESUME retry — forwarding them verbatim tore the manager
    down for drops the library survived. Forwarding is now debounced
    (fatal close codes bypass the window entirely)."""

    async def _connected(self) -> tuple[Any, Any]:
        with _patch_discord(_ReadyThenParkLibClient):
            client = RealDiscordClient("tok")
            await client.connect()
        self.addAsyncCleanup(client.disconnect)
        return client, client._client

    async def test_transient_disconnect_not_forwarded_within_window(self) -> None:
        client, lib = await self._connected()
        seen: list[Any] = []
        client.on_disconnect = seen.append
        with mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 0.4):
            await lib.on_disconnect()
            await asyncio.sleep(0.12)
            self.assertEqual(seen, [])  # inside the window: NOT forwarded
        client._cancel_disconnect_debounce()  # quiet the cleanup

    async def test_resumed_cancels_the_pending_forward(self) -> None:
        client, lib = await self._connected()
        seen: list[Any] = []
        client.on_disconnect = seen.append
        with mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 0.3):
            await lib.on_disconnect()
            await asyncio.sleep(0.1)
            self.assertEqual(seen, [])
            await lib.on_resumed()  # the fork RESUMED inside this client
            await asyncio.sleep(0.6)  # well past the window
            self.assertEqual(seen, [])  # cancelled: never forwarded

    async def test_forwarded_once_window_elapses_without_resume(self) -> None:
        client, lib = await self._connected()
        seen: list[Any] = []
        client.on_disconnect = seen.append
        with mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 0.08):
            await lib.on_disconnect()
            await asyncio.sleep(0.35)  # no resume landed
        self.assertEqual(seen, ["gateway closed"])
        self.assertIsNone(client.last_close_code)
        self.assertFalse(client.fatal_close)

    async def test_nonfatal_close_code_sentinel_after_window(self) -> None:
        client, lib = await self._connected()
        seen: list[Any] = []
        client.on_disconnect = seen.append
        lib.ws = NS(close_code=4007)  # session-limit class: NOT fatal
        with mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 0.08):
            await lib.on_disconnect()
            await asyncio.sleep(0.35)
        self.assertEqual(len(seen), 1)
        self.assertIn("close=4007", seen[0])
        self.assertNotIn("fatal", seen[0])
        self.assertEqual(client.last_close_code, 4007)
        self.assertFalse(client.fatal_close)

    async def test_fatal_close_code_forwards_immediately_with_marker(self) -> None:
        client, lib = await self._connected()
        seen: list[Any] = []
        client.on_disconnect = seen.append
        lib.ws = NS(close_code=4004)  # authentication failed: fatal class
        await lib.on_disconnect()  # NO debounce for fatal codes
        self.assertEqual(seen, ["gateway closed (close=4004) fatal"])
        self.assertEqual(client.last_close_code, 4004)
        self.assertTrue(client.fatal_close)
        self.assertIsNone(client._disconnect_debounce)

    async def test_disconnect_while_not_connected_never_forwards(self) -> None:
        """Startup failures belong to connect()'s AccountError — a
        'disconnect' dispatched mid-connect must not reach the manager."""
        with _patch_discord(_NeverReadyLibClient):
            client = RealDiscordClient("tok")
            seen: list[Any] = []
            client.on_disconnect = seen.append
            task = asyncio.get_running_loop().create_task(client.connect())
            await asyncio.sleep(0.05)
            with mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 0.05):
                lib_client: Any = client._client
                await lib_client.on_disconnect()
                await asyncio.sleep(0.2)  # window long elapsed
                self.assertEqual(seen, [])
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_terminal_run_exit_flushes_pending_forward(self) -> None:
        client, lib = await self._connected()
        seen: list[Any] = []
        client.on_disconnect = seen.append
        with mock.patch.object(client_module, "_DISCONNECT_FORWARD_DELAY_S", 10.0):
            await lib.on_disconnect()
            lib._park.set()  # start() returns: the session is terminal
            for _ in range(100):
                await asyncio.sleep(0.02)
                if seen:
                    break
            # flushed PROMPTLY — no 10s wait for a resume that cannot come
            self.assertEqual(seen, ["gateway closed"])


# ------------------------------------------------- (d) connect idempotency
class ConnectIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_connect_while_pending_raises(self) -> None:
        with _patch_discord(_NeverReadyLibClient):
            client = RealDiscordClient("tok")
            task = asyncio.get_running_loop().create_task(client.connect())
            await asyncio.sleep(0.05)
            with self.assertRaises(AccountError) as ctx:
                await client.connect()  # would orphan the first run task
            self.assertIn("already", str(ctx.exception))
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_connect_with_built_client_raises(self) -> None:
        client = RealDiscordClient("tok")
        prebuilt: Any = object()  # a client exists (no live run task)
        client._client = prebuilt
        with self.assertRaises(AccountError) as ctx:
            await client.connect()  # raises before any library work
        self.assertIn("already", str(ctx.exception))


# ------------------------------------------------- (e) dedup eviction
class DedupEvictionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_eviction_keeps_newest_half(self) -> None:
        limit = client_module._MESSAGE_DEDUP_MAX
        client = RealDiscordClient("tok")
        for i in range(limit):
            self.assertFalse(client._message_already_seen(i))
        # one more arrival: capacity with ALL entries young — the old
        # clear() re-published the whole replay; the fix evicts the
        # oldest half only
        self.assertFalse(client._message_already_seen(10**6))
        seen = client._seen_messages
        self.assertIn(10**6, seen)
        self.assertIn(limit - 1, seen)  # newest half survived
        self.assertIn(limit // 2, seen)
        self.assertNotIn(0, seen)  # oldest half evicted
        # survivors still suppress a RESUME replay ...
        self.assertTrue(client._message_already_seen(limit - 1))
        # ... and an evicted id is re-published (arrives as new)
        self.assertFalse(client._message_already_seen(0))


# ------------------------------------------------- (f) identify gate
class IdentifyGateFixesTests(unittest.TestCase):
    def test_expected_50_holds_per_ip_floor(self) -> None:
        gate = IdentifyGate()  # production defaults
        gate.expected = 50
        self.assertGreaterEqual(gate.effective_interval, 5.0)
        self.assertEqual(gate.effective_interval, 5.0)

    def test_release_restores_budget(self) -> None:
        gate = IdentifyGate(30.0)
        gate.expected = 50
        self.assertEqual(gate.effective_interval, 5.0)  # wave floor
        for _ in range(49):
            gate.release()  # permanent teardowns prune the fleet size
        self.assertEqual(gate.expected, 1)
        self.assertEqual(gate.effective_interval, 30.0)  # spacing back

    def test_release_floors_at_one(self) -> None:
        gate = IdentifyGate()
        for _ in range(5):
            gate.release()
        self.assertEqual(gate.expected, 1)

    def test_loop_change_resets_last_without_spurious_fast_fail(self) -> None:
        gate = IdentifyGate(5.0)

        async def first_loop() -> None:
            await gate.acquire()

        asyncio.run(first_loop())  # binds _lock + _last to THAT loop

        # a long-lived first loop's monotonic _last dwarfs a fresh loop's
        # clock (loop.time() restarts near zero) — the stale value used to
        # fast-fail (projected > wait_budget) or deadlock on the loop-
        # bound lock
        gate._last += 60.0

        async def second_loop() -> None:
            # must neither raise ConnectionError (spurious fast-fail) nor
            # RuntimeError (lock bound to the dead loop) nor park for the
            # stale spacing
            await asyncio.wait_for(gate.acquire(), 2.0)

        asyncio.run(second_loop())


class IdentifyGateReleaseWiringTests(unittest.IsolatedAsyncioTestCase):
    """The adapter releases its gate slot exactly once per client."""

    async def test_disconnect_releases_slot_once(self) -> None:
        gate = IdentifyGate()
        gate.expected = 3
        with _patch_discord(_ReadyThenParkLibClient):
            client = RealDiscordClient("tok", identify_gate=gate)
            await client.connect()
            self.assertTrue(client._gate_counted)
            await client.disconnect()
            self.assertEqual(gate.expected, 2)
            await client.disconnect()  # idempotent: never double-releases
            self.assertEqual(gate.expected, 2)


# --------------------------------------- (g) Bot constructor containment
class BotConstructorContainmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_constructor_type_error_raises_account_error(self) -> None:
        class _Bot:
            def __init__(self, **kwargs: Any) -> None:
                raise TypeError("unexpected keyword argument 'owner_ids'")

        ext_commands = NS(
            Bot=_Bot,
            when_mentioned=lambda c, m: "!",
            when_mentioned_or=lambda s: lambda c, m: "!",
        )
        ext_pkg = NS(commands=ext_commands)
        mod = NS(Client=_NeverReadyLibClient, DMChannel=type("DMChannel", (), {}))
        with mock.patch.dict(
            sys.modules,
            {
                "discord": mod,
                "discord.ext": ext_pkg,
                "discord.ext.commands": ext_commands,
            },
        ):
            client = RealDiscordClient("tok", command_prefix="sm!")
            with self.assertRaises(AccountError) as ctx:
                await client.connect()
        self.assertIn("Bot construction", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, TypeError)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
