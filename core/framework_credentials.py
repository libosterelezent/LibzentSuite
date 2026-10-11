"""FrameworkCredentials — the DB-first credentials view plugins see.

Handed to plugin services as ``services.credentials`` in place of the
raw env ``Credentials``: plugins keep reading ``.owner_user_ids`` and
``.mudae_bot_id`` unchanged, but the values now come from the
admin-managed store (``AccessManager``), falling back to env when the
DB value is unset. The self-command prefix moves to the per-account
access row (see adapters/discord/client.py).
"""
from __future__ import annotations

from typing import Any, FrozenSet, List, Optional


class FrameworkCredentials:
    """Duck-typed Credentials replacement (DB-first, env fallback).

    Constructed per-access-check by the wiring with the already-resolved
    values (no I/O here — the gate paths are hot, the manager caches).
    """

    def __init__(self, *, owner_ids: List[int],
                 mudae_id: Optional[int],
                 self_command_prefix: str = "",
                 env: Any = None) -> None:
        self._owner_ids = frozenset(int(x) for x in owner_ids or ())
        self._mudae_id = mudae_id
        self.self_command_prefix = self_command_prefix
        self._env = env

    @property
    def owner_user_ids(self) -> FrozenSet[int]:
        """Admin ids (the replacement meaning of 'owners')."""
        return self._owner_ids

    @property
    def mudae_bot_id(self) -> Optional[int]:
        """The Mudae bot user id — DB setting first, env fallback."""
        if self._mudae_id is not None:
            return self._mudae_id
        if self._env is not None:
            try:
                return self._env.mudae_bot_id
            except Exception:
                return None
        return None

    def require(self, key: str) -> str:
        """Env passthrough for keys the access model doesn't own."""
        if self._env is not None:
            return self._env.require(key)
        raise KeyError(key)


__all__ = ["FrameworkCredentials"]
