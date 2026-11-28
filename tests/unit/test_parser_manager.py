"""ParserManager tests: registry semantics, dispatch, event unwrapping,
lazy caching, backend registration, and the framework-DTO (flat embed)
normalization path."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from libzentsuite.parsers import (
    InvalidMessageError,
    ParserManager,
    UnknownParserError,
)


def flat_embed(
    author_name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    footer_text: str | None = None,
    fields: tuple[tuple[str, str, bool], ...] = (),
) -> SimpleNamespace:
    """adapters.model.DiscordEmbed shape (FLAT author/footer)."""
    return SimpleNamespace(
        author_name=author_name or "",
        title=title or "",
        description=description or "",
        footer_text=footer_text or "",
        fields=[SimpleNamespace(name=n, value=v, inline=i) for n, v, i in fields],
    )


class StubResult:
    """Minimal typed result a backend might return."""

    family: str = "stub"
    payload: str = ""

    def __init__(self, family: str = "stub", payload: str = "") -> None:
        self.family = family
        self.payload = payload


class StubParser:
    """A pure, sync parser backend (the contract: one parse callable)."""

    def parse(self, message: Any, *, account_username: str | None = None) -> StubResult:
        content = getattr(message, "content", "") or ""
        if not content:
            embeds = getattr(message, "embeds", []) or []
            if embeds:
                e = embeds[0]
                content = f"{getattr(e, 'title', '')} {getattr(e, 'description', '')}".strip()
        return StubResult(payload=content)


class RegistrySemantics(unittest.TestCase):
    def test_empty_registry_rejects_backend_access(self) -> None:
        manager = ParserManager()
        with self.assertRaises(UnknownParserError):
            manager.backend("nonexistent")

    def test_register_and_dispatch(self) -> None:
        manager = ParserManager()
        manager.register_parser(
            name="stub",
            factory=StubParser,
            description="a stub",
            families=("stub",),
        )
        self.assertEqual(manager.available(), ("stub",))
        msg = SimpleNamespace(content="hello", embeds=[])
        result = manager.parse(msg)
        self.assertEqual(result.payload, "hello")

    def test_duplicate_registration_rejected(self) -> None:
        manager = ParserManager()
        manager.register_parser(name="stub", factory=StubParser)
        with self.assertRaises(ValueError):
            manager.register_parser(name="stub", factory=StubParser)
        # replace=True is explicit
        manager.register_parser(name="stub", factory=StubParser, replace=True)

    def test_unregister(self) -> None:
        manager = ParserManager()
        manager.register_parser(name="stub", factory=StubParser)
        self.assertTrue(manager.unregister_parser("stub"))
        self.assertFalse(manager.unregister_parser("stub"))
        self.assertEqual(manager.available(), ())

    def test_invalid_registration(self) -> None:
        manager = ParserManager()
        with self.assertRaises(ValueError):
            manager.register_parser(name="", factory=StubParser)
        with self.assertRaises(ValueError):
            manager.register_parser(name="x", factory="not callable")  # type: ignore[arg-type]

    def test_lazy_instantiation(self) -> None:
        manager = ParserManager()
        built: list[int] = []

        def factory() -> StubParser:
            built.append(1)
            return StubParser()

        manager.register_parser(name="lazy", factory=factory)
        self.assertEqual(built, [])  # not built yet
        manager.backend("lazy")
        self.assertEqual(len(built), 1)  # built once
        manager.backend("lazy")
        self.assertEqual(len(built), 1)  # cached


class EventUnwrap(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ParserManager()
        self.manager.register_parser(name="stub", factory=StubParser)
        self.message = SimpleNamespace(content="embed test", embeds=[])

    def test_event_with_message(self) -> None:
        event = SimpleNamespace(message=self.message)
        result = self.manager.parse_event(event)
        self.assertEqual(result.payload, "embed test")

    def test_bare_message_passthrough(self) -> None:
        result = self.manager.parse_event(self.message)
        self.assertEqual(result.payload, "embed test")

    def test_string_passthrough(self) -> None:
        # a bare string IS a message-shaped object for the stub parser's
        # getattr(message, "content", "") — it has no .content, so the
        # stub returns empty payload. Verify the event dispatch works.
        result = self.manager.parse_event("plain string")
        self.assertIsNotNone(result)

    def test_unwrappable_event_raises(self) -> None:
        with self.assertRaises(InvalidMessageError):
            self.manager.parse_event(SimpleNamespace(other=1))

    def test_flat_embed_normalization(self) -> None:
        embed = flat_embed(
            author_name="Bot",
            title="Character",
            description="kakera: 100",
        )
        msg = SimpleNamespace(content="", embeds=[embed])
        result = self.manager.parse_event(msg)
        self.assertIn("Character", result.payload)
        self.assertIn("kakera: 100", result.payload)


class CatalogShape(unittest.TestCase):
    def test_catalog_lists_registered_backends(self) -> None:
        manager = ParserManager()
        manager.register_parser(
            name="stub",
            factory=StubParser,
            description="a stub backend",
            families=("a", "b"),
            version="1.0",
        )
        catalog = manager.catalog()
        self.assertIn("stub", catalog)
        entry = catalog["stub"]
        self.assertEqual(entry["description"], "a stub backend")
        self.assertEqual(entry["families"], ["a", "b"])
        self.assertEqual(entry["version"], "1.0")


if __name__ == "__main__":
    unittest.main()
