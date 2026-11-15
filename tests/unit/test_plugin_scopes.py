"""plugin_scopes tests - positive channel-set scope."""
import unittest

from core.plugin_scopes import channel_allowed, validate


class ValidateTests(unittest.TestCase):
    def test_none_is_unrestricted(self):
        self.assertIsNone(validate(None))

    def test_empty_set_normalizes(self):
        self.assertEqual(validate({"channels": []}), {"channels": []})

    def test_positive_set_round_trips(self):
        out = validate({"channels": ["456", "787", "456"]})
        self.assertEqual(out["channels"], ["456", "787", "456"])

    def test_ids_are_stripped(self):
        out = validate({"channels": [" 456 "]})
        self.assertEqual(out["channels"], ["456"])

    def test_input_not_mutated(self):
        scope = {"channels": ["456"]}
        validate(scope)
        self.assertEqual(scope["channels"], ["456"])

    def test_channels_must_be_list(self):
        with self.assertRaises(ValueError):
            validate({"channels": "456"})

    def test_channel_ids_must_be_strings(self):
        with self.assertRaises(ValueError):
            validate({"channels": [456]})

    def test_channel_ids_must_be_non_empty(self):
        with self.assertRaises(ValueError):
            validate({"channels": [""]})

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            validate({"channels": ["456"], "mode": "deny"})


class ChannelAllowedTests(unittest.TestCase):
    SCOPE = {"channels": ["456", "787"]}

    def test_none_scope_allows_everything(self):
        self.assertTrue(channel_allowed(None, "1", "999"))

    def test_listed_channel_allowed(self):
        self.assertTrue(channel_allowed(self.SCOPE, "1", "456"))

    def test_unlisted_channel_denied(self):
        self.assertFalse(channel_allowed(self.SCOPE, "1", "999"))

    def test_int_and_str_ids_interchangeable(self):
        self.assertTrue(channel_allowed(self.SCOPE, 1, 456))
        self.assertFalse(channel_allowed(self.SCOPE, 1, 999))

    def test_guild_is_not_part_of_the_set(self):
        """Only the channel id decides - guild scoping lives in the key."""
        self.assertTrue(channel_allowed(self.SCOPE, "999", "456"))

    def test_malformed_scope_fails_open(self):
        self.assertTrue(channel_allowed({"mode": "deny"}, "1", "999"))


if __name__ == "__main__":
    unittest.main()
