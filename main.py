"""SlaughterMudae entrypoint.

Usage:
  python main.py                        interactive REPL (live accounts from config/)
  python main.py <command ...>          one-shot CLI (e.g. `accounts list`, `status`)
  python main.py --tui                  (reserved) full-screen mode
  python main.py --sim [name]           run a simulation scenario locally, no network
  python main.py --sim-list             list available scenarios
  python main.py --start-all            boot every enabled account, then REPL
  python main.py --dashboard --port N   also serve the browser dashboard (+WS)
  python main.py --headless             run forever with no terminal (daemon mode)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from core.bootstrap import build


async def run_sim(name: str | None, list_only: bool) -> int:
    """Run scenario(s) through the offline SimulationRunner (no network)."""
    from adapters.simulator import SCENARIOS, SimulationRunner

    if list_only:
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<22}{scenario.description}")
        return 0
    runner = SimulationRunner()
    if name is None:
        print("available scenarios (--sim <name>):")
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<22}{scenario.description}")
        return 0
    try:
        report = await runner.run(name)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report["summary"], default=str) if False else report["summary"])
    for line in report["log_lines"]:
        print(f"  | {line}")
    for failure in report["failures"]:
        print(f"  FAILED: {failure}")
    return 0 if report["passed"] else 1


async def run_app(args: argparse.Namespace, argv: list[str]) -> int:
    """Boot the live stack and attach the requested surfaces."""
    framework = build(
        env_file=args.env,
        config_dir=args.config,
        db_path=args.db,
        plugin_dir=args.plugins,
    )
    api = framework.api

    if args.start_all:
        try:
            await api.accounts_start()
        except Exception as exc:
            print(f"live mode unavailable: {exc}", file=sys.stderr)
            return 2

    dashboard = None
    if args.dashboard:
        from interfaces.dashboard import DashboardServer

        dashboard = DashboardServer(api, token=api.dashboard_token, port=args.port,
                                     bus=framework.bus)
        dashboard.start().attach_logs(api.logs_service())
        if api.audit_recorder() is not None:
            dashboard.attach_audit(api.audit_recorder())
        print(f"dashboard: {dashboard.url}")

    exit_code = 0
    try:
        if argv:  # one-shot command mode
            from interfaces.cli import run_once

            exit_code = await run_once(api, argv)
        elif args.headless:
            print("running headless — Ctrl-C to stop")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        else:
            from interfaces.cli import Repl

            await Repl(api).run()
    finally:
        if dashboard is not None:
            dashboard.stop()
        await framework.shutdown()
    return exit_code


def main() -> int:
    """
    Entrypoint: parse argv and dispatch to the simulation runner
    (``--sim``/``--sim-list``) or the live app (REPL / one-shot CLI /
    dashboard / headless). Returns a process exit code (130 on Ctrl-C).
    """
    parser = argparse.ArgumentParser(
        prog="slaughtermudae", description="SlaughterMudae automation framework"
    )
    parser.add_argument("--sim", metavar="NAME", const="", default=None, nargs="?",
                        help="run a simulation scenario (no network); omit NAME to list")
    parser.add_argument("--sim-list", action="store_true", help="list scenarios")
    parser.add_argument("--config", default="config", help="config directory (default: ./config)")
    parser.add_argument("--plugins", default="plugins", help="plugin directory (default: ./plugins)")
    parser.add_argument("--env", default=".env", help="credentials file (default: ./.env)")
    parser.add_argument("--start-all", action="store_true",
                        help="start every enabled account on boot")
    parser.add_argument("--dashboard", action="store_true",
                        help="serve the GUI dashboard (browser + WS)")
    parser.add_argument("--headless", action="store_true",
                        help="run forever with no terminal (use with --dashboard)")
    parser.add_argument("--port", type=int, default=8070, help="dashboard port (default 8070)")
    parser.add_argument("--db", default="state/slaughter.db", help="sqlite persistence file")
    args, extra = parser.parse_known_args()

    if args.sim is not None or args.sim_list:
        return asyncio.run(run_sim(args.sim or None, args.sim_list))
    try:
        return asyncio.run(run_app(args, extra))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
