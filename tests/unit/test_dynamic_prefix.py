"""Dynamic command-prefix tests (the fork's callable contract).

The fork invokes ``command_prefix(bot, message)`` on EVERY message and
awaits the result — wiring a callable over the AccessManager's warmed
cache makes an ``adminprefix`` mutation land on the LIVE account's
next message, with no rebuild. These pin that contract end to end:
the factory wiring, the per-message resolution order (provider over
build-time snapshot), the mention/symbol/both mapping, and the
unwired-stack fallback.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from typing import TYPE_CHECKING, Any

from adapters.discord.client import RealDiscordClient

if TYPE_CHECKING:
    from collections.abc import Callable


def _lib() -> tuple[NS, list[str]]:
    """when_mentioned / when_mentioned_or doubles with observable calls."""
    calls: list[str] = []

    def when_mentioned(bot: Any, message: Any) -> list[str]:
        calls.append(f"mention:{getattr(message, 'id', '?')}")
        return [f"<@{bot.user_id}>"]

    def when_mentioned_or(symbol: str) -> Callable[[Any, Any], list[str]]:
        def _both(bot: Any, message: Any) -> list[str]:
            calls.append(f"both:{symbol}:{getattr(message, 'id', '?')}")
            return [f"<@{bot.user_id}>", symbol]

        return _both

    return NS(when_mentioned=when_mentioned, when_mentioned_or=when_mentioned_or), calls


def _bot_msg(msg_id: int = 1, bot_id: int = 555) -> tuple[NS, NS]:
    return NS(user_id=bot_id), NS(id=msg_id)


class DynamicPrefixTests(unittest.TestCase):
    def _client(self, **kw: Any) -> RealDiscordClient:
        base: dict[str, Any] = {"command_prefix": "sm!"}
        base.update(kw)
        # bypass __init__ (its loop-bound state needs a running loop):
        # only the prefix-resolution fields matter to this contract
        client = RealDiscordClient.__new__(RealDiscordClient)
        client._command_prefix = base["command_prefix"]
        client._prefix_mode = base.get("prefix_mode", "mention")
        client._prefix_symbol = base.get("prefix_symbol", "")
        client._prefix_provider = base.get("prefix_provider")
        client._prefix_helpers = ()
        return client

    def test_provider_change_lands_next_message(self) -> None:
        cache: dict[str, dict[str, Any]] = {
            "alpha": {"prefix_mode": "mention", "prefix_symbol": ""}
        }
        # "@me" env: mention mode resolves to the mention form (an env
        # SYMBOL prefix would win over mention — the pinned precedence)
        client = self._client(command_prefix="@me", prefix_provider=lambda: cache.get("alpha"))
        lib, _calls = _lib()
        prefix = client._dynamic_prefix(lib)
        bot, msg = _bot_msg()

        first = prefix(bot, msg)
        self.assertEqual(first, ["<@555>"])  # mention mode

        # the adminprefix mutation: symbol mode lands IMMEDIATELY
        cache["alpha"] = {"prefix_mode": "symbol", "prefix_symbol": "gr!"}
        second = prefix(bot, msg)
        self.assertEqual(second, "gr!")

        # and back to both — mention-or-symbol list
        cache["alpha"] = {"prefix_mode": "both", "prefix_symbol": "gr!"}
        third = prefix(bot, msg)
        self.assertEqual(third, ["<@555>", "gr!"])

    def test_snapshot_fallback_when_unwired(self) -> None:
        client = self._client(prefix_mode="symbol", prefix_symbol="sm!")
        lib, _calls = _lib()
        prefix = client._dynamic_prefix(lib)
        bot, msg = _bot_msg()
        self.assertEqual(prefix(bot, msg), "sm!")  # constructor snapshot

    def test_provider_failure_falls_back_to_snapshot(self) -> None:
        def broken() -> dict[str, Any] | None:
            raise RuntimeError("cache cold")

        client = self._client(prefix_mode="symbol", prefix_symbol="sm!", prefix_provider=broken)
        lib, _calls = _lib()
        prefix = client._dynamic_prefix(lib)
        bot, msg = _bot_msg()
        self.assertEqual(prefix(bot, msg), "sm!")

    def test_env_symbol_prefix_without_provider(self) -> None:
        client = self._client(command_prefix="sm!")  # env, mention default
        lib, calls = _lib()
        prefix = client._dynamic_prefix(lib)
        bot, msg = _bot_msg()
        self.assertEqual(prefix(bot, msg), "sm!")  # env prefix wins over mention
        self.assertEqual(calls, [])  # mention helper never invoked

    def test_at_me_env_resolves_mention(self) -> None:
        client = self._client(command_prefix="@me")
        lib, calls = _lib()
        prefix = client._dynamic_prefix(lib)
        bot, msg = _bot_msg()
        self.assertEqual(prefix(bot, msg), ["<@555>"])
        self.assertEqual(calls, ["mention:1"])


class FactoryProviderWiringTests(unittest.TestCase):
    def test_factory_threads_provider_and_snapshot(self) -> None:
        from adapters.discord.client import RealClientFactory

        class StubAccess:
            def __init__(self) -> None:
                self.cache: dict[str, dict[str, Any] | None] = {
                    "alpha": {"prefix_mode": "both", "prefix_symbol": "gr!"}
                }

            def prefix_config_sync(self, name: str) -> dict[str, Any] | None:
                return self.cache.get(name)

        access = StubAccess()
        factory = RealClientFactory(
            # duck-typed credentials double: only prefix metadata is read
            NS(self_command_prefix="sm!", owner_user_ids=[]),  # type: ignore[arg-type]
            command_prefix="sm!",
            access_manager=access,
        )
        client = factory("alpha", "tok")
        self.assertIsNotNone(client._prefix_provider)
        # the snapshot seeded from the same cache at build time
        self.assertEqual((client._prefix_mode, client._prefix_symbol), ("both", "gr!"))
        # the provider serves the LIVE view
        self.assertEqual(client._current_prefix_config(), ("both", "gr!"))
        access.cache["alpha"] = {"prefix_mode": "symbol", "prefix_symbol": "!!"}
        self.assertEqual(client._current_prefix_config(), ("symbol", "!!"))


if __name__ == "__main__":
    unittest.main()
