"""Settings Manager — component facade (see core/components.py)."""
from .settings import SettingDef, SettingValidationError, SettingsStore  # noqa: F401
from .config import load_config  # noqa: F401
