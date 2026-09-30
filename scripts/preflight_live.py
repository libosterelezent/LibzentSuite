#!/usr/bin/env python3
"""Go-live preflight: verify everything needed for REAL Discord mode.

Checks (in order): Python version, live client library, credentials in .env,
account configs, safety settings. Prints a GO/NO-GO checklist and the exact
launch command. NEVER prints token values.

Usage: venv/bin/python scripts/preflight_live.py
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from core.credentials import Credentials, CredentialsError  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    CHECKS.append((name, ok, detail))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="go-live preflight checks")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--config", default=str(ROOT / "config"))
    args = parser.parse_args()

    print("SlaughterMudae go-live preflight")
    print("=" * 60)

    # 1. Python
    check("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])

    # 2. live client library
    spec = importlib.util.find_spec("discord")
    lib_ok = spec is not None
    check("live client library installed (discord.py-self)", lib_ok,
          "pip install discord.py-self  # into the project venv" if not lib_ok else f"v{getattr(__import__('discord'), '__version__', '?')}")

    # 3. credentials
    creds = Credentials.from_file(args.env)
    token_ok = False
    try:
        accounts = creds.account_tokens()
        token_ok = bool(accounts)
        check("account token(s) present in .env", token_ok,
              f"accounts with tokens: {sorted(accounts) or 'NONE — fill DISCORD_TOKEN_<NAME>'}")
    except Exception as exc:
        check("account token(s) present in .env", False, str(exc))

    try:
        mudae = creds.mudae_bot_id
        check("MUDAE_BOT_ID set", True, str(mudae))
    except CredentialsError as exc:
        check("MUDAE_BOT_ID set", False, str(exc))

    owners = creds.owner_user_ids
    check("OWNER_USER_IDS set (plugins ignore commands otherwise)",
          bool(owners), f"{len(owners)} owner id(s)")

    # 4. account configs
    try:
        config = load_config(args.config)
        check("account configs load", bool(config.accounts),
              f"{len(config.accounts)} account(s): {[a.name for a in config.accounts]}")
        check("rate rules configured", bool(config.rate_rules),
              ", ".join(f"{k}={v}" for k, v in sorted(config.rate_rules.items())))
    except Exception as exc:
        check("account configs load", False, str(exc))

    # 5. dashboard auth
    dash_token = creds.dashboard_token
    check("DASHBOARD_TOKEN set (dashboard auth)", bool(dash_token),
          "recommended before exposing the dashboard" if not dash_token else "set")

    # verdict
    no_go = [name for name, ok, _ in CHECKS if not ok]
    print("=" * 60)
    if no_go:
        print(f"NO-GO — resolve {len(no_go)} item(s): {', '.join(no_go)}")
        print("\nTypical fixes:")
        print("  token missing : edit .env, set DISCORD_TOKEN_<ACCOUNT_NAME>")
        print("  library       : venv/bin/pip install discord.py-self")
        print("  accounts      : add config/accounts/<name>.json (see example.json)")
        return 1
    print("GO — launch live mode with:")
    print(f"  {ROOT}/venv/bin/python {ROOT}/main.py --start-all --dashboard")
    print("\n⚠ Reminder: selfbots violate Discord ToS — accounts may be suspended.")
    print("  Keep rules conservative (floor 1.5s/action is enforced; profiles")
    print("  'conservative'/'balanced' are the safe presets).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
