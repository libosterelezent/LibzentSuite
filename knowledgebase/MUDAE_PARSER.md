# MUDAE PARSER

> Handwritten deep dive (committed with the repository). Engine: `parsers/mudae/`.

The Mudae message parser turns Mudae's Discord messages into typed
Python objects. It is a standalone engine under the root-level
`parsers/` package: pure data-in/data-out, no client access, no I/O —
plugins call it from their normal `handle_event` flow.

```python
from parsers.mudae import MudaeParser, MudaeParserSettingsManager

manager = MudaeParserSettingsManager()      # defaults, no file needed
parser = MudaeParser(manager)
result = parser.parse(message, account_username="nick")
if result.family is MessageFamily.CHARACTER:
    print(result.character.name, result.character.kakera_value)
```

## ParserManager (plugin integration)

``parsers.manager.PARSER_MANAGER`` is the process-wide registry plugins
use — one import, no engine coupling, future parser libraries plug in
without framework changes:

```python
from parsers import PARSER_MANAGER
from parsers.mudae import MessageFamily

def handle_event(ctx, event):
    if event.type != "message_received":
        return
    result = PARSER_MANAGER.parse_event(event, account_username=ctx.account)
    if result.family is MessageFamily.CHARACTER:
        ctx.logger.info("%s worth %s kakera",
                        result.character.name, result.character.kakera_value)
```

Registry semantics: lazy built-once backends (``register_parser(name,
factory, description=…, families=…)``), duplicate-name protection,
``available()``/``catalog()``, ``parse``/``parse_event`` dispatch
(``parse_event`` unwraps the framework's rich ``event.message`` DTO —
flat ``author_name``/``footer_text`` embeds are harvested identically
to nested library embeds), ``UnknownParserError`` for unknown backends.
The mudae backend persists its settings under
``state/parser_settings.json`` so dashboard edits apply process-wide.

## Public API

| Symbol | Path | Purpose |
|---|---|---|
| `MudaeParser` | `parsers.mudae` | main class; `parse(message, *, account_username=None) -> ParseResult` |
| `MudaeParserSettingsManager` | `parsers.mudae` | configuration lifecycle (defaults → overrides → validated active snapshot) |
| `ParseResult` | `parsers.mudae` | typed result + diagnostics; `.to_dict()` for JSON |
| models | `parsers.mudae` | `CharacterInfo`, `KeyInfo`, `CooldownInfo`, `KakeraEvent`, `TimerInfo`, `WishlistInfo`, `KakeralootInfo`, `TradeInfo`, `MarriageInfo` |

`parse` accepts any duck-typed message exposing `content` / `embeds` /
`components` (a discord.py-self message satisfies this) or a plain
string. Unknown messages return `MessageFamily.UNKNOWN` with
diagnostics — never an exception. `None` / non-message objects raise
`InvalidMessageError`.

## Architecture

```
message ──▶ normalization (bounded TextSources with origin paths)
        ──▶ rule engine (string-marker gates, then regex extraction)
        ──▶ trusted handlers ──▶ ParseResult + diagnostics

defaults (locale pack, version-controlled)
    + persisted overrides (state/parser_settings.json)
    ──▶ validation (syntax/groups/targets/safety)
    ──▶ EffectiveConfig (immutable snapshot)
    ──▶ MudaeParser (one snapshot per parse)
```

Separation of concerns: parsing (`parser.py`, `rules/`) / rule
definitions (`registry.py`, `rules/`) / settings models+defaults
(`settings/`, `locales/en/`) / validation (`settings/validator.py`) /
persistence (`settings/storage.py`). The settings manager contains no
extraction logic; the parser contains no persistence.

## Message families (grounded in CuratedMudaeDataset + live captures)

| Family | Evidence | Extracts |
|---|---|---|
| CHARACTER | rolls/marriage/wishlist embeds; live capture | name (embed author), series, claims/likes rank, kakera value, keys, starwish markers, `Belongs to` owner, claimable state |
| KAKERA | `($k)` reaction replies | amount, username, spheres gained |
| KEYS | roll/info embed key lines | key number, tier (bronze/silver/gold/chaos — wiki-verified), +% value |
| COOLDOWN | roulette limit, `$rtu`, `$ku` | kind, remaining (typed; missing ≠ zero), username, metadata |
| TIMER | `$tu` replies | can_claim, claim/rolls resets, rolls left (+$mk), react power |
| TRADE | deficit/gift/give-confirm | amounts, user ids, confirmations |
| KAKERALOOT | payouts, profile titles | kakera gained, rolls stacked, stock, profile username |
| WISHLIST | stock/refill lines | stock, refill countdown |
| MARRIAGE | "are now married!" | pair, kakera bonus, tier |

## Supported object targets

`TargetSource`: CONTENT, EMBED_AUTHOR_NAME, EMBED_TITLE,
EMBED_DESCRIPTION, EMBED_FIELD_NAME, EMBED_FIELD_VALUE, EMBED_FOOTER,
COMPONENT_LABEL (best effort; unsupported component shapes are skipped
safely). Each setting binds exactly one target — identical text in an
embed title and a component label never collides.

