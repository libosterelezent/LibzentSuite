"""``ParserManager`` — the framework's message-parser registry.

One place that owns every parser backend, so plugins never import a
specific engine and future parser libraries (any bot's output,
generic formats...) plug in WITHOUT touching plugin or framework code::

    from parsers import PARSER_MANAGER

    result = PARSER_MANAGER.parse_event(event, parser="my_game")
    if result.family == "character_claim":
        ...

Design:
  * REGISTRY, not a framework service — a process-wide singleton
    (the same pattern as ``core.plugin_manager.commands.COMMAND_HUB``);
    nothing in ``core/`` imports this, keeping the parser subsystem
    decoupled (per the parsers/ design contract);
  * LAZY backends — a registered factory builds on first use and the
    instance is cached (zero cost until a plugin actually parses);
  * each backend OWNS its configuration and persistence; the
    ParserManager never touches backend internals;
  * SYNC + PURE — parse is a pure function of the message payload;
    no client access, no awaits, safe from any plugin handler.

The registry ships EMPTY — parser packs (game-specific or generic)
register themselves through :meth:`ParserManager.register_parser`.

Registering a parser pack::

    from parsers import ParserManager, ParserBackend

    manager.register_parser(
        name="my_game",
        factory=lambda: MyGameParser(),
        description="My game bot's messages",
        families=("character_claim", "cooldown", ...),
    )

The backend contract is one callable:
``parse(message, *, account_username=None) -> typed result`` (any
result type with a ``family``-like discriminator).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "PARSER_MANAGER",
    "ParserBackend",
    "ParserManager",
    "UnknownParserError",
    "parse",
]


class UnknownParserError(KeyError):
    """No parser backend registered under the requested name."""


class InvalidMessageError(ValueError):
    """The event/message carries no parsable payload."""


@dataclass(frozen=True)
class ParserBackend:
    """Metadata + entry point for one registered parser backend."""

    name: str
    #: zero-arg callable building the parser instance (lazy, cached)
    factory: Callable[[], Any]
    description: str = ""
    #: the families/categories the backend can produce (loose strings —
    #: each backend defines its own vocabulary)
    families: tuple[str, ...] = ()
    version: str = ""


class ParserManager:
    """Registry + dispatch facade over parser backends.

    Thread-safe for registration; parse itself is lock-free (backends
    are pure and their active-config snapshots are immutable).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._backends: dict[str, ParserBackend] = {}
        self._instances: dict[str, Any] = {}

    # -------------------------------------------------------- registration
    def register_parser(
        self,
        name: str,
        factory: Callable[[], Any],
        *,
        description: str = "",
        families: tuple[str, ...] = (),
        version: str = "",
        replace: bool = False,
    ) -> None:
        """Register (or replace) a backend. Duplicate names are
        rejected unless ``replace`` is explicit — silent replacement
        would mask typos."""
        if not name or not callable(factory):
            raise ValueError("a parser backend needs a non-empty name and a callable factory")
        with self._lock:
            if name in self._backends and not replace:
                raise ValueError(
                    f"parser {name!r} is already registered (pass replace=True to override)"
                )
            self._backends[name] = ParserBackend(
                name=name,
                factory=factory,
                description=description,
                families=tuple(families),
                version=version,
            )
            self._instances.pop(name, None)

    def unregister_parser(self, name: str) -> bool:
        """Drop a backend (True when it existed)."""
        with self._lock:
            existed = self._backends.pop(name, None) is not None
            self._instances.pop(name, None)
            return existed

    def available(self) -> tuple[str, ...]:
        """Registered backend names (sorted)."""
        with self._lock:
            return tuple(sorted(self._backends))

    def catalog(self) -> dict[str, dict[str, Any]]:
        """Backend metadata for inspection (no instantiation)."""
        with self._lock:
            return {
                name: {
                    "name": backend.name,
                    "description": backend.description,
                    "families": list(backend.families),
                    "version": backend.version,
                }
                for name, backend in sorted(self._backends.items())
            }

    # ------------------------------------------------------------- access
    def backend(self, name: str = "") -> Any:
        """The backend INSTANCE (lazily built once, then cached).

        With no name, the FIRST registered backend is used (there is
        no privileged default — the registry ships empty)."""
        with self._lock:
            if not name:
                if not self._backends:
                    raise UnknownParserError(
                        "no parser backends registered (register one via "
                        "ParserManager.register_parser)"
                    )
                name = next(iter(self._backends))
            backend = self._backends.get(name)
            if backend is None:
                raise UnknownParserError(
                    f"no parser backend {name!r} (available: {', '.join(sorted(self._backends))})"
                )
            instance = self._instances.get(name)
            if instance is None:
                instance = backend.factory()
                self._instances[name] = instance
            return instance

    def parse(self, message: Any, *, account_username: str | None = None, parser: str = "") -> Any:
        """Dispatch one message to a backend (its contract result)."""
        backend = self.backend(parser)
        return backend.parse(message, account_username=account_username)

    def parse_event(
        self, event: Any, *, account_username: str | None = None, parser: str = ""
    ) -> Any:
        """Parse a framework ``MessageReceived``/``MessageEdited`` bus
        event: unwraps the rich ``event.message`` DTO (adapters.model.
        DiscordMessage — already the duck-typed shape parsers accept)
        and dispatches. Plain messages/strings pass through unchanged."""
        message = getattr(event, "message", None)
        if message is None:
            if isinstance(event, str) or hasattr(event, "content") or hasattr(event, "embeds"):
                message = event  # a message object passed directly
            else:
                raise InvalidMessageError(
                    "event carries no parsable message payload (event.message is None)"
                )
        return self.parse(message, account_username=account_username, parser=parser)

    def reset(self) -> None:
        """Drop all backends + cached instances (test isolation)."""
        with self._lock:
            self._backends.clear()
            self._instances.clear()


#: process-wide singleton (one registry per process, like COMMAND_HUB)
PARSER_MANAGER = ParserManager()


def parse(message: Any, *, account_username: str | None = None, parser: str = "") -> Any:
    """Module-level convenience over :data:`PARSER_MANAGER`."""
    return PARSER_MANAGER.parse(message, account_username=account_username, parser=parser)
