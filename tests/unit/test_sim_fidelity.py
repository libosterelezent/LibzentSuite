"""Regression tests for simulator/model fidelity parity (audit fixes).

Covers:
  (e) DM delivery is pair-scoped: account B's on_message never fires for
      DM pages between A and Mudae (cross-pair DM leak);
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

from adapters.model import convert_message
from adapters.simulator.client import SimClient
from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_BETA_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_MUDAE_ID,
    SYNTHETIC_OWNER_ID,
    SimWorld,
)
from core.errors import ServerRateLimit


def _world(responder_id=None) -> SimWorld:
    world = SimWorld(responder_id=responder_id)
    world.add_user(SYNTHETIC_OWNER_ID, "owner")
    world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
    world.add_user(SYNTHETIC_BETA_ID, "beta")
    world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
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
        self.reactions: list = []
        self.on_reaction = lambda *args: self.reactions.append(args)


class DmPairScoping(unittest.IsolatedAsyncioTestCase):
    """(e) only DM-pair members receive DM deliveries."""

    async def test_foreign_account_does_not_receive_dm_pages(self):
        world = _world(responder_id=SYNTHETIC_MUDAE_ID)
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        beta = await _connected(world, SYNTHETIC_BETA_ID)
        alpha_seen: list = []
        beta_seen: list = []
        alpha.on_message = alpha_seen.append
        beta.on_message = beta_seen.append

        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        world.deliver(dm, SYNTHETIC_MUDAE_ID, "page-one", dm=True)

        self.assertEqual([m.content for m in alpha_seen], ["page-one"])
        self.assertEqual(beta_seen, [])  # cross-pair DM leak closed

        # guild traffic still reaches every connected client
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "hi")
        self.assertEqual(len(alpha_seen), 2)
        self.assertEqual(len(beta_seen), 1)

    async def test_foreign_account_does_not_see_dm_reactions_edits_deletes(self):
        # the message-path leak was fixed earlier; reactions/edits/deletes
        # used to fan out to EVERY subscriber — impossible live (a foreign
        # account's gateway never sees the pair's channel)
        world = _world(responder_id=SYNTHETIC_MUDAE_ID)
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        beta = await _connected(world, SYNTHETIC_BETA_ID)
        beta_rx: list = []
        alpha_rx: list = []
        beta.on_reaction = lambda *a, **k: beta_rx.append(("reaction", a))
        beta.on_message_edit = lambda dto: beta_rx.append(("edit", dto))
        beta.on_message_delete = lambda *a: beta_rx.append(("delete", a))
        alpha.on_reaction = lambda *a, **k: alpha_rx.append(("reaction", a))

        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        msg = world.deliver(dm, SYNTHETIC_MUDAE_ID, "page", dm=True)
        world.react(SYNTHETIC_MUDAE_ID, msg.id, "❤")  # reactor: pair member
        # (alpha reacting would be self-filtered by the client)
        world.edit_message(msg.id, "edited page")
        world.delete_message(msg.id)

        self.assertEqual(beta_rx, [], "foreign account saw DM traffic")
        self.assertEqual(len(alpha_rx), 1)  # pair member sees the reaction

    async def test_dm_pair_other_member_still_receives(self):
        world = _world()
        alpha = await _connected(world, SYNTHETIC_ALPHA_ID)
        mudae = await _connected(world, SYNTHETIC_MUDAE_ID)
        alpha_seen: list = []
        mudae_seen: list = []
        alpha.on_message = alpha_seen.append
        mudae.on_message = mudae_seen.append
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        world.deliver(dm, SYNTHETIC_MUDAE_ID, "page", dm=True)
        self.assertEqual([m.content for m in alpha_seen], ["page"])
        self.assertEqual(mudae_seen, [])  # own message: self-filtered


class CapabilityServerLimits(unittest.IsolatedAsyncioTestCase):
    """(f) add_reaction/click_button honor the one-shot 429 queue."""

    async def test_armed_limit_blocks_next_add_reaction(self):
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        message = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "target")
        world.server_limit_next(
            channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.6)
        with self.assertRaises(ServerRateLimit) as caught:
            await client.add_reaction(
                SYNTHETIC_GUILD_CHANNEL, message.id, "❤")
        self.assertEqual(caught.exception.retry_after, 0.6)
        self.assertEqual(len(world.blocked_sends), 1)  # consumed + recorded
        # one-shot: the retry goes through and mutates the aggregate
        await client.add_reaction(SYNTHETIC_GUILD_CHANNEL, message.id, "❤")
        self.assertEqual(message.reactions[0].count, 1)

    async def test_armed_limit_blocks_next_click_button(self):
        from adapters.model import DiscordComponent

        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        button = DiscordComponent(
            type=2, type_name="button", custom_id="kakera:claim")
        row = DiscordComponent(
            type=1, type_name="action_row", components=(button,))
        message = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "$mu",
            components=(row,))
        world.server_limit_next(
            channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.4)
        with self.assertRaises(ServerRateLimit) as caught:
            await client.click_button(
                SYNTHETIC_GUILD_CHANNEL, message.id, "kakera:claim")
        self.assertEqual(caught.exception.retry_after, 0.4)
        self.assertEqual(client.button_clicks, [])  # click never registered
        await client.click_button(
            SYNTHETIC_GUILD_CHANNEL, message.id, "kakera:claim")
        self.assertEqual(client.button_clicks, [(message.id, "kakera:claim")])

    async def test_vanished_message_does_not_consume_the_limit(self):
        """A 429 is never spent on an action whose target is already gone
        (same ordering contract as send_message)."""
        world = _world()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        world.server_limit_next(
            channel_id=SYNTHETIC_GUILD_CHANNEL, retry_after=0.5)
        from core.errors import TargetGone

        with self.assertRaises(TargetGone):
            await client.add_reaction(SYNTHETIC_GUILD_CHANNEL, 424242, "❤")
        self.assertEqual(world.blocked_sends, [])  # still armed
        message = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "later")
        with self.assertRaises(ServerRateLimit):
            await client.add_reaction(
                SYNTHETIC_GUILD_CHANNEL, message.id, "❤")


class ResponderFidelity(unittest.IsolatedAsyncioTestCase):
    """(g) bot flag + created_at on the sim's rich boundary."""

    async def test_mudae_delivery_is_bot_with_created_at(self):
        world = _world(responder_id=SYNTHETIC_MUDAE_ID)
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list = []
        client.on_message = seen.append
        world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_MUDAE_ID, "$mu",
            author_name="Mudae")
        message = seen[0]
        self.assertTrue(message.author.bot)
        self.assertIsNotNone(message.created_at)
        self.assertIsNotNone(message.created_at.tzinfo)  # tz-aware like live
        self.assertEqual(message.created_at.utcoffset().total_seconds(), 0)
        self.assertEqual(message.author.display_name, "Mudae")
        # timestamps are faithful to the world clock (UTC epoch; tiny
        # float round-trip slack from datetime <-> epoch)
        self.assertAlmostEqual(
            message.created_at.timestamp(),
            world.journal[-1].ts, places=6)

    async def test_user_authors_stay_non_bot(self):
        world = _world(responder_id=SYNTHETIC_MUDAE_ID)
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list = []
        client.on_message = seen.append
        world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "hello",
            author_name="owner")
        self.assertFalse(seen[0].author.bot)
        self.assertIsNotNone(seen[0].created_at)

    async def test_dm_pages_from_responder_carry_bot_flag(self):
        world = SimWorld(
            responder_id=SYNTHETIC_MUDAE_ID,
            responses={"$slud-s": ["page-one"]},
            reply_delay=0.01,
        )
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "guild-channel")
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list = []
        client.on_message = seen.append
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "$slud-s")
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        await world.wait_for_quiet(dm, quiet=0.15, limit=2.0)
        pages = [m for m in seen if m.dm]
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0].author.bot)
        self.assertIsNotNone(pages[0].created_at)


