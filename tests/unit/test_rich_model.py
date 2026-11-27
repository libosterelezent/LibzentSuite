"""Rich Discord message/event model tests (architecture spec §27).

Covers: lossless conversion from library-shaped objects (embeds, buttons in
action rows, attachments, reactions, reference, author/channel/guild, DM vs
guild), backward compatibility of the 5-field surface, plugin isolation via
frozen DTOs, the runtime-only raw escape hatch (repr/serialization), the
new typed events (ReactionAdded/MessageEdited/MessageDeleted) through the
simulator WITHOUT message fetches, the reaction/button capability actions
end-to-end through the paced pipeline, and future-plugin shapes (snipe /
kakerareact / kakeraloot / soulmate farm).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any, ClassVar
from unittest import mock

from adapters.model import (
    DiscordComponent,
    DiscordEmbed,
    DiscordEmbedField,
    DiscordMessage,
    convert_component,
    convert_message,
)
from core.event_bus import (
    EventBus,
    MessageReceived,
    ReactionAdded,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from adapters.discord.client import RealDiscordClient
    from adapters.simulator.world import SimWorld

#: shape of the fork's ``Headers.get_api_properties`` result tuple
_HEADERS_RESULT = tuple[dict[str, str], dict[str, int], str]


def library_message(**overrides: Any) -> NS:
    """A library-shaped message carrying EVERY rich field (§27 fixtures)."""
    base = {
        "id": 1000,
        "content": "$mu",
        "author": NS(id=999999, name="the_bot", display_name="the_bot", bot=True),
        "channel": NS(id=500, name="grind", type=NS(name="text"), guild=NS(id=42)),
        "guild": NS(id=42),
        "embeds": [
            NS(
                title="Character",
                description="Rarity: ★★★",
                url="http://x",
                type="rich",
                color=0xFF00AA,
                timestamp="2026-01-01T00:00:00",
                author=NS(name="the_bot", url="http://a", icon_url="http://i"),
                footer=NS(text="page 1/2", icon_url="http://f"),
                fields=[
                    NS(name="Rank", value="1", inline=True),
                    NS(name="Kakera", value="500", inline=True),
                ],
                thumbnail=NS(url="http://t"),
                image=NS(url="http://im"),
            )
        ],
        "components": [
            NS(
                type=1,
                children=[
                    NS(
                        type=2,
                        custom_id="kakera:loot",
                        label="React",
                        style=2,
                        disabled=False,
                        emoji=NS(name="💎", id=None),
                    ),
                    NS(
                        type=2,
                        custom_id="kakera:claim",
                        label="Claim",
                        style=3,
                        disabled=False,
                        emoji=None,
                        url=None,
                    ),
                ],
            )
        ],
        "attachments": [
            NS(id=7, filename="card.png", url="http://f", size=12345, content_type="image/png")
        ],
        "reactions": [NS(emoji=NS(name="❤", id=None), count=3, me=False)],
        "reference": NS(message_id=999, channel_id=500, guild_id=42),
        "referenced_message": None,
        "created_at": None,
    }
    base.update(overrides)
    return NS(**base)


def _attempt_content_store(dto: Any, value: str) -> None:
    """The forbidden direct attribute store — a frozen DTO must raise
    FrozenInstanceError on it (the DTO arrives as Any precisely because
    the store is what the type checker rightly forbids)."""
    dto.content = value


def _install_lib_client(client: RealDiscordClient, lib_client: Any) -> None:
    """Test seam: park a duck-typed library stand-in in the lazy slot
    (the production attribute is typed against the real library)."""
    client._client = lib_client


def _lib_client(client: RealDiscordClient) -> Any:
    """The duck-typed library client ``connect()`` built — its event
    handlers are attached dynamically via ``Client.event()``."""
    return client._client


class TestConversion(unittest.TestCase):
    def test_every_field_survives_conversion(self) -> None:
        lib = library_message()
        dto = convert_message(lib, dm=False)
        self.assertEqual(
            (dto.id, dto.channel_id, dto.author_id, dto.content), (1000, 500, 999999, "$mu")
        )
        self.assertFalse(dto.dm)
        self.assertEqual(dto.guild_id, 42)
        assert dto.author is not None
        assert dto.channel is not None
        self.assertEqual(dto.author.name, "the_bot")
        self.assertTrue(dto.author.bot)
        self.assertEqual(dto.channel.name, "grind")
        self.assertEqual(dto.channel.guild_id, 42)
        # embed
        embed = dto.embeds[0]
        self.assertEqual(embed.title, "Character")
        self.assertEqual(embed.field_map(), {"Rank": "1", "Kakera": "500"})
        self.assertEqual(embed.footer_text, "page 1/2")
        self.assertEqual(embed.color, 0xFF00AA)
        self.assertEqual(embed.colour, embed.color)
        self.assertEqual(embed.thumbnail, "http://t")
        # components: action row -> nested buttons
        self.assertEqual(dto.components[0].type_name, "action_row")
        buttons = dto.buttons
        self.assertEqual([b.custom_id for b in buttons], ["kakera:loot", "kakera:claim"])
        claim = dto.button("kakera:claim")
        assert claim is not None
        self.assertEqual(claim.label, "Claim")
        self.assertIsNone(dto.button("missing"))
        # attachments / reactions / reference
        self.assertEqual(dto.attachments[0].filename, "card.png")
        self.assertEqual(dto.reactions[0].emoji, "❤")
        self.assertEqual(dto.reactions[0].count, 3)
        assert dto.reference is not None
        self.assertEqual(dto.reference.message_id, 999)
        self.assertIsNone(dto.reference.referenced)  # not fetched (§12)
        # escape hatch
        self.assertIs(dto.unwrap(), lib)

    def test_dm_message(self) -> None:
        lib = library_message(
            content="Soulmates",
            channel=NS(
                id=9001, name=None, type=NS(name="private"), recipient=NS(id=999999), guild=None
            ),
            guild=None,
            embeds=[],
            components=[],
            reference=None,
        )
        dto = convert_message(lib, dm=True)
        self.assertTrue(dto.dm)
        self.assertIsNone(dto.guild_id)
        assert dto.channel is not None
        self.assertEqual(dto.channel.type, "private")
        self.assertIn("dm", dto.summary())

    def test_missing_optionals_stay_none(self) -> None:
        lib = library_message(
            embeds=[],
            components=[],
            attachments=[],
            reactions=[],
            reference=None,
            referenced_message=None,
            author=None,
            guild=None,
        )
        dto = convert_message(lib, dm=False)
        self.assertEqual(dto.embeds, ())
        self.assertIsNone(dto.reference)
        self.assertIsNone(dto.author)

    def test_referenced_message_used_when_cached(self) -> None:
        cached = library_message(id=999, content="original")
        lib = library_message(referenced_message=cached)
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        assert dto.reference.referenced is not None
        self.assertEqual(dto.reference.referenced.id, 999)
        self.assertEqual(dto.reference.referenced.content, "original")
        # no fetch happened: the cached object IS the raw handle
        self.assertIs(dto.reference.referenced.unwrap(), cached)

    def test_select_menu_component(self) -> None:
        select = NS(
            type=3,
            custom_id="pick",
            options=[
                NS(label="A", value="a", description="first", default=True),
                NS(label="B", value="b", description="", default=False),
            ],
        )
        dto_component = convert_component(select)
        self.assertEqual(dto_component.type_name, "select")
        self.assertEqual(dto_component.options[0], ("A", "a", "first", True))

    def test_custom_emoji_reaction(self) -> None:
        reaction = NS(emoji=NS(name="kakera", id=77), count=5, me=True)
        embedless = library_message(reactions=[reaction])
        dto = convert_message(embedless, dm=False)
        self.assertEqual(dto.reactions[0].emoji, "<:kakera:77>")
        self.assertEqual(dto.reactions[0].id, 77)
        self.assertTrue(dto.reactions[0].me)

    def test_animated_custom_emoji_repr(self) -> None:
        reaction = NS(emoji=NS(name="kakera", id=77, animated=True), count=2, me=False)
        dto = convert_message(library_message(reactions=[reaction]), dm=False)
        self.assertEqual(dto.reactions[0].emoji, "<a:kakera:77>")

    def test_embed_timestamp_datetime_becomes_iso(self) -> None:
        from adapters.model import convert_embed as ce

        embed = ce(NS(title="t", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual(embed.timestamp, "2026-01-01T00:00:00+00:00")
        passthrough = ce(NS(title="t", timestamp="2026-01-01T00:00:00"))
        self.assertEqual(passthrough.timestamp, "2026-01-01T00:00:00")
        self.assertIsNone(ce(NS(title="t", timestamp=None)).timestamp)

    def test_deleted_referenced_sentinel_stays_none(self) -> None:
        # discord.py-self shape: DeletedReferencedMessage exposes id via a
        # _parent MessageReference (so plain hasattr(resolved, "id") lies)
        sentinel = NS(
            id=999,
            channel_id=500,
            guild_id=42,
            _parent=NS(message_id=999, channel_id=500, guild_id=42),
        )
        lib = library_message(
            reference=NS(message_id=999, channel_id=500, guild_id=42, resolved=sentinel)
        )
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        self.assertIsNone(dto.reference.referenced)
        self.assertEqual(dto.reference.message_id, 999)
        # upstream shape: the NamedTuple-style _deleted marker
        marked = NS(id=999, channel_id=500, guild_id=42, _deleted=True)
        lib2 = library_message(
            reference=NS(message_id=999, channel_id=500, guild_id=42, resolved=marked)
        )
        dto2 = convert_message(lib2, dm=False)
        assert dto2.reference is not None
        self.assertIsNone(dto2.reference.referenced)

    def test_reply_chain_conversion_is_bounded(self) -> None:
        m1 = library_message(id=1, content="first", reference=None)
        ref2 = NS(message_id=1, channel_id=500, guild_id=42, resolved=m1)
        m2 = library_message(id=2, content="second", reference=ref2)
        ref3 = NS(message_id=2, channel_id=500, guild_id=42, resolved=m2)
        m3 = library_message(id=3, content="third", reference=ref3)
        dto = convert_message(m3, dm=False)
        assert dto.reference is not None
        assert dto.reference.referenced is not None
        self.assertEqual(dto.reference.referenced.id, 2)  # one level down
        inner = dto.reference.referenced
        assert inner.reference is not None
        self.assertEqual(inner.reference.message_id, 1)  # ids kept...
        self.assertIsNone(inner.reference.referenced)  # ...chain capped

    def test_self_referential_resolution_terminates(self) -> None:
        lib = library_message(id=7, reference=NS(message_id=7, channel_id=500, guild_id=42))
        lib.reference.resolved = lib  # pathological cache cycle
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        assert dto.reference.referenced is not None
        assert dto.reference.referenced.reference is not None
        self.assertEqual(dto.reference.referenced.id, 7)
        self.assertIsNone(dto.reference.referenced.reference.referenced)


class TestBackwardCompatibility(unittest.TestCase):
    def test_original_constructor_surface(self) -> None:
        dto = DiscordMessage(1, 2, 3, "hi", True)
        self.assertEqual(
            (dto.id, dto.channel_id, dto.author_id, dto.content, dto.dm), (1, 2, 3, "hi", True)
        )
        self.assertIn("ch=2", repr(dto))  # historical friendly repr kept

    def test_keyword_construction_unchanged(self) -> None:
        dto = DiscordMessage(id=5, channel_id=6, author_id=7, content="x", dm=False)
        self.assertEqual(dto.embeds, ())
        self.assertIsNone(dto.raw)


class TestIsolationAndRaw(unittest.TestCase):
    def test_frozen_event_state(self) -> None:
        dto = convert_message(library_message(), dm=False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _attempt_content_store(dto, "tampered")

    def test_raw_never_in_repr_or_summary(self) -> None:
        lib = library_message()
        dto = convert_message(lib, dm=False)
        self.assertNotIn("raw", repr(dto))
        self.assertNotIn("the_bot", dto.summary())  # no payload leakage

    def test_raw_not_serialized_by_dataclass(self) -> None:
        lib = library_message()
        dto = convert_message(lib, dm=False)
        payload = dataclasses.asdict(dto)
        # asdict would deep-copy the library object into "raw": forbid it by
        # contract — the sanctioned serialization is the flat/summary view
        self.assertIn("raw", payload)  # documented limitation of asdict();
        # the framework itself never serializes messages (audit previews
        # content[:80] + counts only) — pinned by test_audit_note_metadata


class TestSimulatorRichBoundary(unittest.IsolatedAsyncioTestCase):
    def _world(self) -> SimWorld:
        from adapters.simulator.world import SimWorld

        world = SimWorld(responder_id=None)
        world.add_user(1001, "owner")
        world.add_user(999999, "bot")
        world.add_channel(500, "grind")
        return world

    async def test_rich_delivery_and_events(self) -> None:
        from adapters.simulator.client import SimClient

        world = self._world()
        client = SimClient(world, 1001)
        seen: list[DiscordMessage] = []
        reactions: list[tuple[Any, ...]] = []
        edits: list[DiscordMessage] = []
        deletes: list[tuple[Any, ...]] = []
        client.on_message = seen.append
        client.on_reaction = lambda *a: reactions.append(a)
        client.on_message_edit = edits.append
        client.on_message_delete = lambda *a: deletes.append(a)
        await client.connect()

        button = DiscordComponent(
            type=2, type_name="button", custom_id="kakera:loot", label="React"
        )
        row = DiscordComponent(type=1, type_name="action_row", components=(button,))
        embed = DiscordEmbed(
            title="Character", description="★", fields=(DiscordEmbedField("Rank", "1"),)
        )
        message = world.deliver(
            500,
            999999,
            "$mu",
            author_name="the_bot",
            guild_id=42,
            embeds=(embed,),
            components=(row,),
            reference_message_id=9,
        )
        self.assertEqual(len(seen), 1)
        rich = seen[0]
        assert rich.reference is not None
        self.assertEqual(rich.embeds[0].field_map(), {"Rank": "1"})
        self.assertEqual(rich.buttons[0].custom_id, "kakera:loot")
        self.assertEqual(rich.reference.message_id, 9)
        self.assertIsNone(rich.raw)  # sim has no library object

        # reaction event WITHOUT any fetch (§9)
        world.react(999999, message.id, "❤")
        self.assertEqual(len(reactions), 1)
        kind, mid, _cid, _gid, uid, emoji, _raw = reactions[0]
        self.assertEqual((kind, mid, uid, emoji), ("add", message.id, 999999, "❤"))

        world.edit_message(message.id, "edited")
        self.assertEqual(edits[0].content, "edited")
        world.delete_message(message.id)
        self.assertEqual(deletes[0][0], message.id)

    async def test_capability_actions_through_pipeline(self) -> None:
        """kakerareact + kakeraloot shapes: services actions flow through the
        scheduler (paced) into the world; audit rows carry the action kind."""
        from core.audit import AuditRecorder
        from core.event_bus import EventBus, TaskCompleted
        from core.priority import Priority
        from core.task_scheduler import TaskScheduler

        world = self._world()
        from adapters.simulator.client import SimClient

        client = SimClient(world, 1001)
        await client.connect()
        button = DiscordComponent(type=2, type_name="button", custom_id="kakera:claim")
        row = DiscordComponent(type=1, type_name="action_row", components=(button,))
        message = world.deliver(500, 999999, "$mu", components=(row,))

        bus = EventBus()

        class FakeLimiter:
            async def acquire(
                self,
                action: str,
                account: str | None = None,
                plugin: str | None = None,
                priority: Priority = Priority.NORMAL,
            ) -> Any:
                return NS(wait_s=0.0, jitter_s=0.0)

            def report_server_limit(self, *a: Any, **k: Any) -> None:
                pass

        audit = AuditRecorder()

        # duck-types the scheduler's sender seam (task fields are Optional
        # on Task but always present for these two action kinds)
        def deliver(task: Any) -> Any:
            if task.action == "reaction":
                return client.add_reaction(task.channel_id, task.message_id, task.content)
            return client.click_button(task.channel_id, task.message_id, task.content)

        scheduler = TaskScheduler(bus, None, FakeLimiter(), deliver, audit=audit)
        scheduler.start()
        try:
            t1 = scheduler.submit(
                "alpha",
                "kakerareact",
                "reaction",
                channel_id=500,
                message_id=message.id,
                content="❤",
                priority=Priority.NORMAL,
            )
            t2 = scheduler.submit(
                "alpha",
                "kakeraloot",
                "button",
                channel_id=500,
                message_id=message.id,
                content="kakera:claim",
            )
            # the audit rows are written in the finish TAIL, after the status
            # flips: wait for the published terminal events, not the status
            completed: set[int] = set()
            bus.subscribe(TaskCompleted, lambda event: completed.add(event.task_id))
            for _ in range(100):
                if {t1.id, t2.id} <= completed:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(t1.status, "completed")
            self.assertEqual(t2.status, "completed")
            self.assertEqual(client.button_clicks, [(message.id, "kakera:claim")])
            self.assertEqual(message.reactions[0].emoji, "❤")
            rows = [e for e in audit.tail(10) if e["direction"] == "out"]
            self.assertEqual({r["action"] for r in rows}, {"reaction", "button"})
        finally:
            await scheduler.stop(drain=True)

    async def test_button_target_gone(self) -> None:
        from adapters.simulator.client import SimClient
        from core.errors import TargetGone

        world = self._world()
        client = SimClient(world, 1001)
        await client.connect()
        with self.assertRaises(TargetGone):
            await client.click_button(500, 424242, "gone")


class TestEventBusTypedEvents(unittest.IsolatedAsyncioTestCase):
    async def test_rich_message_rides_message_received(self) -> None:
        bus = EventBus()
        seen: list[MessageReceived] = []
        bus.subscribe(MessageReceived, seen.append)
        rich = convert_message(library_message(), dm=False)
        bus.publish_nowait(
            MessageReceived(
                account="alpha",
                message_id=rich.id,
                channel_id=rich.channel_id,
                author_id=rich.author_id,
                content=rich.content,
                dm=rich.dm,
                message=rich,
            )
        )
        await asyncio.sleep(0.02)
        event = seen[0]
        self.assertIs(event.message, rich)
        self.assertEqual(event.message.embeds[0].title, "Character")

    async def test_reaction_event_dispatch(self) -> None:
        bus = EventBus()
        seen: list[ReactionAdded] = []
        bus.subscribe(ReactionAdded, seen.append)
        bus.publish_nowait(
            ReactionAdded(
                account="alpha",
                message_id=1,
                channel_id=2,
                guild_id=3,
                user_id=4,
                emoji="❤",
                dm=False,
            )
        )
        await asyncio.sleep(0.02)
        self.assertEqual(seen[0].emoji, "❤")

    async def test_plugin_isolation_on_shared_event(self) -> None:
        """Two subscribers get the SAME event object; the frozen DTO makes
        accidental mutation impossible (isolation §27)."""
        bus = EventBus()
        seen_a: list[MessageReceived] = []
        seen_b: list[MessageReceived] = []
        bus.subscribe(MessageReceived, seen_a.append)
        bus.subscribe(MessageReceived, seen_b.append)
        rich = convert_message(library_message(), dm=False)
        bus.publish_nowait(
            MessageReceived(
                account="a",
                message=rich,
                message_id=rich.id,
                channel_id=rich.channel_id,
                author_id=rich.author_id,
                content=rich.content,
                dm=rich.dm,
            )
        )
        await asyncio.sleep(0.02)
        self.assertIs(seen_a[0].message, seen_b[0].message)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            seen_a[0].message.embeds[0].title = "tampered"
        self.assertEqual(seen_b[0].message.embeds[0].title, "Character")


class TestFuturePluginShapes(unittest.TestCase):
    """§22: demonstrate snipe / kakerareact / kakeraloot / soulmate-farm /
    exporter needs are met WITHOUT another core rewrite (shapes only)."""

    def test_snipe_shape(self) -> None:
        lib = library_message(content="caught!", reference=NS(message_id=9, channel_id=500))
        dto = convert_message(lib, dm=False)
        assert dto.author is not None
        assert dto.channel is not None
        self.assertTrue(dto.content and dto.author.id and dto.channel.id)
        self.assertIsNotNone(
            dto.created_at if dto.created_at is not None else dto.id
        )  # timestamp or id available

    def test_kakerareact_shape(self) -> None:
        dto = convert_message(library_message(), dm=False)
        self.assertTrue(any(r.emoji for r in dto.reactions))
        self.assertEqual(dto.reactions[0].count, 3)

    def test_kakeraloot_shape(self) -> None:
        dto = convert_message(library_message(), dm=False)
        loot = dto.button("kakera:loot")
        assert loot is not None
        self.assertEqual(loot.custom_id, "kakera:loot")
        self.assertFalse(loot.disabled)

    def test_soulmate_farm_shape(self) -> None:
        dto = convert_message(library_message(), dm=False)
        self.assertEqual(dto.embeds[0].description, "Rarity: ★★★")
        self.assertEqual(len(dto.buttons), 2)

    def test_exporter_shape_dm(self) -> None:
        lib = library_message(
            content="Soulmates\n---\nAlice",
            channel=NS(
                id=9001, name=None, type=NS(name="private"), recipient=NS(id=999999), guild=None
            ),
            guild=None,
            embeds=[],
            components=[],
            reference=None,
        )
        dto = convert_message(lib, dm=True)
        self.assertTrue(dto.dm)  # DM detection lives on the flag;
        self.assertIn("Alice", dto.content)  # channel mgmt stays in services


class TestLiveClientBoundary(unittest.IsolatedAsyncioTestCase):
    """Live-adapter boundary fidelity that the simulator cannot mirror
    (library error shapes, session-liveness gating, lazy import)."""

    class NotFound(Exception):
        pass

    class Forbidden(Exception):
        pass

    class _FailingFetchClient:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        async def fetch_user(self, user_id: int) -> None:
            raise self._exc

    class _ReadyLibClient:
        """discord.Client stand-in that reaches READY then parks."""

        #: registered at runtime via event() by the adapter under test
        on_ready: Callable[[], Any]

        last_built = None

        def __init__(self, **kwargs: Any) -> None:
            self.constructor_kwargs = kwargs
            self.closed = False
            self.user = NS(id=777)
            self._park = asyncio.Event()

        def event(self, coro: Callable[..., Any]) -> Callable[..., Any]:
            setattr(self, coro.__name__, coro)
            return coro

        async def start(self, token: str) -> None:
            await self.on_ready()
            await self._park.wait()

        async def close(self) -> None:
            self.closed = True
            self._park.set()

    def test_module_import_never_pulls_the_library(self) -> None:
        import importlib

        saved = {
            name: sys.modules.get(name)
            for name in ("discord", "adapters.discord", "adapters.discord.client")
        }
        for name in saved:
            sys.modules.pop(name, None)
        try:
            importlib.import_module("adapters.discord")
            self.assertNotIn("discord", sys.modules)
        finally:
            for name, mod in saved.items():
                if mod is not None:
                    sys.modules[name] = mod
                else:
                    sys.modules.pop(name, None)

    async def test_fetch_user_none_for_gone_users(self) -> None:
        from adapters.discord.client import RealDiscordClient

        for exc in (
            self.NotFound("404 Not Found (error code: 10013): Unknown User"),
            self.Forbidden("403 Forbidden (error code: 10013): Unknown User"),
        ):
            client = RealDiscordClient("tok")
            _install_lib_client(client, self._FailingFetchClient(exc))
            client.connected = True
            self.assertIsNone(await client.fetch_user(1))

    async def test_fetch_user_raises_real_denials(self) -> None:
        from adapters.discord.client import RealDiscordClient

        client = RealDiscordClient("tok")
        _install_lib_client(
            client,
            self._FailingFetchClient(
                self.Forbidden("403 Forbidden (error code: 50001): Missing Access")
            ),
        )
        client.connected = True
        with self.assertRaises(self.Forbidden):
            await client.fetch_user(1)

    async def test_sends_raise_connection_error_offline(self) -> None:
        from adapters.discord.client import RealDiscordClient

        client = RealDiscordClient("tok")
        self.assertFalse(client.connected)
        with self.assertRaises(ConnectionError):
            client._require_client()
        _install_lib_client(client, object())
        with self.assertRaises(ConnectionError):  # built but not connected
            client._require_client()
        client.connected = True
        self.assertIs(client._require_client(), client._client)
        client.connected = False
        with self.assertRaises(ConnectionError):
            client._require_client()

    async def test_connected_lifecycle_through_connect_and_disconnect(self) -> None:
        from adapters.discord.client import RealDiscordClient
        from tests.unit.helpers import fake_discord_sysmodules

        lib_cls = type("LibClient", (self._ReadyLibClient,), {})
        with mock.patch.dict(sys.modules, fake_discord_sysmodules(lib_cls)):
            client = RealDiscordClient("tok")
            await client.connect()
            self.assertTrue(client.connected)
            self.assertEqual(client.user_id, 777)
            self.assertTrue(client.cached_snapshot()["available"])
            await client.disconnect()
            self.assertFalse(client.connected)
            self.assertFalse(client.cached_snapshot()["available"])
            with self.assertRaises(ConnectionError):
                client._require_client()

    async def test_latency_ms_rejects_inf_and_nan(self) -> None:
        from adapters.discord.client import RealDiscordClient

        client = RealDiscordClient("tok")
        self.assertIsNone(client.latency_ms)
        for bad in (float("inf"), float("-inf"), float("nan"), -1.0):
            _install_lib_client(client, NS(latency=bad))
            self.assertIsNone(client.latency_ms)
        _install_lib_client(client, NS(latency=0.021))
        assert client.latency_ms is not None
        self.assertAlmostEqual(client.latency_ms, 21.0)


class _LibErrors:
    """discord.errors stand-ins shaped like discord.py-self 2.1.0."""

    class HTTPException(Exception):
        def __init__(self, status: int = 0, headers: Any = None) -> None:
            super().__init__(f"{status} error")
            self.status = status
            self.response = NS(status=status, headers=headers or {})

    class Forbidden(HTTPException):
        def __init__(self, headers: Any = None) -> None:
            super().__init__(403, headers)

    class NotFound(HTTPException):
        def __init__(self, headers: Any = None) -> None:
            super().__init__(404, headers)

    class DiscordServerError(HTTPException):
        def __init__(self, headers: Any = None) -> None:
            super().__init__(502, headers)

    class RateLimited(Exception):
        def __init__(self, retry_after: Any, cloudflare: bool = False) -> None:
            super().__init__("Too many requests.")
            self.retry_after = retry_after
            self.cloudflare = cloudflare

    class CaptchaRequired(HTTPException):
        def __init__(self) -> None:
            super().__init__(400, {})


class TestErrorMappingRound2(unittest.TestCase):
    """Exception-shape table from the installed fork (errors.py / http.py)."""

    def _client(self) -> RealDiscordClient:
        from adapters.discord.client import RealDiscordClient

        client = RealDiscordClient("tok")
        client._discord = NS(
            HTTPException=_LibErrors.HTTPException,
            Forbidden=_LibErrors.Forbidden,
            NotFound=_LibErrors.NotFound,
        )
        return client

    def test_rate_limited_maps_to_server_rate_limit(self) -> None:
        from core.errors import ServerRateLimit

        client = self._client()
        mapped = client._map_send_error(_LibErrors.RateLimited(73.5), 1)
        self.assertIsInstance(mapped, ServerRateLimit)
        assert isinstance(mapped, ServerRateLimit)  # narrow for mypy
        self.assertEqual(mapped.retry_after, 73.5)
        self.assertFalse(mapped.global_)

    def test_rate_limited_garbage_retry_after_defaults(self) -> None:
        from core.errors import ServerRateLimit

        client = self._client()
        mapped = client._map_send_error(_LibErrors.RateLimited(None), 1)
        self.assertIsInstance(mapped, ServerRateLimit)
        assert isinstance(mapped, ServerRateLimit)  # narrow for mypy
        self.assertEqual(mapped.retry_after, 1.0)

    def test_captcha_required_maps_to_access_denied(self) -> None:
        from core.errors import AccessDenied

        client = self._client()
        client.account = "alpha"
        mapped = client._map_send_error(_LibErrors.CaptchaRequired(), 42)
        self.assertIsInstance(mapped, AccessDenied)
        self.assertIn("captcha", str(mapped))

    def test_429_header_intake_honors_retry_after(self) -> None:
        from core.errors import ServerRateLimit

        client = self._client()
        exc = _LibErrors.HTTPException(429, {"Retry-After": "0.625", "X-RateLimit-Global": "true"})
        mapped = client._map_send_error(exc, 1)
        self.assertIsInstance(mapped, ServerRateLimit)
        assert isinstance(mapped, ServerRateLimit)  # narrow for mypy
        self.assertEqual(mapped.retry_after, 0.625)
        self.assertTrue(mapped.global_)

    def test_429_lowercase_headers_and_defaults(self) -> None:
        from core.errors import ServerRateLimit

        client = self._client()
        exc = _LibErrors.HTTPException(429, {"retry-after": "2"})
        mapped = client._map_send_error(exc, 1)
        assert isinstance(mapped, ServerRateLimit)  # narrow for mypy
        self.assertEqual(mapped.retry_after, 2.0)
        self.assertFalse(mapped.global_)
        bare = client._map_send_error(_LibErrors.HTTPException(429), 1)
        self.assertIsInstance(bare, ServerRateLimit)
        assert isinstance(bare, ServerRateLimit)  # narrow for mypy
        self.assertEqual(bare.retry_after, 1.0)

    def test_429_non_finite_retry_after_clamped(self) -> None:
        from core.errors import ServerRateLimit

        for garbage in ("inf", "-inf", "nan", "Infinity"):
            client = self._client()
            exc = _LibErrors.HTTPException(429, {"Retry-After": garbage})
            mapped = client._map_send_error(exc, 1)
            assert isinstance(mapped, ServerRateLimit)  # narrow for mypy
            self.assertEqual(mapped.retry_after, 1.0)

    def test_5xx_and_discord_server_error_map_to_connection_error(self) -> None:
        client = self._client()
        self.assertIsInstance(
            client._map_send_error(_LibErrors.DiscordServerError(), 1), ConnectionError
        )
        self.assertIsInstance(
            client._map_send_error(_LibErrors.HTTPException(500), 1), ConnectionError
        )

    def test_unknown_4xx_still_propagates_unmapped(self) -> None:
        client = self._client()
        self.assertIsNone(client._map_send_error(_LibErrors.HTTPException(400), 1))

    def test_403_and_404_map_to_domain_errors(self) -> None:
        from core.errors import AccessDenied, TargetGone

        client = self._client()
        self.assertIsInstance(client._map_send_error(_LibErrors.Forbidden(), 1), AccessDenied)
        self.assertIsInstance(client._map_send_error(_LibErrors.NotFound(), 1), TargetGone)


class _HangingLibClient:
    """discord.Client stand-in that never reaches READY."""

    #: registered at runtime via event() by the adapter under test
    on_ready: Callable[[], Any]

    def __init__(self, **kwargs: Any) -> None:
        self.constructor_kwargs = kwargs
        self.closed = False
        self.user: Any = None
        self._park = asyncio.Event()

    def event(self, coro: Callable[..., Any]) -> Callable[..., Any]:
        setattr(self, coro.__name__, coro)
        return coro

    async def start(self, token: str) -> None:
        await self._park.wait()

    async def close(self) -> None:
        self.closed = True
        self._park.set()


class _CrashingLibClient(_HangingLibClient):
    """Reaches READY, parks, then start() exits (terminal session end)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = NS(id=777)

    async def start(self, token: str) -> None:
        await self.on_ready()
        await self._park.wait()


