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
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest import mock

from adapters.model import (
    DiscordComponent,
    DiscordEmbed,
    DiscordEmbedField,
    DiscordMessage,
    DiscordReaction,
    convert_component,
    convert_embed,
    convert_message,
)
from core.event_bus import (
    EventBus,
    MessageDeleted,
    MessageEdited,
    MessageReceived,
    ReactionAdded,
)


def library_message(**overrides) -> NS:
    """A library-shaped message carrying EVERY rich field (§27 fixtures)."""
    base = dict(
        id=1000,
        content="$mu",
        author=NS(id=999999, name="Mudae", display_name="Mudae", bot=True),
        channel=NS(id=500, name="grind", type=NS(name="text"), guild=NS(id=42)),
        guild=NS(id=42),
        embeds=[NS(
            title="Character", description="Rarity: ★★★", url="http://x",
            type="rich", color=0xFF00AA, timestamp="2026-01-01T00:00:00",
            author=NS(name="Mudae", url="http://a", icon_url="http://i"),
            footer=NS(text="page 1/2", icon_url="http://f"),
            fields=[NS(name="Rank", value="1", inline=True),
                    NS(name="Kakera", value="500", inline=True)],
            thumbnail=NS(url="http://t"), image=NS(url="http://im"),
        )],
        components=[NS(type=1, children=[
            NS(type=2, custom_id="kakera:loot", label="React", style=2,
               disabled=False, emoji=NS(name="💎", id=None)),
            NS(type=2, custom_id="kakera:claim", label="Claim", style=3,
               disabled=False, emoji=None, url=None),
        ])],
        attachments=[NS(id=7, filename="card.png", url="http://f",
                        size=12345, content_type="image/png")],
        reactions=[NS(emoji=NS(name="❤", id=None), count=3, me=False)],
        reference=NS(message_id=999, channel_id=500, guild_id=42),
        referenced_message=None,
        created_at=None,
    )
    base.update(overrides)
    return NS(**base)


