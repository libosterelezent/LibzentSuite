"""Test package: process-wide isolation for the self-command hub.

``COMMAND_HUB`` is a module-level singleton whose late-bound seams
(``access``, ``pacing``, ``api_provider``) the bootstrap — and a number
of tests — assign. Anything left behind leaked into whichever test ran
next (a stale ``api_provider`` rerouted ``prefixrate`` through another
test's ControlAPI), making failures depend on execution order. Every
test now runs between a snapshot and a restore of those seams.
"""

import unittest
from collections.abc import Callable
from typing import Any, TypeVar

_R = TypeVar("_R")

_SEAMS = ("access", "pacing", "api_provider")


def _isolate_hub(run: Callable[..., _R]) -> Callable[..., _R]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> _R:
        try:
            from core.plugin_manager.commands import COMMAND_HUB
        except Exception:  # production tree not importable: nothing to guard
            return run(self, *args, **kwargs)
        saved = {name: getattr(COMMAND_HUB, name, None) for name in _SEAMS}
        try:
            return run(self, *args, **kwargs)
        finally:
            for name, value in saved.items():
                setattr(COMMAND_HUB, name, value)

    # ``__wrapped__`` is the marker the class-patch loop below checks;
    # function objects have no such attribute in their stubs, so it is
    # planted through ``__dict__`` (same instance slot setattr uses).
    wrapper.__dict__["__wrapped__"] = run
    return wrapper


# ``type[Any]`` keeps mypy from flagging the (deliberate) reassignment
# of ``run`` on the stock unittest classes as a method-assign violation.
_CASE_CLASSES: tuple[type[Any], ...] = (unittest.TestCase, unittest.IsolatedAsyncioTestCase)

for _cls in _CASE_CLASSES:
    if "run" in vars(_cls) and not hasattr(vars(_cls)["run"], "__wrapped__"):
        _cls.run = _isolate_hub(vars(_cls)["run"])
