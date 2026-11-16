"""Regression tests for simulator/model fidelity parity (audit fixes).

Covers:
  (e) DM delivery is pair-scoped: account B's on_message never fires for
      DM pages between A and the_bot (cross-pair DM leak);
  (f) add_reaction/click_button consume the one-shot server limit exactly
      like send_message (an armed ServerRateLimit raises instead of being
      silently spent by an unrelated later message);
  (g) scripted-responder deliveries cross with author.bot True and a
      tz-aware created_at (bot-gate plugins behave the same in sim/live);
  (h) world.react 'remove' decrements the aggregate, recomputes the raw
      count AFTER the mutation, parses custom-emoji names, and sets
      me=True for the reactor's own view;
  (i) adapters.model: the nested referenced DTO inherits the OUTER dm
      flag, the reference channel_id falls back to the referenced
      message's channel, and forward hydration reports snapshot_count
      (multi-forward truncation is detectable).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from typing import Any

from adapters.model import DiscordMessage, convert_message
from adapters.simulator.client import SimClient
from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_BETA_ID,
    SYNTHETIC_BOT_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_OWNER_ID,
    SimWorld,
    WorldMessage,
)
from core.errors import ServerRateLimit


def _world(responder_id: int | None = None) -> SimWorld:
    world = SimWorld(responder_id=responder_id)
    world.add_user(SYNTHETIC_OWNER_ID, "owner")
    world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
    world.add_user(SYNTHETIC_BETA_ID, "beta")
    world.add_user(SYNTHETIC_BOT_ID, "bot")
    world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
    return world


async def _connected(world: SimWorld, user_id: int) -> SimClient:
    client = SimClient(world, user_id)
    await client.connect()
    return client


class _Sub:
    """Minimal reaction-observing subscriber (plain double, live-raw shape)."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.reactions: list[tuple[Any, ...]] = []
        self.on_reaction = lambda *args: self.reactions.append(args)


class DmPairScoping(unittest.IsolatedAsyncioTestCase):
    """(e) only DM-pair members receive DM deliveries."""

    async def test_foreign_account_does_not_receive_dm_pages(self) -> None:
        world = _world(responder_id=SYNTHETIC_BOT_ID)
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        beta = await _connected(world, SYNTHETIC_BETA_ID)
        alpha_seen: list[DiscordMessage] = []
        beta_seen: list[DiscordMessage] = []
        alpha.on_message = alpha_seen.append
        beta.on_message = beta_seen.append

        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_BOT_ID)
        world.deliver(dm, SYNTHETIC_BOT_ID, "page-one", dm=True)

        self.assertEqual([m.content for m in alpha_seen], ["page-one"])
        self.assertEqual(beta_seen, [])  # cross-pair DM leak closed

        # guild traffic still reaches every connected client
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "hi")
        self.assertEqual(len(alpha_seen), 2)
        self.assertEqual(len(beta_seen), 1)

    async def test_foreign_account_does_not_see_dm_reactions_edits_deletes(self) -> None:
        # the message-path leak was fixed earlier; reactions/edits/deletes
        # used to fan out to EVERY subscriber — impossible live (a foreign
        # account's gateway never sees the pair's channel)
        world = _world(responder_id=SYNTHETIC_BOT_ID)
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        beta = await _connected(world, SYNTHETIC_BETA_ID)
        beta_rx: list[tuple[str, Any]] = []
        alpha_rx: list[tuple[str, Any]] = []
        beta.on_reaction = lambda *a, **k: beta_rx.append(("reaction", a))
        beta.on_message_edit = lambda dto: beta_rx.append(("edit", dto))
        beta.on_message_delete = lambda *a: beta_rx.append(("delete", a))
        alpha.on_reaction = lambda *a, **k: alpha_rx.append(("reaction", a))

        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_BOT_ID)
        msg = world.deliver(dm, SYNTHETIC_BOT_ID, "page", dm=True)
        world.react(SYNTHETIC_BOT_ID, msg.id, "❤")  # reactor: pair member
        # (alpha reacting would be self-filtered by the client)
        world.edit_message(msg.id, "edited page")
        world.delete_message(msg.id)

        self.assertEqual(beta_rx, [], "foreign account saw DM traffic")
        self.assertEqual(len(alpha_rx), 1)  # pair member sees the reaction

    async def test_dm_pair_other_member_still_receives(self) -> None:
        world = _world()
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        bot_client = await _connected(world, SYNTHETIC_BOT_ID)
        alpha_seen: list[DiscordMessage] = []
        bot_seen: list[DiscordMessage] = []
        alpha.on_message = alpha_seen.append
        bot_client.on_message = bot_seen.append
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_BOT_ID)
        world.deliver(dm, SYNTHETIC_BOT_ID, "page", dm=True)
        self.assertEqual([m.content for m in alpha_seen], ["page"])
        self.assertEqual(bot_seen, [])  # own message: self-filtered


