"""Plugin Manager (spec §2): discovery, per-account instances, dispatch.

Public surface: PluginManager (+ PluginRuntime). The loader lives in
``core/plugin_manager.loader`` (PluginLoader/PluginRecord), the plugin
contract in ``core.plugin_base`` (PluginBase/PluginContext/
PluginServices), and the process-wide seam hub in
``core.plugin_manager.commands`` (SelfCommandHub — bind/unbind +
access). The command face IS the plugin cog surface (owner call
2026-11-17): framework verbs no longer ride the accounts; plugins
declare COG classes, hosted per (account, plugin) while attached.
"""

from .commands import COMMAND_HUB, SelfCommandHub
from .loader import PluginLoader, PluginRecord
from .manager import PluginManager, PluginRuntime

__all__ = [
    "COMMAND_HUB",
    "PluginLoader",
    "PluginManager",
    "PluginRecord",
    "PluginRuntime",
    "SelfCommandHub",
]
