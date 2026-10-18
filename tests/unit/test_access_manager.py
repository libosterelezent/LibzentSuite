"""AccessManager + admin-gate tests: per-account command permissions,
prefix settings, admin accounts, mudae id, and the env migration."""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

from core.access_manager import (
    AccessManager, PREFIX_MODES, account_key, KEY_ADMINS,
)


class FakeKv:
    """KvRepository double: dict-backed get/set/prefix/delete."""

    def __init__(self) -> None:
        self.rows: Dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return self.rows.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self.rows[key] = value

    async def delete(self, key: str) -> bool:
        return self.rows.pop(key, None) is not None

    async def prefix(self, p: str) -> Dict[str, Any]:
        return {k: v for k, v in self.rows.items() if k.startswith(p)}


class FakeCredentials:
    """Credentials double for the migration + mudae fallback paths."""

    def __init__(self, owner_ids="", mudae_id=None):
        self._owner = owner_ids
        self._mudae = mudae_id

    @property
    def owner_user_ids(self):
        if not self._owner:
            return frozenset()
        return frozenset(int(x) for x in self._owner.split(",") if x.strip())

    @property
    def mudae_bot_id(self):
        if self._mudae is None:
            raise KeyError("MUDAE_BOT_ID")
        return int(self._mudae)


def make(kv=None, creds=None) -> AccessManager:
    return AccessManager(kv or FakeKv(), credentials=creds)


# ------------------------------------------------------------ admin list
class TestAdminIds(unittest.IsolatedAsyncioTestCase):
    async def test_empty_by_default_fail_closed(self):
        am = make()
        self.assertEqual(await am.admin_ids(), [])
        self.assertFalse(await am.is_admin_id(123))
        self.assertFalse(await am.can_invoke("alpha", 123))

    async def test_add_remove_set(self):
        am = make()
        self.assertEqual(await am.add_admin(10), [10])
        self.assertEqual(await am.add_admin(5), [5, 10])
        self.assertEqual(await am.add_admin(10), [5, 10])   # idempotent
        self.assertEqual(await am.remove_admin(10), [5])

    async def test_admin_bypasses_per_account_list(self):
        am = make()
        await am.add_admin(1)
        self.assertTrue(await am.can_invoke("alpha", 1))
        self.assertFalse(await am.can_invoke("alpha", 2))  # not on list


# ------------------------------------------------------------- migration
class TestMigration(unittest.IsolatedAsyncioTestCase):
    async def test_seeds_once_from_env(self):
        am = make(creds=FakeCredentials(owner_ids="111,222"))
        n = await am.migrate_from_credentials()
        self.assertEqual(n, 2)
        self.assertEqual(await am.admin_ids(), [111, 222])
        # second call is a no-op even if env changes
        n2 = await am.migrate_from_credentials()
        self.assertEqual(n2, 0)
        self.assertEqual(await am.admin_ids(), [111, 222])

    async def test_no_seed_when_list_exists(self):
        am = make(creds=FakeCredentials(owner_ids="111"))
        await am.set_admin_ids([999])
        await am.migrate_from_credentials()
        self.assertEqual(await am.admin_ids(), [999])

    async def test_no_env_no_admins(self):
        am = make(creds=FakeCredentials(owner_ids=""))
        n = await am.migrate_from_credentials()
        self.assertEqual(n, 0)
        self.assertEqual(await am.admin_ids(), [])


# ---------------------------------------------------- command permissions
class TestCommandPermissions(unittest.IsolatedAsyncioTestCase):
    async def test_empty_list_fails_closed(self):
        am = make()
        self.assertFalse(await am.can_invoke("alpha", 42))

    async def test_listed_id_allowed(self):
        am = make()
        await am.set_allowed_user_ids("alpha", [42, 7])
        self.assertTrue(await am.can_invoke("alpha", 42))
        self.assertTrue(await am.can_invoke("alpha", 7))
        self.assertFalse(await am.can_invoke("alpha", 99))
        self.assertFalse(await am.can_invoke("beta", 42))  # per-account

    async def test_add_remove(self):
        am = make()
        self.assertEqual(await am.add_allowed("a", 5), [5])
        self.assertEqual(await am.add_allowed("a", 3), [3, 5])
        self.assertEqual(await am.remove_allowed("a", 5), [3])

    async def test_dedup_and_sort(self):
        am = make()
        self.assertEqual(await am.set_allowed_user_ids(
            "a", [9, 3, 9, 1]), [1, 3, 9])


