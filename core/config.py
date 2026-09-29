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
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def load_config(config_dir: str | Path) -> ProjectConfig:
    """Load config dir into a ProjectConfig (accounts from config/accounts/*.json)."""
    base = Path(config_dir)
    global_settings = _read_json(base / "global_settings.json")
    rate_rules = dict(DEFAULT_RATE_LIMITS)
    rate_rules.update(_read_json(base / "rate_limits.json"))
    accounts: List[AccountConfig] = []
    accounts_dir = base / "accounts"
    if accounts_dir.is_dir():
        for path in sorted(accounts_dir.glob("*.json")):
            data = _read_json(path)
            if not isinstance(data, dict) or "name" not in data:
                LOG.warning("skipping account file without 'name': %s", path)
                continue
            accounts.append(AccountConfig.from_dict(data))
    config = ProjectConfig(global_settings=global_settings, rate_rules=rate_rules, accounts=accounts)
    LOG.info(
        "config loaded: %d account(s), rate rules %s", len(accounts), rate_rules
    )
    return config
