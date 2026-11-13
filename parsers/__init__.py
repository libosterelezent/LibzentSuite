"""Parsers: Mudae message parsing (framework-agnostic, plugin-consumed).

Pure-Python parsers that turn bot messages into typed objects — for
Mudae: waifu/husbando claims, values (kakera, keys, spheres...), and
the other structured payloads. Parsers take message DATA
(content/embeds/components as delivered by the event boundary), never a
live client, so they stay unit-testable offline and usable from any
plugin on any account.

Design rules (mirroring the plugin contract, core/plugin_base.py):
  * zero I/O — no sends, no awaits, no client access; parsing is a pure
    function of the message payload,
  * typed results with explicit "not a Mudae message" outcomes (a
    parser must never guess on foreign messages),
  * consumed by plugins through their normal event flow — the framework
    routes every MessageReceived to plugins; parsers are the shared
    translation layer plugins call to interpret Mudae's output.

Plugin usage (the one-liner)::

    from parsers import PARSER_MANAGER, MessageFamily

    result = PARSER_MANAGER.parse_event(event)
    if result.family is MessageFamily.CHARACTER:
        ...result.character.kakera_value...

Packages:
  * ``parsers.mudae`` — the MudaeParser engine (modular rules,
    configurable regex patterns and literal message strings, validated
    settings lifecycle; see knowledgebase/MUDAE_PARSER.md);
  * ``parsers.manager`` — ``ParserManager``/``PARSER_MANAGER``: the
    process-wide registry dispatching to parser backends. Ships with
    ``mudae`` registered; future parser libraries register through
    ``ParserManager.register_parser`` without touching framework code.
"""

from __future__ import annotations

from .manager import (
    DEFAULT_PARSER,
    PARSER_MANAGER,
    ParserBackend,
    ParserManager,
    UnknownParserError,
    parse,
    register_builtin_parsers,
)

__all__ = [
    "DEFAULT_PARSER",
    "PARSER_MANAGER",
    "ParserBackend",
    "ParserManager",
    "UnknownParserError",
    "parse",
    "register_builtin_parsers",
]