# --------------------------------------------------------- prefix config
class TestPrefixConfig(unittest.IsolatedAsyncioTestCase):
    async def test_default_is_mention(self):
        am = make()
        cfg = await am.prefix_config("alpha")
        self.assertEqual(cfg["prefix_mode"], "mention")
        self.assertEqual(cfg["prefix_symbol"], "")

    async def test_set_valid_modes(self):
        am = make()
        for mode, symbol in (("mention", ""), ("symbol", "sm!"),
                             ("both", "!")):
            cfg = await am.set_prefix_config("alpha", mode, symbol)
            self.assertEqual(cfg["prefix_mode"], mode)
            self.assertEqual(cfg["prefix_symbol"], symbol)

    async def test_symbol_requires_symbol(self):
        am = make()
        from core.errors import CredentialsError
        with self.assertRaises(CredentialsError):
            await am.set_prefix_config("a", "symbol", "")
        with self.assertRaises(CredentialsError):
            await am.set_prefix_config("a", "both", "")

    async def test_symbol_validation(self):
        am = make()
        from core.errors import CredentialsError
        with self.assertRaises(CredentialsError):   # too long
            await am.set_prefix_config("a", "symbol", "toolong")
        with self.assertRaises(CredentialsError):   # whitespace
            await am.set_prefix_config("a", "symbol", "a b")

    async def test_invalid_mode_rejected(self):
        am = make()
        from core.errors import CredentialsError
        with self.assertRaises(CredentialsError):
            await am.set_prefix_config("a", "bogus")

    async def test_sync_cache_warmed_by_set(self):
        am = make()
        self.assertIsNone(am.prefix_config_sync("alpha"))
        await am.set_prefix_config("alpha", "symbol", "sm!")
        self.assertEqual(am.prefix_config_sync("alpha"),
                         {"prefix_mode": "symbol", "prefix_symbol": "sm!"})


# ------------------------------------------------------------- admin flag
class TestAdminFlag(unittest.IsolatedAsyncioTestCase):
    async def test_flag_off_by_default(self):
        am = make()
        row = await am.account_view("alpha")
        self.assertFalse(row["is_admin"])

    async def test_flag_set_and_clear(self):
        am = make()
        row = await am.set_admin_flag("alpha", True)
        self.assertTrue(row["is_admin"])
        row = await am.set_admin_flag("alpha", False)
        self.assertFalse(row["is_admin"])

    async def test_admin_account_user_id_gates(self):
        am = make()
        await am.set_admin_flag("alpha", True)
        await am.set_account_user_id("alpha", 777)
        self.assertTrue(await am.is_admin_id(777))       # flagged account
        self.assertFalse(await am.is_admin_id(888))

    async def test_flag_without_user_id_does_not_gate(self):
        am = make()
        await am.set_admin_flag("alpha", True)  # no user_id recorded
        self.assertFalse(await am.is_admin_id(1))


# -------------------------------------------------------------- mudae id
class TestMudaeId(unittest.IsolatedAsyncioTestCase):
    async def test_none_when_unset_no_env(self):
        am = make()
        self.assertIsNone(await am.mudae_bot_id())

    async def test_db_value_wins(self):
        am = make(creds=FakeCredentials(mudae_id=111))
        await am.set_mudae_bot_id(222)
        self.assertEqual(await am.mudae_bot_id(), 222)

    async def test_env_fallback_when_db_unset(self):
        am = make(creds=FakeCredentials(mudae_id=111))
        self.assertEqual(await am.mudae_bot_id(), 111)

    async def test_clear_reverts_to_env(self):
        am = make(creds=FakeCredentials(mudae_id=111))
        await am.set_mudae_bot_id(222)
        await am.set_mudae_bot_id(None)
        self.assertEqual(await am.mudae_bot_id(), 111)


# ------------------------------------------------------------- overview
class TestOverview(unittest.IsolatedAsyncioTestCase):
    async def test_all_account_rows(self):
        am = make()
        await am.add_allowed("alpha", 5)
        await am.set_admin_flag("beta", True)
        rows = await am.all_account_rows()
        self.assertEqual(set(rows), {"alpha", "beta"})
        self.assertEqual(rows["alpha"]["allowed_user_ids"], [5])
        self.assertTrue(rows["beta"]["is_admin"])

    async def test_unknown_account_gets_defaults(self):
        am = make()
        row = await am.account_view("ghost")
        self.assertEqual(row["prefix_mode"], "mention")
        self.assertEqual(row["allowed_user_ids"], [])


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------- connect records user id
class TestConnectRecordsUserId(unittest.IsolatedAsyncioTestCase):
    async def test_account_manager_records_user_id_for_admin_gate(self):
        import logging
        from core.account_manager import AccountManager
        from core.plugin_manager.commands import COMMAND_HUB

        am = make()
        await am.set_admin_flag("alpha", True)
        saved, COMMAND_HUB.access = COMMAND_HUB.access, am
        try:
            stub = type("S", (), {"_logger": logging.getLogger("t")})()
            await AccountManager._record_access_user_id(stub, "alpha", 777)
            await AccountManager._record_access_user_id(stub, "alpha", None)
        finally:
            COMMAND_HUB.access = saved
        self.assertTrue(await am.is_admin_id(777))
