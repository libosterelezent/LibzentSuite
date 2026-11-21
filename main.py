"""LibzentSuite entrypoint.

Usage:
  python main.py                        interactive REPL (live accounts from config/)
  python main.py <command ...>          one-shot CLI (e.g. `accounts list`, `status`)
  python main.py --sim [name]           run a simulation scenario locally, no network
  python main.py --sim-list             list available scenarios
  python main.py --start-all            boot every enabled account, then REPL
  python main.py --headless             run forever with no terminal (daemon mode)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from core.bootstrap import build


async def run_sim(
    name: str | None, list_only: bool, plugin_dir: str = "tests/fixtures/sim_plugins"
) -> int:
    # sim callers default to the workload fixture (the live default is
    # dispatched in main(); this entry is simulation-only)
    """Run scenario(s) through the offline SimulationRunner (no network).

    The default plugin dir is the SIM workload fixture (where the
    scenario exporter lives), not ./plugins — the live plugin directory
    carries no simulator workload, so every scenario failed there
    (found by the live-debugging toolkit's first pass, 2026-11-17);
    --plugins still overrides for custom runs."""
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
        # telemetry runs live (also available via LZ_DB_TELEMETRY=1 alone).
        logging.getLogger("libzent").setLevel(logging.DEBUG)
        logging.getLogger("discord").setLevel(logging.INFO)
        os.environ.setdefault("LZ_DB_TELEMETRY", "1")
        # the TELEMETRY singleton read the env var at IMPORT time (before
        # this point), so setting the variable alone enabled nothing
        from database.telemetry import TELEMETRY

        TELEMETRY.enabled = True
        logging.getLogger("libzent").info(
            "DEBUG MODE: libzent tree at DEBUG, discord at INFO, DB worker telemetry enabled"
        )
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
    exit_code = 0
    try:
        if args.start_all:
            try:
                await api.accounts_start()
            except Exception as exc:
                print(f"live mode unavailable: {exc}", file=sys.stderr)
                return 2  # the finally below still shuts the stack down

        if argv:  # one-shot command mode
            from interfaces.cli import run_once

            loop = asyncio.get_running_loop()
            task = asyncio.ensure_future(run_once(api, argv))

            def _sig_stop() -> None:
                # a service manager's stop is SIGTERM: cancel the command so
                # the finally below still tears the whole stack down
                task.cancel()

            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.add_signal_handler(sig, _sig_stop)
            try:
                exit_code = await task
            except asyncio.CancelledError:
                exit_code = 130
            finally:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                        loop.remove_signal_handler(sig)
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
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.add_signal_handler(sig, _sig_exit, code)
            try:
                await repl.run()
            finally:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                        loop.remove_signal_handler(sig)
    finally:
        # ALWAYS runs (start-all failure, Ctrl-C...): a leaked database
        # connection/worker thread can hang process exit
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
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)


def main() -> int:
    """
    Entrypoint: parse argv and dispatch to the simulation runner
    (``--sim``/``--sim-list``) or the live app (REPL / one-shot CLI /
    dashboard / headless). Returns a process exit code (130 on Ctrl-C).
    """
    parser = argparse.ArgumentParser(
        prog="libzent", description="LibzentSuite automation framework"
    )
    parser.add_argument(
        "--sim",
        metavar="NAME",
        const="",
        default=None,
        nargs="?",
        help="run a simulation scenario (no network); omit NAME to list",
    )
    parser.add_argument("--sim-list", action="store_true", help="list scenarios")
    parser.add_argument(
        "--bench",
        metavar="SCENARIO",
        const="",
        default=None,
        nargs="?",
        help="run the framework benchmark (isolated temp "
        "state, fakes only); omit SCENARIO for all; "
        "pass '--bench --list' to list; extra flags "
        "pass through to the bench CLI",
    )
    parser.add_argument("--config", default="config", help="config directory (default: ./config)")
    parser.add_argument(
        "--plugins",
        default=None,
        help="plugin directory override (default: the simulator workload"
        " fixture in --sim mode, ./plugins for live startup)",
    )
    parser.add_argument("--env", default=".env", help="credentials file (default: ./.env)")
    parser.add_argument(
        "--start-all", action="store_true", help="start every enabled account on boot"
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="run forever with no terminal (daemon mode; SIGTERM for graceful stop)",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="debugging mode: DEBUG-level structured logs "
        "(dashboard Logs page), discord gateway INFO "
        "lines (reconnect/close-code visibility), DB "
        "worker telemetry, scheduler pressure in "
        "status",
    )
    parser.add_argument("--db", default="state/libzent.db", help="sqlite persistence file")
    args, extra = parser.parse_known_args()

    # the plugin-dir default is MODE-DISPATCHED (CodeRabbit PR review
    # 2026-11-17): simulation wants the workload fixture, live startup
    # wants the production directory — one shared default leaked the
    # fixture into --start-all and every live command
    if args.plugins is None:
        args.plugins = (
            "tests/fixtures/sim_plugins" if (args.sim is not None or args.sim_list) else "plugins"
        )

    if args.sim is not None or args.sim_list:
        if args.start_all or args.headless or extra or args.db != "state/libzent.db":
            print(
                "error: --sim/--sim-list cannot be combined with live-stack "
                "flags (--start-all/--dashboard/--headless/--db or a command)",
                file=sys.stderr,
            )
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
        owned = {
            "--sim",
            "--sim-list",
            "--bench",
            "--config",
            "--plugins",
            "--env",
            "--start-all",
            "--headless",
            "--db",
            "--debug",
        }
        # parse_known_args already consumed the main parser's flags; every
        # remaining token belongs to the bench CLI — pass verbatim
        bench_argv.extend(tok for tok in extra if tok.split("=", 1)[0] not in owned)
        return run_bench_cli(bench_argv)
    try:
        return asyncio.run(run_app(args, extra))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
