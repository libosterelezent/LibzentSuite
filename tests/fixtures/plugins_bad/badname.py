"""Fixture with an ILLEGAL name — loader must reject it with PluginError."""
from __future__ import annotations

from core.plugin_base import PluginBase


class BadNamePlugin(PluginBase):
    name = "BadName!"  # violates [a-z0-9_-]+
    description = "must never load"
