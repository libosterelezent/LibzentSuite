"""Repositories: one module per table family over ``BaseRepository``.

Import surface used by ``database.db.Database`` (and tests):

    AccountsRepository, AccountSettingsRepository,
    PluginRepository, RatePolicyRepository, AuditRepository,
    LogRepository, TaskHistoryRepository, KvRepository
"""

from .account_channels import AccountChannelsRepository
from .accounts import AccountsRepository
from .base import BaseRepository, loads_tolerant
from .cache import CacheRepository
from .kv import KvRepository
from .logs import AuditRepository, LogRepository
from .plugins import PluginRepository
from .presets import PresetsRepository
from .rate_policies import RatePolicyRepository
from .settings import AccountSettingsRepository
from .signals import SignalsRepository
from .sync_groups import SyncGroupsRepository
from .tasks import TaskHistoryRepository
from .workspaces import WorkspaceRepository

__all__ = [
    "AccountChannelsRepository",
    "AccountSettingsRepository",
    "AccountsRepository",
    "AuditRepository",
    "BaseRepository",
    "CacheRepository",
    "KvRepository",
    "LogRepository",
    "PluginRepository",
    "PresetsRepository",
    "RatePolicyRepository",
    "SignalsRepository",
    "SyncGroupsRepository",
    "TaskHistoryRepository",
    "WorkspaceRepository",
    "loads_tolerant",
]
