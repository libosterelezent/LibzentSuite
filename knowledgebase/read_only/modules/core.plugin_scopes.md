# Module `core.plugin_scopes`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/plugin_scopes.py`

```
Reserved settings structure: channel scopes (single source of truth).

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
```

## Functions

- `def validate(scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]`
  - Parse + validate a channel scope; returns a normalized COPY.
- `def channel_allowed(scope: Optional[Dict[str, Any]], guild_id: Any, channel_id: Any) -> bool`
  - Whether (guild_id, channel_id) passes the scope (None = all allowed).
