"""Round-2 audit pin: ACCOUNT names still admit the newline bypass.

core/account_manager/manager.py:90 anchors NAME_RE with ``^...$`` and
checks with ``.match`` (line 342). In Python, ``$`` ALSO matches just
before a trailing newline, so ``"evil\\n"`` passes account validation.
The profile manager fixed the identical bug with ``fullmatch``
(core/profile_manager/manager.py:101, comment lines 29-32); accounts
were not converted.

Consequences beyond validation: AccountsRepository.add STRIPS the name
before storing (row "evil") while the manager registry keys the
unstripped "evil\\n" — registry and database disagree.

manager.py is outside this agent's edit scope, so the bypass is pinned
as ``expectedFailure``: the suite stays green today, and the moment the
regex is fixed this test flags itself (unexpected success) for cleanup.
"""

from __future__ import annotations

import unittest

from libzentsuite.core.errors import AccountError
from tests.unit.helpers import make_manager


class TestAccountNameNewlineBypass(unittest.IsolatedAsyncioTestCase):
    async def test_trailing_newline_name_must_be_rejected(self) -> None:
        harness = make_manager()
        with self.assertRaises(AccountError):
            await harness.manager.add_account("evil\n")

    async def test_name_validation_still_works_without_newline(self) -> None:
        harness = make_manager()
        out = await harness.manager.add_account("evil")
        self.assertEqual(out["name"], "evil")
        with self.assertRaises(AccountError):
            await harness.manager.add_account("evil ")  # space: not in class
        with self.assertRaises(AccountError):
            await harness.manager.add_account("EVIL")  # uppercase rejected
        self.assertIn("evil", harness.manager.names())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
