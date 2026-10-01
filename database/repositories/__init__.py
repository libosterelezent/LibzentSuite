"""Repositories: one module per table family over ``BaseRepository``.

Import surface used by ``database.db.Database`` (and tests):

    AccountsRepository, AccountSettingsRepository, ProfilesRepository,
    PluginRepository, RatePolicyRepository, AuditRepository,
    LogRepository, TaskHistoryRepository, KvRepository
"""
from .accounts import AccountsRepository
from .base import BaseRepository, loads_tolerant
from .kv import KvRepository
from .logs import AuditRepository, LogRepository
from .plugins import PluginRepository
from .profiles import ProfilesRepository
from .rate_policies import RatePolicyRepository
from .settings import AccountSettingsRepository
from .tasks import TaskHistoryRepository

__all__ = [
    "AccountsRepository", "AccountSettingsRepository", "ProfilesRepository",
    "PluginRepository", "RatePolicyRepository", "AuditRepository",
    "LogRepository", "TaskHistoryRepository", "KvRepository",
    "BaseRepository", "loads_tolerant",
]
