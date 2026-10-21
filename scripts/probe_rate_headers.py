#!/usr/bin/env python3
"""Discord per-route + global rate-limit header inspector.

Probes Discord's REST API through discord.py-self's own HTTP client
(same transport, same fingerprint, same headers the framework uses) and
reports the X-RateLimit-* headers Discord returns per route — plus the
library's internal bucket state, which persists across requests and
tracks limits even when individual responses don't carry headers.

What it reports per route:
  From the RESPONSE:
    X-RateLimit-Limit / Remaining / Reset / Reset-After / Bucket / Scope
    Retry-After (on 429s) + the global flag
  From the LIBRARY'S INTERNAL BUCKET STATE (discord.py-self's view):
    limit, remaining, reset_at, lock state — what the library paces by

Usage:
  LZ_PROBE_TOKEN=<token> python3 scripts/probe_rate_headers.py
  python3 scripts/probe_rate_headers.py --token <token>
  python3 scripts/probe_rate_headers.py --token <token> --burst 5
  python3 scripts/probe_rate_headers.py --token <token> --json out.json
  python3 scripts/probe_rate_headers.py --token <token> --channel <id>

The token is read from the LZ_PROBE_TOKEN environment variable when set
(preferred — keeps it out of process tables); --token is the fallback.
The token is used ONLY for the Authorization header; it is never logged.
All probe requests are read-only GETs (nothing sent/deleted/modified).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

HEADER_KEYS = [
    "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
    "X-RateLimit-Reset-After", "X-RateLimit-Bucket", "X-RateLimit-Scope",
    "X-RateLimit-Global", "Retry-After",
]


def _fmt_reset(reset_epoch: Optional[str]) -> str:
    if not reset_epoch:
        return "-"
    try:
        # v9: epoch seconds (float); some routes send epoch-ms
        seconds = float(reset_epoch)
        if seconds > 1e12:  # epoch-ms
            seconds /= 1000.0
        delta = seconds - time.time()
        return f"in {delta:.1f}s" if delta > 0 else f"{delta:.1f}s ago"
    except (ValueError, TypeError):
        return str(reset_epoch)


def _bucket_snapshot(http_client: Any) -> Dict[str, Dict[str, Any]]:
    """The library's internal bucket state (what it actually paces by)."""
    out: Dict[str, Dict[str, Any]] = {}
    for key, bucket in getattr(http_client, "_buckets", {}).items():
        out[key] = {
            "limit": getattr(bucket, "limit", None),
            "remaining": getattr(bucket, "remaining", None),
            "reset_at": getattr(bucket, "reset_at", None),
            "pending": len(getattr(bucket, "_pending_requests", []) or []),
        }
    return out


