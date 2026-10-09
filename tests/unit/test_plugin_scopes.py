"""core.plugin_scopes — the channel-scope validator/evaluator contract.

Pure unit tests: validation error messages name the problem field, and
channel_allowed pins the all/allow/deny matrix including guild grouping,
str/int id tolerance and the None/absent = no-restriction rule.
"""
from __future__ import annotations

import unittest

from core.plugin_scopes import channel_allowed, validate


class ValidateTests(unittest.TestCase):
    def test_none_scope_validates_to_none(self) -> None:
        self.assertIsNone(validate(None))

    def test_absent_mode_defaults_to_all(self) -> None:
        self.assertEqual(validate({}), {"mode": "all", "rules": []})
        self.assertEqual(validate({"rules": []}),
                         {"mode": "all", "rules": []})

    def test_mode_null_is_all(self) -> None:
        self.assertEqual(validate({"mode": None}),
                         {"mode": "all", "rules": []})

    def test_full_structure_round_trips_normalized(self) -> None:
        scope = {"mode": "deny",
                 "rules": [{"guild_id": "123",
                            "channel_ids": ["456", "787"]}]}
        self.assertEqual(validate(scope),
                         {"mode": "deny",
                          "rules": [{"guild_id": "123",
                                     "channel_ids": ["456", "787"]}]})

    def test_rules_absent_or_null_means_empty(self) -> None:
        self.assertEqual(validate({"mode": "allow"}),
                         {"mode": "allow", "rules": []})
        self.assertEqual(validate({"mode": "allow", "rules": None}),
                         {"mode": "allow", "rules": []})

    def test_input_not_mutated(self) -> None:
        scope = {"mode": "all"}
        validate(scope)
        self.assertEqual(scope, {"mode": "all"})

    # ------------------------------------------------------- rejections
    def test_non_dict_scope_rejected(self) -> None:
        for bad in ("all", 42, [], True):
            with self.assertRaises(ValueError) as ctx:
                validate(bad)
            self.assertIn("channels", str(ctx.exception))

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "whitelist"})
        self.assertIn("mode", str(ctx.exception))

    def test_non_string_mode_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": 1})
        self.assertIn("mode", str(ctx.exception))

    def test_unknown_top_level_key_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "all", "rulez": []})
        self.assertIn("rulez", str(ctx.exception))

    def test_rules_not_a_list_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow", "rules": {"guild_id": "1"}})
        self.assertIn("rules", str(ctx.exception))

    def test_rule_not_an_object_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow", "rules": ["456"]})
        self.assertIn("rules[0]", str(ctx.exception))

    def test_rule_missing_guild_id_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow", "rules": [{"channel_ids": ["1"]}]})
        self.assertIn("guild_id", str(ctx.exception))

    def test_rule_guild_id_must_be_string(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow",
                      "rules": [{"guild_id": 123, "channel_ids": []}]})
        self.assertIn("guild_id", str(ctx.exception))

    def test_channel_ids_must_be_list_of_strings(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow",
                      "rules": [{"guild_id": "1", "channel_ids": "456"}]})
        self.assertIn("channel_ids", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow",
                      "rules": [{"guild_id": "1", "channel_ids": [456]}]})
        self.assertIn("channel_ids", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow",
                      "rules": [{"guild_id": "1", "channel_ids": [""]}]})
        self.assertIn("channel_ids", str(ctx.exception))

    def test_unknown_rule_key_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate({"mode": "allow",
                      "rules": [{"guild_id": "1", "channel_ids": [],
                                 "extra": 1}]})
        self.assertIn("extra", str(ctx.exception))


