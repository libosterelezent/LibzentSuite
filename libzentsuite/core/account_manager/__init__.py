"""Account manager package (A6): Account (first-class), AccountManager, DiscordCache."""

from .cache import ChannelInfo, DiscordCache, resolve_dm_channel
from .manager import Account, AccountManager, AccountRuntime, AccountStatus
from .services import AccountPluginServices, MessageWaiterRegistry, TaskAckRegistry

__all__ = [
    "Account",
    "AccountManager",
    "AccountPluginServices",
    "AccountRuntime",
    "AccountStatus",
    "ChannelInfo",
    "DiscordCache",
    "MessageWaiterRegistry",
    "TaskAckRegistry",
    "resolve_dm_channel",
]
