"""Fixture plugin: records lifecycle + events into class-level buffers."""
from __future__ import annotations

from typing import List, Tuple

from core.events import Event, MessageEvent
from core.plugin_base import PluginBase
from core.settings import SettingDef

CALLS: List[str] = []
EVENTS: List[Tuple[str, int, str]] = []


def reset() -> None:
    CALLS.clear()
    EVENTS.clear()


class SpyPlugin(PluginBase):
    name = "spy"
    version = "0.1.0"
    description = "test fixture plugin"

    settings = (
        SettingDef("greeting", "str", "hi", "word logged on enable"),
        SettingDef("volume", "int", 5, "bounded int", 1, 10),
        SettingDef("loud", "bool", False, "boolean toggle"),
    )

    async def on_load(self, ctx) -> None:
        CALLS.append(f"load:{ctx.account}")

    async def on_enable(self, ctx) -> None:
        CALLS.append(f"enable:{ctx.account}:{ctx.settings['greeting']}")

    async def on_disable(self, ctx) -> None:
        CALLS.append(f"disable:{ctx.account}")

    async def handle_event(self, ctx, event: Event) -> None:
        if isinstance(event, MessageEvent):
            EVENTS.append((ctx.account, event.author_id, event.content))
