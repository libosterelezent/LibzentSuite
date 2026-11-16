"""ParserManager tests: registry semantics, dispatch, event unwrapping,
lazy caching, future-backend registration, and the framework-DTO
(flat embed) normalization path."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from parsers import (
    DEFAULT_PARSER,
    ParserManager,
    PARSER_MANAGER,
    UnknownParserError,
    register_builtin_parsers,
)
from parsers.mudae import MessageFamily


def flat_embed(author_name=None, title=None, description=None,
               footer_text=None, fields=()):
    """adapters.model.DiscordEmbed shape (FLAT author/footer)."""
    return SimpleNamespace(
        author_name=author_name or "",
        title=title or "",
        description=description or "",
        footer_text=footer_text or "",
        fields=[SimpleNamespace(name=n, value=v, inline=i)
                for n, v, i in fields],
    )


def roll_message_flat_dto():
    """A roll as the framework's rich DTO (adapters.model shapes)."""
    return SimpleNamespace(
        content="",
        embeds=[flat_embed(
            author_name="Ena Shinonome",
            description="Project SEKAI: Colorful Stage! | Claims: #892 | "
                        "Likes: #689 | **1,043**"
                        "<:kakera:469835869059153940>",
            footer_text="Belongs to libosterelezent")],
        components=(),
    )


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.manager = ParserManager()
        register_builtin_parsers(self.manager)

    def test_builtin_registered(self):
        self.assertIn("mudae", self.manager.available())
        catalog = self.manager.catalog()
        self.assertEqual(catalog["mudae"]["name"], "mudae")
        self.assertIn("character", catalog["mudae"]["families"])

    def test_parse_dispatch_default(self):
        result = self.manager.parse(roll_message_flat_dto())
        self.assertIs(result.family, MessageFamily.CHARACTER)
        self.assertEqual(result.character.name, "Ena Shinonome")

    def test_parse_explicit_backend_name(self):
        result = self.manager.parse(
            roll_message_flat_dto(), parser=DEFAULT_PARSER)
        self.assertIs(result.family, MessageFamily.CHARACTER)

    def test_unknown_backend_raises(self):
        with self.assertRaises(UnknownParserError):
            self.manager.parse("x", parser="does-not-exist")

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValueError):
            self.manager.register_parser(
                "mudae", factory=lambda: object())

    def test_replace_allows_reregistration(self):
        built = {"count": 0}

        def factory():
            built["count"] += 1
            return SimpleNamespace(
                parse=lambda message, account_username=None:
                SimpleNamespace(family="replaced"))

        self.manager.register_parser(
            "mudae", factory=factory, replace=True)
        result = self.manager.parse("anything")
        self.assertEqual(result.family, "replaced")

    def test_unregister_and_reregister(self):
        self.assertTrue(self.manager.unregister_parser("mudae"))
        self.assertFalse(self.manager.unregister_parser("mudae"))
        with self.assertRaises(UnknownParserError):
            self.manager.backend("mudae")
        register_builtin_parsers(self.manager)
        self.assertIn("mudae", self.manager.available())

    def test_lazy_factory_called_once(self):
        calls = {"count": 0}

        def factory():
            calls["count"] += 1
            return SimpleNamespace(
                parse=lambda message, account_username=None: None)

        self.manager.register_parser("lazy", factory=factory)
        self.assertEqual(calls["count"], 0)          # not built yet
        self.manager.parse("x", parser="lazy")
        self.manager.parse("y", parser="lazy")
        self.assertEqual(calls["count"], 1)          # cached instance

    def test_registration_validation(self):
        with self.assertRaises(ValueError):
            self.manager.register_parser("", factory=lambda: None)
        with self.assertRaises(ValueError):
            self.manager.register_parser("broken", factory="not callable")


class FutureBackendTests(unittest.TestCase):
    """The extension point: a third parser library registers without
    touching framework or plugin code."""

    def test_register_future_library(self):
        manager = ParserManager()

        class PokeslotParser:
            def parse(self, message, *, account_username=None):
                text = getattr(message, "content", message) or ""
                if "A wild Pikachu" in text:
                    return SimpleNamespace(
                        family="pokemon_encounter", name="Pikachu")
                return SimpleNamespace(family="unknown")

        manager.register_parser(
            "pokeslot", factory=PokeslotParser,
            description="Pokeslot encounters", families=("pokemon",))
        result = manager.parse("A wild Pikachu appears!",
                                parser="pokeslot")
        self.assertEqual(result.family, "pokemon_encounter")
        self.assertIn("pokeslot", manager.available())


class EventUnwrapTests(unittest.TestCase):
    def setUp(self):
        self.manager = ParserManager()
        register_builtin_parsers(self.manager)

    def test_message_received_event_unwrapped(self):
        event = SimpleNamespace(  # core.event_bus.MessageReceived shape
            message_id=1,
            message=roll_message_flat_dto(),   # rich DiscordMessage DTO
        )
        result = self.manager.parse_event(event)
        self.assertIs(result.family, MessageFamily.CHARACTER)
        self.assertEqual(result.character.claimed_by, "libosterelezent")

    def test_flat_dto_embed_harvested(self):
        """The framework's FLAT DiscordEmbed (author_name/footer_text)
        must yield the same extraction as nested library embeds."""
        result = self.manager.parse(roll_message_flat_dto())
        character = result.character
        self.assertEqual(character.name, "Ena Shinonome")
        self.assertEqual(character.claimed_by, "libosterelezent")
        self.assertEqual(character.series,
                         "Project SEKAI: Colorful Stage!")
        self.assertEqual(character.kakera_value, 1043)

    def test_message_passed_directly(self):
        result = self.manager.parse_event(
            "<:kakeraP:609264156347990016>(Free) **htbao7 +700** ($k)")
        self.assertIs(result.family, MessageFamily.KAKERA)

    def test_event_without_payload_raises(self):
        from parsers.mudae.exceptions import InvalidMessageError

        event = SimpleNamespace(message_id=5, message=None)
        with self.assertRaises(InvalidMessageError):
            self.manager.parse_event(event)

    def test_account_username_flows_through(self):
        event = SimpleNamespace(message=SimpleNamespace(
            content="<:kakeraP:609264156347990016>(Free) **u +700** ($k)",
            embeds=[], components=()))
        result = self.manager.parse_event(event,
                                          account_username="main-acc")
        self.assertEqual(result.account_username, "main-acc")
        self.assertEqual(result.usernames, ("u",))


class SingletonTests(unittest.TestCase):
    def test_process_singleton_has_builtin(self):
        self.assertIn("mudae", PARSER_MANAGER.available())

    def test_singleton_dispatch(self):
        result = PARSER_MANAGER.parse(roll_message_flat_dto())
        self.assertIs(result.family, MessageFamily.CHARACTER)


if __name__ == "__main__":
    unittest.main()
