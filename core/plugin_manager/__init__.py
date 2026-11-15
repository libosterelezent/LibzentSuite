"""Plugin Manager (spec §2): discovery, per-account instances, dispatch.

Public surface: PluginManager (+ PluginRuntime). The loader lives in
``core.plugin_manager.loader`` (PluginLoader/PluginRecord), the plugin
contract in ``core.plugin_base`` (PluginBase/PluginContext/PluginServices/
CommandSpec/CommandContext) and the per-account self-command registry in
``core.plugin_manager.commands`` (SelfCommandHub + framework pg/say/help).
"""

from .commands import (
    COMMAND_HUB,
    FRAMEWORK_COMMANDS,
    FRAMEWORK_PLUGIN,
    RegisteredCommand,
    SelfCommandHub,
)
from .loader import PluginLoader, PluginRecord
from .manager import PluginManager, PluginRuntime

__all__ = [
    "COMMAND_HUB",
    "FRAMEWORK_COMMANDS",
    "FRAMEWORK_PLUGIN",
    "PluginLoader",
    "PluginManager",
    "PluginRecord",
    "PluginRuntime",
    "RegisteredCommand",
    "SelfCommandHub",
]