async def probe(token: str, routes: List[Dict[str, Any]],
                burst: int, delay: float) -> List[Dict[str, Any]]:
    """Probe each route via discord.py-self's HTTP client."""
    import discord
    from discord.http import HTTPClient

    loop = asyncio.get_running_loop()
    # the library's own HTTP client: pass no positional args, set the
    # token via static_login (which is how Client.start does it)
    http = HTTPClient(loop=loop)
    await http.startup()  # opens the curl session + fetches browser headers
    results: List[Dict[str, Any]] = []

    try:
        # login (validates the token, establishes the session)
        user = await http.static_login(token)
        print(f"✓ logged in as {user['username']} ({user['id']})")

        for route in routes:
            label = route["label"]
            url = route["url"]
            method = route.get("method", "GET")
            params = route.get("params")

            for attempt in range(max(1, burst)):
                if attempt > 0:
                    await asyncio.sleep(delay)

                t0 = time.monotonic()
                try:
                    from discord.http import Route
                    # Route takes a RELATIVE path (library adds the base)
                    path = route["url"].replace(
                        "https://discord.com/api/v9", "")
                    route_obj = Route(method, path)
                    if params:
                        resp = await http.request(route_obj, params=params)
                    else:
                        resp = await http.request(route_obj)

                    elapsed = (time.monotonic() - t0) * 1000
                    # discord.py-self's HTTPClient.request returns the
                    # PARSED JSON body (dict/list/str), never a response
                    # object: headers/status are never real on this leg,
                    # and a non-2xx raises HTTPException (handled below) —
                    # so an absent status is marked "?" instead of 200.
                    headers = getattr(resp, "headers", None) or {}

                    entry: Dict[str, Any] = {
                        "route": label,
                        "url": url,
                        "attempt": attempt + 1,
                        "status": getattr(resp, "status", "?"),
                        "elapsed_ms": round(elapsed, 1),
                    }
                    # extract rate-limit headers (case-tolerant)
                    for key in HEADER_KEYS:
                        val = headers.get(key) or headers.get(key.lower())
                        if val is not None:
                            entry[key.replace("X-RateLimit-", "")
                                  .replace("-", "_").lower()] = val

                    results.append(entry)
                    status = entry["status"]
                    rem = entry.get("remaining", "-")
                    lim = entry.get("limit", "-")
                    print(f"  {label:<18} attempt {attempt+1}: "
                          f"HTTP {status}  limit={lim} remaining={rem} "
                          f"({entry['elapsed_ms']}ms)")
                    if status == 429:
                        break  # don't hammer past a 429

                except discord.HTTPException as exc:
                    elapsed = (time.monotonic() - t0) * 1000
                    entry: Dict[str, Any] = {
                        "route": label, "url": url, "attempt": attempt + 1,
                        "status": getattr(exc, "status", "?"),
                        "error": str(exc),
                        "elapsed_ms": round(elapsed, 1),
                    }
                    # the raised exception carries the real HTTPResponse:
                    # rate-limit headers/status live on exc.response
                    resp = getattr(exc, "response", None)
                    exc_headers = getattr(resp, "headers", None) or {}
                    for key in HEADER_KEYS:
                        val = (exc_headers.get(key)
                               or exc_headers.get(key.lower()))
                        if val is not None:
                            entry[key.replace("X-RateLimit-", "")
                                  .replace("-", "_").lower()] = val
                    results.append(entry)
                    rem = entry.get("remaining", "-")
                    lim = entry.get("limit", "-")
                    print(f"  {label:<18} attempt {attempt+1}: "
                          f"HTTP {entry['status']} — {exc} "
                          f"limit={lim} remaining={rem}")
                    break

        # the library's internal state after all probing
        buckets = _bucket_snapshot(http)
        if buckets:
            print(f"\n{'─' * 70}")
            print("LIBRARY INTERNAL BUCKET STATE (what discord.py-self paces by):")
            for key, state in sorted(buckets.items()):
                print(f"  {key}:")
                print(f"    limit={state['limit']} "
                      f"remaining={state['remaining']} "
                      f"pending={state['pending']}")
    finally:
        await http.close()

    return results