class CapabilityServerLimits(unittest.IsolatedAsyncioTestCase):
    """(f) add_reaction/click_button honor the one-shot 429 queue."""

    async def test_armed_limit_blocks_next_add_reaction(self) -> None:
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        message = world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "target")
        world.server_limit_next(channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.6)
        with self.assertRaises(ServerRateLimit) as caught:
            await client.add_reaction(SYNTHETIC_GUILD_CHANNEL, message.id, "❤")
        self.assertEqual(caught.exception.retry_after, 0.6)
        self.assertEqual(len(world.blocked_sends), 1)  # consumed + recorded
        # one-shot: the retry goes through and mutates the aggregate
        await client.add_reaction(SYNTHETIC_GUILD_CHANNEL, message.id, "❤")
        self.assertEqual(message.reactions[0].count, 1)

    async def test_armed_limit_blocks_next_click_button(self) -> None:
        from adapters.model import DiscordComponent

        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        button = DiscordComponent(type=2, type_name="button", custom_id="kakera:claim")
        row = DiscordComponent(type=1, type_name="action_row", components=(button,))
        message = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "$mu", components=(row,)
        )
        world.server_limit_next(channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.4)
        with self.assertRaises(ServerRateLimit) as caught:
            await client.click_button(SYNTHETIC_GUILD_CHANNEL, message.id, "kakera:claim")
        self.assertEqual(caught.exception.retry_after, 0.4)
        self.assertEqual(client.button_clicks, [])  # click never registered
        await client.click_button(SYNTHETIC_GUILD_CHANNEL, message.id, "kakera:claim")
        self.assertEqual(client.button_clicks, [(message.id, "kakera:claim")])

    async def test_vanished_message_does_not_consume_the_limit(self) -> None:
        """A 429 is never spent on an action whose target is already gone
        (same ordering contract as send_message)."""
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        world.server_limit_next(channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.5)
        from core.errors import TargetGone

        with self.assertRaises(TargetGone):
            await client.add_reaction(SYNTHETIC_GUILD_CHANNEL, 424242, "❤")
        self.assertEqual(world.blocked_sends, [])  # still armed
        message = world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "later")
        with self.assertRaises(ServerRateLimit):
            await client.add_reaction(SYNTHETIC_GUILD_CHANNEL, message.id, "❤")


class ResponderFidelity(unittest.IsolatedAsyncioTestCase):
    """(g) bot flag + created_at on the sim's rich boundary."""

    async def test_mudae_delivery_is_bot_with_created_at(self) -> None:
        world = _world(responder_id=SYNTHETIC_BOT_ID)
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list[DiscordMessage] = []
        client.on_message = seen.append
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_BOT_ID, "$mu", author_name="the_bot")
        message = seen[0]
        author = message.author
        assert author is not None  # narrowing: sim always sets an author
        self.assertTrue(author.bot)
        created_at = message.created_at
        assert created_at is not None  # narrowing: sim stamps every message
        self.assertIsNotNone(created_at)
        self.assertIsNotNone(created_at.tzinfo)  # tz-aware like live
        offset = created_at.utcoffset()
        assert offset is not None  # tz-aware datetimes always carry one
        self.assertEqual(offset.total_seconds(), 0)
        self.assertEqual(author.display_name, "the_bot")
        # timestamps are faithful to the world clock (UTC epoch; tiny
        # float round-trip slack from datetime <-> epoch)
        self.assertAlmostEqual(created_at.timestamp(), world.journal[-1].ts, places=6)

    async def test_user_authors_stay_non_bot(self) -> None:
        world = _world(responder_id=SYNTHETIC_BOT_ID)
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list[DiscordMessage] = []
        client.on_message = seen.append
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "hello", author_name="owner")
        msg = seen[0]
        author = msg.author
        assert author is not None  # narrowing: sim always sets an author
        self.assertFalse(author.bot)
        self.assertIsNotNone(msg.created_at)

    async def test_dm_pages_from_responder_carry_bot_flag(self) -> None:
        world = SimWorld(
            responder_id=SYNTHETIC_BOT_ID,
            responses={"$dump": ["page-one"]},
            reply_delay=0.01,
        )
        world.add_user(SYNTHETIC_BOT_ID, "bot")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list[DiscordMessage] = []
        client.on_message = seen.append
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$dump")
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_BOT_ID)
        await world.wait_for_quiet(dm, quiet=0.15, limit=2.0)
        pages = [m for m in seen if m.dm]
        self.assertEqual(len(pages), 1)
        page_author = pages[0].author
        assert page_author is not None  # narrowing: sim always sets an author
        self.assertTrue(page_author.bot)
        self.assertIsNotNone(pages[0].created_at)


