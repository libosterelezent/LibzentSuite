"""Plugin Manager — component facade (see core/components.py)."""
from .plugin_base import PluginBase, PluginContext  # noqa: F401
from .plugin_loader import PluginLoader, PluginRecord  # noqa: F401
from .settings import SettingDef  # noqa: F401
