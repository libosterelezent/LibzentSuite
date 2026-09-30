"""SlaughterMudae entrypoint.

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
from core.audit import AuditRecorder
from core.config import load_config
from core.container import AccountContainer
from core.credentials import Credentials
from core.database import Database
from core.discord_adapter import AdapterNotAvailable, RealClientFactory
from core.events import EventBus
from core.logging_setup import attach_ring_buffer, setup_logging
from core.plugin_loader import PluginLoader
from core.profiles import ProfileManager
from core.rate_limiter import RateLimiterService
from core.roles import RoleRegistry
from core.send_engine import SendPolicy
from core.settings import SettingsStore


def build_live_container(env_file: str, config_dir: str, db_path: str) -> tuple[ControlAPI, AccountContainer, AuditRecorder, Database]:
    """Assemble the live stack: credentials + config + database + container."""
    credentials = Credentials.from_file(env_file)
    config = load_config(config_dir)
    database = Database(db_path)
    database.migrate_legacy_state()  # one-time import from state/state.json
    loader = PluginLoader(f"{config_dir}/../plugins")
    loader.discover()
    settings = SettingsStore(
        global_settings=config.global_settings,
        account_overrides=database.setting_overrides(),
    )
    limiter = RateLimiterService(default_rules=config.rate_rules)
    # AUDIT FIX: restore global rules changed at runtime (persisted via kv)
    for key, value in database.kv_prefix("global_rule:").items():
        limiter.set_default_rule(key[len("global_rule:"):], value)
    for account, rules in database.limit_overrides().items():
        for action, rule in rules.items():
            limiter.set_account_rule(account, action, rule)
    delays = database.delays()
    for action, pair in delays["global"].items():
        limiter.set_delay(action, pair[0], pair[1])
    for account, actions in delays["accounts"].items():
        for action, pair in actions.items():
            limiter.set_delay(action, pair[0], pair[1], account)
    roles = RoleRegistry(database)
    audit = AuditRecorder(database)
    profiles = ProfileManager(database)
    container = AccountContainer(
        credentials=credentials,
        settings=settings,
        limiter=limiter,
        loader=loader,
        bus=EventBus(),
        client_factory=RealClientFactory(),
        state=database,
        roles=roles,
        audit=audit,
        # FarmLite send lane: serialize per-account sends (never concurrent),
        # spaced by the prefixrate; tunable at runtime via CLI/dashboard
        send_policy=SendPolicy(enabled=True, prefixrate=1.5),
    )
    for account in config.accounts:
        container.add_account(account)
    for account in database.load_accounts():  # DB is the runtime source of truth
        if account.name not in container.account_names():
            container.add_account(account)
    api = ControlAPI(
        container, limiter, loader, credentials, settings,
        state=database, roles=roles, audit=audit, profiles=profiles,
    )
    return api, container, audit, database


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
    api, _container, audit, _database = build_live_container(args.env, args.config, args.db)
    if args.start_all:
        try:
            await api.start()
        except AdapterNotAvailable as exc:
            print(f"live mode unavailable: {exc}", file=sys.stderr)
            return 2
    _container.start_scheduler()  # interval jobs fire through the safety stack
    if args.dashboard:
        from dashboard import DashboardServer

        server = DashboardServer(api, token=api.dashboard_token, port=args.port)
        server.start().attach_logs(ring)
        if audit is not None:
            server.attach_audit(audit)
        print(f"dashboard: {server.url}")
    if args.tui:
        from cli.tui import Tui

        await Tui(api, ring).run()
        return 0
    if args.headless:
        # BACKGROUND MODE: run forever, no terminal attached (the REPL would
        # hit EOF on stdin and shut down immediately when daemonized)
        print("running headless — Ctrl-C to stop")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        await api.stop()
        return 0
    from cli.terminal import Terminal

    await Terminal(api).run()
    return 0


def main() -> int:
    """Parse args and dispatch to sim or live mode."""
    parser = argparse.ArgumentParser(prog="slaughtermudae", description="SlaughterMudae framework")
    parser.add_argument("--sim", action="store_true", help="run the local simulation demo")
    parser.add_argument("--config", default="config", help="config directory (default: ./config)")
    parser.add_argument("--env", default=".env", help="credentials file (default: ./.env)")
    parser.add_argument("--start-all", action="store_true", help="start all configured accounts on boot")
    parser.add_argument("--dashboard", action="store_true", help="serve the GUI dashboard (browser + WS)")
    parser.add_argument("--tui", action="store_true", help="full-screen TUI with live log tailing")
    parser.add_argument("--headless", action="store_true", help="run forever with no terminal (use with --dashboard)")
    parser.add_argument("--port", type=int, default=8070, help="dashboard port (default 8070)")
    parser.add_argument("--db", default="state/slaughter.db", help="sqlite persistence file")
    args = parser.parse_args()
    if args.sim:
        return asyncio.run(run_sim())
    try:
        return asyncio.run(run_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
