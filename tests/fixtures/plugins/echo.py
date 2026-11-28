"""Second fixture plugin (single-FILE form) for discovery/multi-plugin tests."""

from __future__ import annotations

from libzentsuite.core.plugin_base import PluginBase


class EchoPlugin(PluginBase):
    name = "echo"
    version = "0.1.0"
    description = "single-file fixture plugin"


ECHO_MARKER = "echo-module-loaded"
