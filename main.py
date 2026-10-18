"""SlaughterMudae entrypoint.

Usage:
  python main.py                        interactive REPL (live accounts from config/)
  python main.py <command ...>          one-shot CLI (e.g. `accounts list`, `status`)
  python main.py --sim [name]           run a simulation scenario locally, no network
  python main.py --sim-list             list available scenarios
  python main.py --start-all            boot every enabled account, then REPL
  python main.py --dashboard --port N   also serve the browser dashboard (+WS)
  python main.py --headless             run forever with no terminal (daemon mode)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from core.bootstrap import build


async def run_sim(name: str | None, list_only: bool,
                  plugin_dir: str = "plugins") -> int:
    """Run scenario(s) through the offline SimulationRunner (no network)."""
    from adapters.simulator import SCENARIOS, SimulationRunner

    if list_only:
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<22}{scenario.description}")
        return 0
    runner = SimulationRunner(plugin_dir=plugin_dir)
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
    print(report["summary"])
    for line in report["log_lines"]:
        print(f"  | {line}")
    for failure in report["failures"]:
        print(f"  FAILED: {failure}")
    return 0 if report["passed"] else 1


async def run_app(args: argparse.Namespace, argv: list[str]) -> int:
    """Boot the live stack and attach the requested surfaces."""
    if getattr(args, "debug", False):
        # DEBUGGING MODE: structured DEBUG records reach the bridge (the
        # Logs page filters by severity client-side), the discord library's
        # gateway lines surface at INFO (reconnect/close-code visibility —
        # heartbeats would drown everything at DEBUG), and the DB worker
        # telemetry runs live (also available via SM_DB_TELEMETRY=1 alone).
        logging.getLogger("slaughtermudae").setLevel(logging.DEBUG)
        logging.getLogger("discord").setLevel(logging.INFO)
        os.environ.setdefault("SM_DB_TELEMETRY", "1")
        # the TELEMETRY singleton read the env var at IMPORT time (before
        # this point), so setting the variable alone enabled nothing
        from database.telemetry import TELEMETRY
        TELEMETRY.enabled = True
        logging.getLogger("slaughtermudae").info(
            "DEBUG MODE: slaughtermudae tree at DEBUG, discord at INFO, "
            "DB worker telemetry enabled")
    try:
        framework = await build(
            env_file=args.env,
            config_dir=args.config,
            db_path=args.db,
            plugin_dir=args.plugins,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        # boot-time USER errors (unwritable --db path, malformed config,
        # failed migration): one clean line + exit 2, never a raw traceback
        print(f"error: cannot boot: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    api = framework.api
    dashboard = None
    exit_code = 0
    try:
        if args.start_all:
            try:
                await api.accounts_start()
            except Exception as exc:
                print(f"live mode unavailable: {exc}", file=sys.stderr)
                return 2  # the finally below still shuts the stack down

        if args.dashboard:
            from interfaces.dashboard import DashboardServer

            if api.dashboard_token is None:
                # FINAL-PHASE (F7): the dashboard would run WITHOUT auth
                # (loopback + credential routes stay locked, but start/
                # stop/estop are drivable by any local process).
                print("WARNING: dashboard starting WITHOUT auth on "
                      "127.0.0.1 — set DASHBOARD_TOKEN in .env; "
                      "credential management stays locked", flush=True)
            try:
                dashboard = DashboardServer(
                    api, token=api.dashboard_token, port=args.port,
                    bus=framework.bus, exit_signal=True)
                dashboard.start().attach_logs(api.logs_service())
                if api.audit_recorder() is not None:
                    dashboard.attach_audit(api.audit_recorder())
            except (OSError, OverflowError, ValueError) as exc:
                # bind failures: port in use / out of range (0-65535) —
                # a clean line + exit 2, never a raw bind traceback
                print(f"error: cannot start dashboard on port {args.port}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                return 2
            print(f"dashboard: {dashboard.url}")

        if argv:  # one-shot command mode
            from interfaces.cli import run_once

            loop = asyncio.get_running_loop()
            task = asyncio.ensure_future(run_once(api, argv))

            def _sig_stop() -> None:
                # a service manager's stop is SIGTERM: cancel the command so
                # the finally below still tears the whole stack down
                task.cancel()

            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, _sig_stop)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            try:
                exit_code = await task
            except asyncio.CancelledError:
                exit_code = 130
            finally:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    try:
                        loop.remove_signal_handler(sig)
                    except (NotImplementedError, RuntimeError, ValueError):
                        pass
        elif args.headless:
            print("running headless — Ctrl-C / SIGTERM to stop")
            await _wait_for_stop_signal()
        else:
            from interfaces.cli import Repl

            stop = asyncio.Event()
            repl = Repl(api, stop_event=stop)

            def _sig_exit(code: int) -> None:
                # SIGTERM/SIGINT end the REPL cleanly (a service manager's
                # stop is SIGTERM — the default disposition would kill the
                # process with NO teardown: queues dropped, DB unclosed)
                nonlocal exit_code
                exit_code = code
                stop.set()
                repl.running = False

            loop = asyncio.get_running_loop()
            for sig, code in ((signal.SIGTERM, 0), (signal.SIGINT, 130)):
                try:
                    loop.add_signal_handler(sig, _sig_exit, code)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            try:
                await repl.run()
            finally:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    try:
                        loop.remove_signal_handler(sig)
                    except (NotImplementedError, RuntimeError, ValueError):
                        pass
    finally:
        # ALWAYS runs (start-all failure, dashboard bind error, Ctrl-C...):
        # a leaked database connection/worker thread can hang process exit
        if dashboard is not None:
            try:
                dashboard.stop()
            except Exception as exc:
                print(f"warning: dashboard stop failed: {exc}", file=sys.stderr)
        await framework.shutdown()
    return exit_code


async def _wait_for_stop_signal() -> None:
    """Block until SIGINT/SIGTERM (a service manager's `stop` is SIGTERM,
    which previously killed the process WITHOUT the graceful shutdown)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):  # HUP: daemon on terminal close
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows / non-main thread: fall back to KeyboardInterrupt
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass


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
    parser.add_argument("--bench", metavar="SCENARIO", const="", default=None, nargs="?",
                        help="run the framework benchmark (isolated temp "
                             "state, fakes only); omit SCENARIO for all; "
                             "pass '--bench --list' to list; extra flags "
                             "pass through to the bench CLI")
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
    parser.add_argument("--debug", action="store_true",
                        help="debugging mode: DEBUG-level structured logs "
                             "(dashboard Logs page), discord gateway INFO "
                             "lines (reconnect/close-code visibility), DB "
                             "worker telemetry, scheduler pressure in "
                             "status")
    parser.add_argument("--db", default="state/slaughter.db", help="sqlite persistence file")
    args, extra = parser.parse_known_args()

    if args.sim is not None or args.sim_list:
        if (args.start_all or args.dashboard or args.headless
                or extra or args.db != "state/slaughter.db"):
            print("error: --sim/--sim-list cannot be combined with live-stack "
                  "flags (--start-all/--dashboard/--headless/--db or a command)",
                  file=sys.stderr)
            return 2
        try:
            return asyncio.run(run_sim(args.sim or None, args.sim_list, args.plugins))
        except KeyboardInterrupt:
            return 130
    if args.bench is not None:
        # isolated benchmark process: never boots the live framework
        from benchmark.cli import run_bench_cli

        bench_argv: list[str] = []
        if args.bench:
            bench_argv.extend(["--scenario", args.bench])
        # everything except the main parser's own flags passes through
        # (values included — the bench parser validates the rest)
        owned = {"--sim", "--sim-list", "--bench", "--config", "--plugins",
                 "--env", "--start-all", "--dashboard", "--headless",
                 "--port", "--db", "--debug"}
        # parse_known_args already consumed the main parser's flags; every
        # remaining token belongs to the bench CLI — pass verbatim
        bench_argv.extend(
            tok for tok in extra if tok.split("=", 1)[0] not in owned)
        return run_bench_cli(bench_argv)
    try:
        return asyncio.run(run_app(args, extra))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
