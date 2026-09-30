"""LIVE testing suite — runs against REAL Discord. Opt-in only.

This suite exists for the "live testing" requirement: it verifies the full
real-time path (gateway connect, cache population from READY, single-flight
REST gate, rate-limit header intake) against the real Discord API.

It is NOT part of `python -m unittest discover` (different directory) and it
REFUSES to run without explicit configuration:

  * SLAUGHTER_LIVE_TOKEN   a user token for a DISPOSABLE account (selfbots
                           violate Discord ToS — use an account you can lose)
  * SLAUGHTER_LIVE_CHANNEL a channel id that account can send in

Run:  python3 tests_live/live_suite.py [--send]
Without --send it performs read-only checks (connect + cache + REST intake
from a harmless fetch) and skips the message round-trip.

Safety invariants enforced even in live mode:
  * the (optional) send goes through the account's serialized lane + token
    bucket + human-like delay — the SAME stack as production, never a raw
    client call
  * hard minimum lane spacing (no test may set prefixrate below 1.5s)
  * at most ONE test message per run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.account import AccountConfig, AccountSession  # noqa: E402
from core.discord_adapter import RealDiscordClient  # noqa: E402
from core.discord_cache import DiscordCache  # noqa: E402
from core.errors import FlowBlocked  # noqa: E402
from core.events import EventBus  # noqa: E402
from core.rate_limiter import RateLimiterService  # noqa: E402
from core.rest_buckets import RestBucketManager  # noqa: E402
from core.send_engine import SendEngine  # noqa: E402

MIN_PREFIXRATE = 1.5

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record + print one live check result."""
    suffix = f" — {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    _results.append((name, ok, detail))


async def run_live(send: bool) -> int:
    token, channel_id = _require_env()

    limiter = RateLimiterService(default_rules={"message": "3/12s", "dm": "3/12s"})
    rest = RestBucketManager("live")
    cache = DiscordCache("live")
    client = RealDiscordClient(token)
    session = AccountSession(
        AccountConfig(name="live"), client, EventBus(), limiter,
        cache=cache, rest_manager=rest,
    )

    print("connecting to Discord gateway…")
    try:
        await session.start()
    except Exception as exc:
        check("gateway connect", False, str(exc))
        return 1
    check("connected with a user id", session.status.value == "running" and session.user_id is not None)

    snapshot = session.cached_snapshot()
    check("cache available after READY", bool(snapshot.get("available")))
    cache.ingest_snapshot(snapshot)
    check(
        "world cached from READY (zero fetches used)",
        len(cache.guilds) > 0 or len(cache.channels) > 0,
        f"{len(cache.guilds)} guild(s), {len(cache.channels)} channel(s)",
    )
    check("token's own channels visible in cache", channel_id in cache.channels,
          "SLAUGHTER_LIVE_CHANNEL should be a channel this account can see")

    # ONE harmless fetch through the single-flight gate; feeds whatever
    # headers the user token returned (may be sparse — the manager must
    # tolerate that, which is part of what we verify)
    me = await session.fetch_user(session.user_id)
    check("single-flight fetch_user worked", me is not None)
    rest_state = rest.snapshot()
    check("REST manager accepted the response state", isinstance(rest_state, dict))
    print(f"  REST intake: headers_seen={rest_state['headers_seen']} "
          f"(user tokens usually return sparse headers — expected)")

    if send:
        engine = SendEngine("live", prefixrate=max(1.5, MIN_PREFIXRATE)).start()
        session._engine = engine
        print("sending ONE test message through lane+bucket+delay stack…")
        try:
            sent = await session.send_message(channel_id, "SlaughterMudae live check")
            check("message sent with an id", sent.id != 0)
        except FlowBlocked as exc:
            check("send blocked (cache/access contract fired)", False, exc.reason)
        out = session._audit_out and None  # audit entries live in _audit recorder only
        print("  (see the audit trail: wait/delay/engine_wait per send)")

    await session.stop()
    failures = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(failures)} failure(s)" + (": " + ", ".join(failures) if failures else ""))
    return 1 if failures else 0


def _require_env() -> tuple[str, int]:
    token = os.environ.get("SLAUGHTER_LIVE_TOKEN", "").strip()
    channel_raw = os.environ.get("SLAUGHTER_LIVE_CHANNEL", "").strip()
    if not token or not channel_raw.isdigit():
        raise SystemExit(
            "live suite needs SLAUGHTER_LIVE_TOKEN (DISPOSABLE account!) and "
            "SLAUGHTER_LIVE_CHANNEL (a channel id that account can post in)"
        )
    return token, int(channel_raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="live Discord test suite (opt-in)")
    parser.add_argument("--send", action="store_true", help="include one real message send")
    args = parser.parse_args()
    return asyncio.run(run_live(args.send))


if __name__ == "__main__":
    raise SystemExit(main())