class ChannelAllowedTests(unittest.TestCase):
    ALLOW = {"mode": "allow",
             "rules": [{"guild_id": "111", "channel_ids": ["aaa", "bbb"]}]}

    def test_none_scope_allows_everything(self) -> None:
        self.assertTrue(channel_allowed(None, "111", "aaa"))
        self.assertTrue(channel_allowed(None, 111, 999))

    def test_all_mode_allows_everything(self) -> None:
        scope = {"mode": "all", "rules": self.ALLOW["rules"]}
        self.assertTrue(channel_allowed(scope, "222", "zzz"))

    def test_allow_listed_channel(self) -> None:
        self.assertTrue(channel_allowed(self.ALLOW, "111", "aaa"))

    def test_allow_unlisted_channel_same_guild_denied(self) -> None:
        self.assertFalse(channel_allowed(self.ALLOW, "111", "ccc"))

    def test_allow_unlisted_guild_denied(self) -> None:
        # grouped by guild: no rule for the guild = nothing of it allowed
        self.assertFalse(channel_allowed(self.ALLOW, "222", "aaa"))

    def test_allow_empty_rules_allow_nothing(self) -> None:
        # documented semantics: an empty allow-list is NOT "allow all"
        scope = {"mode": "allow", "rules": []}
        self.assertFalse(channel_allowed(scope, "111", "aaa"))

    def test_allow_empty_channel_ids_allow_nothing(self) -> None:
        scope = {"mode": "allow",
                 "rules": [{"guild_id": "111", "channel_ids": []}]}
        self.assertFalse(channel_allowed(scope, "111", "aaa"))

    def test_deny_listed_channel_denied(self) -> None:
        scope = {"mode": "deny",
                 "rules": [{"guild_id": "111", "channel_ids": ["aaa"]}]}
        self.assertFalse(channel_allowed(scope, "111", "aaa"))

    def test_deny_unlisted_channel_allowed(self) -> None:
        scope = {"mode": "deny",
                 "rules": [{"guild_id": "111", "channel_ids": ["aaa"]}]}
        self.assertTrue(channel_allowed(scope, "111", "bbb"))

    def test_deny_unlisted_guild_allowed(self) -> None:
        scope = {"mode": "deny",
                 "rules": [{"guild_id": "111", "channel_ids": ["aaa"]}]}
        self.assertTrue(channel_allowed(scope, "222", "aaa"))

    def test_deny_empty_rules_allow_everything(self) -> None:
        scope = {"mode": "deny", "rules": []}
        self.assertTrue(channel_allowed(scope, "111", "aaa"))

    def test_int_and_str_ids_interchangeable(self) -> None:
        scope = {"mode": "allow",
                 "rules": [{"guild_id": "123", "channel_ids": ["456"]}]}
        self.assertTrue(channel_allowed(scope, 123, 456))
        self.assertTrue(channel_allowed(scope, "123", "456"))
        self.assertTrue(channel_allowed(scope, "123", 456))
        self.assertFalse(channel_allowed(scope, "124", "456"))
        self.assertFalse(channel_allowed(scope, "123", "457"))

    def test_malformed_scope_evaluates_leniently(self) -> None:
        # defensive READ path: stored junk never crashes the evaluator.
        # allow-mode stays fail-closed (unreadable rule = not listed);
        # deny-mode stays fail-open (unreadable rule = not blocked).
        self.assertTrue(channel_allowed("junk", "1", "2"))
        self.assertTrue(channel_allowed({"mode": "???"}, "1", "2"))
        self.assertFalse(
            channel_allowed({"mode": "allow", "rules": ["junk"]}, "1", "2"))
        self.assertTrue(
            channel_allowed({"mode": "deny", "rules": ["junk"]}, "1", "2"))
        self.assertTrue(
            channel_allowed({"mode": "allow",
                             "rules": [{"guild_id": "1",
                                        "channel_ids": [7]}]},
                            "1", "7"))  # odd int id still str-matches...
        self.assertFalse(
            channel_allowed({"mode": "allow",
                             "rules": [{"guild_id": "1",
                                        "channel_ids": [7]}]},
                            "1", "2"))  # ...and anything else stays denied


if __name__ == "__main__":
    unittest.main()