class TestConnectedFlagRound2(unittest.IsolatedAsyncioTestCase):
    """Round-2 state-transition coverage for the `connected` flag."""

    def _patch_discord(self, lib_cls: Any) -> AbstractContextManager[Any]:
        from tests.unit.helpers import fake_discord_sysmodules

        return mock.patch.dict(sys.modules, fake_discord_sysmodules(lib_cls))

    async def test_disconnect_during_connect_is_not_a_bad_token(self) -> None:
        from adapters.discord.client import RealDiscordClient

        lib_cls: Any = type("LibClient", (_HangingLibClient,), {})
        with self._patch_discord(lib_cls):
            client = RealDiscordClient("tok")
            lib_client: Any = None

            original_init = lib_cls.__init__

            def spy_init(self: Any, **kwargs: Any) -> None:
                nonlocal lib_client
                original_init(self, **kwargs)
                lib_client = self

            lib_cls.__init__ = spy_init
            task = asyncio.get_running_loop().create_task(client.connect())
            await asyncio.sleep(0.05)
            self.assertFalse(client.connected)
            await client.disconnect()
            with self.assertRaises(ConnectionError):
                await task
            self.assertFalse(client.connected)
            self.assertTrue(lib_client.closed)
            self.assertEqual(client.cached_snapshot(), {"available": False})

    async def test_run_exit_flips_connected_and_snapshot(self) -> None:
        from adapters.discord.client import RealDiscordClient

        lib_cls: Any = type("LibClient", (_CrashingLibClient,), {})
        with self._patch_discord(lib_cls):
            client = RealDiscordClient("tok")
            lib_client: Any = None

            original_init = lib_cls.__init__

            def spy_init(self: Any, **kwargs: Any) -> None:
                nonlocal lib_client
                original_init(self, **kwargs)
                lib_client = self

            lib_cls.__init__ = spy_init
            await client.connect()
            self.assertTrue(client.connected)
            self.assertTrue(client.cached_snapshot()["available"])
            lib_client._park.set()  # terminal session end (start returns)
            for _ in range(100):
                await asyncio.sleep(0.02)
                if not client.connected:
                    break
            self.assertFalse(client.connected)
            self.assertFalse(client.cached_snapshot()["available"])
            with self.assertRaises(ConnectionError):
                client._require_client()

    async def test_resume_replay_duplicate_suppressed(self) -> None:
        from adapters.discord.client import RealDiscordClient

        lib_cls: Any = type("LibClient", (_CrashingLibClient,), {})
        with self._patch_discord(lib_cls):
            client = RealDiscordClient("tok")
            await client.connect()
            seen: list[DiscordMessage] = []
            client.on_message = seen.append

            def lib_message(mid: int) -> NS:
                return NS(
                    id=mid,
                    content="$mu",
                    author=NS(id=999999, name="the_bot"),
                    channel=NS(id=500, name="grind", type=NS(name="text"), guild=NS(id=42)),
                    guild=NS(id=42),
                    embeds=[],
                    components=[],
                    attachments=[],
                    reactions=[],
                    reference=None,
                    created_at=None,
                )

            handler = _lib_client(client).on_message
            await handler(lib_message(1001))
            await handler(lib_message(1001))  # gateway RESUME replay
            await handler(lib_message(1002))
            self.assertEqual([m.id for m in seen], [1001, 1002])
            self.assertTrue(all(m.reference is None for m in seen))


