"""Reserved settings structure: channel scopes (single source of truth).

A plugin profile's ``settings_json`` may carry the RESERVED top-level
key ``"channels"`` restricting where the account's plugin may act::

    {"channels": ["456", "787"]}

Semantics (POSITIVE SET — no modes):

* the key being absent/None/empty — no restriction (every channel);
* a channel is allowed ONLY when its id is in the set.

Guild/channel ids are STRINGS in the stored structure (JSON keys and
Discord snowflakes exceed JS integer safety); :func:`channel_allowed`
tolerantly accepts ``int`` arguments too.

HISTORY: the original structure was a mode-rule envelope
(``{"mode": "all"|"allow"|"deny", "rules": [...]}``). Removed with the
legacy ProfileManager cleanup (2026-10-23): three ways to say the same
thing was the wrong UX, and the picker is a positive selection. Stored
scopes in the old shape read as UNRESTRICTED (fail-open transition) —
re-edit and re-save to convert.

This module is PURE (no I/O, no framework imports) so every later
phase — the services layer's send gate, the dashboard UI picker, the
command interface — validates and evaluates EXACTLY this structure.
The validation lives here and nowhere else.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["validate", "channel_allowed", "CHANNELS_KEY"]

#: the reserved top-level settings key carrying the scope
CHANNELS_KEY = "channels"


def _err(field: str, problem: str) -> ValueError:
    """One clear error NAMING the problem field (never a bare 'invalid')."""
    return ValueError(f"channel scope field {field!r}: {problem}")


def validate(scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse + validate a channel scope; returns a normalized COPY.

    ``scope`` is the VALUE of the reserved ``"channels"`` key:
    ``{"channels": [id, ...]}`` (or None = unrestricted).

    Returns a normalized copy: ids kept as the strings they must
    already be. The input is never mutated.

    Raises:
        ValueError: naming the problem field — non-dict scope, a
            ``channels`` value that is not a list of non-empty
            strings, or an unknown key (typo trap).
    """
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise _err(CHANNELS_KEY, f"must be an object, got {type(scope).__name__}")

    unknown_keys = sorted(set(scope) - {"channels"})
    if unknown_keys:
        raise _err(CHANNELS_KEY,
                   f"unknown key(s) {unknown_keys} (supports: ['channels'])")

    ids_raw = scope.get("channels", [])
    if not isinstance(ids_raw, list):
        raise _err("channels", f"must be a list of channel ids, got {type(ids_raw).__name__}")

    cleaned: List[str] = []
    for index, channel_id in enumerate(ids_raw):
        field = f"channels[{index}]"
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise _err(field,
                       f"must be a non-empty string, got {channel_id!r}")
        cleaned.append(channel_id.strip())

    return {"channels": cleaned}


def channel_allowed(
    scope: Optional[Dict[str, Any]],
    guild_id: Any,
    channel_id: Any,
) -> bool:
    """Whether (guild_id, channel_id) passes the scope (None = all allowed).

    ``guild_id``/``channel_id`` accept ``str`` OR ``int`` (normalized by
    :class:`str` — never compared cross-type). An UNVALIDATED or
    legacy-shaped scope is evaluated leniently, never crashing; only a
    clean positive set restricts (fail-open applies ONLY to shape
    problems here — callers validate at write time).
    """
    if not scope:
        return True
    if not isinstance(scope, dict):
        return True
    listed = scope.get("channels")
    if not isinstance(listed, list):
        return True
    channel = str(channel_id)
    return any(str(c) == channel for c in listed)
