"""Fixture plugin with an invalid (non-lowercase) name; loader must reject it."""
from core.plugin_base import PluginBase


class BadPlugin(PluginBase):
    name = "Bad"
    version = "0.0.0"
    description = "invalid name fixture"
