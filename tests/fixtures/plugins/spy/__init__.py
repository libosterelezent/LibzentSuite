"""Spy fixture plugin: records every event/hook call ON THE INSTANCE.

State is per-instance (each (account, plugin) pair gets a fresh object), so
tests can assert routing/isolation precisely: alpha's spy and beta's spy are
different instances with different `seen`/`calls` lists.
"""
from __future__ import annotations

from typing import Any, List

from core.event_bus import Event
from core.plugin_base import PluginBase, PluginContext
from core.settings_manager import Setting


class SpyPlugin(PluginBase):
    name = "spy"
    version = "1.0.0"
    description = "records lifecycle + events"

    settings = (
        Setting("greeting", "str", "hi", "a string"),
        Setting("count", "int", 3, "a number", min_value=0, max_value=10),
        Setting("enabled_flag", "bool", True, "a bool"),
    )

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.seen: list[Any] = []

    async def on_load(self, ctx: PluginContext) -> None:
        self.calls.append("on_load")

    async def on_enable(self, ctx: PluginContext) -> None:
        self.calls.append("on_enable")

    async def on_disable(self, ctx: PluginContext) -> None:
        self.calls.append("on_disable")

    async def on_unload(self, ctx: PluginContext) -> None:
        self.calls.append("on_unload")

    async def handle_event(self, ctx: PluginContext, event: Event) -> None:
        self.seen.append(event)
        if getattr(event, "content", "") == "boom-spy":
            raise RuntimeError("spy exploded on purpose")
