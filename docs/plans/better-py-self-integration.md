# better.py-self — Fork Study for LibzentSuite Integration

**Date:** 2026-11-13 · **Fork:** https://github.com/Demonic1594/better.py-self.git
**Current dependency:** `discord.py-self>=2.1.0` (upstream dolfies) · **Our adapter:** `adapters/discord/client.py` (1,700+ lines)

---

## What better.py-self adds over upstream discord.py-self

### 1. Performance (drop-in gains for our adapter)

| Feature | Impact on LibzentSuite | Our adapter change needed |
|---|---|---|
| O(1) message cache lookups (was O(n)) | Faster message dedup in `_message_already_seen` | None — internal to the library |
| No-op suppression for member/thread/presence updates | 4.4x faster gateway hot path; fewer phantom events to filter | None — we don't subscribe to these |
| Lazy `MESSAGE_UPDATE` parsing when nobody listens | Lower CPU on message edits we don't handle | None — we already gate on message-edit subscribers |
| `gc.freeze` after READY | Less GC pause during operation | None — opt-in via `gc_freeze=True` |
| `aiohttp` loads on demand | Faster cold import | None — transparent |

### 2. Memory (critical for multi-account operation)

| Feature | Impact | Our constructor change |
|---|---|---|
| `max_member_cache` (LRU eviction) | Caps per-guild member cache | Add to `discord.Client()` call |
| `max_total_member_cache` | Caps total across all guilds | Add |
| `max_thread_cache` / `max_total_thread_cache` | Caps thread memory | Add |
| `max_presence_cache` / `max_guild_presence_cache` | Caps presence data | Add |
| **Net effect** | 150k-member account: ~100 MB growing → ~38 MB capped | Constructor kwargs only |

### 3. Observability (new API surfaces we can consume)

| API | What it gives us | Where it plugs in |
|---|---|---|
| `Client.cache_stats()` → `CacheStats` | Every cache's size, caps, evictions, hit/miss ratios, dispatch backlog, event loop lag | Dashboard/System page (future FastAPI endpoint); health monitoring |
| `on_dispatch_backlog` event | Slow handler warning instead of silent accumulation | EventBus bridge → `RateLimitTriggered` or a new `DispatchBacklog` event |
| `Client.http_route_latencies()` → `Dict[str, float]` | Per-route EWMA latency — **the correct congestion signal for user accounts** (Discord queues sends server-side; no 429s until hard throttle) | Feed into `RateLimitManager` as a pacing signal; the lane governor recipe uses this |
| `Client.http_throttle_events()` + `on_http_throttle` event | Every wire-level 429 the library absorbed (sleep+retry), per route | Bridge to `RateLimitManager.report_server_limit()` — currently we only see 429s the library DOESN'T absorb |
| `Client.http_retry_events()` | 5xx/connection-error retries per route | Diagnostic; explain slow requests without 429s |

### 4. Fast restarts (cache persistence)

| Feature | Impact | Our change |
|---|---|---|
| `persistence_path` constructor kwarg | Caches snapshotted to disk on ready/close, replayed on startup — 75 ms warm replay vs seconds of member re-scraping | Add `persistence_path` to Client constructor; point to `state/<account>-cache.bin` |
| `persistence_max_age` | Stale cache expiry | Set to match our session TTL |

### 5. Rate-limit correctness (shared-scope 429s)

| Feature | Impact |
|---|---|
| Shared-scope 429s (`x-ratelimit-scope: shared`) now gate ALL routes via the global lock | The account-wide reaction bucket correctly pauses everything, not just the route that tripped |

**This matters for our `RateLimitManager`**: the library now handles shared-scope backpressure internally. Our limiter's server-cooldown logic should be updated to consume `on_http_throttle` rather than relying on catching `HTTPException` with 429 status.

### 6. Robustness

- Runtime-audited against hostile input (2,000+ mutated gateway payloads, ReDoS, zlib bombs)
- Gateway self-recovery: 5xx on resume URL → fallback to default gateway; receive timeout closes stalled socket; duplicate `GUILD_MEMBER_ADD` ignored
- **Credentials redacted in DEBUG logs** — no token fragments in ~50 MB of logs

### 7. New API surfaces (from v2.3.2)

