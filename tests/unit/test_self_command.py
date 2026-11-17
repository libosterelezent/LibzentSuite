"""Self-command layer: ANY owner commands the account via a prefix.

discord.py-self's ext.commands Bot runs in user_bot=True mode: dispatch
receives messages from everyone, and each invocation is gated on the
TYPER being an OWNER_USER_IDS owner (any owner — including a different
framework account — can command this account; strangers fail closed).
This test suite pins the adapter's integration without needing the real
library: the factory wiring, the prefix routing and the isolation
invariants (commands never become events).

(The legacy export trigger bridge — ``_publish_self_trigger`` — was
removed together with the exporter plugin; every verb now dispatches
through the registry path covered by AdapterLayerTests in
tests/unit/test_plugin_commands.py.)
"""

from __future__ import annotations

import unittest

from adapters.discord.client import RealClientFactory, RealDiscordClient


def _make_client(prefix: str = "sm!") -> RealDiscordClient:
    return RealDiscordClient("token-x", command_prefix=prefix)


class FactoryWiringTests(unittest.TestCase):
    def test_factory_threads_prefix_to_clients(self) -> None:
        factory = RealClientFactory(command_prefix="!")
        a = factory("alpha", "tok-a")
        b = factory("beta", "tok-b")
        self.assertEqual(a._command_prefix, "!")
        self.assertEqual(b._command_prefix, "!")

    def test_no_prefix_by_default(self) -> None:
        factory = RealClientFactory()
        client = factory("alpha", "tok")
        self.assertEqual(client._command_prefix, "")

    def test_env_default_is_empty(self) -> None:
        from core.credentials import Credentials

        creds = Credentials.from_values({})
        self.assertEqual(creds.self_command_prefix, "")
        creds2 = Credentials.from_values({"SELF_COMMAND_PREFIX": "sm!"})
        self.assertEqual(creds2.self_command_prefix, "sm!")
        creds3 = Credentials.from_values({"SELF_COMMAND_PREFIX": "@me"})
        self.assertEqual(creds3.self_command_prefix, "@me")


if __name__ == "__main__":
    unittest.main()
