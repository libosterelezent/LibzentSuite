"""Project configuration loading.

Files under config/ (all optional; sane defaults otherwise):
  * global_settings.json  : {plugin_name: {setting_key: value}}
  * rate_limits.json      : {action: "n/seconds"} global defaults for all bots
  * accounts/<name>.json  : one AccountConfig per file (never contains tokens)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .account import AccountConfig

LOG = logging.getLogger("slaughtermudae.config")

#: conservative defaults if rate_limits.json is absent
DEFAULT_RATE_LIMITS: Dict[str, str] = {
    "message": "8/12s",
    "dm": "8/12s",
}


@dataclass
class ProjectConfig:
    """Everything the framework needs besides credentials."""

    global_settings: Dict[str, Dict] = field(default_factory=dict)
    rate_rules: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_RATE_LIMITS))
    accounts: List[AccountConfig] = field(default_factory=list)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        # AUDIT FIX: non-dict JSON gets a clear error naming the file
        raise ValueError(f"{path} must contain a JSON object, got {type(parsed).__name__}")
    return parsed

def load_config(config_dir: str | Path) -> ProjectConfig:
    """Load config dir into a ProjectConfig (accounts from config/accounts/*.json)."""
    base = Path(config_dir)
    global_settings = _read_json(base / "global_settings.json")
    rate_rules = dict(DEFAULT_RATE_LIMITS)
    # LIVE-BOOT FIX: skip documentation keys ("_comment") — they would crash
    # RateRule.parse at startup; also drop every underscore-prefixed key
    for action, rule in _read_json(base / "rate_limits.json").items():
        if isinstance(action, str) and not action.startswith("_"):
            rate_rules[action] = rule
    accounts: List[AccountConfig] = []
    accounts_dir = base / "accounts"
    if accounts_dir.is_dir():
        seen_names = set()
        for path in sorted(accounts_dir.glob("*.json")):
            data = _read_json(path)
            if not isinstance(data, dict) or "name" not in data:
                LOG.warning("skipping account file without 'name': %s", path)
                continue
            account = AccountConfig.from_dict(data)
            if account.name in seen_names:
                LOG.warning("skipping duplicate account %r from %s", account.name, path)
                continue
            seen_names.add(account.name)
            accounts.append(account)
    config = ProjectConfig(global_settings=global_settings, rate_rules=rate_rules, accounts=accounts)
    LOG.info(
        "config loaded: %d account(s), rate rules %s", len(accounts), rate_rules
    )
    return config
