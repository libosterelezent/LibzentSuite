"""MudaeScripts entrypoint.

Usage:
  python main.py --sim                 run the local simulation demo (no network)
  python main.py                       interactive terminal (live accounts from config/)
  python main.py --tui                 full-screen TUI with live log tailing
  python main.py --start-all           boot every configured account, then terminal
  python main.py --dashboard --port N  also serve the GUI dashboard (browser UI + WS)
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from core.api import ControlAPI
from core.config import load_config
from core.container import AccountContainer
from core.credentials import Credentials
from core.discord_adapter import AdapterNotAvailable, RealClientFactory
from core.events import EventBus
from core.logging_setup import attach_ring_buffer, setup_logging
from core.persistence import StateStore
from core.plugin_loader import PluginLoader
from core.rate_limiter import RateLimiterService
from core.settings import SettingsStore


def build_live_container(env_file: str, config_dir: str, state_path: str) -> tuple[ControlAPI, AccountContainer]:
    """Assemble the live stack: credentials + config + state + container + limiter."""
    credentials = Credentials.from_file(env_file)
    config = load_config(config_dir)
    state = StateStore(state_path)
    loader = PluginLoader(f"{config_dir}/../plugins")
    loader.discover()
    settings = SettingsStore(global_settings=config.global_settings)
    limiter = RateLimiterService(default_rules=config.rate_rules)
    for account, rules in state.limit_overrides().items():
        for action, rule in rules.items():
            limiter.set_account_rule(account, action, rule)
    container = AccountContainer(
        credentials=credentials,
        settings=settings,
        limiter=limiter,
        loader=loader,
        bus=EventBus(),
        client_factory=RealClientFactory(),
        state=state,
    )
    for account in config.accounts:
        container.add_account(account)
    api = ControlAPI(container, limiter, loader, credentials, settings, state=state)
    return api, container


async def run_sim() -> int:
    """Local simulation demo: prints the full soulcopy transcript."""
    from core.simulator import SimulatedCluster

    credentials = Credentials.from_file()
    try:
        mudae_id = credentials.mudae_bot_id
    except Exception:
        mudae_id = 0
        print("MUDAE_BOT_ID missing in .env — sim needs it; aborting.", file=sys.stderr)
        return 2
    cluster = SimulatedCluster(mudae_bot_id=mudae_id)
    await cluster.start()
    transcript = await cluster.run_soulcopy_transcript()
    print("--- simulation: guild channel transcript ---")
    for line in transcript:
        print(f"  | {line}")
    await cluster.stop()
    print("--- simulation finished ---")
    return 0


async def run_main(args: argparse.Namespace) -> int:
    """Wire the requested surfaces (terminal/TUI/dashboard) and run."""
    setup_logging()
    ring = attach_ring_buffer()
    api, _container = build_live_container(args.env, args.config, args.state)
    if args.start_all:
        try:
            await api.start()
        except AdapterNotAvailable as exc:
            print(f"live mode unavailable: {exc}", file=sys.stderr)
            return 2
    if args.dashboard:
        from dashboard import DashboardServer

        server = DashboardServer(api, token=api._credentials.dashboard_token, port=args.port)
        server.attach_logs(ring).start()
        print(f"dashboard: {server.url}")
    if args.tui:
        from cli.tui import Tui

        await Tui(api, ring).run()
        return 0
    from cli.terminal import Terminal

    await Terminal(api).run()
    return 0


def main() -> int:
    """Parse args and dispatch to sim or live mode."""
    parser = argparse.ArgumentParser(prog="mudae", description="MudaeScripts framework")
    parser.add_argument("--sim", action="store_true", help="run the local simulation demo")
    parser.add_argument("--config", default="config", help="config directory (default: ./config)")
    parser.add_argument("--env", default=".env", help="credentials file (default: ./.env)")
    parser.add_argument("--start-all", action="store_true", help="start all configured accounts on boot")
    parser.add_argument("--dashboard", action="store_true", help="serve the GUI dashboard (browser + WS)")
    parser.add_argument("--tui", action="store_true", help="full-screen TUI with live log tailing")
    parser.add_argument("--port", type=int, default=8070, help="dashboard port (default 8070)")
    parser.add_argument("--state", default="state/state.json", help="persisted runtime state file")
    args = parser.parse_args()
    if args.sim:
        return asyncio.run(run_sim())
    try:
        return asyncio.run(run_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
