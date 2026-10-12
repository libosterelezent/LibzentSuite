# Module `core.rate_limit_manager.policies`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `core/rate_limit_manager/policies.py`

```
Rate policies: the 4-level policy model + validation (spec §5, A4).

A policy is a *pacing* rule, not a token bucket (the audited old limiter used
n/window token buckets; this rewrite paces actions with a fixed minimum
interval — same safety goal, simpler mental model):

    interval      minimum seconds between two consecutive fires of the same
                  action on the same pacing key
    jitter_min..jitter_max
                  extra uniformly sampled human-like delay applied AFTER each
                  grant so traffic does not look metronome-perfect

Policy levels (composition is max-interval: an account can never make itself
FASTER than a global rule, only slower — see engine.effective):

    global          owner=None                    every account, every plugin
    account         owner="<account name>"        one account, any plugin
    plugin          owner="<plugin name>"         one plugin, any account
    account_plugin  owner="<account>/<plugin>"    one account+plugin pair

Validation philosophy (ported from the audited code): every value is checked
at construction time — finite numbers (NaN previously slipped through float
comparisons and poisoned buckets), sane caps so a stray 1e9 can not wedge the
fleet, and precise human-readable RatePolicyError messages.

The *safety floor* (MIN_ACTION_INTERVAL — no policy faster than 1.5s/action)
is enforced by the manager (engine.py) at set time, not here: RatePolicy
itself only guarantees structural sanity so a malformed object can never
exist, even programmatically.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `RatePolicy` | dataclass | yes | One pacing rule: minimum interval + optional jitter window. |

### `RatePolicy`


## Functions

- `def parse_scope(level: Any, owner: Any) -> Tuple[str, Optional[str]]`
  - Validate + normalize a (level, owner) policy scope.
- `def policy_from_row(row: Union[Any, dict]) -> Tuple[str, Optional[str], RatePolicy]`
  - Convert one repository row into a validated ``(level, owner, policy)``.