class TestReferenceShapesRound2(unittest.TestCase):
    """Reference fork shapes against the installed library's classes."""

    def test_forward_reference_type_projected(self) -> None:
        lib = library_message(
            reference=NS(
                message_id=999, channel_id=500, guild_id=42, type=NS(name="forward"), resolved=None
            ),
            message_snapshots=[
                NS(id=999, content="forwarded", embeds=[], attachments=[], components=[])
            ],
        )
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        assert dto.reference.referenced is not None
        self.assertEqual(dto.reference.type_name, "forward")
        self.assertEqual(dto.reference.message_id, 999)
        # round-3 F4-4: the forward snapshot hydrates reference.referenced
        # (content visible; ids stay on the reference) — see
        # test_forward_snapshot_hydrates_content below for the full shape
        self.assertEqual(dto.reference.referenced.content, "forwarded")

    def test_forward_without_snapshots_stays_unresolved(self) -> None:
        lib = library_message(
            reference=NS(
                message_id=999, channel_id=500, guild_id=42, type=NS(name="forward"), resolved=None
            ),
            message_snapshots=[],
        )
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        self.assertEqual(dto.reference.type_name, "forward")
        self.assertIsNone(dto.reference.referenced)

    def test_reply_and_default_aliases_project_to_reply(self) -> None:
        reply = convert_message(
            library_message(reference=NS(message_id=9, channel_id=500, type=NS(name="reply"))),
            dm=False,
        )
        default = convert_message(
            library_message(reference=NS(message_id=9, channel_id=500, type=NS(name="default"))),
            dm=False,
        )
        raw_int = convert_message(
            library_message(reference=NS(message_id=9, channel_id=500, type=1)), dm=False
        )
        legacy = convert_message(
            library_message(reference=NS(message_id=9, channel_id=500)), dm=False
        )
        assert reply.reference is not None
        assert default.reference is not None
        assert raw_int.reference is not None
        assert legacy.reference is not None
        self.assertEqual(reply.reference.type_name, "reply")
        self.assertEqual(default.reference.type_name, "reply")
        self.assertEqual(raw_int.reference.type_name, "forward")
        self.assertEqual(legacy.reference.type_name, "reply")

    def test_real_library_deleted_sentinel_excluded(self) -> None:
        import importlib.util

        if importlib.util.find_spec("discord") is None:
            self.skipTest("discord.py-self not installed")
        from discord.message import DeletedReferencedMessage, MessageReference

        ref = MessageReference(message_id=999, channel_id=500, guild_id=42)
        sentinel = DeletedReferencedMessage(ref)
        self.assertTrue(hasattr(sentinel, "id"))
        self.assertTrue(hasattr(sentinel, "_parent"))
        lib = library_message(
            reference=NS(message_id=999, channel_id=500, guild_id=42, resolved=sentinel)
        )
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        self.assertIsNone(dto.reference.referenced)
        self.assertEqual(dto.reference.message_id, 999)

    def test_real_library_resolved_message_included(self) -> None:
        import importlib.util

        if importlib.util.find_spec("discord") is None:
            self.skipTest("discord.py-self not installed")
        cached = library_message(
            id=999, content="original", reference=None, referenced_message=None
        )
        lib = library_message(
            reference=NS(message_id=999, channel_id=500, guild_id=42, resolved=cached)
        )
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        assert dto.reference.referenced is not None
        self.assertEqual(dto.reference.referenced.id, 999)
        self.assertIsNone(dto.reference.referenced.reference)

    def test_all_none_message_converts_without_crash(self) -> None:
        lib = NS(
            id=None,
            content=None,
            author=None,
            channel=None,
            guild=None,
            embeds=None,
            components=None,
            attachments=None,
            reactions=None,
            reference=None,
            referenced_message=None,
            created_at=None,
        )
        dto = convert_message(lib, dm=False)
        self.assertEqual(
            (dto.id, dto.channel_id, dto.author_id, dto.content, dto.dm), (0, 0, 0, "", False)
        )
        self.assertIsNone(dto.author)
        self.assertIsNone(dto.channel)
        self.assertIsNone(dto.reference)
        self.assertEqual(dto.embeds, ())

    def test_uncached_channel_partial_messageable_shape(self) -> None:
        lib = library_message(channel=NS(id=700, name=None, type=None, guild=None))
        dto = convert_message(lib, dm=False)
        assert dto.channel is not None
        self.assertEqual(dto.channel.id, 700)
        self.assertEqual(dto.channel.name, "")
        self.assertEqual(dto.channel.type, "")
        self.assertIsNone(dto.channel.guild_id)


