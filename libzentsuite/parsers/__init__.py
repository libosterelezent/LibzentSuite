"""Parsers: pluggable message-parser registry (framework-agnostic).

Pure-Python parsers turn bot messages into typed objects. Parsers take
message DATA (content/embeds/components as delivered by the event
boundary), never a live client, so they stay unit-testable offline and
usable from any plugin on any account.

Design rules (mirroring the plugin contract, core/plugin_base.py):
  * zero I/O — no sends, no awaits, no client access; parsing is a pure
    function of the message payload,
  * typed results with explicit "not a match" outcomes (a parser must
    never guess on foreign messages),
  * consumed by plugins through their normal event flow — the framework
    routes every MessageReceived to plugins; parsers are the shared
    translation layer plugins call to interpret bot output.

The registry ships EMPTY — game/parser packs register themselves at
import time (or explicitly). See ``ParserManager.register_parser``.

Plugin usage::

    from libzentsuite.parsers import PARSER_MANAGER

    result = PARSER_MANAGER.parse_event(event, parser="my_parser")
    if result.family == "character_claim":
        ...

Registering a parser pack::

    from libzentsuite.parsers import ParserManager, ParserBackend

    manager.register_parser(
        name="my_game",
        factory=lambda: MyGameParser(),
        description="My game's bot messages",
        families=("character_claim", "cooldown", ...),
    )

The backend contract is one callable:
``parse(message, *, account_username=None) -> typed result`` (any
result type with a ``family``-like discriminator).
"""

from __future__ import annotations

from .manager import (
    PARSER_MANAGER,
    InvalidMessageError,
    ParserBackend,
    ParserManager,
    UnknownParserError,
    parse,
)

__all__ = [
    "PARSER_MANAGER",
    "InvalidMessageError",
    "ParserBackend",
    "ParserManager",
    "UnknownParserError",
    "parse",
]
