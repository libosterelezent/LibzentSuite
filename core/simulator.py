"""Local simulation: a complete in-memory Discord world.

Provides:
  * FakeWorld  — users, channels, DMs, message history, scripted Mudae responder
  * FakeClient — DiscordClientProtocol implementation bound to the world
  * SimulatedCluster — builds a ready-to-run container with N synthetic accounts

The simulator is used by the test suite AND `python main.py --sim` so the whole
framework can be exercised locally with zero network access and no real tokens.

SYNTHETIC ids below are fixtures only — real ids/tokens always come from .env.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .account import AccountConfig, DiscordMessage, ReconnectPolicy
from .container import AccountContainer
from .credentials import Credentials
from .events import EventBus
from .plugin_loader import PluginLoader
from .rate_limiter import RateLimiterService
from .settings import SettingsStore

LOG = logging.getLogger("slaughtermudae.sim")

# --- synthetic fixture ids (NOT real Discord ids; real ones live in .env) ---
SYNTHETIC_OWNER_ID = 1001
SYNTHETIC_ALPHA_ID = 2001
SYNTHETIC_BETA_ID = 2002
SYNTHETIC_GUILD_CHANNEL = 500

MUDAE_PAGES = [
    "Soulmates\n---------\nAlice\nBob\nCharlie",
    "Dana\nEve\nFinn",
]


@dataclass
class _WorldMessage:
    id: int
    channel_id: int
    author_id: int
    content: str
    dm: bool


class FakeWorld:
    """In-memory Discord: users, channels, DM channels, history, responders."""

    def __init__(self, mudae_user_id: int) -> None:
        self.users: Dict[int, str] = {}
        self.channels: Dict[int, str] = {}
        self.dm_channels: Dict[int, bool] = {}
        self.history: Dict[int, List[_WorldMessage]] = {}
        self._dm_pairs: Dict[Tuple[int, int], int] = {}
        self._reverse_dm: Dict[int, Tuple[int, int]] = {}
        self._dm_seq = 0
        self._next_id = 1
        self.subscribers: List["FakeClient"] = []
        self.auto_mudae_id: Optional[int] = mudae_user_id
        self.mudae_pages: List[str] = list(MUDAE_PAGES)
        self.mudae_reply_delay: float = 0.01

    # ------------------------------------------------------------- topology
    def add_user(self, user_id: int, name: str) -> None:
        self.users[user_id] = name

    def add_channel(self, channel_id: int, name: str, dm: bool = False) -> None:
        self.channels[channel_id] = name
        self.dm_channels[channel_id] = dm
        self.history.setdefault(channel_id, [])

    def dm_channel_for(self, user_a: int, user_b: int) -> int:
        """Deterministic, symmetric DM channel id for a user pair.

        AUDIT FIX: collision-checked — if the hash-style id already belongs to
        a different pair, fall back to a sequential id instead of merging two
        conversations.
        """
        pair = (min(user_a, user_b), max(user_a, user_b))
        if pair not in self._dm_pairs:
            channel_id = 1_000_000 + pair[0] * 7919 + pair[1]
            while channel_id in self._reverse_dm or channel_id in self.channels:
                self._dm_seq += 1
                channel_id = 2_000_000_000 + self._dm_seq
            self._dm_pairs[pair] = channel_id
            self._reverse_dm[channel_id] = pair
            self.add_channel(channel_id, f"dm-{pair[0]}-{pair[1]}", dm=True)
        return self._dm_pairs[pair]

    def dm_pairs_of(self, channel_id: int) -> list:
        """The (one) user pair behind a DM channel id (per-account filtering)."""
        pair = self._reverse_dm.get(channel_id)
        return [pair] if pair else []

    # -------------------------------------------------------------- traffic
    def deliver(
        self, channel_id: int, author_id: int, content: str, dm: Optional[bool] = None
    ) -> _WorldMessage:
        """Append a message and push it to every connected client."""
        if channel_id not in self.channels:
            raise KeyError(f"unknown channel {channel_id}")
        if dm is None:
            dm = self.dm_channels.get(channel_id, False)
        message = _WorldMessage(self._next_id, channel_id, author_id, content, dm)
        self._next_id += 1
        self.history[channel_id].append(message)
        for client in list(self.subscribers):
            client.receive(message)
        self._maybe_auto_respond(message)
        return message

    def _maybe_auto_respond(self, message: _WorldMessage) -> None:
        """Scripted Mudae: any '$slud-s' in a channel -> soulmate pages via DM."""
        if self.auto_mudae_id is None or message.content.strip() != "$slud-s":
            return
        if message.author_id == self.auto_mudae_id or message.author_id not in self.users:
            return
        author, mudae = message.author_id, self.auto_mudae_id
        asyncio.get_running_loop().create_task(self._mudae_reply(author, mudae))

    async def _mudae_reply(self, author: int, mudae: int) -> None:
        await asyncio.sleep(self.mudae_reply_delay)
        channel_id = self.dm_channel_for(author, mudae)
        for i, page in enumerate(self.mudae_pages):
            await asyncio.sleep(self.mudae_reply_delay)
            self.deliver(channel_id, mudae, page, dm=True)

    # -------------------------------------------------------------- queries
    def messages_of(self, channel_id: int) -> List[str]:
        return [m.content for m in self.history.get(channel_id, [])]

    async def wait_for_quiet(self, channel_id: int, quiet: float = 0.4, limit: float = 10.0) -> None:
        """Return once no new messages appear in a channel for `quiet` seconds.

        AUDIT FIX: previously this ran to the full `limit` regardless of
        silence (the quiet window was never checked) — every caller waited
        far longer than intended. Now it returns as soon as `quiet` seconds
        of silence accumulate, with `limit` as the hard cap.
        """
        seen = len(self.history.get(channel_id, []))
        quiet_elapsed, total = 0.0, 0.0
        while total < limit:
            step = min(0.05, max(quiet - quiet_elapsed, 0.001))
            await asyncio.sleep(step)
            total += step
            current = len(self.history.get(channel_id, []))
            if current != seen:
                seen = current
                quiet_elapsed = 0.0
            else:
                quiet_elapsed += step
                if quiet_elapsed >= quiet:
                    return


class FakeClient:
    """DiscordClientProtocol implementation over FakeWorld (per account)."""

    def __init__(self, world: FakeWorld, user_id: int) -> None:
        self._world = world
        self.user_id = user_id
        self.on_message: Optional[Callable[[DiscordMessage], None]] = None
        self.on_disconnect: Optional[Callable[[str], None]] = None
        self.connected = False
        self._rate_counters: Dict[int, int] = {}

    def rate_feedback_for(self, channel_id: Optional[int]) -> Any:
        """Simulated Discord headers for the channel just used (REST intake).

        A 5-cap bucket per channel that drains then refills — enough to
        exercise RestBucketManager's header intake offline.
        """
        from core.rest_buckets import RestFeedback

        counter = self._rate_counters.get(channel_id, 0) + 1
        self._rate_counters[channel_id] = counter
        remaining = max(0, 4 - (counter % 5))
        return RestFeedback(
            status=200,
            headers={
                "X-RateLimit-Limit": "5",
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset-After": "1.0",
                "X-RateLimit-Bucket": f"sim-{channel_id}",
                "X-RateLimit-Scope": "user",
            },
        )

    def delete_channel(self, channel_id: int) -> bool:
        """Simulate a channel deletion: world forgets it, sends now raise."""
        if channel_id not in self._world.channels:
            return False
        self._world.channels.pop(channel_id, None)
        self._world.dm_channels.pop(channel_id, None)
        self._world.history.pop(channel_id, None)
        return True

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if self.connected:  # AUDIT FIX: idempotent — no duplicate subscriptions
            return
        self._world.subscribers.append(self)
        self.connected = True

    async def disconnect(self) -> None:
        if self in self._world.subscribers:
            self._world.subscribers.remove(self)
        self.connected = False

    def drop(self, reason: str = "simulated network drop") -> None:
        """Simulate an UNEXPECTED connection loss (fires on_disconnect)."""
        if not self.connected:
            return
        if self in self._world.subscribers:
            self._world.subscribers.remove(self)
        self.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(reason)

    # ------------------------------------------------------------- messaging
    async def send_message(self, channel_id: int, content: str) -> DiscordMessage:
        if not self.connected:
            raise ConnectionError("client not connected")
        sent = self._world.deliver(channel_id, self.user_id, content)
        return DiscordMessage(sent.id, sent.channel_id, sent.author_id, sent.content, sent.dm)

    async def fetch_user(self, user_id: int) -> Optional[Any]:
        if user_id not in self._world.users:
            return None
        return SimpleNamespace(id=user_id, name=self._world.users[user_id])

    async def open_dm(self, user_id: int) -> int:
        return self._world.dm_channel_for(self.user_id, user_id)

    def cached_snapshot(self) -> dict:
        """World-backed equivalent of the live client's in-memory cache.

        AUDIT FIX: DM channels are filtered to pairs involving THIS account —
        previously the world-level view leaked other accounts' DMs.
        """
        guild_channels = [
            {"id": cid, "name": name}
            for cid, name in self._world.channels.items()
            if not self._world.dm_channels.get(cid, False)
        ]
        dm_channels = [
            {"id": cid, "name": name}
            for cid, name in self._world.channels.items()
            if self._world.dm_channels.get(cid, False)
            and any(self.user_id in pair for pair in self._world.dm_pairs_of(cid))
        ]
        return {
            "available": self.connected,
            "user": {"id": self.user_id, "name": self._world.users.get(self.user_id, "?")},
            "guilds": [{"id": 0, "name": "sim-guild", "channels": guild_channels}],
            "dm_channels": dm_channels,
        }

    # -------------------------------------------------------------- internal
    def receive(self, message: _WorldMessage) -> None:
        """World push -> session callback (ignores our own messages)."""
        if self.on_message is not None and message.author_id != self.user_id:
            self.on_message(
                DiscordMessage(
                    id=message.id,
                    channel_id=message.channel_id,
                    author_id=message.author_id,
                    content=message.content,
                    dm=message.dm,
                )
            )


class SimpleNamespace:
    """Tiny attr-bag (avoids importing types for fake users)."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


