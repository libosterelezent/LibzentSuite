"""Plugin Manager (spec §2): discovery, per-account instances, dispatch.

Public surface: PluginManager (+ PluginRuntime). The loader lives in
``core.plugin_manager.loader`` (PluginLoader/PluginRecord) and the plugin
contract in ``core.plugin_base`` (PluginBase/PluginContext/PluginServices).
"""
from .loader import PluginLoader, PluginRecord
from .manager import PluginManager, PluginRuntime

__all__ = [
    "PluginManager", "PluginRuntime", "PluginLoader", "PluginRecord",
]