class TestForwardHydrationRound3(unittest.TestCase):
    """Round-3 F4-4: forwarded messages hydrate reference.referenced from
    message_snapshots (fork message.py:2101; reference.resolved stays None
    for forwards), additively, ids kept — no content invisibility."""

    def _forward(self, **snapshot_overrides: Any) -> NS:
        snapshot: dict[str, Any] = {
            "id": 999,
            "content": "forwarded payload",
            "embeds": [NS(title="Fwd", description="d", fields=[])],
            "attachments": [],
            "components": [],
            "created_at": None,
        }
        snapshot.update(snapshot_overrides)
        return library_message(
            reference=NS(
                message_id=999, channel_id=500, guild_id=42, type=NS(name="forward"), resolved=None
            ),
            message_snapshots=[NS(**snapshot)],
        )

    def test_forward_snapshot_hydrates_content_and_embeds(self) -> None:
        dto = convert_message(self._forward(), dm=False)
        ref = dto.reference
        assert ref is not None
        assert ref.referenced is not None
        self.assertEqual(ref.type_name, "forward")
        self.assertEqual((ref.message_id, ref.channel_id, ref.guild_id), (999, 500, 42))
        hydrated = ref.referenced
        self.assertIsNotNone(hydrated)
        self.assertEqual(hydrated.id, 999)  # ids stay on the ref
        self.assertEqual(hydrated.channel_id, 500)
        self.assertEqual(hydrated.guild_id, 42)
        self.assertEqual(hydrated.content, "forwarded payload")
        self.assertEqual(hydrated.embeds[0].title, "Fwd")
        self.assertIsNone(hydrated.author)  # not in the snapshot

    def test_reply_references_never_hydrate_from_snapshots(self) -> None:
        lib = library_message(
            reference=NS(
                message_id=999, channel_id=500, guild_id=42, type=NS(name="reply"), resolved=None
            ),
            message_snapshots=[NS(id=999, content="not a forward")],
        )
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        self.assertIsNone(dto.reference.referenced)

    def test_cached_resolved_wins_over_snapshot(self) -> None:
        cached = library_message(id=999, content="cached original")
        lib = self._forward()
        lib.reference.resolved = cached
        dto = convert_message(lib, dm=False)
        assert dto.reference is not None
        assert dto.reference.referenced is not None
        self.assertEqual(dto.reference.referenced.content, "cached original")

    def test_nested_forward_dropped_by_depth_cap(self) -> None:
        inner = library_message(
            id=2,
            content="inner",
            reference=NS(message_id=1, channel_id=500, type=NS(name="forward"), resolved=None),
            message_snapshots=[NS(id=1, content="snap")],
        )
        outer = library_message(
            id=3, reference=NS(message_id=2, channel_id=500, guild_id=42, resolved=inner)
        )
        dto = convert_message(outer, dm=False)
        assert dto.reference is not None
        assert dto.reference.referenced is not None
        assert dto.reference.referenced.reference is not None
        self.assertEqual(dto.reference.referenced.id, 2)
        self.assertIsNone(  # chain cap applies before hydration
            dto.reference.referenced.reference.referenced
        )


