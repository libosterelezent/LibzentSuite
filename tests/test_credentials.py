"""Credentials: .env parsing, id accessors, redaction."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.credentials import Credentials, CredentialsError

ENV_TEXT = """
# comment line
MUDAE_BOT_ID=432610292342587392
OWNER_USER_IDS=111, 222;333
export DISCORD_TOKEN_ALPHA="tok-alpha"
DISCORD_TOKEN_BETA='tok-beta'
EMPTY=
DASHBOARD_TOKEN=local-secret
"""


class TestCredentials(unittest.TestCase):
    def test_parse_env_basics(self) -> None:
        parsed = Credentials.parse_env(ENV_TEXT)
        self.assertEqual(parsed["MUDAE_BOT_ID"], "432610292342587392")
        self.assertEqual(parsed["DISCORD_TOKEN_ALPHA"], "tok-alpha")  # quotes stripped
        self.assertEqual(parsed["DISCORD_TOKEN_BETA"], "tok-beta")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertNotIn("comment line", parsed.values())

    def test_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(ENV_TEXT, encoding="utf-8")
            creds = Credentials.from_file(path)
            self.assertEqual(creds.mudae_bot_id, 432610292342587392)

    def test_missing_file_is_empty(self) -> None:
        creds = Credentials.from_file("/nonexistent/.env")
        self.assertIsNone(creds.get("MUDAE_BOT_ID"))
        with self.assertRaises(CredentialsError):
            creds.require("MUDAE_BOT_ID")

    def test_account_tokens(self) -> None:
        creds = Credentials.from_values(Credentials.parse_env(ENV_TEXT))
        tokens = creds.account_tokens()
        self.assertEqual(tokens, {"alpha": "tok-alpha", "beta": "tok-beta"})
        self.assertEqual(creds.token_for("alpha"), "tok-alpha")
        self.assertIsNone(creds.token_for("gamma"))

    def test_owner_ids_parsing(self) -> None:
        creds = Credentials.from_values(Credentials.parse_env(ENV_TEXT))
        self.assertEqual(creds.owner_user_ids, frozenset({111, 222, 333}))
        empty = Credentials.from_values({"OWNER_USER_IDS": ""})
        self.assertEqual(empty.owner_user_ids, frozenset())

    def test_invalid_ids_raise(self) -> None:
        creds = Credentials.from_values({"MUDAE_BOT_ID": "not-a-number"})
        with self.assertRaises(CredentialsError):
            _ = creds.mudae_bot_id
        bad_owner = Credentials.from_values({"OWNER_USER_IDS": "12,abc"})
        with self.assertRaises(CredentialsError):
            _ = bad_owner.owner_user_ids

    def test_redaction_hides_tokens_keeps_ids(self) -> None:
        creds = Credentials.from_values(Credentials.parse_env(ENV_TEXT))
        safe = creds.redacted()
        self.assertEqual(safe["MUDAE_BOT_ID"], "432610292342587392")
        self.assertNotIn("tok-alpha", safe["DISCORD_TOKEN_ALPHA"])
        self.assertNotIn("local-secret", safe["DASHBOARD_TOKEN"])
        self.assertIn("tok-", safe["DISCORD_TOKEN_ALPHA"])  # partial hint only
        self.assertNotIn("tok-beta", repr(creds))


if __name__ == "__main__":
    unittest.main()
