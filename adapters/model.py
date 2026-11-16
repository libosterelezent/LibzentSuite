"""Rich project-owned Discord message/event model (the adapter boundary).

DESIGN PRINCIPLE (architecture spec): capture the useful information ONCE at
the adapter boundary, preserve it in a stable internal representation, and
keep library-specific details isolated behind an explicit, runtime-only
escape hatch. Plugins consume these DTOs — never ``discord.py-self`` types
for ordinary operations — so future plugins (snipe / kakerareact /
kakeraloot / soulmate farm / component interactions) are additive instead
of requiring another core rewrite.

Stable data vs library-specific objects:
  * everything except ``raw`` is PROJECT-OWNED, frozen (plugins cannot
    corrupt shared event state), JSON-describable metadata;
  * ``raw`` is the optional escape hatch for advanced plugins: the original
    library object, ``repr=False`` (never dumped in logs), runtime-only
    (never serialized into the database, tasks, or caches), accessed via
    ``message.unwrap()`` which documents the contract.

Events carry DATA ONLY (§16): no rate limiting, sleeping, retrying, or
scheduling lives here — actions flow Plugin -> PluginServices -> scheduler
-> limiter -> adapter, exactly like sends.

No blocking I/O anywhere in conversion (§17/§15): the converter reads only
what the library event already holds; missing optionals stay ``None``
rather than triggering fetches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any


# ------------------------------------------------------------------ entities
@dataclass(frozen=True)
class DiscordUser:
    """Stable author/user projection (no fetch; what the event held)."""

    id: int
    name: str = ""
    display_name: str = ""
    bot: bool = False


@dataclass(frozen=True)
class DiscordChannel:
    """Stable channel projection (id/name/type; guild via guild_id)."""

    id: int
    name: str = ""
    type: str = ""
    guild_id: int | None = None


@dataclass(frozen=True)
class DiscordEmbedField:
    """
    One embed field (name/value/inline) — the unit kakerareact-style
    parsing reads via :meth:`DiscordEmbed.field_map`.
    """

    name: str = ""
    value: str = ""
    inline: bool = False


@dataclass(frozen=True)
class DiscordEmbed:
    """Structured embed (§11): the commonly useful properties, project-stable.

    ``color`` is the integer RGB value; ``timestamp`` the ISO string the
    library exposes; media are URL strings. Rich site embeds are not
    modelled — ``raw`` on the message covers exotic needs.
    """

    title: str = ""
    description: str = ""
    url: str = ""
    type: str = ""
    color: int | None = None
    timestamp: str | None = None
    author_name: str = ""
    author_url: str = ""
    author_icon: str = ""
    footer_text: str = ""
    footer_icon: str = ""
    fields: tuple[DiscordEmbedField, ...] = ()
    thumbnail: str = ""
    image: str = ""

    @property
    def colour(self) -> int | None:
        """British spelling alias (mirrors the library's dual naming)."""
        return self.color

    def field_map(self) -> dict:
        """{name: value} for quick lookups (roster-style parsing)."""
        return {f.name: f.value for f in self.fields}


@dataclass(frozen=True)
class DiscordComponent:
    """One component (§10): action rows nest their children.

    ``type_name`` is the stable projection ("action_row" | "button" |
    "select" | "text_input" | "unknown"); ``custom_id`` identifies the
    component to interactions — the key kakeraloot-style plugins need.
    Select options are (label, value, description, default) tuples.
    """

    type: int = 0
    type_name: str = "unknown"
    custom_id: str | None = None
    label: str = ""
    style: int | None = None
    disabled: bool = False
    emoji: str | None = None
    url: str | None = None
    options: tuple[tuple[str, str, str, bool], ...] = ()
    components: tuple[DiscordComponent, ...] = ()

    def walk(self) -> tuple[DiscordComponent, ...]:
        """Every component in the tree, depth-first (self included)."""
        out = [self]
        for child in self.components:
            out.extend(child.walk())
        return tuple(out)

    def find(self, custom_id: str) -> DiscordComponent | None:
        """Locate a component by custom_id anywhere in the tree."""
        for component in self.walk():
            if component.custom_id == custom_id:
                return component
        return None


@dataclass(frozen=True)
class DiscordAttachment:
    """
    One message attachment (file): id, filename, url, size, content type.
    """

    id: int = 0
    filename: str = ""
    url: str = ""
    size: int = 0
    content_type: str = ""


@dataclass(frozen=True)
class DiscordReaction:
    """Aggregate reaction state as carried ON the message (no user info —
    per-user reaction events are ``ReactionAdded`` on the bus)."""

    emoji: str = ""  # printable repr (name or <:name:id>)
    name: str = ""
    id: int | None = None
    count: int = 0
    me: bool = False


@dataclass(frozen=True)
class DiscordMessageReference:
    """Reply/reference identifiers (§12): ids only, never a fetch.

    ``referenced`` is populated ONLY when the library already cached the
    referenced message — otherwise None (explicitly not fetched) — with
    ONE exception (round-3 F4-4): for FORWARD references the fork ships
    the forwarded content as ``message_snapshots`` on the message itself
    (discord.py-self message.py:2101; ``reference.resolved`` stays None),
    and that snapshot is hydrated into ``referenced`` additively (the ids
    stay on the reference; nothing is fetched). TRUNCATION: only
    ``snapshots[0]`` is hydrated — a multi-message forward's remaining
    snapshots are dropped (bounding one DTO per reference); check
    ``snapshot_count`` (the number of snapshots the event carried, 1 when
    absent) against the hydrated ``referenced`` to detect that.
    ``type_name`` is the stable projection of the library's
    MessageReferenceType ("reply" | "forward"; the fork's "default"
    aliases value 0 = reply).
    """

    message_id: int | None = None
    channel_id: int | None = None
    guild_id: int | None = None
    type_name: str = "reply"
    referenced: DiscordMessage | None = None
    #: how many message_snapshots the raw event carried (1 when absent) —
    #: lets plugins detect multi-forward truncation of ``referenced``
    snapshot_count: int = 1


# ------------------------------------------------------------------- message
@dataclass(frozen=True, repr=False)
class DiscordMessage:
    """Rich inbound message DTO — the ONE crossing the adapter boundary.

    COMPATIBILITY (§23): the five original fields keep their exact order
    and names; every rich addition is defaulted, so all existing
    constructions and consumers (flow plugins, remote control, scheduler
    summaries, sim scenarios) are untouched.

    Rich data is captured once at conversion (§15) — plugins never re-fetch
    what the event already held. ``raw`` is the documented escape hatch
    (§26): runtime-only, ``repr=False`` so logs never dump library objects,
    and never persisted.
    """

    id: int
    channel_id: int
    author_id: int
    content: str
    dm: bool = False
    # ------------------------------------------------------ rich additions
    guild_id: int | None = None
    author: DiscordUser | None = None
    channel: DiscordChannel | None = None
    embeds: tuple[DiscordEmbed, ...] = ()
    components: tuple[DiscordComponent, ...] = ()
    attachments: tuple[DiscordAttachment, ...] = ()
    reactions: tuple[DiscordReaction, ...] = ()
    reference: DiscordMessageReference | None = None
    created_at: datetime | None = None
    #: library-specific escape hatch — see module docstring; repr=False so
    #: logging/audit can never accidentally dump it
    raw: Any | None = field(default=None, repr=False)

    # ------------------------------------------------------------ helpers
    def __repr__(self) -> str:
        """Historical friendly one-liner (kept for log/broadcast compat).

        Rich metadata has ``summary()``; the full field view is the
        dataclass-generated one via ``dataclasses.repr`` if ever needed —
        the default repr must stay payload-light because messages appear
        in task results and logs.
        """
        preview = (self.content or "").replace("\n", "\\n")[:40]
        return f"<msg ch={self.channel_id} author={self.author_id}: {preview!r}>"

    @property
    def buttons(self) -> tuple[DiscordComponent, ...]:
        """Every BUTTON component in the tree (kakeraloot convenience)."""
        out: list = []
        for top in self.components:
            for component in top.walk():
                if component.type_name == "button":
                    out.append(component)
        return tuple(out)

    def button(self, custom_id: str) -> DiscordComponent | None:
        """Find a button by custom_id anywhere in the component tree."""
        for top in self.components:
            found = top.find(custom_id)
            if found is not None:
                return found
        return None

    def unwrap(self) -> Any:
        """Escape hatch (§26): the original library message object.

        Contract: RUNTIME-ONLY (never serialize/persist it), library-
        specific (code using it couples to discord.py-self), optional
        (None for messages built synthetically, e.g. in the simulator).
        """
        return self.raw

    def summary(self) -> str:
        """Compact one-liner for logs/audit: metadata + counts, no payloads."""
        parts = [f"id={self.id}", f"ch={self.channel_id}"]
        if self.guild_id is not None:
            parts.append(f"g={self.guild_id}")
        parts.append(f"author={self.author_id}")
        if self.dm:
            parts.append("dm")
        if self.embeds:
            parts.append(f"embeds={len(self.embeds)}")
        if self.components:
            parts.append(f"components={len(self.components)}")
        if self.attachments:
            parts.append(f"attachments={len(self.attachments)}")
        if self.reactions:
            parts.append(f"reactions={len(self.reactions)}")
        if self.reference is not None and self.reference.message_id:
            parts.append(f"ref={self.reference.message_id}")
        return " ".join(parts)


# --------------------------------------------------------------- conversion
def _text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _timestamp_text(value: Any) -> str:
    """ISO 8601 text: the library hands embed timestamps over as aware
    ``datetime`` objects (str() would not be ISO-formatted)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


_COMPONENT_TYPE_NAMES = {1: "action_row", 2: "button", 3: "select", 4: "text_input"}

#: how deep ``reference.referenced`` nesting converts cached reply chains;
#: beyond it the ids stay but the nested DTO is dropped (bounds pathological
#: reply-chain recursion without losing the documented one-level surface)
_MAX_REFERENCED_DEPTH = 1


def convert_component(component: Any) -> DiscordComponent:
    """Library component -> stable projection (recursive for action rows)."""
    ctype = _optional_int(getattr(component, "type", 0)) or 0
    children = getattr(component, "children", None) or ()
    emoji = getattr(component, "emoji", None)
    options = []
    for option in getattr(component, "options", None) or ():
        options.append(
            (
                _text(getattr(option, "label", "")),
                _text(getattr(option, "value", "")),
                _text(getattr(option, "description", "")),
                bool(getattr(option, "default", False)),
            )
        )
    return DiscordComponent(
        type=ctype,
        type_name=_COMPONENT_TYPE_NAMES.get(ctype, "unknown"),
        custom_id=getattr(component, "custom_id", None),
        label=_text(getattr(component, "label", "")),
        style=_optional_int(getattr(component, "style", None)),
        disabled=bool(getattr(component, "disabled", False)),
        emoji=(_text(emoji) if emoji is not None else None),
        url=getattr(component, "url", None),
        options=tuple(options),
        components=tuple(convert_component(child) for child in children),
    )


def convert_embed(embed: Any) -> DiscordEmbed:
    """Library embed -> stable projection (§11 fields, no deep site data)."""
    author = getattr(embed, "author", None)
    footer = getattr(embed, "footer", None)
    thumbnail = getattr(embed, "thumbnail", None)
    image = getattr(embed, "image", None)
    color = getattr(embed, "color", None)
    timestamp = getattr(embed, "timestamp", None)
    return DiscordEmbed(
        title=_text(getattr(embed, "title", "")),
        description=_text(getattr(embed, "description", "")),
        url=_text(getattr(embed, "url", "")),
        type=_text(getattr(embed, "type", "")) or "rich",
        color=_optional_int(color),
        timestamp=(_timestamp_text(timestamp) if timestamp is not None else None),
        author_name=_text(getattr(author, "name", "")) if author else "",
        author_url=_text(getattr(author, "url", "")) if author else "",
        author_icon=_text(getattr(author, "icon_url", "")) if author else "",
        footer_text=_text(getattr(footer, "text", "")) if footer else "",
        footer_icon=_text(getattr(footer, "icon_url", "")) if footer else "",
        fields=tuple(
            DiscordEmbedField(
                _text(getattr(f, "name", "")),
                _text(getattr(f, "value", "")),
                bool(getattr(f, "inline", False)),
            )
            for f in (getattr(embed, "fields", None) or ())
        ),
        thumbnail=_text(getattr(thumbnail, "url", "")) if thumbnail else "",
        image=_text(getattr(image, "url", "")) if image else "",
    )


def _emoji_repr(emoji: Any) -> str:
    if emoji is None:
        return ""
    if isinstance(emoji, str):
        return emoji
    name = getattr(emoji, "name", "")
    eid = _optional_int(getattr(emoji, "id", None))
    if not eid:
        return _text(name)
    if getattr(emoji, "animated", False):
        return f"<a:{name}:{eid}>"
    return f"<:{name}:{eid}>"


def convert_user(user: Any) -> DiscordUser:
    """
    Library user object -> stable DiscordUser projection (no fetch).
    """
    return DiscordUser(
        id=_optional_int(getattr(user, "id", None)) or 0,
        name=_text(getattr(user, "name", "")),
        display_name=_text(getattr(user, "display_name", "") or getattr(user, "name", "")),
        bot=bool(getattr(user, "bot", False)),
    )


def convert_channel(channel: Any, *, dm: bool) -> DiscordChannel:
    """
    Library channel object -> stable DiscordChannel projection.

    Args:
        channel: the library channel (guild text/voice or DM).
        dm: caller-computed DM flag (the adapter knows its library types).

    Returns:
        The stable channel projection; DM channels without a name get a
        ``dm:<recipient id>`` label so downstream rendering stays useful.
    """
    guild = getattr(channel, "guild", None)
    ctype = getattr(channel, "type", None)
    recipient = getattr(channel, "recipient", None)
    name = _text(getattr(channel, "name", ""))
    if dm and not name and recipient is not None:
        name = f"dm:{_optional_int(getattr(recipient, 'id', None))}"
    return DiscordChannel(
        id=_optional_int(getattr(channel, "id", None)) or 0,
        name=name,
        type=ctype.name if hasattr(ctype, "name") else _text(ctype),
        guild_id=(_optional_int(getattr(guild, "id", None)) if guild else None),
    )


def _reference_type_name(reference: Any) -> str:
    ref_type = getattr(reference, "type", None)
    name = getattr(ref_type, "name", None)
    if isinstance(name, str) and name:
        return name if name == "forward" else "reply"
    try:
        if ref_type == 1:
            return "forward"
    except Exception:
        pass
    return "reply"


def _forward_snapshot_view(snapshot: Any, reference: Any) -> Any:
    """Message-shaped view over ONE MessageSnapshot (the forward source).

    discord.py-self 2.1 keeps forwarded content in
    ``message.message_snapshots`` (message.py:2101) while
    ``reference.resolved`` stays None for forwards; the snapshot object
    itself carries no channel/author. This view re-attaches the reference
    ids (MessageSnapshot.id already mirrors reference.message_id —
    message.py:574) so ``convert_message`` can project it like any cached
    message, additively and without any fetch (§12/§15).
    """
    channel_id = _optional_int(getattr(reference, "channel_id", None))
    guild_id = _optional_int(getattr(reference, "guild_id", None))
    guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
    return SimpleNamespace(
        id=getattr(snapshot, "id", None),
        content=getattr(snapshot, "content", ""),
        embeds=getattr(snapshot, "embeds", None) or (),
        attachments=getattr(snapshot, "attachments", None) or (),
        components=getattr(snapshot, "components", None) or (),
        reactions=(),
        created_at=getattr(snapshot, "created_at", None),
        author=getattr(snapshot, "author", None),
        channel=SimpleNamespace(id=channel_id, name="", type=None, guild=guild),
        guild=guild,
        reference=None,
        referenced_message=None,
    )


def convert_message(
    message: Any,
    *,
    dm: bool,
    viewer_id: int | None = None,
    _depth: int = 0,
) -> DiscordMessage:
    """Library message -> rich DiscordMessage (pure, non-blocking, no fetch).

    ``dm`` is computed by the CALLER (the adapter knows its library's DM
    channel type); ``viewer_id`` only documents whose client saw the event
    (the library already filters self-messages upstream). Everything read
    here already exists on the event object — nothing is fetched (§15), and
    absent optionals stay None/empty (§12).
    """
    _ = viewer_id  # documented no-op (self-filtering happens in the adapter)
    author = getattr(message, "author", None)
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    reference = getattr(message, "reference", None)
    # 2.x forks moved the referenced message onto reference.resolved (the
    # old attribute no longer exists there); tolerate both library shapes
    referenced = getattr(message, "referenced_message", None)
    if referenced is None:
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        # DeletedReferencedMessage (the deleted-reply marker: a _deleted
        # flag upstream, a _parent slot in discord.py-self) is not a
        # message — leave None rather than feed a sentinel to the DTO
        if (
            resolved is not None
            and hasattr(resolved, "id")
            and not getattr(resolved, "_deleted", False)
            and not hasattr(resolved, "_parent")
        ):
            referenced = resolved
    snapshots = getattr(message, "message_snapshots", None) or ()
    if (
        referenced is None
        and reference is not None
        and snapshots
        and _reference_type_name(reference) == "forward"
    ):
        # round-3 F4-4: a FORWARD keeps its content in message_snapshots
        # (the fork leaves reference.resolved None) — hydrate additively
        # from the snapshot so the forwarded content is not invisible
        referenced = _forward_snapshot_view(snapshots[0], reference)
    if _depth >= _MAX_REFERENCED_DEPTH:
        referenced = None

    def _ref_ids(ref: Any) -> tuple:
        if ref is None:
            return (None, None, None)
        return (
            _optional_int(getattr(ref, "message_id", None)),
            _optional_int(getattr(ref, "channel_id", None)),
            _optional_int(getattr(ref, "guild_id", None)),
        )

    def _ref_channel_id(ref_msg: Any) -> int | None:
        # the referenced message may expose channel_id directly (snapshot
        # views, other DTOs) or only channel.id (library Message objects)
        if ref_msg is None:
            return None
        direct = _optional_int(getattr(ref_msg, "channel_id", None))
        if direct is not None:
            return direct
        channel = getattr(ref_msg, "channel", None)
        return _optional_int(getattr(channel, "id", None)) if channel is not None else None

    ref_message_id, ref_channel_id, ref_guild_id = _ref_ids(reference)
    reference_dto = None
    if reference is not None or referenced is not None:
        reference_dto = DiscordMessageReference(
            message_id=ref_message_id
            or (_optional_int(getattr(referenced, "id", None)) if referenced else None),
            # symmetric fallback: message_id falls back to referenced.id,
            # channel_id falls back to referenced's channel (the old
            # asymmetry left channel_id None for id-only references)
            channel_id=ref_channel_id or _ref_channel_id(referenced),
            guild_id=ref_guild_id or (_optional_int(getattr(guild, "id", None)) if guild else None),
            type_name=_reference_type_name(reference),
            referenced=(
                # references share the outer message's channel in virtually
                # all cases (same-channel replies/forwards) — inherit the
                # OUTER dm flag so DM-channel references are not mislabeled
                convert_message(referenced, dm=dm, _depth=_depth + 1)
                if referenced is not None
                else None
            ),
            snapshot_count=len(snapshots) or 1,
        )

    return DiscordMessage(
        id=_optional_int(getattr(message, "id", None)) or 0,
        channel_id=_optional_int(getattr(channel, "id", None)) or 0,
        author_id=(_optional_int(getattr(author, "id", None)) if author else 0) or 0,
        content=_text(getattr(message, "content", "")),
        dm=bool(dm),
        guild_id=(_optional_int(getattr(guild, "id", None)) if guild else None),
        author=(convert_user(author) if author else None),
        channel=(convert_channel(channel, dm=dm) if channel else None),
        embeds=tuple(convert_embed(e) for e in (getattr(message, "embeds", None) or ())),
        components=tuple(
            convert_component(c) for c in (getattr(message, "components", None) or ())
        ),
        attachments=tuple(
            DiscordAttachment(
                id=_optional_int(getattr(a, "id", None)) or 0,
                filename=_text(getattr(a, "filename", "")),
                url=_text(getattr(a, "url", "")),
                size=_optional_int(getattr(a, "size", None)) or 0,
                content_type=_text(getattr(a, "content_type", "")),
            )
            for a in (getattr(message, "attachments", None) or ())
        ),
        reactions=tuple(
            DiscordReaction(
                emoji=_emoji_repr(getattr(r, "emoji", None)),
                name=_text(getattr(getattr(r, "emoji", None), "name", "")),
                id=_optional_int(getattr(getattr(r, "emoji", None), "id", None)),
                count=_optional_int(getattr(r, "count", None)) or 0,
                me=bool(getattr(r, "me", False)),
            )
            for r in (getattr(message, "reactions", None) or ())
        ),
        reference=reference_dto,
        created_at=getattr(message, "created_at", None),
        raw=message,
    )


__all__ = [
    "DiscordAttachment",
    "DiscordChannel",
    "DiscordComponent",
    "DiscordEmbed",
    "DiscordEmbedField",
    "DiscordMessage",
    "DiscordMessageReference",
    "DiscordReaction",
    "DiscordUser",
    "convert_component",
    "convert_embed",
    "convert_message",
]