## Regex pattern settings (Feature A)

`RegexPatternSetting`: stable `setting_id` (identity never changes when
the expression is edited), family, target, locale, `expression`,
`case_sensitive`, `required_groups` / `optional_groups`, priority,
`default_version`, enabled.

API on `MudaeParserSettingsManager`:
`list_patterns(family)` · `get_pattern(id)` / `get_pattern_default(id)`
· `has_override(id)` · `origin(id)` · `validate_pattern_expr(id, expr)`
(no activation) · `update_pattern(id, expression=…,
case_sensitive=…)` · `set_pattern_enabled(id, enabled)` ·
`reset_pattern(id)` · `preview_pattern(id, sample, …)` ·
`effective_config` · `catalog()` (grouped, JSON-ready) ·
`supported_targets()` / `supported_families()`.

Validation rejects: syntax errors, missing required named groups
(the extraction contract), expressions > 2000 chars, > 500 registered
patterns, unknown targets, nested unbounded quantifiers
(`(a+)+`-family), empty expressions. Undeclared extra groups are a
warning. **Python's `re` has no reliable per-match timeout** — safety
comes from the bounds above plus input-size limits
(`MAX_MESSAGE_TEXT_CHARS`), not from a claimed timeout. If untrusted
authors ever get pattern access, an isolated engine would be required
(deliberately out of scope).

## Message-string settings (Feature B)

`MessageStringSetting`: the same metadata plus `text` (VERBATIM dataset
string — punctuation/emoji/case never silently altered), `match_mode`
(EXACT / CONTAINS / PREFIX — literal only, never regex), and an
explicit `case_sensitive` flag. Case-insensitive matching folds at
comparison time only; stored raw text and extracted names are never
rewritten.

API: `list_strings` / `get_string` / `get_string_default` /
`validate_string_text` / `update_string(text, match_mode,
case_sensitive)` / `set_string_enabled` / `reset_string` /
`preview_string(id, sample, …)` — previews never mutate active or
stored state.

Rules combine both kinds explicitly: a literal marker classifies the
message family, then the regex extracts variable fields.

## Defaults & override precedence

1. Built-in defaults load (version-controlled locale pack; always
   available, no settings file required).
2. Persisted overrides load and merge by stable ID.
3. Every merged setting is validated; a rejected override falls back
   to the built-in default with a `rejected_override` diagnostic.
4. The validated snapshot is published atomically (generation counter).

Reset restores the CURRENT built-in default. When a software update
changes a shipped default, its `default_version` bumps; overrides are
never auto-rewritten — the catalog exposes both values for diffing.

## Atomic updates & runtime consistency

Every mutation is a transaction: build candidate → validate → persist
(atomic file replace) → publish. Validation failure: nothing changes.
Persistence failure: active config and stored overrides stay exactly
as before (`persistence_failed` result; tested). Parses hold one
immutable `EffectiveConfig` snapshot — an update mid-parse can never
split a message across old and new rules (tested via the `_snapshot`
seam).

## Persistence

`JSONFileSettingsStorage` → `state/parser_settings.json` (gitignored
runtime state, atomic tmp+replace writes, `schema_version` field,
deterministic sorted serialization, corrupt files quarantined not
deleted, future schema versions treated as absent, credentials never
stored). `MemorySettingsStorage` for tests/ephemeral stacks. The
`ParserSettingsStorage` protocol is the narrow seam a future dashboard
manager or the framework DB can implement instead.

## Error handling & diagnostics

Programmer errors raise typed exceptions (`InvalidMessageError`,
`UnknownSettingError`, `UnsupportedLocaleError`, …). Data-level
problems are structured `Diagnostic`s on results
(`unknown_format`, `rejected_override`, `corrupt_store`,
`oversized_input`, `ambiguous_match`, …) — expected nonmatches never
raise, and configuration defects are never swallowed as nonmatches.

## Locales

English ships built-in (`locales/en/`). New locales are additive:
`register_locale(LocalePack(code="fr", patterns=…, strings=…))` with
the same stable setting IDs. **No other locale is implemented today.**

## Future dashboard Parser Manager (NOT built yet)

The engine is dashboard-ready: the manager's catalog/preview/update/
reset/enable APIs are the complete surface a Parser Manager service
needs. No HTTP routes, UI, or WebSocket anything exists for this yet —
that is the next phase. The dashboard must consume the Python API and
never touch compiled patterns or storage files directly.

## Grounding notes

- Defaults were derived during development from a temporary clustered
  dataset (~3.76M messages, removed after build — the evidence now
  lives inline in the locale packs and tests) and the 2026-10-20 live
  captures (real roll/timer/trade/claim traffic).
- Key tiers (bronze/silver/gold/chaos) and starwish ($sw) semantics
  verified against the Mudae wiki (mudae.fandom.com API).
- Known variants NOT parsed (documented limitations): footer-layout
  keys/spheres (`$setfooter` servers), `$im` info-embed extras,
  profile character-list pages, "spend N?" confirms, mudapin releases,
  per-server custom claim messages (overridable settings cover them).
