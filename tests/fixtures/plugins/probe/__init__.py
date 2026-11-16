"""Probe fixture plugin: the framework's reference test subject.

Minimal but complete: a settings manifest (str + bounded float), a
declared self-command (``probeinfo``) and lifecycle bookkeeping. It
backs the loader/manager/command-registry/isolation suites now that
the framework ships with no bundled plugins.
"""
from .plugin import ProbePlugin

__all__ = ["ProbePlugin"]
