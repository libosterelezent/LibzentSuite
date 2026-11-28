"""Domain errors for the whole framework.

Grouping rules:
  * FlowBlocked family — a plugin flow for ONE account hit a dead end; the
    account's flow aborts with a human reason while everything else runs on.
  * ServerRateLimit    — Discord itself said WAIT (429 / Retry-After); raised
    by adapters, consumed by the scheduler which reports it to the limiter.
  * Config/validation  — ValueError subclasses with precise, human messages;
    both interfaces render them as plain 'error: ...' lines / 400 responses.
"""

from __future__ import annotations


# ------------------------------------------------------------------ flow
class FlowBlocked(RuntimeError):
    """A selfbot flow cannot continue, with a reason.

    Raising this aborts the current plugin flow for ONE account. Other
    accounts/plugins are unaffected (isolation is the dispatcher's job).
    """

    def __init__(self, account: str, reason: str) -> None:
        super().__init__(f"[{account}] flow blocked: {reason}")
        self.account = account
        self.reason = reason


class CacheInvalid(FlowBlocked):
    """The cached state needed for this action is gone or stale."""


class TargetGone(CacheInvalid):
    """The cached channel/target no longer exists (deleted, evicted)."""


class AccessDenied(FlowBlocked):
    """Discord refused access (permissions revoked, banned from channel)."""


class FetchRequired(FlowBlocked):
    """The target is not in cache and fetching is disabled/unavailable."""


# ------------------------------------------------------------------ server
class ServerRateLimit(RuntimeError):
    """Discord's own rate limit said WAIT (429 Retry-After / global).

    Attributes:
        retry_after: seconds Discord asked us to wait (>= 0).
        global_: True when the limit is account-wide (X-RateLimit-Global).
    """

    def __init__(self, retry_after: float, global_: bool = False) -> None:
        try:
            retry_after = float(retry_after)
        except (TypeError, ValueError):
            retry_after = 1.0
        if retry_after != retry_after or retry_after in (float("inf"), float("-inf")):
            retry_after = 1.0  # NaN/inf from a hostile header: safe default
        retry_after = max(0.0, retry_after)
        scope = "GLOBAL" if global_ else "route"
        super().__init__(f"server rate limit ({scope}) for {retry_after:.2f}s")
        self.retry_after = retry_after
        self.global_ = bool(global_)


# ------------------------------------------------------------- accounts etc.
class AccountError(RuntimeError):
    """Invalid account configuration or invalid account state."""


class PluginError(RuntimeError):
    """Plugin loading/lifecycle problem (bad module, duplicate name...)."""


class ProfileError(ValueError):
    """Invalid profile definition or assignment."""


class SyncGroupError(ValueError):
    """Invalid sync group definition or membership operation."""


class SettingsValidationError(ValueError):
    """A setting value failed schema validation (path included)."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class RatePolicyError(ValueError):
    """Malformed rate policy (bad interval/jitter/level/owner)."""


class UnsafePolicyError(RatePolicyError):
    """A policy would break the minimum-pacing safety floor (1.5s/action)."""


class SchedulerError(RuntimeError):
    """Invalid task/scheduler operation (unknown task, scheduler stopped...)."""


class DatabaseError(RuntimeError):
    """Persistence-layer failure or invalid database operation."""


class CredentialsError(RuntimeError):
    """A required credential is missing or malformed."""


class SimulationError(RuntimeError):
    """Scenario/runner problem (unknown scenario, broken script)."""


#: Domain errors render their (safe, human-written) message bare as an
#: ``error: …`` reply/line; anything else is an INTERNAL failure the
#: surface must mask (``failed internally, see logs`` — logs carry the
#: detail). NOTE: bare RuntimeError is deliberately NOT here — typed
#: domain errors that subclass RuntimeError (AccountError etc.) still
#: render. Shared by interfaces/cli/repl.py and the hub command layer
#: (each may layer its own additions on top, but never a bare
#: RuntimeError).
DOMAIN_ERRORS = (
    AccountError,
    CredentialsError,
    PluginError,
    ProfileError,
    RatePolicyError,
    SettingsValidationError,
    ValueError,
)


__all__ = [
    "DOMAIN_ERRORS",
    "AccessDenied",
    "AccountError",
    "CacheInvalid",
    "CredentialsError",
    "DatabaseError",
    "FetchRequired",
    "FlowBlocked",
    "PluginError",
    "ProfileError",
    "RatePolicyError",
    "SchedulerError",
    "ServerRateLimit",
    "SettingsValidationError",
    "SimulationError",
    "SyncGroupError",
    "TargetGone",
    "UnsafePolicyError",
]


def flow_block_reason(exc: BaseException) -> str:
    """One-line human reason for a flow abort.

    FlowBlocked (and subclasses) carry a curated ``reason``; anything else
    is rendered generically as ``"<TypeName>: <message>"``.
    """
    if isinstance(exc, FlowBlocked):
        return exc.reason
    return f"{type(exc).__name__}: {exc}"