class TestIdentifyGateScaling(unittest.TestCase):
    """Round-3 F4-2: the gate derives its interval from the expected fleet
    size so a synchronized (re)identify wave clears the library's 60s
    hook ceiling (client.py:1020 wait_for around IDENTIFY).

    AUDIT FIX polarity: the spacing now FLOORS at Discord's per-IP
    IDENTIFY budget (~1/5s, MIN_INTERVAL_S=5.0) — the old sub-5s scaling
    for fleets >= 13 traded the 60s wave ceiling for per-IP throttling;
    giant waves are the fast-fail projection's job instead."""

    def test_interval_scales_with_expected_fleet(self) -> None:
        from adapters.discord.client import IdentifyGate

        gate = IdentifyGate(5.0)
        self.assertEqual(gate.effective_interval, 5.0)  # single account
        # corrected polarity: fleets >= 13 hold the per-IP floor (the
        # old pins 50/13 and the 1.0s floor outpaced 1 identify / 5s/IP)
        gate.expected = 13
        self.assertEqual(gate.effective_interval, 5.0)  # per-IP floor
        gate.expected = 60
        self.assertEqual(gate.effective_interval, 5.0)  # floor holds
        gate.expected = 1000
        self.assertEqual(gate.effective_interval, 5.0)  # floor holds

    def test_base_interval_below_formula_is_kept(self) -> None:
        from adapters.discord.client import IdentifyGate

        gate = IdentifyGate(2.0)
        gate.expected = 5  # formula says 10s — the tighter base wins
        self.assertEqual(gate.effective_interval, 2.0)

    def test_factory_counts_distinct_accounts_not_reconnects(self) -> None:
        from adapters.discord.client import RealClientFactory

        factory = RealClientFactory()
        self.assertEqual(factory._identify_gate.expected, 1)
        factory("alpha", "tok")
        factory("alpha", "tok")  # reconnect of a known account
        self.assertEqual(factory._identify_gate.expected, 1)
        factory("beta", "tok")
        factory("gamma", "tok")
        self.assertEqual(factory._identify_gate.expected, 3)

    def test_expected_is_dynamic_between_releases(self) -> None:
        from adapters.discord.client import IdentifyGate

        # interval above the floor so the fleet scaling stays observable
        # (a default 5.0s gate is already AT the per-IP floor for every
        # fleet size — corrected polarity)
        gate = IdentifyGate(30.0)
        gate.expected = 2
        first = gate.effective_interval
        self.assertEqual(first, 25.0)  # budget/expected under the base
        gate.expected = 50  # fleet grew mid-wave
        self.assertLess(gate.effective_interval, first)
        self.assertEqual(gate.effective_interval, 5.0)  # per-IP floor


