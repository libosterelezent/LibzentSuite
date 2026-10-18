"""Test package: process-wide isolation for the self-command hub.

``COMMAND_HUB`` is a module-level singleton whose late-bound seams
(``access``, ``pacing``, ``api_provider``) the bootstrap — and a number
of tests — assign. Anything left behind leaked into whichever test ran
next (a stale ``api_provider`` rerouted ``prefixrate`` through another
test's ControlAPI), making failures depend on execution order. Every
test now runs between a snapshot and a restore of those seams.
"""
import unittest

_SEAMS = ("access", "pacing", "api_provider")


def _isolate_hub(run):
    def wrapper(self, *args, **kwargs):
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
    wrapper.__wrapped__ = run
    return wrapper


for _cls in (unittest.TestCase, unittest.IsolatedAsyncioTestCase):
    if "run" in vars(_cls) and not hasattr(vars(_cls)["run"], "__wrapped__"):
        _cls.run = _isolate_hub(vars(_cls)["run"])