class ReactRemoveAndMe(unittest.IsolatedAsyncioTestCase):
    """(h) remove branch, post-mutation counts, per-subscriber me."""

    def _world_with_message(self):
        world = _world()
        message = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "target")
        return world, message

    async def test_remove_decrements_and_reports_post_mutation_count(self):
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

    async def test_remove_last_reaction_deletes_aggregate_entry(self):
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

    async def test_me_true_for_reactor_false_for_others(self):
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

    async def test_sim_client_filters_own_reactions_like_live(self):
        """SimClient.reaction_event applies the live adapter's self-filter:
        the session never sees its own reaction events."""
        world, message = self._world_with_message()
        client = await _connected(world, SYNTHETIC_ALPHA_ID)
        seen: list = []
        client.on_reaction = lambda *args: seen.append(args)
        world.react(SYNTHETIC_ALPHA_ID, message.id, "❤", kind="add")
        self.assertEqual(seen, [])  # own reaction: dropped
        world.react(SYNTHETIC_BETA_ID, message.id, "❤", kind="add")
        self.assertEqual(len(seen), 1)
        reaction, _user = seen[0][6]
        self.assertFalse(reaction.me)

    async def test_custom_emoji_aggregate_parses_name_and_id(self):
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

    def _lib_dm_reply(self):
        cached = NS(
            id=999, content="original", author=NS(id=999999, name="Mudae"),
            channel=NS(id=9001, name=None, type=NS(name="private"),
                       recipient=NS(id=999999), guild=None),
            guild=None, embeds=[], components=[], attachments=[],
            reactions=[], reference=None, created_at=None,
        )
        outer = NS(
            id=1000, content="reply", author=NS(id=2001, name="alpha"),
            channel=NS(id=9001, name=None, type=NS(name="private"),
                       recipient=NS(id=999999), guild=None),
            guild=None, embeds=[], components=[], attachments=[],
            reactions=[],
            reference=NS(message_id=999, channel_id=9001, guild_id=None,
                         resolved=cached),
            referenced_message=None, created_at=None,
        )
        return outer

    def test_referenced_inherits_outer_dm_flag(self):
        dto = convert_message(self._lib_dm_reply(), dm=True)
        self.assertTrue(dto.dm)
        self.assertIsNotNone(dto.reference.referenced)
        # the nested DTO shares the outer channel's DM-ness (was hardcoded
        # False, mislabeling references inside DM channels)
        self.assertTrue(dto.reference.referenced.dm)

    def test_reference_channel_id_falls_back_to_referenced(self):
        dto = convert_message(self._lib_dm_reply(), dm=True)
        self.assertEqual(dto.reference.channel_id, 9001)
        # asymmetric-fallback fix: an id-only reference (channel_id None on
        # the reference object itself) now inherits the referenced message's
        # channel, exactly like message_id inherits referenced.id
        lib = self._lib_dm_reply()
        lib.reference.channel_id = None
        lib.reference.guild_id = None
        dto2 = convert_message(lib, dm=True)
        self.assertEqual(dto2.reference.message_id, 999)
        self.assertEqual(dto2.reference.channel_id, 9001)

    def test_forward_snapshot_count_reports_truncation(self):
        snapshots = [
            NS(id=999, content="forwarded one", embeds=[], attachments=[],
               components=[], created_at=None),
            NS(id=998, content="forwarded two", embeds=[], attachments=[],
               components=[], created_at=None),
        ]
        lib = NS(
            id=1000, content="", author=NS(id=999999, name="Mudae"),
            channel=NS(id=500, name="grind", type=NS(name="text"),
                       guild=NS(id=42)),
            guild=NS(id=42), embeds=[], components=[], attachments=[],
            reactions=[],
            reference=NS(message_id=999, channel_id=500, guild_id=42,
                         type=NS(name="forward"), resolved=None),
            referenced_message=None, created_at=None,
            message_snapshots=snapshots,
        )
        dto = convert_message(lib, dm=False)
        self.assertEqual(dto.reference.snapshot_count, 2)  # both carried
        # only snapshots[0] is hydrated — the documented truncation
        self.assertEqual(dto.reference.referenced.content, "forwarded one")

    def test_snapshot_count_defaults_to_one(self):
        lib = self._lib_dm_reply()
        dto = convert_message(lib, dm=True)
        self.assertEqual(dto.reference.snapshot_count, 1)
        no_reference = NS(
            id=1, content="x", author=None, channel=None, guild=None,
            embeds=[], components=[], attachments=[], reactions=[],
            reference=None, referenced_message=None, created_at=None,
        )
        self.assertIsNone(convert_message(no_reference, dm=False).reference)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
