"""Plugin Manager (spec §2): discovery, per-account instances, dispatch.

Public surface: PluginManager (+ PluginRuntime). The loader lives in
``core.plugin_manager.loader`` (PluginLoader/PluginRecord) and the
plugin contract in ``core.plugin_base`` (PluginBase/PluginContext/
PluginServices). The command face IS the plugin cog surface (owner
call 2026-11-17): framework verbs no longer ride the accounts; plugins
declare COG classes, hosted per (account, plugin) while attached. The
self-command hub that lived here until 2026-11-28 retired to
dump/fragments — access wiring is direct injection now.
"""

from .loader import PluginLoader, PluginRecord
from .manager import PluginManager, PluginRuntime

__all__ = [
    "PluginLoader",
    "PluginManager",
    "PluginRecord",
    "PluginRuntime",
]
