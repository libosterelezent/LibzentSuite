"""The three namespace validators (accounts, profiles, plugin slugs).

One source of truth for all three: PluginStateService key-scoping
correctness DEPENDS on every name class forbidding ':' and '/' — keep
that separator invariant here, in one place, if these ever change.
"""
from __future__ import annotations

import re

#: account names (core/account_manager) — may start with a digit, <= 32 chars
ACCOUNT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

#: sync group names (core/sync_group_manager) — profile-style; must never
#: contain ':'/'/' (the limiter paces groups under ``group:<name>`` keys)
GROUP_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")

#: plugin slugs (core/plugin_manager loader) — directory/file names; no cap
PLUGIN_NAME_RE = re.compile(r"[a-z0-9_-]+")


__all__ = ["ACCOUNT_NAME_RE", "GROUP_NAME_RE",
           "PLUGIN_NAME_RE"]