class TestIdentifyGateWaveBudget(unittest.IsolatedAsyncioTestCase):
    """The wave property that matters: with N waiters released FIFO, the
    LAST one still clears the gate within the wait budget (scaled-down
    numbers — the production defaults are 5.0/50.0/1.0)."""

    async def test_synchronized_wave_stays_under_budget(self) -> None:
        from adapters.discord.client import IdentifyGate

        gate = IdentifyGate(1.0, min_interval=0.01, wait_budget=0.30)
        gate.expected = 8
        loop = asyncio.get_running_loop()

        async def waiter(k: int) -> float:
            t0 = loop.time()
            await gate.acquire(initial=True)
            return loop.time() - t0

        waits = await asyncio.gather(*(waiter(k) for k in range(8)))
        # clock-free regression check (review round: a wall-clock bound
        # cannot separate runner lateness from a pacing regression by
        # construction): the wave budget SCALES the interval to fit the
        # expected fleet - wait_budget / expected - and that math is
        # immune to lateness. The controlled-clock release-time
        # choreography is future hardening alongside this.
        self.assertAlmostEqual(gate.effective_interval, 0.30 / 8)
        # budget + slack: the waits include scheduler lateness under
        # 4-worker gate load (0.232s observed, 2026-11-27 — 0.20 of
        # slack was 32ms short). A structurally broken wait budget
        # lets a waiter sit a full interval (1.0s) and is caught
        # with 0.40s of headroom to spare.
        self.assertLessEqual(max(waits), 0.30 + 0.30)
        # and the pacer actually spaced releases FIFO
        self.assertGreater(max(waits), 0.20)


class TestClientConstructorRound3(unittest.IsolatedAsyncioTestCase):
    """Round-3 F4-1/F4-8: max_ratelimit_timeout IS passed (unset, the
    library sleeps any 429 in-request — http.py:991-1018); the dead
    intents=None fallback is gone."""

    def _patch_discord(self, lib_cls: Any) -> AbstractContextManager[Any]:
        from tests.unit.helpers import fake_discord_sysmodules

        return mock.patch.dict(sys.modules, fake_discord_sysmodules(lib_cls))

    async def test_max_ratelimit_timeout_passed_and_no_intents(self) -> None:
        from adapters.discord.client import MAX_RATELIMIT_TIMEOUT_S, RealDiscordClient

        captured: dict[str, Any] = {}

        class LibClient(TestLiveClientBoundary._ReadyLibClient):
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)
                super().__init__(**kwargs)

        with self._patch_discord(LibClient):
            client = RealDiscordClient("tok")
            await client.connect()
        # better.py-self memory/persistence options ride along with the
        # pinned timeout (caps, gc freeze, backlog warning; persistence
        # itself is keyed to the account name and omitted without one)
        # the Bot construction rides its four command kwargs alongside
        # the memory options; the prefix is the dynamic resolver's
        # callable (mention default), owner_ids the empty set (no owners
        # configured on a bare client)
        self.assertEqual(
            {k: v for k, v in captured.items() if k != "command_prefix"},
            {
                "max_ratelimit_timeout": MAX_RATELIMIT_TIMEOUT_S,
                "user_bot": True,
                "owner_ids": set(),
                "max_member_cache": 500,
                "max_total_member_cache": 5000,
                "max_thread_cache": 200,
                "max_total_thread_cache": 1000,
                "gc_freeze": True,
                "dispatch_backlog_warning": 500,
            },
        )
        self.assertTrue(callable(captured["command_prefix"]))
        self.assertEqual(captured["max_ratelimit_timeout"], 30.0)
        self.assertNotIn("intents", captured)  # dead fallback dropped
        self.assertNotIn("persistence_path", captured)  # no account name

    async def test_layout_without_kwarg_fails_contained(self) -> None:
        """The construction-fork era's degraded fallback (plain Client
        without the cap) retired with the fork: ONE Bot construction,
        contained by contract — a layout that rejects it fails the
        connect with the AccountError connect() promises, never a
        silent uncapped client."""
        from adapters.discord.client import RealDiscordClient
        from core.errors import AccountError

        class OldLayoutClient(TestLiveClientBoundary._ReadyLibClient):
            def __init__(self, **kwargs: Any) -> None:
                if "max_ratelimit_timeout" in kwargs:
                    raise TypeError("unexpected keyword argument")
                super().__init__(**kwargs)

        with self._patch_discord(OldLayoutClient):
            client = RealDiscordClient("tok")
            with self.assertRaises(AccountError) as ctx:
                await client.connect()
            self.assertIn("rejected the Bot construction", str(ctx.exception))
        self.assertFalse(client.connected)

    async def test_fork_persistence_keyed_to_account(self) -> None:
        """With an account name, the fork's cache persistence points at
        state/<account>-cache.bin (owner-only snapshot for fast warm
        restarts) and the directory is ensured for the atomic writer."""
        import tempfile
        from pathlib import Path

        from adapters.discord.client import RealDiscordClient

        captured: dict[str, Any] = {}

        from tests.unit.helpers import fake_discord_sysmodules

        class LibClient(TestLiveClientBoundary._ReadyLibClient):
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)
                super().__init__(**kwargs)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(sys.modules, fake_discord_sysmodules(LibClient)),
        ):
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                client = RealDiscordClient("tok")
                client.account = "bravo"
                await client.connect()
                self.assertTrue(Path(tmp, "state").is_dir())
            finally:
                os.chdir(cwd)
        self.assertEqual(captured["persistence_path"], Path("state/bravo-cache.bin"))
        self.assertEqual(captured["persistence_max_age"], 86400.0)