class ReactRemoveAndMe(unittest.IsolatedAsyncioTestCase):
    """(h) remove branch, post-mutation counts, per-subscriber me."""

    def _world_with_message(self) -> tuple[SimWorld, WorldMessage]:
        world = _world()
        message = world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "target")
        return world, message

    async def test_remove_decrements_and_reports_post_mutation_count(self) -> None:
        world, message = self._world_with_message()
        reactor = _Sub(SYNTHETIC_ALPHA_ID)
        other = _Sub(SYNTHETIC_BETA_ID)
        world.subscribers.extend((reactor, other))

        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="add")
        world.react(SYNTHETIC_BETA_ID, message.id, "❤", kind="add")
        self.assertEqual(message.reactions[0].count, 2)

        reactor.reactions.clear()
        other.reactions.clear()
        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="remove")
        self.assertEqual(message.reactions[0].count, 1)  # decremented
        for sub in (reactor, other):
            self.assertEqual(len(sub.reactions), 1)
            kind, _mid, _cid, _gid, uid, _emoji, raw = sub.reactions[0]
            self.assertEqual(kind, "remove")
            self.assertEqual(uid, SYNTHETIC_ALPHA_ID)
            reaction, _user = raw
            self.assertEqual(reaction.count, 1)  # AFTER the mutation

    async def test_remove_last_reaction_deletes_aggregate_entry(self) -> None:
        world, message = self._world_with_message()
        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="add")
        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="remove")
        self.assertEqual(message.reactions, ())
        # the raw payload reports the post-mutation (zero) count
        sub = _Sub(SYNTHETIC_BETA_ID)
        world.subscribers.append(sub)
        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="remove")
        reaction, _user = sub.reactions[0][6]
        self.assertEqual(reaction.count, 0)

    async def test_me_true_for_reactor_false_for_others(self) -> None:
        world, message = self._world_with_message()
        reactor = _Sub(SYNTHETIC_ALPHA_ID)
        other = _Sub(SYNTHETIC_BETA_ID)
        world.subscribers.extend((reactor, other))
        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="add")
        # the reactor's own raw payload says me=True; everyone else False
        reaction, _user = reactor.reactions[0][6]
        self.assertTrue(reaction.me)
        reaction_other, _user = other.reactions[0][6]
        self.assertFalse(reaction_other.me)

    async def test_sim_client_filters_own_reactions_like_live(self) -> None:
        """SimClient.reaction_event applies the live adapter's self-filter:
        the session never sees its own reaction events."""
        world, message = self._world_with_message()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list[tuple[Any, ...]] = []
        client.on_reaction = lambda *args: seen.append(args)
        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="add")
        self.assertEqual(seen, [])  # own reaction: dropped
        world.react(SYNTHETIC_BETA_ID, message.id, "❤", kind="add")
        self.assertEqual(len(seen), 1)
        reaction, _user = seen[0][6]
        self.assertFalse(reaction.me)

    async def test_custom_emoji_aggregate_parses_name_and_id(self) -> None:
        world, message = self._world_with_message()
        world.react(SYNTHETIC_ALPHA_ID, message.id, "<:kakera:77>")
        stored = message.reactions[0]
        self.assertEqual(stored.emoji, "<:kakera:77>")
        self.assertEqual(stored.name, "kakera")  # bare name, not the repr
        self.assertEqual(stored.id, 77)
        world.react(SYNTHETIC_BETA_ID, message.id, "<:kakera:77>")
        self.assertEqual(message.reactions[0].count, 2)  # same emoji merges