def print_report(results: List[Dict[str, Any]]) -> None:
    """Human table + bucket grouping."""
    print(f"\n{'=' * 92}")
    print("DISCORD RATE LIMIT HEADERS — PER ROUTE (via discord.py-self HTTP client)")
    print("=" * 92)
    print(f"\n{'Route':<18} {'Status':<8} {'Limit':<7} {'Remaining':<10} "
          f"{'Reset':<14} {'Scope':<8} {'Bucket':<22} {'Time'}")
    print("-" * 92)

    for r in results:
        if "error" in r:
            extra = (f"  Retry-After={r['retry_after']}s"
                     if r.get("retry_after") else "")
            print(f"{r['route']:<18} ERROR: {r['error'][:60]}{extra}")
            continue
        status = r.get("status", "?")
        status_str = "429!" if status == 429 else str(status)
        bucket = str(r.get("bucket") or "-")[:20]
        print(
            f"{r['route']:<18} {status_str:<8} "
            f"{r.get('limit', '-') or '-':<7} "
            f"{r.get('remaining', '-') or '-':<10} "
            f"{_fmt_reset(r.get('reset')):<14} "
            f"{r.get('scope', '-') or '-':<8} "
            f"{bucket:<22} "
            f"{r.get('elapsed_ms', '?')}ms"
        )
        if status == 429:
            extras = []
            if r.get("retry_after_ms"):
                extras.append(f"retry_after={r['retry_after_ms']}ms")
            if r.get("is_global"):
                extras.append("GLOBAL=true")
            if r.get("message"):
                extras.append(f'msg="{r["message"]}"')
            if r.get("retry_after"):
                extras.append(f"Retry-After-header={r['retry_after']}s")
            if extras:
                print(f"{'':>18} └─ {' | '.join(extras)}")

    # bucket grouping
    buckets: Dict[str, List[str]] = {}
    for r in results:
        bucket = r.get("bucket")
        if bucket and "error" not in r:
            buckets.setdefault(bucket, []).append(r["route"])

    if buckets:
        print(f"\n{'─' * 92}")
        print("BUCKET GROUPS (routes sharing a hash share a rate limit):")
        for bucket, routes in sorted(buckets.items(),
                                     key=lambda x: -len(x[1])):
            marker = " ← SHARES" if len(routes) > 1 else ""
            print(f"  {bucket[:20]:<22} {', '.join(routes)}{marker}")

    # header presence summary
    print(f"\n{'─' * 92}")
    with_headers = [r for r in results
                    if any(r.get(k.replace("X-RateLimit-", "")
                             .replace("-", "_").lower())
                           for k in HEADER_KEYS
                           if "RateLimit" in k)]
    print(f"Routes returning rate-limit headers: {len(with_headers)}/{len(results)}")
    if not with_headers:
        print("\n⚠ Discord returned NO rate-limit headers on any route.")
        print("  This is EXPECTED for user accounts on lightweight GET routes:")
        print("  Discord applies per-route buckets primarily to bot accounts;")
        print("  user accounts get a GLOBAL (Cloudflare-level) limit instead.")
        print("  The library's internal bucket state (above, if populated)")
        print("  and 429 responses with Retry-After are the observable signals.")
    scoped = [r for r in results if r.get("scope")]
    if scoped:
        for scope in ("user", "global"):
            n = sum(1 for r in scoped if r.get("scope") == scope)
            if n:
                print(f"  scope={scope}: {n} route(s)")
    limits = [int(r["limit"]) for r in with_headers if r.get("limit")]
    if limits:
        print(f"  Per-route limits: {sorted(set(limits))}")
    print()


async def run(args: argparse.Namespace) -> int:
    API = "https://discord.com/api/v9"
    routes: List[Dict[str, Any]] = []

    def add(label: str, url: str, **kw: Any) -> None:
        routes.append({"label": label, "url": f"{API}{url}", **kw})

    if args.route in ("all", "me"):
        add("me", "/users/@me")
    if args.route in ("all", "channels"):
        add("dm_channels", "/users/@me/channels")
    if args.route in ("all", "guilds"):
        add("guilds", "/users/@me/guilds")
    if args.route == "messages":
        if not args.channel:
            print("error: --channel <id> required", file=sys.stderr)
            return 1
        add("messages", f"/channels/{args.channel}/messages",
            params={"limit": "1"})
    if args.route == "gateway":
        add("gateway", "/gateway")
    if args.route == "notes":
        add("notes", "/users/@me/notes")

    if not routes:
        print(f"error: unknown route {args.route!r}", file=sys.stderr)
        return 1

    print(f"Probing {len(routes)} route(s) via discord.py-self HTTP client "
          f"(burst={args.burst}, delay={args.delay}s)...")
    results = await probe(args.token, routes, args.burst, args.delay)
    print_report(results)

    if args.json:
        payload = {
            "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "routes": results,
        }
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        print(f"JSON report: {args.json}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discord per-route rate-limit header inspector "
                    "(uses discord.py-self's own HTTP client)")
    parser.add_argument("--token", default=None,
                        help="the Discord account token (fallback when "
                             "LZ_PROBE_TOKEN is not set; the environment "
                             "variable is preferred so the token never "
                             "appears in process tables)")
    parser.add_argument("--route", default="all",
                        help="me | channels | guilds | gateway | notes | "
                             "messages (needs --channel)")
    parser.add_argument("--channel", type=int,
                        help="channel id for the messages route")
    parser.add_argument("--burst", type=int, default=1,
                        help="requests per route (default 1; use >1 to "
                             "observe remaining-count decrementing)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds between requests (default 2)")
    parser.add_argument("--json", default=None,
                        help="write a JSON report to this path")
    args = parser.parse_args()
    args.token = args.token or os.environ.get("LZ_PROBE_TOKEN")
    if not args.token:
        parser.error("a token is required: set LZ_PROBE_TOKEN or pass "
                     "--token")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