- Soundboard (request/create/edit/delete/play sounds)
- `on_voice_channel_effect` (emoji and sound effects)
- `Message.interaction_metadata` (which command/button/modal produced a message)
- `captcha_handler` constructor kwarg (callable for solving CaptchaRequired)

---

## Constructor options we should adopt

Our current client construction (`adapters/discord/client.py`):
```python
discord.Client(max_ratelimit_timeout=MAX_RATELIMIT_TIMEOUT_S)
```

With better.py-self, the full surface:
```python
discord.Client(
    # existing
    max_ratelimit_timeout=MAX_RATELIMIT_TIMEOUT_S,
    # NEW: memory caps (LRU eviction)
    max_member_cache=500,           # per-guild (None = unlimited, upstream default)
    max_total_member_cache=5000,    # across all guilds
    max_thread_cache=200,
    max_total_thread_cache=1000,
    max_presence_cache=1000,
    max_guild_presence_cache=500,
    # NEW: fast restarts
    persistence_path=f"state/{account}-cache.bin",
    persistence_max_age=86400.0,    # 24h
    # NEW: GC freeze after READY
    gc_freeze=True,
    # NEW: observability
    dispatch_backlog_warning=500,   # warn at 500 queued events
)
```

## Event hooks we should bridge to our EventBus

| Fork event | Our EventBus event | Priority |
|---|---|---|
| `on_http_throttle(route, retry_after, scope)` | `RateLimitTriggered` (with route metadata) | HIGH — this is the real throttle signal for user accounts |
| `on_dispatch_backlog(count)` | New `DispatchBacklog` event or log warning | MEDIUM — operational visibility |
| `on_http_throttle` with `scope="shared"` | `RateLimitTriggered` + fleet-wide pause | HIGH — shared-scope is account-wide |

## Integration path

1. **Switch the dependency**: `discord.py-self>=2.1.0` → `better.py-self` (the fork uses the same package name `discord` internally, so imports are drop-in)
2. **Add constructor kwargs** for memory caps + persistence
3. **Bridge `on_http_throttle`** to `RateLimitManager.report_server_limit()`
4. **Expose `cache_stats()`** through `ControlAPI` for the future dashboard
5. **Adopt the lane governor recipe** (already validated by the fork's live rate studies — matches our learned defaults)

---

## Lane governor recipe (validated pacing)

The fork's `examples/lane_governor.py` implements the exact pacing model our `RateLimitManager` already uses, with additions we should adopt:

| Concept | Fork's implementation | Our current | Gap |
|---|---|---|---|
| Three serialized lanes (send/react/click) | Per-action-class serialized lane per account | We have per-account lanes + a global send lane | Align: per-action-class lanes |
| Validated intervals | send 2.6s, react 3.0s, click 3.0s | Our learned defaults: message/dm 2.6s, reaction 2.5s, button 3.0s | Close — reaction is 2.5 vs 3.0 |
| Latency EWMA as congestion signal | `http_route_latencies()` — queuing inflation, not 429s, is the ceiling | We don't consume latency signals | **Gap: add EWMA watch** |
| Backoff: x2 on throttle, probe-down ≤10% after 2min clean | Bounded probe-down, never below base interval | We have cooldowns but no probe-down recovery | **Gap: add probe-down** |
| Shared-scope 429 → pause ALL lanes | Account-wide cooldown | We have account cooldowns | Aligned |

---

## Compatibility check with our adapter

| Our usage | Fork compatibility |
|---|---|
| `discord.Client(max_ratelimit_timeout=...)` | ✓ Same constructor, additional kwargs optional |
| `discord.DMChannel` isinstance checks | ✓ Unchanged |
| `discord.HTTPException` handling | ✓ Unchanged |
| `discord.ext.commands` (self-command prefix) | ✓ Unchanged |
| `discord.utils.Headers` | ✓ Unchanged |
| `discord.User`, `discord.Guild`, etc. | ✓ Unchanged — fork maintains public API |
| No `Intents` (user accounts) | ✓ Same — fork still doesn't use Intents |

**Verdict: drop-in replacement.** The fork maintains the same public API; every existing import and call in our adapter works unchanged. The new features (memory caps, persistence, observability, throttle events) are all opt-in constructor kwargs or new methods.