class ModelReferenceFidelity(unittest.TestCase):
    """(i) adapters.model reference/reference hydration fixes."""

    def _lib_dm_reply(self) -> NS:
        cached = NS(
            id=999,
            content="original",
            author=NS(id=999999, name="the_bot"),
            channel=NS(
                id=9001, name=None, type=NS(name="private"), recipient=NS(id=999999), guild=None
            ),
            guild=None,
            embeds=[],
            components=[],
            attachments=[],
            reactions=[],
            reference=None,
            created_at=None,
        )
        outer = NS(
            id=1000,
            content="reply",
            author=NS(id=2001, name="alpha"),
            channel=NS(
                id=9001, name=None, type=NS(name="private"), recipient=NS(id=999999), guild=None
            ),
            guild=None,
            embeds=[],
            components=[],
            attachments=[],
            reactions=[],
            reference=NS(message_id=999, channel_id=9001, guild_id=None, resolved=cached),
            referenced_message=None,
            created_at=None,
        )
        return outer

    def test_referenced_inherits_outer_dm_flag(self) -> None:
        dto = convert_message(self._lib_dm_reply(), dm=True)
        self.assertTrue(dto.dm)
        reference = dto.reference
        assert reference is not None  # narrowing: the fixture carries one
        self.assertIsNotNone(reference.referenced)
        # the nested DTO shares the outer channel's DM-ness (was hardcoded
        # False, mislabeling references inside DM channels)
        assert reference.referenced is not None  # narrowing for the check
        self.assertTrue(reference.referenced.dm)

    def test_reference_channel_id_falls_back_to_referenced(self) -> None:
        dto = convert_message(self._lib_dm_reply(), dm=True)
        reference = dto.reference
        assert reference is not None  # narrowing: the fixture carries one
        self.assertEqual(reference.channel_id, 9001)
        # asymmetric-fallback fix: an id-only reference (channel_id None on
        # the reference object itself) now inherits the referenced message's
        # channel, exactly like message_id inherits referenced.id
        lib = self._lib_dm_reply()
        lib.reference.channel_id = None
        lib.reference.guild_id = None
        dto2 = convert_message(lib, dm=True)
        reference2 = dto2.reference
        assert reference2 is not None  # narrowing: same fixture shape
        self.assertEqual(reference2.message_id, 999)
        self.assertEqual(reference2.channel_id, 9001)

    def test_forward_snapshot_count_reports_truncation(self) -> None:
        snapshots = [
            NS(
                id=999,
                content="forwarded one",
                embeds=[],
                attachments=[],
                components=[],
                created_at=None,
            ),
            NS(
                id=998,
                content="forwarded two",
                embeds=[],
                attachments=[],
                components=[],
                created_at=None,
            ),
        ]
        lib = NS(
            id=1000,
            content="",
            author=NS(id=999999, name="the_bot"),
            channel=NS(id=500, name="grind", type=NS(name="text"), guild=NS(id=42)),
            guild=NS(id=42),
            embeds=[],
            components=[],
            attachments=[],
            reactions=[],
            reference=NS(
                message_id=999, channel_id=500, guild_id=42, type=NS(name="forward"), resolved=None
            ),
            referenced_message=None,
            created_at=None,
            message_snapshots=snapshots,
        )
        dto = convert_message(lib, dm=False)
        reference = dto.reference
        assert reference is not None  # narrowing: forward fixture has one
        self.assertEqual(reference.snapshot_count, 2)  # both carried
        # only snapshots[0] is hydrated — the documented truncation
        assert reference.referenced is not None  # narrowing for the check
        self.assertEqual(reference.referenced.content, "forwarded one")

    def test_snapshot_count_defaults_to_one(self) -> None:
        lib = self._lib_dm_reply()
        dto = convert_message(lib, dm=True)
        reference = dto.reference
        assert reference is not None  # narrowing: the fixture carries one
        self.assertEqual(reference.snapshot_count, 1)
        no_reference = NS(
            id=1,
            content="x",
            author=None,
            channel=None,
            guild=None,
            embeds=[],
            components=[],
            attachments=[],
            reactions=[],
            reference=None,
            referenced_message=None,
            created_at=None,
        )
        self.assertIsNone(convert_message(no_reference, dm=False).reference)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