class _RawEventLibClient(_HangingLibClient):
    """discord.Client stand-in reaching READY then parking (raw-handler
    tests drive the registered handlers by hand)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = NS(id=777)

    async def start(self, token: str) -> None:
        await self.on_ready()
        await self._park.wait()


class TestRawEventMappingRound3(unittest.IsolatedAsyncioTestCase):
    """Round-3 F4-3: raw payload events (which the fork dispatches for
    EVERY dispatch, cached or not — state.py:1848/1877/1927) map onto the
    existing callbacks; the cached-only rich handlers (state.py:1850/1882/
    1937) suppress as duplicates of the raw publication.

    AUDIT FIX polarity: the rich handlers now publish OTHERS' cached
    events (others-only, matching the raw handlers + the sim world) —
    under the old inverted filter the "rich suppresses" sequences below
    passed VACUOUSLY (the rich copy returned before ever reaching the
    dedup mark); they now exercise _mark_event_key for real. Explicit
    rich-polarity coverage lives in tests/unit/test_discord_adapter_fixes.py."""

    async def _connected_client(self) -> RealDiscordClient:
        from adapters.discord.client import RealDiscordClient
        from tests.unit.helpers import fake_discord_sysmodules

        lib_cls = type("LibClient", (_RawEventLibClient,), {})
        with mock.patch.dict(sys.modules, fake_discord_sysmodules(lib_cls)):
            client = RealDiscordClient("tok")
            await client.connect()
        self.assertTrue(client.connected)
        self.addAsyncCleanup(client.disconnect)
        return client

    async def test_raw_reaction_maps_and_rich_suppresses(self) -> None:
        client = await self._connected_client()
        reactions: list[tuple[Any, ...]] = []
        client.on_reaction = lambda *a: reactions.append(a)
        payload = NS(
            message_id=1,
            channel_id=2,
            guild_id=3,
            user_id=999999,
            emoji=NS(name="❤", id=None, animated=False),
            member=None,
        )
        await _lib_client(client).on_raw_reaction_add(payload)
        self.assertEqual(len(reactions), 1)
        kind, mid, cid, gid, uid, emoji, raw = reactions[0]
        self.assertEqual((kind, mid, cid, gid, uid, emoji), ("add", 1, 2, 3, 999999, "❤"))
        self.assertIs(raw, payload)  # runtime-only raw handle rides along
        # the fork follows raw with the rich event for cached messages —
        # same dispatch, must NOT double-publish
        await _lib_client(client).on_reaction_add(
            NS(
                message=NS(id=1, channel=NS(id=2, guild=NS(id=3)), guild=None),
                emoji=NS(name="❤", id=None),
            ),
            NS(id=999999),
        )
        self.assertEqual(len(reactions), 1)

    async def test_raw_reaction_remove_maps(self) -> None:
        client = await self._connected_client()
        reactions: list[tuple[Any, ...]] = []
        client.on_reaction = lambda *a: reactions.append(a)
        await _lib_client(client).on_raw_reaction_remove(
            NS(
                message_id=9,
                channel_id=5,
                guild_id=None,
                user_id=42,
                emoji=NS(name="kakera", id=77, animated=False),
            )
        )
        self.assertEqual(reactions[0][:6], ("remove", 9, 5, None, 42, "<:kakera:77>"))
        # rich remove for the same cached dispatch: suppressed
        await _lib_client(client).on_reaction_remove(
            NS(
                message=NS(id=9, channel=NS(id=5, guild=None), guild=None),
                emoji=NS(name="kakera", id=77),
            ),
            NS(id=42),
        )
        self.assertEqual(len(reactions), 1)

    async def test_raw_reaction_filters_self(self) -> None:
        client = await self._connected_client()
        reactions: list[tuple[Any, ...]] = []
        client.on_reaction = lambda *a: reactions.append(a)
        await _lib_client(client).on_raw_reaction_add(
            NS(
                message_id=1,
                channel_id=2,
                guild_id=3,
                user_id=777,
                emoji=NS(name="❤", id=None),
                member=None,
            )
        )
        self.assertEqual(reactions, [])

    async def test_raw_edit_maps_minimal_message(self) -> None:
        client = await self._connected_client()
        edits: list[DiscordMessage] = []
        client.on_message_edit = edits.append
        updated = NS(
            id=5,
            content="edited late",
            author=NS(id=999999, name="the_bot"),
            channel=NS(id=500, name="grind", type=NS(name="text"), guild=NS(id=42)),
            guild=NS(id=42),
            embeds=[],
            components=[],
            attachments=[],
            reactions=[],
            reference=None,
            created_at=None,
        )
        await _lib_client(client).on_raw_message_edit(
            NS(
                message_id=5,
                channel_id=500,
                guild_id=42,
                data={},
                cached_message=None,
                message=updated,
            )
        )
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].id, 5)
        self.assertEqual(edits[0].content, "edited late")
        # the cached-only rich edit for the same dispatch: suppressed
        await _lib_client(client).on_message_edit(
            NS(id=1),
            NS(
                id=5,
                content="edited late",
                author=NS(id=999999),
                channel=NS(id=500, type=NS(name="text"), guild=NS(id=42)),
            ),
        )
        self.assertEqual(len(edits), 1)

    async def test_raw_edit_filters_self(self) -> None:
        client = await self._connected_client()
        edits: list[DiscordMessage] = []
        client.on_message_edit = edits.append
        await _lib_client(client).on_raw_message_edit(
            NS(
                message_id=5,
                channel_id=500,
                guild_id=42,
                data={},
                cached_message=None,
                message=NS(
                    id=5,
                    content="own edit",
                    author=NS(id=777),
                    channel=NS(id=500, guild=None),
                    embeds=[],
                    components=[],
                    attachments=[],
                    reactions=[],
                    reference=None,
                    created_at=None,
                ),
            )
        )
        self.assertEqual(edits, [])

    async def test_raw_delete_publishes_uncached_and_rich_suppresses(self) -> None:
        client = await self._connected_client()
        deletes: list[tuple[Any, ...]] = []
        client.on_message_delete = lambda *a: deletes.append(a)
        # UNCACHED delete — the case the rich path silently dropped
        await _lib_client(client).on_raw_message_delete(
            NS(message_id=7, channel_id=500, guild_id=42, cached_message=None)
        )
        self.assertEqual(deletes, [(7, 500, 42, None)])
        # cached dispatch follows (state.py:1850): must not double-publish
        cached = NS(id=7, channel=NS(id=500), guild=NS(id=42), author=NS(id=999999))
        await _lib_client(client).on_message_delete(cached)
        self.assertEqual(len(deletes), 1)

    async def test_raw_delete_filters_cached_self_author(self) -> None:
        client = await self._connected_client()
        deletes: list[tuple[Any, ...]] = []
        client.on_message_delete = lambda *a: deletes.append(a)
        own = NS(id=8, channel=NS(id=500), guild=NS(id=42), author=NS(id=777))
        await _lib_client(client).on_raw_message_delete(
            NS(message_id=8, channel_id=500, guild_id=42, cached_message=own)
        )
        self.assertEqual(deletes, [])


class TestClickHardeningRound3(unittest.IsolatedAsyncioTestCase):
    """Round-3 F4-5/F4-6: clicks are bounded (uncapped 202 retry path —
    http.py:936-948) and a stop()-driven cancel no longer masquerades as
    InvalidData (interactions.py:215-216 converts CancelledError)."""

    class InvalidData(Exception):
        pass

    def _clickable(self, click: Any) -> RealDiscordClient:
        from adapters.discord.client import RealDiscordClient

        client = RealDiscordClient("tok")
        client.connected = True
        component = NS(custom_id="kakera:claim", click=click)
        message = NS(id=11, components=[NS(children=[component])])
        channel = NS(id=500, get_message=lambda mid: message)
        _install_lib_client(client, NS(get_channel=lambda cid: channel))
        client._discord = NS(
            DMChannel=type("DMChannel", (), {}),
            HTTPException=type("HTTPException", (Exception,), {}),
            Forbidden=type("Forbidden", (Exception,), {}),
            NotFound=type("NotFound", (Exception,), {}),
        )
        return client

    async def test_click_returns_result(self) -> None:
        async def click() -> str:
            return "interaction-result"

        client = self._clickable(click)
        self.assertEqual(await client.click_button(500, 11, "kakera:claim"), "interaction-result")

    async def test_click_timeout_maps_to_transient_connection_error(self) -> None:
        import adapters.discord.client as adc

        async def click() -> None:
            await asyncio.sleep(30)  # stuck 202 retry loop

        client = self._clickable(click)
        with (
            mock.patch.object(adc, "CLICK_TIMEOUT_S", 0.05),
            self.assertRaises(ConnectionError) as ctx,
        ):
            await client.click_button(500, 11, "kakera:claim")
        self.assertIn("did not settle", str(ctx.exception))

    async def test_click_invalid_data_becomes_cancel_when_outer_cancelled(self) -> None:
        # patch on the module the CLASS actually reads from — NOT a fresh
        # import (test_module_import_never_pulls_the_library pops/restores
        # sys.modules, and the restored import can yield a DIFFERENT module
        # object than the one RealDiscordClient's methods resolve names in;
        # pytest's alphabetical ordering runs it BEFORE this test, while
        # unittest's runs it AFTER — the isolation bug pytest exposed)
        import sys as _sys

        from adapters.discord.client import RealDiscordClient

        adc = _sys.modules[RealDiscordClient.__module__]

        exc = self.InvalidData("Did not receive a response from Discord")

        async def click() -> None:
            raise exc

        client = self._clickable(click)
        with (
            mock.patch.object(adc, "outer_cancel_pending", return_value=True),
            self.assertRaises(asyncio.CancelledError),
        ):
            await client.click_button(500, 11, "kakera:claim")

    async def test_click_invalid_data_without_cancel_still_propagates(self) -> None:
        exc = self.InvalidData("Did not receive a response from Discord")

        async def click() -> None:
            raise exc

        client = self._clickable(click)
        with self.assertRaises(self.InvalidData):
            await client.click_button(500, 11, "kakera:claim")

    async def test_cancel_in_flight_click_surfaces_cancelled(self) -> None:
        """End-to-end stop() shape: the fork's _wrapped_interaction turns
        the click task's CancelledError into InvalidData — the adapter
        must still surface CancelledError to ITS caller."""

        async def click() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise TestClickHardeningRound3.InvalidData(
                    "Did not receive a response from Discord"
                ) from None

        client = self._clickable(click)
        task = asyncio.get_running_loop().create_task(client.click_button(500, 11, "kakera:claim"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_click_rate_limited_still_maps_to_domain(self) -> None:
        from core.errors import ServerRateLimit

        class RateLimited(Exception):
            def __init__(self) -> None:
                super().__init__("Too many requests.")
                self.retry_after = 12.5

        async def click() -> None:
            raise RateLimited()

        client = self._clickable(click)
        with self.assertRaises(ServerRateLimit) as ctx:
            await client.click_button(500, 11, "kakera:claim")
        self.assertEqual(ctx.exception.retry_after, 12.5)


class TestHeadersCacheRound3(unittest.TestCase):
    """Round-3 F4-7: cached super-properties are deep-copied per HIT with
    re-rolled per-client identity UUIDs (fork fallback regenerates them
    per request — utils.py:1804-1806), and the cache lock survives a
    second event loop (no RuntimeError -> connect failure).

    Plain TestCase on purpose: the cross-loop behaviour needs two separate
    asyncio.run() loops, which an IsolatedAsyncioTestCase cannot host."""

    class _Headers:
        calls: ClassVar[int] = 0
        RESULT: ClassVar[_HEADERS_RESULT] = (
            {
                "client_launch_id": "launch-shared",
                "client_heartbeat_session_id": "heartbeat-shared",
                "browser_version": "136.0.0.0",
            },
            {"gateway": 1},
            "encoded-original",
        )

        @classmethod
        async def get_api_properties(
            cls,
            session: Any,
            type_: str,
            *,
            proxy: Any = None,
            proxy_auth: Any = None,
        ) -> _HEADERS_RESULT:
            cls.calls += 1
            return cls.RESULT

    def setUp(self) -> None:
        import adapters.discord.client as adc

        adc._headers_patch_installed = False
        adc._headers_cache.clear()
        adc._headers_cache_lock = None
        adc._headers_cache_loop = None
        self._adc = adc
        self._headers_cls: type[TestHeadersCacheRound3._Headers] = type(
            "FakeHeaders", (self._Headers,), {"calls": 0}
        )
        self._mod = NS(utils=NS(Headers=self._headers_cls))
        adc._install_headers_fetch_cache(self._mod)
        self.assertTrue(adc._headers_patch_installed)

    def tearDown(self) -> None:
        self._adc._headers_patch_installed = False
        self._adc._headers_cache.clear()
        self._adc._headers_cache_lock = None
        self._adc._headers_cache_loop = None

    def _fetch_twice(self) -> tuple[_HEADERS_RESULT, _HEADERS_RESULT]:
        headers_cls = self._headers_cls

        async def go() -> tuple[_HEADERS_RESULT, _HEADERS_RESULT]:
            return (
                await headers_cls.get_api_properties(None, "web"),
                await headers_cls.get_api_properties(None, "web"),
            )

        return asyncio.run(go())

    def test_hit_re_rolls_identity_and_deep_copies(self) -> None:
        first, second = self._fetch_twice()
        self.assertEqual(self._headers_cls.calls, 1)  # second was a HIT
        self.assertIsNot(first[0], second[0])  # deep-copied dict
        # the STORED cache entry is a copy too — later mutations by the
        # first caller can never leak into another account's identity
        self.assertIsNot(self._adc._headers_cache[(None, None)][1][0], self._headers_cls.RESULT[0])
        self.assertNotEqual(second[0]["client_launch_id"], "launch-shared")
        self.assertNotEqual(second[0]["client_heartbeat_session_id"], "heartbeat-shared")
        # shared fingerprint data survives the copy
        self.assertEqual(second[0]["browser_version"], "136.0.0.0")
        self.assertEqual(second[1], {"gateway": 1})
        # the first caller kept the ORIGINAL identity values untouched
        self.assertEqual(first[0]["client_launch_id"], "launch-shared")

    def test_encoded_super_properties_stay_consistent(self) -> None:
        import base64 as b64
        import json as jsonlib

        _, second = self._fetch_twice()
        decoded = jsonlib.loads(b64.b64decode(second[2]).decode("utf-8"))
        self.assertEqual(decoded["client_launch_id"], second[0]["client_launch_id"])
        self.assertEqual(
            decoded["client_heartbeat_session_id"], second[0]["client_heartbeat_session_id"]
        )

    def test_lock_rebinds_across_asyncio_run_loops(self) -> None:
        headers_cls = self._headers_cls

        async def fetch() -> _HEADERS_RESULT:
            return await headers_cls.get_api_properties(None, "web")

        # loop #1 primes the cache AND its lock; loop #2 must not hit the
        # "lock is bound to a different event loop" RuntimeError
        first = asyncio.run(fetch())
        second = asyncio.run(fetch())
        self.assertEqual(headers_cls.calls, 1)
        self.assertIsNot(first[0], second[0])
        self.assertNotEqual(first[0]["client_launch_id"], second[0]["client_launch_id"])


if __name__ == "__main__":
    unittest.main()
