#!/usr/bin/env python3
"""Live dashboard demo: the full management stack over the in-memory world.

Boots the simulated cluster (alpha runs soulcopy, beta idles in the sync
group), serves the browser dashboard WITH token auth, and triggers a
soulcopy flow every --interval seconds so accounts, rate-limit stats and
log lines stay visibly alive in the UI.

Everything here is the production code path (ControlAPI + DashboardServer +
ring-buffer logs) — only the Discord gateway is simulated.

Usage:
  python3 scripts/sim_server.py [--port 8070] [--interval 20] [--env .env]
  open http://127.0.0.1:8070/?token=<DASHBOARD_TOKEN from .env>
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.api import ControlAPI
from core.audit import AuditRecorder
from core.credentials import Credentials
from core.logging_setup import attach_ring_buffer, setup_logging
from core.roles import RoleRegistry
from core.simulator import SYNTHETIC_GUILD_CHANNEL, SimulatedCluster
from dashboard.server import DashboardServer


async def amain(args: argparse.Namespace) -> None:
    setup_logging(level=logging.INFO)
    ring = attach_ring_buffer()
    demo_log = logging.getLogger("slaughtermudae.demo")

    credentials = Credentials.from_file(args.env)
    try:
        mudae_id = credentials.mudae_bot_id
    except Exception as exc:
        raise SystemExit(f"cannot start demo: {exc}") from exc

    token = args.token or credentials.dashboard_token
    if not token:
        raise SystemExit(
            "no dashboard token: set DASHBOARD_TOKEN in .env (e.g. "
            "`openssl rand -hex 16 >> /dev/null` style) or pass --token"
        )

    cluster = SimulatedCluster(
        mudae_bot_id=mudae_id, plugin_dir=str(ROOT / "plugins"),
        audit=AuditRecorder(),  # live interaction trail for the Audit page
    )
    roles = RoleRegistry()  # in-memory registry for the demo
    roles.set_role("copier", ["soulcopy"], "demo role: soul copying duty")
    api = ControlAPI(
        cluster.container, cluster.limiter, cluster.loader,
        cluster.credentials, settings_store=cluster.settings, roles=roles,
        audit=cluster.audit,
    )
    server = DashboardServer(api, token=token, port=args.port)

    await cluster.start()
    server.start().attach_logs(ring)
    if cluster.audit is not None:
        server.attach_audit(cluster.audit)
    demo_log.info("dashboard live at %s (token auth ON)", server.url)
    demo_log.info("demo loop: triggering slcopy every %.0fs", args.interval)

    try:
        while True:
            await asyncio.sleep(args.interval)
            demo_log.info("demo: owner triggers slcopy")
            await cluster.owner_says("slcopy")
            await cluster.world.wait_for_quiet(SYNTHETIC_GUILD_CHANNEL, quiet=1.0, limit=30.0)
            demo_log.info(
                "demo: transcript now %d message(s) in the grind channel",
                len(cluster.world.messages_of(SYNTHETIC_GUILD_CHANNEL)),
            )
    except asyncio.CancelledError:
        pass
    finally:
        server.stop()
        await cluster.stop()
        demo_log.info("demo shut down cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(prog="sim-server", description="live dashboard demo over the simulated world")
    parser.add_argument("--port", type=int, default=8070)
    parser.add_argument("--interval", type=float, default=20.0, help="seconds between slcopy triggers")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--token", default=None, help="override DASHBOARD_TOKEN")
    args = parser.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
