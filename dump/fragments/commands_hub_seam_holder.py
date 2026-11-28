"""Self-command hub — the process-wide seam holder (lean, post-C2/C4).

What remains after the command convergence retired the registry and
dispatch machinery (2026-11-15): the hub is the LATE-BOUND SEAM HOLDER
the live surfaces read —

  * ``bind``/``unbind``/``bound_manager`` — the simulation runner and
    tests swap plugin managers in/out of the process binding;
  * ``api_provider`` — legacy seam, unconsumed since the framework
    verbs left the accounts (2026-11-17); retained for the dump-era
    record shape
    (bootstrap registers it right after the API is built);
  * ``access`` — the account manager's command-gate fallback (the
    invoker authorization when no direct AccessManager is wired).

The registry, the framework spec table, the executor chain, the help
renderer, and the pacing bridge moved to ``dump/`` — replaced by the
library cog surface (plugins carry their own COGs; the framework-verbs
plugins declare COG classes). See ``dump/README.md``.
"""

from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger("libzent.selfcommands")

__all__ = ["SelfCommandHub"]


class SelfCommandHub:
    """Process-wide late-bound seam registry (one framework per process).

    Tests that build several managers bind the last one (documented
    last-wins)."""

    def __init__(self) -> None:
        self._manager: Any = None
        #: optional AccessManager (bootstrap registers; the account
        #: manager's command gate falls back to it)
        self.access: Any = None
        #: zero-arg ControlAPI provider (bootstrap registers right after
        #: the API is built; unconsumed since the verbs cog retired)
        self.api_provider: Any = None

    # ---------------------------------------------------------- bindings
    def bind(self, manager: Any) -> None:
        """Bind the process's plugin manager (last-wins; tests rebind)."""
        self._manager = manager

    def unbind(self, manager: Any) -> None:
        """Drop the binding if THIS manager still owns it (shutdown)."""
        if self._manager is manager:
            self._manager = None

    @property
    def bound_manager(self) -> Any:
        """The currently bound plugin manager (None when unbound).

        Read-only view for callers that must save/restore the process
        binding around a temporary manager (the simulation runner)."""
        return self._manager


#: process-wide hub (the ONE instance; bootstrap and tests share it)
COMMAND_HUB = SelfCommandHub()
