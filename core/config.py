"""Project configuration loading (config/ directory, all optional).

Files: ``global_settings.json`` (plugin setting overrides — layer 2 of
the settings merge, account layer wins), ``rate_limits.json`` (the policy
format below), and ``accounts/<name>.json`` (one account bootstrap per
file; never tokens — only the ``.env`` key reference).

Policy file shape::

    {"global":    {action: {"interval": 2.0, "jitter": [0.1, 0.5]}},
     "accounts": {name: {action: {...}}},
     "lane":      {"enabled": true, "prefixrate": 1.5}}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

LOG = logging.getLogger("slaughtermudae.config")

#: conservative defaults when rate_limits.json is absent (spec example:
#: base interval 2.0s, random jitter 0.0-0.5s; floor is 1.5s everywhere)
DEFAULT_POLICIES: Dict[str, Dict[str, Any]] = {
    "global": {
        "message": {"interval": 2.0, "jitter": [0.0, 0.5]},
        "dm": {"interval": 2.0, "jitter": [0.0, 0.5]},
    },
    "accounts": {},
}


@dataclass
class ProjectConfig:
    """Everything the framework needs besides credentials + the database."""

    global_settings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    policies: Dict[str, Any] = field(default_factory=lambda: _deep_copy(DEFAULT_POLICIES))
    accounts: List[Dict[str, Any]] = field(default_factory=list)


def _deep_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))  # tolerate a BOM
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must contain a JSON object, got {type(parsed).__name__}")
    return parsed


def _skip_doc_keys(data: dict) -> dict:
    """Drop ``_comment``-style documentation keys (typo traps at boot)."""
    return {k: v for k, v in data.items() if not (isinstance(k, str) and k.startswith("_"))}


def load_config(config_dir: str | Path) -> ProjectConfig:
    """Load the config dir; malformed pieces fail LOUD (typo protection)."""
    base = Path(config_dir)
    global_settings = _skip_doc_keys(_read_json(base / "global_settings.json"))

    policies = _deep_copy(DEFAULT_POLICIES)
    raw_policies = _skip_doc_keys(_read_json(base / "rate_limits.json"))
    lane = raw_policies.pop("lane", None) or {}
    if lane and not isinstance(lane, dict):
        raise ValueError(
            "config/rate_limits.json: 'lane' must be a JSON object "
            f"(got {type(lane).__name__})")
    if "global" in raw_policies or "accounts" in raw_policies:
        glob = raw_policies.get("global") or {}
        accts = raw_policies.get("accounts") or {}
        if not isinstance(glob, dict) or not isinstance(accts, dict):
            raise ValueError(
                "rate_limits.json: 'global' and 'accounts' must be JSON objects")
        policies["global"].update(glob)
        for account, actions in accts.items():
            if actions is not None and not isinstance(actions, dict):
                raise ValueError(
                    f"rate_limits.json: policies for account {account!r} "
                    "must be a JSON object")
            policies["accounts"].setdefault(account, {}).update(actions or {})
    else:  # tolerate a flat {action: {...}} file -> global level
        policies["global"].update(raw_policies)
    if isinstance(lane, dict) and lane:
        # server-wide sequential send lane config (kv runtime state wins on
        # later boots; this is the first-boot baseline)
        try:
            prefixrate = float(lane.get("prefixrate", 1.5))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"rate_limits.json: lane.prefixrate must be a number, "
                f"got {lane.get('prefixrate')!r}") from exc
        policies["lane"] = {
            "enabled": bool(lane.get("enabled", False)),
            "prefixrate": prefixrate,
        }

    accounts: List[Dict[str, Any]] = []
    accounts_dir = base / "accounts"
    if accounts_dir.is_dir():
        seen = set()
        for path in sorted(accounts_dir.glob("*.json")):
            data = _read_json(path)
            if not isinstance(data, dict) or "name" not in data:
                LOG.warning("skipping account file without 'name': %s", path)
                continue
            if data["name"] in seen:
                LOG.warning("skipping duplicate account %r from %s", data["name"], path)
                continue
            seen.add(data["name"])
            accounts.append(_skip_doc_keys(data))
    config = ProjectConfig(
        global_settings=global_settings, policies=policies, accounts=accounts
    )
    LOG.info("config loaded: %d account file(s)", len(accounts))
    return config


__all__ = ["ProjectConfig", "load_config", "DEFAULT_POLICIES"]
