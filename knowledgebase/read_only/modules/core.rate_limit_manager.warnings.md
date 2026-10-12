# Module `core.rate_limit_manager.warnings`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/rate_limit_manager/warnings.py`

```
Safety warnings for the rate-limit configuration (A4).

Taxonomy (recalibrated for Discord-driven pacing):

    below_floor  (danger) interval < the current safety floor — cannot be
                 SET through the API (UnsafePolicyError refuses it), so it
                 means a hand-edited database or a floor raised afterwards
    no_jitter    (info) jitter_max == 0 — perfectly metronomic timing looks
                 robotic; add a range (e.g. 0.20..0.80s)
    narrow_jitter(info) a jitter range exists but its spread is < 0.3s —
                 still metronome-like; widen it

Two further codes are composed in by the ENGINE (RateLimitManager.warnings,
not this module — they need the learned-default table):

    faster_than_learned (warn) a stored policy's interval is below the
                 learned zero-throttle rate for its action: it REPLACES the
                 learned default wherever it applies (deliberate
                 displacement, but worth a second look)
    default_no_jitter   (info) an action paced by the learned default has
                 zero effective jitter — metronomic default pacing

The historical "aggressive < 3s" lint and the always-present floor info
entry were REMOVED: pacing is Discord-driven (bucket-aware blocking is
the primary constraint and the floor enforces the minimum), so a
policy between the floor and 3s is a legitimate configuration, and the
floor value is surfaced by the dashboard's Safety Floor card and the
CLI status — not as a permanent "warning".

Warnings never block anything — the hard floor (UnsafePolicyError at set
time) does the blocking. Warnings are advisory, for the CLI ``ratelimit
warnings`` command and the dashboard banner.

Output shape is pinned by INTEGRATION.md: ``{"level", "code", "message"}``
with level one of ``"danger" | "warn" | "info"``, sorted danger > warn > info.
```

## Functions

- `def describe_scope(level: str, owner: Optional[str]) -> str`
  - Human description of a policy scope, e.g. ``"global"`` or ``"alpha/probe"``.
- `def evaluate_policy(policy: RatePolicy, level: str, owner: Optional[str], floor: float) -> List[dict]`
  - Advisory verdicts for ONE stored policy (never blocks anything).
- `def floor_warning(floor: float) -> dict`
  - The floor as a displayable entry (info).
- `def sort_warnings(items: Iterable[dict]) -> List[dict]`
  - Order danger first, then warn, then info (stable + deterministic).
- `def evaluate_policies(policies: Iterable[Tuple[RatePolicy, str, Optional[str]]], floor: float) -> List[dict]`
  - Review every stored policy; sorted danger > warn > info.