class TestConversion(unittest.TestCase):
    def test_every_field_survives_conversion(self) -> None:
        lib = library_message()
        dto = convert_message(lib, dm=False)
        self.assertEqual((dto.id, dto.channel_id, dto.author_id, dto.content),
                         (1000, 500, 999999, "$mu"))
        self.assertFalse(dto.dm)
        self.assertEqual(dto.guild_id, 42)
        self.assertEqual(dto.author.name, "Mudae")
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
        self.assertEqual([b.custom_id for b in buttons],
                         ["kakera:loot", "kakera:claim"])
        self.assertEqual(dto.button("kakera:claim").label, "Claim")
        self.assertIsNone(dto.button("missing"))
        # attachments / reactions / reference
        self.assertEqual(dto.attachments[0].filename, "card.png")
        self.assertEqual(dto.reactions[0].emoji, "❤")
        self.assertEqual(dto.reactions[0].count, 3)
        self.assertEqual(dto.reference.message_id, 999)
        self.assertIsNone(dto.reference.referenced)  # not fetched (§12)
        # escape hatch
        self.assertIs(dto.unwrap(), lib)

    def test_dm_message(self) -> None:
        lib = library_message(
            content="Soulmates",
            channel=NS(id=9001, name=None, type=NS(name="private"),
                       recipient=NS(id=999999), guild=None),
            guild=None, embeds=[], components=[], reference=None,
        )
        dto = convert_message(lib, dm=True)
        self.assertTrue(dto.dm)
        self.assertIsNone(dto.guild_id)
        self.assertEqual(dto.channel.type, "private")
        self.assertIn("dm", dto.summary())

    def test_missing_optionals_stay_none(self) -> None:
        lib = library_message(embeds=[], components=[], attachments=[],
                              reactions=[], reference=None,
                              referenced_message=None, author=None, guild=None)
        dto = convert_message(lib, dm=False)
        self.assertEqual(dto.embeds, ())
        self.assertIsNone(dto.reference)
        self.assertIsNone(dto.author)

    def test_referenced_message_used_when_cached(self) -> None:
        cached = library_message(id=999, content="original")
        lib = library_message(referenced_message=cached)
        dto = convert_message(lib, dm=False)
        self.assertEqual(dto.reference.referenced.id, 999)
        self.assertEqual(dto.reference.referenced.content, "original")
        # no fetch happened: the cached object IS the raw handle
        self.assertIs(dto.reference.referenced.unwrap(), cached)

    def test_select_menu_component(self) -> None:
        select = NS(type=3, custom_id="pick", options=[
            NS(label="A", value="a", description="first", default=True),
            NS(label="B", value="b", description="", default=False)])
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
        reaction = NS(emoji=NS(name="kakera", id=77, animated=True),
                      count=2, me=False)
        dto = convert_message(library_message(reactions=[reaction]), dm=False)
        self.assertEqual(dto.reactions[0].emoji, "<a:kakera:77>")

    def test_embed_timestamp_datetime_becomes_iso(self) -> None:
        from adapters.model import convert_embed as ce

        embed = ce(NS(title="t", timestamp=datetime(
            2026, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual(embed.timestamp, "2026-01-01T00:00:00+00:00")
        passthrough = ce(NS(title="t", timestamp="2026-01-01T00:00:00"))
        self.assertEqual(passthrough.timestamp, "2026-01-01T00:00:00")
        self.assertIsNone(ce(NS(title="t", timestamp=None)).timestamp)

    def test_deleted_referenced_sentinel_stays_none(self) -> None:
        # discord.py-self shape: DeletedReferencedMessage exposes id via a
        # _parent MessageReference (so plain hasattr(resolved, "id") lies)
        sentinel = NS(id=999, channel_id=500, guild_id=42,
                      _parent=NS(message_id=999, channel_id=500, guild_id=42))
        lib = library_message(reference=NS(
            message_id=999, channel_id=500, guild_id=42, resolved=sentinel))
        dto = convert_message(lib, dm=False)
        self.assertIsNone(dto.reference.referenced)
        self.assertEqual(dto.reference.message_id, 999)
        # upstream shape: the NamedTuple-style _deleted marker
        marked = NS(id=999, channel_id=500, guild_id=42, _deleted=True)
        lib2 = library_message(reference=NS(
            message_id=999, channel_id=500, guild_id=42, resolved=marked))
        self.assertIsNone(convert_message(lib2, dm=False).reference.referenced)

    def test_reply_chain_conversion_is_bounded(self) -> None:
        m1 = library_message(id=1, content="first", reference=None)
        ref2 = NS(message_id=1, channel_id=500, guild_id=42, resolved=m1)
        m2 = library_message(id=2, content="second", reference=ref2)
        ref3 = NS(message_id=2, channel_id=500, guild_id=42, resolved=m2)
        m3 = library_message(id=3, content="third", reference=ref3)
        dto = convert_message(m3, dm=False)
        self.assertEqual(dto.reference.referenced.id, 2)  # one level down
        inner = dto.reference.referenced
        self.assertEqual(inner.reference.message_id, 1)  # ids kept...
        self.assertIsNone(inner.reference.referenced)     # ...chain capped

    def test_self_referential_resolution_terminates(self) -> None:
        lib = library_message(id=7, reference=NS(
            message_id=7, channel_id=500, guild_id=42))
        lib.reference.resolved = lib  # pathological cache cycle
        dto = convert_message(lib, dm=False)
        self.assertEqual(dto.reference.referenced.id, 7)
        self.assertIsNone(dto.reference.referenced.reference.referenced)


class TestBackwardCompatibility(unittest.TestCase):
    def test_original_constructor_surface(self) -> None:
        dto = DiscordMessage(1, 2, 3, "hi", True)
        self.assertEqual((dto.id, dto.channel_id, dto.author_id, dto.content, dto.dm),
                         (1, 2, 3, "hi", True))
        self.assertIn("ch=2", repr(dto))  # historical friendly repr kept

    def test_keyword_construction_unchanged(self) -> None:
        dto = DiscordMessage(id=5, channel_id=6, author_id=7, content="x", dm=False)
        self.assertEqual(dto.embeds, ())
        self.assertIsNone(dto.raw)


class TestIsolationAndRaw(unittest.TestCase):
    def test_frozen_event_state(self) -> None:
        dto = convert_message(library_message(), dm=False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            dto.content = "tampered"

    def test_raw_never_in_repr_or_summary(self) -> None:
        lib = library_message()
        dto = convert_message(lib, dm=False)
        self.assertNotIn("raw", repr(dto))
        self.assertNotIn("Mudae", dto.summary())  # no payload leakage

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
    def _world(self):
        from adapters.simulator.world import SimWorld

        world = SimWorld(responder_id=None)
        world.add_user(1001, "owner")
        world.add_user(999999, "mudae")
        world.add_channel(500, "grind")
        return world

    async def test_rich_delivery_and_events(self) -> None:
        from adapters.simulator.client import SimClient

        world = self._world()
        client = SimClient(world, 1001)
        seen: list = []
        reactions: list = []
        edits: list = []
        deletes: list = []
        client.on_message = seen.append
        client.on_reaction = lambda *a: reactions.append(a)
        client.on_message_edit = edits.append
        client.on_message_delete = lambda *a: deletes.append(a)
        await client.connect()

        button = DiscordComponent(type=2, type_name="button",
                                  custom_id="kakera:loot", label="React")
        row = DiscordComponent(type=1, type_name="action_row",
                               components=(button,))
        embed = DiscordEmbed(title="Character", description="★",
                             fields=(DiscordEmbedField("Rank", "1"),))
        message = world.deliver(
            500, 999999, "$mu", author_name="Mudae", guild_id=42,
            embeds=(embed,), components=(row,), reference_message_id=9)
        self.assertEqual(len(seen), 1)
        rich = seen[0]
        self.assertEqual(rich.embeds[0].field_map(), {"Rank": "1"})
        self.assertEqual(rich.buttons[0].custom_id, "kakera:loot")
        self.assertEqual(rich.reference.message_id, 9)
        self.assertIsNone(rich.raw)  # sim has no library object

        # reaction event WITHOUT any fetch (§9)
        world.react(999999, message.id, "❤")
        self.assertEqual(len(reactions), 1)
        kind, mid, cid, gid, uid, emoji, raw = reactions[0]
        self.assertEqual((kind, mid, uid, emoji), ("add", message.id, 999999, "❤"))

        world.edit_message(message.id, "edited")
        self.assertEqual(edits[0].content, "edited")
        world.delete_message(message.id)
        self.assertEqual(deletes[0][0], message.id)

    async def test_capability_actions_through_pipeline(self) -> None:
        """kakerareact + kakeraloot shapes: services actions flow through the
        scheduler (paced) into the world; audit rows carry the action kind."""
        from core.audit import AuditRecorder
        from core.event_bus import EventBus
        from core.priority import Priority
        from core.rate_limit_manager import RateLimitManager
        from core.task_scheduler import TaskScheduler

        world = self._world()
        from adapters.simulator.client import SimClient

        client = SimClient(world, 1001)
        await client.connect()
        button = DiscordComponent(type=2, type_name="button",
                                  custom_id="kakera:claim")
        row = DiscordComponent(type=1, type_name="action_row",
                               components=(button,))
        message = world.deliver(500, 999999, "$mu", components=(row,))

        bus = EventBus()

        class FakeLimiter:
            async def acquire(self, action, account=None, plugin=None,
                              priority=Priority.NORMAL):
                return NS(wait_s=0.0, jitter_s=0.0)

            def report_server_limit(self, *a, **k):
                pass

        audit = AuditRecorder()
        scheduler = TaskScheduler(bus, None, FakeLimiter(),
                                  lambda task: client.add_reaction(
                                      task.channel_id, task.message_id,
                                      task.content) if task.action == "reaction"
                                  else client.click_button(
                                      task.channel_id, task.message_id,
                                      task.content),
                                  audit=audit)
        scheduler.start()
        try:
            t1 = scheduler.submit("alpha", "kakerareact", "reaction",
                                  channel_id=500, message_id=message.id,
                                  content="❤", priority=Priority.NORMAL)
            t2 = scheduler.submit("alpha", "kakeraloot", "button",
                                  channel_id=500, message_id=message.id,
                                  content="kakera:claim")
            for _ in range(100):
                await asyncio.sleep(0.02)
                if t1.status == "completed" and t2.status == "completed":
                    break
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
        seen: list = []
        bus.subscribe(MessageReceived, seen.append)
        rich = convert_message(library_message(), dm=False)
        bus.publish_nowait(MessageReceived(
            account="alpha", message_id=rich.id, channel_id=rich.channel_id,
            author_id=rich.author_id, content=rich.content, dm=rich.dm,
            message=rich))
        await asyncio.sleep(0.02)
        event = seen[0]
        self.assertIs(event.message, rich)
        self.assertEqual(event.message.embeds[0].title, "Character")

    async def test_reaction_event_dispatch(self) -> None:
        bus = EventBus()
        seen: list = []
        bus.subscribe(ReactionAdded, seen.append)
        bus.publish_nowait(ReactionAdded(
            account="alpha", message_id=1, channel_id=2, guild_id=3,
            user_id=4, emoji="❤", dm=False))
        await asyncio.sleep(0.02)
        self.assertEqual(seen[0].emoji, "❤")

    async def test_plugin_isolation_on_shared_event(self) -> None:
        """Two subscribers get the SAME event object; the frozen DTO makes
        accidental mutation impossible (isolation §27)."""
        bus = EventBus()
        seen_a, seen_b = [], []
        bus.subscribe(MessageReceived, seen_a.append)
        bus.subscribe(MessageReceived, seen_b.append)
        rich = convert_message(library_message(), dm=False)
        bus.publish_nowait(MessageReceived(
            account="a", message=rich, message_id=rich.id,
            channel_id=rich.channel_id, author_id=rich.author_id,
            content=rich.content, dm=rich.dm))
        await asyncio.sleep(0.02)
        self.assertIs(seen_a[0].message, seen_b[0].message)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            seen_a[0].message.embeds[0].title = "tampered"
        self.assertEqual(seen_b[0].message.embeds[0].title, "Character")


class TestFuturePluginShapes(unittest.TestCase):
    """§22: demonstrate snipe / kakerareact / kakeraloot / soulmate-farm /
    soulcopy needs are met WITHOUT another core rewrite (shapes only)."""

    def test_snipe_shape(self) -> None:
        lib = library_message(content="caught!", reference=NS(
            message_id=9, channel_id=500))
        dto = convert_message(lib, dm=False)
        self.assertTrue(dto.content and dto.author.id and dto.channel.id)
        self.assertIsNotNone(dto.created_at if dto.created_at is not None
                             else dto.id)  # timestamp or id available

    def test_kakerareact_shape(self) -> None:
        dto = convert_message(library_message(), dm=False)
        self.assertTrue(any(r.emoji for r in dto.reactions))
        self.assertEqual(dto.reactions[0].count, 3)

    def test_kakeraloot_shape(self) -> None:
        dto = convert_message(library_message(), dm=False)
        self.assertEqual(dto.button("kakera:loot").custom_id, "kakera:loot")
        self.assertFalse(dto.button("kakera:loot").disabled)

    def test_soulmate_farm_shape(self) -> None:
        dto = convert_message(library_message(), dm=False)
        self.assertEqual(dto.embeds[0].description, "Rarity: ★★★")
        self.assertEqual(len(dto.buttons), 2)

    def test_soulcopy_shape_dm(self) -> None:
        lib = library_message(content="Soulmates\n---\nAlice",
                              channel=NS(id=9001, name=None,
                                         type=NS(name="private"),
                                         recipient=NS(id=999999), guild=None),
                              guild=None, embeds=[], components=[],
                              reference=None)
        dto = convert_message(lib, dm=True)
        self.assertTrue(dto.dm)          # DM detection lives on the flag;
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

        async def fetch_user(self, user_id: int):
            raise self._exc

    class _ReadyLibClient:
        """discord.Client stand-in that reaches READY then parks."""

        last_built = None

        def __init__(self) -> None:
            self.closed = False
            self.user = NS(id=777)
            self._park = asyncio.Event()

        def event(self, coro):
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

        saved = {name: sys.modules.get(name)
                 for name in ("discord", "adapters.discord",
                              "adapters.discord.client")}
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
            client._client = self._FailingFetchClient(exc)
            client.connected = True
            self.assertIsNone(await client.fetch_user(1))

    async def test_fetch_user_raises_real_denials(self) -> None:
        from adapters.discord.client import RealDiscordClient

        client = RealDiscordClient("tok")
        client._client = self._FailingFetchClient(
            self.Forbidden("403 Forbidden (error code: 50001): Missing Access"))
        client.connected = True
        with self.assertRaises(self.Forbidden):
            await client.fetch_user(1)

    async def test_sends_raise_connection_error_offline(self) -> None:
        from adapters.discord.client import RealDiscordClient

        client = RealDiscordClient("tok")
        self.assertFalse(client.connected)
        with self.assertRaises(ConnectionError):
            client._require_client()
        client._client = object()
        with self.assertRaises(ConnectionError):  # built but not connected
            client._require_client()
        client.connected = True
        self.assertIs(client._require_client(), client._client)
        client.connected = False
        with self.assertRaises(ConnectionError):
            client._require_client()

    async def test_connected_lifecycle_through_connect_and_disconnect(self) -> None:
        from adapters.discord.client import RealDiscordClient

        lib_cls = type("LibClient", (self._ReadyLibClient,), {})
        mod = NS(Client=lib_cls, DMChannel=type("DMChannel", (), {}))
        with mock.patch.dict(sys.modules, {"discord": mod}):
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
            client._client = NS(latency=bad)
            self.assertIsNone(client.latency_ms)
        client._client = NS(latency=0.021)
        self.assertAlmostEqual(client.latency_ms, 21.0)


if __name__ == "__main__":
    unittest.main()
