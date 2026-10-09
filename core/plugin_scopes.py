"""Reserved settings structure: channel scopes (single source of truth).

A plugin profile's ``settings_json`` may carry the RESERVED top-level
key ``"channels"`` restricting where the account's plugin may act::

    {"channels": {"mode": "all" | "allow" | "deny",
                  "rules": [{"guild_id": "123",
                             "channel_ids": ["456", "787"]}]}}

Semantics:

* ``"all"`` (or the key being absent/None) — no restriction;
* ``"allow"`` — channel ALLOWlist grouped by guild: a channel is
  allowed only when its guild has a rule listing it. An empty (or
  guild-less) allow-list therefore allows NOTHING — never everything;
* ``"deny"`` — the mirror image: listed channels are blocked, every
  other channel (including unlisted guilds) is allowed.

Guild/channel ids are STRINGS in the stored structure (JSON keys and
Discord snowflakes exceed JS integer safety); :func:`channel_allowed`
tolerantly accepts ``int`` arguments too.

This module is PURE (no I/O, no framework imports) so every later
phase — the services layer's send gate, the dashboard UI preview, the
command interface — validates and evaluates EXACTLY this structure.
The validation lives here and nowhere else.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["validate", "channel_allowed", "CHANNELS_KEY"]

#: the reserved top-level settings key carrying the scope
CHANNELS_KEY = "channels"

#: accepted scope modes
MODES = ("all", "allow", "deny")


def _err(field: str, problem: str) -> ValueError:
    """One clear error NAMING the problem field (never a bare 'invalid')."""
    return ValueError(f"channel scope field {field!r}: {problem}")


def validate(scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse + validate a channel scope; returns a normalized COPY.

    ``scope`` is the VALUE of the reserved ``"channels"`` key — the
    ``{"mode": ..., "rules": [...]}`` object (a caller holding a whole
    settings dict extracts it first). ``None`` (or an all-mode scope
    with no rules) validates trivially and means "no restriction".

    Returns a normalized copy: ``mode`` defaulted to ``"all"``, ids kept
    as the strings they must already be. The input is never mutated.

    Raises:
        ValueError: naming the problem field — non-dict scope, unknown
            mode, bad rules type, a rule that is not an object, missing
            or non-string guild_id, missing/non-list channel_ids, or a
            non-string channel id / unknown rule key (typo trap).
    """
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise _err(CHANNELS_KEY, f"must be an object, got {type(scope).__name__}")

    unknown_keys = sorted(set(scope) - {"mode", "rules"})
    if unknown_keys:
        raise _err(CHANNELS_KEY,
                   f"unknown key(s) {unknown_keys} (supports: ['mode', 'rules'])")

    mode = scope.get("mode", "all")
    if mode is None:
        mode = "all"
    if not isinstance(mode, str) or mode not in MODES:
        raise _err("mode", f"must be one of {list(MODES)}, got {mode!r}")

    rules_raw = scope.get("rules", [])
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, list):
        raise _err("rules", f"must be a list of rules, got {type(rules_raw).__name__}")

    rules: List[Dict[str, List[str]]] = []
    for index, rule in enumerate(rules_raw):
        field = f"rules[{index}]"
        if not isinstance(rule, dict):
            raise _err(field, f"must be an object, got {type(rule).__name__}")
        unknown = sorted(set(rule) - {"guild_id", "channel_ids"})
        if unknown:
            raise _err(field,
                       f"unknown key(s) {unknown} (supports: "
                       "['guild_id', 'channel_ids'])")
        guild_id = rule.get("guild_id")
        if not isinstance(guild_id, str) or not guild_id.strip():
            raise _err(f"{field}.guild_id",
                       f"must be a non-empty string, got {guild_id!r}")
        channel_ids = rule.get("channel_ids", [])
        if channel_ids is None:
            channel_ids = []
        if not isinstance(channel_ids, list):
            raise _err(f"{field}.channel_ids",
                       f"must be a list of channel-id strings, got "
                       f"{type(channel_ids).__name__}")
        cleaned_ids: List[str] = []
        for channel_id in channel_ids:
            if not isinstance(channel_id, str) or not channel_id.strip():
                raise _err(f"{field}.channel_ids",
                           f"channel ids must be non-empty strings, got "
                           f"{channel_id!r}")
            cleaned_ids.append(channel_id)
        rules.append({"guild_id": guild_id, "channel_ids": cleaned_ids})

    return {"mode": mode, "rules": rules}


def channel_allowed(
    scope: Optional[Dict[str, Any]],
    guild_id: Any,
    channel_id: Any,
) -> bool:
    """Whether (guild_id, channel_id) passes the scope (None = all allowed).

    ``guild_id``/``channel_id`` accept ``str`` OR ``int`` (normalized by
    :class:`str` — never compared cross-type). An UNVALIDATED scope is
    evaluated leniently, never crashing: a malformed rule is skipped,
    and an unreadable mode falls back to "allow" (fail-open applies ONLY
    to shape problems here — callers validate at write time, so this
    path exists for defensive reads of stored data).
    """
    if not scope:
        return True
    if not isinstance(scope, dict):
        return True
    mode = scope.get("mode") or "all"
    if mode == "all":
        return True
    if mode not in ("allow", "deny"):
        return True

    guild = str(guild_id)
    channel = str(channel_id)
    listed = False
    for rule in scope.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rule_guild = rule.get("guild_id")
        if rule_guild is None or str(rule_guild) != guild:
            continue
        for rule_channel in rule.get("channel_ids") or []:
            if str(rule_channel) == channel:
                listed = True
                break
        if listed:
            break
    # allow: ONLY listed channels pass (empty list = nothing allowed);
    # deny: listed channels are blocked, everything else passes.
    return listed if mode == "allow" else not listed
