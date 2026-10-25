"""Repositories: one module per table family over ``BaseRepository``.

Import surface used by ``database.db.Database`` (and tests):

    AccountsRepository, AccountSettingsRepository,
    PluginRepository, RatePolicyRepository, AuditRepository,
    LogRepository, TaskHistoryRepository, KvRepository
"""

from .accounts import AccountsRepository
from .base import BaseRepository, loads_tolerant
from .cache import CacheRepository
from .kv import KvRepository
from .logs import AuditRepository, LogRepository
from .plugins import PluginRepository
from .rate_policies import RatePolicyRepository
from .settings import AccountSettingsRepository
from .sync_groups import SyncGroupsRepository
from .tasks import TaskHistoryRepository

__all__ = [
    "AccountSettingsRepository",
    "AccountsRepository",
    "AuditRepository",
    "BaseRepository",
    "CacheRepository",
    "KvRepository",
    "LogRepository",
    "PluginRepository",
    "RatePolicyRepository",
    "SyncGroupsRepository",
    "TaskHistoryRepository",
    "loads_tolerant",
]
