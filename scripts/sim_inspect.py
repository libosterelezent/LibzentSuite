#!/usr/bin/env python3
"""Live simulation test suite with a detailed interaction audit view.

Runs the soulcopy scenario against the in-memory world with the audit trail
ENABLED, then prints every Discord interaction the selfbot performed —
including how long the rate limiter made it wait and the human-like delay
applied — plus a safety review of the current configuration. The goal (user
spec): spot robotic interaction patterns BEFORE they get an account banned.

Everything is local: no network, no tokens, deterministic enough to diff.

Usage:
  python3 scripts/sim_inspect.py [--cycles 2] [--delay 0.5 1.5] [--rule 8/12s]
  python3 scripts/sim_inspect.py --json           # machine-readable output
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.audit import AuditRecorder, format_audit
from core.config import DEFAULT_RATE_LIMITS
from core.credentials import Credentials
from core.logging_setup import setup_logging
from core.rate_limiter import RateLimiterService
from core.simulator import SYNTHETIC_GUILD_CHANNEL, SimulatedCluster
from core.warnings import evaluate_limiter, format_warnings


async def run(args: argparse.Namespace) -> int:
    setup_logging(level=logging.INFO)
    credentials = Credentials.from_file()
    try:
        mudae_id = credentials.mudae_bot_id
    except Exception as exc:
        print(f"cannot run inspector: {exc}", file=sys.stderr)
        return 2

    audit = AuditRecorder(database=None, capacity=5000)
    limiter = RateLimiterService(default_rules={
        "message": args.rule, "dm": args.rule,
    })
    delay_max = 0.0
    if args.delay:
        limiter.set_delay("message", args.delay[0], args.delay[1])
        limiter.set_delay("dm", args.delay[0], args.delay[1])
        delay_max = args.delay[1]

    cluster = SimulatedCluster(
        mudae_bot_id=mudae_id,
        plugin_dir=str(ROOT / "plugins"),
        limiter=limiter,
        audit=audit,
        # demo pacing: the collect window must outlast the human delay applied
        # to the export send, or the sim collects zero pages (production uses
        # 10s defaults where a <=2.4s delay can never matter)
        settings_overrides={
            "soulcopy": {"dm_read_timeout": max(0.6, delay_max + 1.0)},
        },
    )
    await cluster.start()
    # the quiet window must outlast the human delay range, or we stop early
    quiet = max(0.8, (args.delay[1] if args.delay else 0.0) + 0.8)
    for cycle in range(args.cycles):
        await cluster.owner_says("slcopy")
        await cluster.world.wait_for_quiet(SYNTHETIC_GUILD_CHANNEL, quiet=quiet, limit=60.0)
    await cluster.stop()

    entries = audit.tail(5000)
    warnings = evaluate_limiter(limiter)
    if args.json:
        print(json.dumps({
            "cycles": args.cycles,
            "rule": args.rule,
            "delay": args.delay,
            "warnings": [w.as_dict() for w in warnings],
            "interactions": [e.as_dict() for e in entries],
            "transcript": cluster.world.messages_of(SYNTHETIC_GUILD_CHANNEL),
        }, indent=2))
        return 0

    print("=" * 78)
    print(f"SIMULATION INSPECTOR — {args.cycles} cycle(s), rule={args.rule}, delay={args.delay or 'off'}")
    print("=" * 78)
    print("\nDISCORD INTERACTIONS (-> outbound send, <- inbound event):")
    for line in format_audit(entries):
        print(line)
    out_sends = [e for e in entries if e.direction == "out"]
    waits = [e.wait_ms for e in out_sends if e.wait_ms > 0]
    delays = [e.delay_ms for e in out_sends if e.delay_ms > 0]
    print("\nSUMMARY:")
    print(f"  outbound sends      : {len(out_sends)}")
    print(f"  inbound events      : {len(entries) - len(out_sends)}")
    if waits:
        print(f"  limiter waits (ms)  : min {min(waits):.0f} / avg {sum(waits)/len(waits):.0f} / max {max(waits):.0f}")
    else:
        print("  limiter waits (ms)  : none (bucket never empty — try --rule 2/3s to exercise waits)")
    if delays:
        print(f"  human delays (ms)   : min {min(delays):.0f} / avg {sum(delays)/len(delays):.0f} / max {max(delays):.0f}")
    else:
        print("  human delays (ms)   : none — PASS --delay 0.5 1.5 to see them")
    print("\nSAFETY REVIEW:")
    for line in format_warnings(warnings):
        print(line)
    print("\nTRANSCRIPT (guild channel):")
    for message in cluster.world.messages_of(SYNTHETIC_GUILD_CHANNEL):
        print(f"  | {message}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="sim-inspect", description="audited local simulation run")
    parser.add_argument("--cycles", type=int, default=2, help="slcopy cycles to simulate")
    parser.add_argument("--rule", default=DEFAULT_RATE_LIMITS["message"], help="rate rule, e.g. 8/12s")
    parser.add_argument("--delay", nargs=2, type=float, metavar=("MIN", "MAX"), help="human delay range e.g. 0.5 1.5")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