class SimulatedCluster:
    """Builds a fully wired container over an in-memory world.

    Accounts 'alpha' and 'beta' both run the soulcopy plugin; they share sync
    group 'grinders'. The world contains a synthetic owner and a Mudae responder
    using the real bot id supplied via credentials (never hardcoded).
    """

    def __init__(
        self,
        *,
        mudae_bot_id: int,
        owner_id: int = SYNTHETIC_OWNER_ID,
        plugin_dir: str = "plugins",
        settings_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        rate_rules: Optional[Dict[str, str]] = None,
        limiter: Optional[RateLimiterService] = None,
        audit: Optional[Any] = None,
        roles: Optional[Any] = None,
        send_policy: Optional[Any] = None,
    ) -> None:
        # demo pacing: seconds, not minutes (production values live in config/).
        # dm_read_timeout is generous so runtime delay ranges set from the
        # dashboard (which slow the export send) can't starve the collector.
        demo_settings: Dict[str, Dict[str, Any]] = {
            "soulcopy": {"dm_read_timeout": 3.0, "inter_chunk_delay": 0.05, "max_chunk_length": 60}
        }
        for plugin, overrides in (settings_overrides or {}).items():
            demo_settings.setdefault(plugin, {}).update(overrides)
        self.credentials = Credentials.from_values(
            {
                "MUDAE_BOT_ID": str(mudae_bot_id),
                "OWNER_USER_IDS": str(owner_id),
                "DISCORD_TOKEN_ALPHA": "sim-token-alpha",
                "DISCORD_TOKEN_BETA": "sim-token-beta",
            }
        )
        self.world = FakeWorld(mudae_user_id=mudae_bot_id)
        self.world.add_user(owner_id, "sim-owner")
        self.world.add_user(mudae_bot_id, "mudae")
        self.world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        self.world.add_user(SYNTHETIC_BETA_ID, "beta")
        self.world.add_channel(SYNTHETIC_GUILD_CHANNEL, "grind-channel")
        self._user_ids = {"alpha": SYNTHETIC_ALPHA_ID, "beta": SYNTHETIC_BETA_ID}

        loader = PluginLoader(plugin_dir)
        loader.discover()
        self.loader = loader
        self.settings = SettingsStore(global_settings=demo_settings)
        self.limiter = limiter if limiter is not None else RateLimiterService(
            default_rules=rate_rules or {"message": "2000/0.05s", "dm": "2000/0.05s"}
        )
        self.audit = audit
        self.container = AccountContainer(
            credentials=self.credentials,
            settings=self.settings,
            limiter=self.limiter,
            loader=loader,
            bus=EventBus(),
            client_factory=self._client_factory,
            # sims don't reconnect unless a test asks for it
            reconnect_policy=ReconnectPolicy(enabled=False),
            roles=roles,
            audit=audit,
            send_policy=send_policy,
        )
        # alpha runs every discovered plugin; beta idles in the group — the
        # account x plugin matrix is per-account config (independent use)
        self.container.add_account(
            AccountConfig(name="alpha", sync_group="grinders", plugins=None)
        )
        self.container.add_account(
            AccountConfig(name="beta", sync_group="grinders", plugins=[])
        )

    def _client_factory(self, config: AccountConfig, credentials: Credentials) -> FakeClient:
        return FakeClient(self.world, self._user_ids[config.name])

    async def start(self) -> None:
        await self.container.start_all()

    async def stop(self) -> None:
        await self.container.stop_all()

    async def owner_says(self, content: str, author_id: Optional[int] = None) -> None:
        """Simulate the owner typing a command in the guild channel."""
        self.world.deliver(SYNTHETIC_GUILD_CHANNEL, author_id or SYNTHETIC_OWNER_ID, content)

    async def run_soulcopy_transcript(self) -> List[str]:
        """Trigger slcopy and wait until the output settles; return transcript.

        AUDIT FIX: the quiet window now derives from the effective
        dm_read_timeout so the default 3.0s demo pacing can't make this
        return before the chunks land.
        """
        await self.owner_says("slcopy")
        read_timeout = self.settings._global.get("soulcopy", {}).get("dm_read_timeout", 3.0)
        await self.world.wait_for_quiet(
            SYNTHETIC_GUILD_CHANNEL, quiet=float(read_timeout) + 1.2, limit=30.0
        )
        return self.world.messages_of(SYNTHETIC_GUILD_CHANNEL)
