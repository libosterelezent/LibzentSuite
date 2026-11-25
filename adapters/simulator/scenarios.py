"""Scenario definitions for the offline simulation suite (spec §16).

Each :class:`Scenario` bundles everything the runner needs:
  * ``policies``  — rate policies applied BEFORE the run (interval + jitter);
    the runner clamps every interval to the 1.5s safety floor — simulations
    never configure unsafe pacing,
  * ``world_setup`` — builds the synthetic topology (users, the trigger
    channel, the scripted the primary bot responder and its pages),
  * ``script``     — drives the built stack (usually: the owner types the
    exporter fixture's trigger, then the world settles),
  * ``checks``     — pure post-mortem over (audit dicts, world) returning
    human-readable failure strings ([] == pass).

All ids are SYNTHETIC (module constants in adapters.simulator.world); real
Discord ids/tokens only ever live in .env.

Documented decisions (contract ambiguities — see docs/architecture/integration.md ground rules):
  * Retry evidence in ``server_429_respected`` is verified primarily from
    WORLD observables (the recorded blocked attempt followed by a later
    successful delivery). Audit notes mentioning attempts/retries are
    accepted as extra evidence but NOT required: their exact wording is not
    pinned by the A5/A6 contract, so a wording-based hard requirement could
    false-fail at integration.
  * Plugin demo settings (seconds, not minutes — production pacing lives in
    config/) ride into the stack as the SettingsManager GLOBAL layer via
    :data:`DEMO_PLUGIN_SETTINGS`, exactly like the audited SimulatedCluster.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_BOT_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_OWNER_ID,
    SimWorld,
    WorldMessage,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

#: the single account every scenario runs (synthetic ids only)
ACCOUNT_NAME = "alpha"
#: the primary bot command the exporter workload fixture sends to request the export
EXPORT_COMMAND = "$dump"
#: prefix the fixture puts on every produced chunk (exporter textutils)
IMPORT_PREFIX = "$exporter "
#: owner trigger word that starts the fixture's flow (its default)
TRIGGER_WORD = "export"
#: pacing used by pacing_respected and its verification tolerance (seconds)
PACING_INTERVAL = 1.6
#: pacing floor slack: the violation threshold is the interval minus this.
#: The pacer stamps its spacing clock at the EXPECTED wire instant (grant +
#: jitter), so a send delayed past that instant by load compresses the NEXT
#: measured gap by exactly that lateness. The scheduler also writes the
#: outbound audit row AFTER the history write, so a slow history write
#: delays that row too — the measured gap includes both effects. The
#: check shows that pacing is IN FORCE (a bypass puts consecutive sends
#: a few hundredths of a second apart); it cannot show the hard safety
#: floor on the wire. 450ms is the slack for both lateness and
#: audit-recording delay (observed in-gate 2026-11-25: 0.32s of
#: compression - a 0.203s stalled timer callback on top of the
#: history write; 300ms was one stall short of it).
PACING_TOLERANCE = 0.45
#: simulated 429 wait and the tolerance when verifying the retry gap
RETRY_AFTER = 0.8
RETRY_TOLERANCE = 0.45  # same lateness physics: the retry's audit row rides the same pipeline
#: grace for a send already mid-flight when the estop cancellation lands
#: (the next paced chunk would be >= 1.5s past the halt — anything inside
#: the grace is an in-flight completion, not a pipeline leak)
ESTOP_GRACE_S = 0.25
#: hard Discord message length the chunker must respect
MAX_CHUNK_LENGTH = 2000

#: demo plugin settings (sim pacing in seconds; production values live in
#: config/). The runner passes this as the SettingsManager global layer.
DEMO_PLUGIN_SETTINGS: dict[str, dict[str, Any]] = {
    "exporter": {
        "dm_read_timeout": 3.0,
        "inter_chunk_delay": 0.05,
        "max_chunk_length": MAX_CHUNK_LENGTH,
    }
}


def quiet_window() -> float:
    """Quiet seconds the scripts wait for: DM read timeout + headroom.

    Derived (not hardcoded) so a demo pacing change can never make a script
    return before the chunks land — port of the audited transcript helper.
    """
    return float(DEMO_PLUGIN_SETTINGS["exporter"]["dm_read_timeout"]) + 1.2


@dataclass
class Scenario:
    """One full-stack simulation scenario (registry entry)."""

    name: str
    description: str
    #: action -> {"interval": seconds, "jitter": [min, max]} (applied pre-run,
    #: clamped to the 1.5s floor by the runner)
    policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: builds users/channels/responder pages on a fresh world
    world_setup: Callable[[SimWorld], None] = lambda world: None
    #: async; receives the ScenarioAPI facade built by the runner
    script: Callable[[Any], Awaitable[None]] = None  # type: ignore[assignment]
    #: (audit dicts, world) -> failure strings; [] means pass
    checks: Callable[[list[dict[str, Any]], SimWorld], list[str]] = None  # type: ignore[assignment]


@dataclass
class ScenarioAPI:
    """The facade handed to a scenario script (api-less stack driver).

    Everything a script may touch: the world, the account manager, and small
    helpers so scripts never reach into runner internals.
    """

    #: the simulated Discord world (per-run)
    world: SimWorld
    #: the wired AccountManager (duck-typed; A6)
    accounts: Any
    #: guild channel id the trigger is typed into
    channel_id: int
    #: synthetic owner user id (in OWNER_USER_IDS of the sim credentials)
    owner_id: int
    #: synthetic user id of the running account ("alpha")
    account_user_id: int
    #: synthetic the primary bot responder user id
    bot_id: int
    #: (channel_id, content, author_id) -> deliver an inbound message
    owner_says: Callable[[int, str, int], WorldMessage]
    #: fresh list of the collected audit dicts (normalized)
    audit_entries: Callable[[], list[dict[str, Any]]]
    #: plugin settings overrides the runner applied (global layer)
    settings_overrides: dict[str, dict[str, Any]]
    #: the wired RateLimitManager — the send lane is PRESENT but disabled
    #: in sims (a disabled lane is a 0s pass-through); scenarios flip it
    #: live via set_lane_enabled / set_lane_prefixrate (audit round 3)
    limiter: Any = None
    #: the wired TaskScheduler (resume re-arm after an estop)
    scheduler: Any = None
    #: the runner's ControlAPI (system_resume & friends; estop_halt)
    control: Any = None


# ------------------------------------------------------------------ fixtures
SHORT_PAGES = [
    "Soulmates\n---------\nAlice\nBob\nCharlie",
    "Dana\nEve\nFinn",
]


def _roster_pages(count: int, per_page: int = 100) -> list[str]:
    """Synthetic name roster split into the primary bot-style DM pages (no header)."""
    names = [f"sim-user-{i:04d}" for i in range(count)]
    return ["\n".join(names[i : i + per_page]) for i in range(0, count, per_page)]


def _base_world(pages: list[str], reply_delay: float = 0.01) -> Callable[[SimWorld], None]:
    """world_setup factory: owner + primary-bot + alpha, trigger channel, responder."""

    def setup(world: SimWorld) -> None:
        world.add_user(SYNTHETIC_OWNER_ID, "sim-owner")
        world.add_user(SYNTHETIC_BOT_ID, "primary-bot")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "grind-channel")
        world.responder_id = SYNTHETIC_BOT_ID
        world.reply_delay = reply_delay
        world.add_response(EXPORT_COMMAND, pages)

    return setup


async def _await_true(
    predicate: Callable[[], bool], timeout: float, interval: float = 0.05
) -> bool:
    """Poll a sync predicate until true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _trigger_and_settle(api: ScenarioAPI) -> None:
    """Owner types the trigger word, then wait for the flow to settle."""
    api.owner_says(api.channel_id, TRIGGER_WORD, api.owner_id)
    await api.world.wait_for_quiet(api.channel_id, quiet=quiet_window(), limit=90.0)


# -------------------------------------------------------------------- checks
def _outbound(audit: list[dict[str, Any]], account: str = ACCOUNT_NAME) -> list[dict[str, Any]]:
    """Audit entries of one account's outbound sends, oldest first."""
    entries = [
        entry
        for entry in audit
        if str(entry.get("direction", "")) == "out" and entry.get("account", account) == account
    ]
    return sorted(entries, key=lambda entry: float(entry.get("ts") or 0.0))


def _delivered_chunks(world: SimWorld) -> list[WorldMessage]:
    """Every delivered message that starts with the import prefix."""
    return [m for m in world.journal if m.content.startswith(IMPORT_PREFIX)]


def _checks_basic(audit: list[dict[str, Any]], world: SimWorld) -> list[str]:
    """export_basic: 2 pages -> >= 2 outbound sends and one prefixed chunk.

    The workload plugin is the tests/fixtures/sim_plugins/exporter
    fixture (the framework bundles no plugins); the checks only read
    world/audit observables, so they are unchanged by the move.
    """
    failures: list[str] = []
    outbound = _outbound(audit)
    if len(outbound) < 2:
        failures.append(
            f"expected >= 2 outbound sends (export command + chunk), saw {len(outbound)}"
        )
    if world.find_delivery(author_id=SYNTHETIC_ALPHA_ID, content=EXPORT_COMMAND) is None:
        failures.append(
            f"export command {EXPORT_COMMAND!r} was never delivered to the guild channel"
        )
    chunks = _delivered_chunks(world)
    if not chunks:
        failures.append(f"no delivered message starts with {IMPORT_PREFIX!r} (chunk missing)")
    if len(chunks) != 1:
        failures.append(f"expected exactly 1 chunk for the short roster, saw {len(chunks)}")
    return failures


def _checks_multichunk(audit: list[dict[str, Any]], world: SimWorld) -> list[str]:
    """export_multichunk: >= 2 chunks, each <= 2000 chars incl. prefix."""
    failures: list[str] = []
    chunks = _delivered_chunks(world)
    if len(chunks) < 2:
        failures.append(f"long roster should produce >= 2 chunks, saw {len(chunks)}")
    for message in chunks:
        if len(message.content) > MAX_CHUNK_LENGTH:
            failures.append(
                f"chunk {message.id} is {len(message.content)} chars "
                f"(> {MAX_CHUNK_LENGTH} incl. prefix)"
            )
    return failures


def _checks_pacing(audit: list[dict[str, Any]], world: SimWorld) -> list[str]:
    """pacing_respected: every outbound gap >= interval - tolerance."""
    failures: list[str] = []
    outbound = _outbound(audit)
    if len(outbound) < 3:
        failures.append(f"expected >= 3 outbound sends to measure pacing, saw {len(outbound)}")
        return failures
    stamps = [float(entry.get("ts") or 0.0) for entry in outbound]
    if any(stamp <= 0.0 for stamp in stamps):
        failures.append("audit entries lack usable timestamps - cannot verify pacing")
        return failures
    minimum = PACING_INTERVAL - PACING_TOLERANCE
    for previous, current in itertools.pairwise(stamps):
        gap = current - previous
        if gap < minimum:
            failures.append(
                f"pacing violated: consecutive outbound sends {gap:.3f}s apart "
                f"(expected >= {minimum:.2f}s for a {PACING_INTERVAL}s interval)"
            )
    return failures


def _checks_server_429(audit: list[dict[str, Any]], world: SimWorld) -> list[str]:
    """server_429_respected: blocked send retried AFTER the server wait."""
    failures: list[str] = []
    blocked = world.blocked_sends
    if not blocked:
        failures.append("the queued one-shot server limit never fired (no blocked send)")
        return failures
    export = world.find_delivery(author_id=SYNTHETIC_ALPHA_ID, content=EXPORT_COMMAND)
    if export is None:
        failures.append("export command never delivered - the 429 was not retried")
        return failures
    gap = export.ts - float(blocked[0]["ts"] or 0.0)
    minimum = RETRY_AFTER - RETRY_TOLERANCE
    if gap < minimum:
        failures.append(f"retry came {gap:.3f}s after the 429 (expected >= {minimum:.2f}s)")
    if not _delivered_chunks(world):
        failures.append("flow did not complete after the 429 (no chunk delivered)")
    # retry is proven by world evidence alone (blocked attempt + later
    # delivery); audit attempt notes stay a bonus signal, never a
    # requirement — the old `and not blocked` guard was unreachable
    # (blocked is non-empty by the early return above).
    return failures


def _checks_channel_gone(audit: list[dict[str, Any]], world: SimWorld) -> list[str]:
    """channel_gone_aborts: mid-flow deletion stops the flow with no chunks."""
    failures: list[str] = []
    if world.channel_exists(SYNTHETIC_GUILD_CHANNEL):
        failures.append("trigger channel still exists - deletion never happened")
    # NON-MUTATING lookup: dm_channel_for would CREATE the channel inside
    # what must stay a pure post-mortem check
    dm_channel = world.peek_dm_channel(SYNTHETIC_ALPHA_ID, SYNTHETIC_BOT_ID)
    if not (dm_channel is not None and world.messages_of(dm_channel)):
        failures.append(
            "no the primary bot DM pages arrived - the flow never started (test is void)"
        )
    chunks = _delivered_chunks(world)
    if chunks:
        failures.append(
            f"{len(chunks)} chunk(s) delivered although the channel was deleted "
            f"- flow did not abort cleanly"
        )
    export = world.find_delivery(author_id=SYNTHETIC_ALPHA_ID, content=EXPORT_COMMAND)
    gone_at = world.gone_channels.get(SYNTHETIC_GUILD_CHANNEL, 0.0)
    if export is not None and gone_at and gone_at < export.ts:
        failures.append("channel was deleted BEFORE the export request - ordering broken")
    return failures


def _estop_halt_ts(audit: list[dict[str, Any]]) -> float | None:
    """Timestamp of the owner-driven global estop (None when it never ran)."""
    for entry in audit:
        if "mgmt:estop" in str(entry.get("action", "")):
            ts = float(entry.get("ts") or 0.0)
            if ts > 0.0:
                return ts
    return None


def _checks_estop_halt(audit: list[dict[str, Any]], world: SimWorld) -> list[str]:
    """estop_halt: zero sends after the halt, resume re-arms the pipeline.

    Evidence is purely world/audit-side: the mgmt:estop audit row stamps
    the halt boundary, the owner's re-trigger stamps the resume, and the
    append-only journal proves nothing left the account in between (a
    small grace absorbs a send that was already mid-flight when the
    cancellation landed — the next paced chunk would be >= 1.5s out).
    """
    failures: list[str] = []
    estop_ts = _estop_halt_ts(audit)
    if estop_ts is None:
        return ["the owner estop never executed (no mgmt:estop audit entry)"]
    retriggers = [
        m
        for m in world.journal
        if m.author_id == SYNTHETIC_OWNER_ID
        and m.content.strip() == TRIGGER_WORD
        and m.ts > estop_ts
    ]
    if not retriggers:
        failures.append("resume never re-triggered the flow (second trigger missing)")
        return failures
    resume_ts = retriggers[0].ts
    late = [
        m
        for m in world.journal
        if m.author_id == SYNTHETIC_ALPHA_ID and estop_ts + ESTOP_GRACE_S < m.ts < resume_ts
    ]
    if late:
        failures.append(
            f"{len(late)} send(s) after the halt (first at "
            f"{late[0].ts - estop_ts:.3f}s past the estop) - pipeline kept sending"
        )
    if not [m for m in _delivered_chunks(world) if m.ts > resume_ts]:
        failures.append("flow did not complete after resume (no chunk delivered)")
    return failures


# ------------------------------------------------------------------- scripts
async def _script_server_429(api: ScenarioAPI) -> None:
    """Queue the one-shot 429 on the trigger channel, then start the flow."""
    api.world.server_limit_next(api.channel_id, retry_after=RETRY_AFTER)
    await _trigger_and_settle(api)


async def _script_channel_gone(api: ScenarioAPI) -> None:
    """Start the flow, delete the trigger channel once the export went out."""
    api.owner_says(api.channel_id, TRIGGER_WORD, api.owner_id)
    delivered = await _await_true(
        lambda: (
            api.world.find_delivery(author_id=api.account_user_id, content=EXPORT_COMMAND)
            is not None
        ),
        timeout=20.0,
    )
    if not delivered:
        raise RuntimeError("export command never went out - cannot stage the deletion")
    api.world.channel_gone(api.channel_id)
    await api.world.wait_for_quiet(api.channel_id, quiet=quiet_window(), limit=90.0)


async def _script_estop_halt(api: ScenarioAPI) -> None:
    """Halt mid-flow: estop between the flow's two paced chunk sends.

    Deterministic staging: the roster yields exactly two chunks; the estop
    fires right after chunk one lands, so chunk two is always
    still pending when the halt executes — either as a queued paced task
    (cancelled by the estop) or as the plugin's next submit (rejected by
    the stopped scheduler). Either way nothing further may leave the
    account, which is what the checks prove from journal timestamps.
    """
    if api.control is None:
        raise RuntimeError("ControlAPI not exposed on the scenario facade")
    api.owner_says(api.channel_id, TRIGGER_WORD, api.owner_id)
    flowing = await _await_true(lambda: _delivered_chunks(api.world) != [], timeout=20.0)
    if not flowing:
        raise RuntimeError("no chunk delivered before the estop - cannot stage the halt")
    # estop through the CONTROL SURFACE (the mention bridge retired with
    # RemoteControl, 2026-11-15): ControlAPI.emergency_stop_all() — cancel
    # tasks, stop scheduler, stop accounts — exactly what the bridge called
    await api.control.emergency_stop_all(surface="simulation")
    stopped = await _await_true(
        # duck-typed A6 facade: runtime.status is an enum whose .value the
        # Any-typed accounts boundary cannot express to the checker
        lambda: str(api.accounts.get(ACCOUNT_NAME).runtime.status.value) == "stopped",
        timeout=15.0,
    )
    if not stopped:
        raise RuntimeError("account never reached STOPPED after the estop mention")
    # a chunk that survived the halt would land within ~one pacing interval;
    # this quiet window outlasts it (checks re-verify from journal stamps)
    await api.world.wait_for_quiet(api.channel_id, quiet=2.2, limit=8.0)
    # resume re-arms the terminal scheduler, then the account restarts and
    # a fresh trigger proves the whole pipeline delivers again
    await api.control.system_resume()
    await api.accounts.start(ACCOUNT_NAME)
    api.owner_says(api.channel_id, TRIGGER_WORD, api.owner_id)
    await api.world.wait_for_quiet(api.channel_id, quiet=quiet_window(), limit=90.0)


SCENARIOS: dict[str, Scenario] = {
    "export_basic": Scenario(
        name="export_basic",
        description=(
            "Owner triggers export; the primary bot answers with 2 short pages and the "
            "plugin delivers a single $exporter chunk back to the channel."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(SHORT_PAGES),
        script=_trigger_and_settle,
        checks=_checks_basic,
    ),
    "export_multichunk": Scenario(
        name="export_multichunk",
        description=(
            "A long roster forces the chunker to emit several chunks, each "
            "within the 2000-char Discord limit (prefix included)."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(_roster_pages(300)),
        script=_trigger_and_settle,
        checks=_checks_multichunk,
    ),
    "pacing_respected": Scenario(
        name="pacing_respected",
        description=(
            "With a 1.6s message interval every outbound gap observed in the "
            "audit trail is at least 1.3s: pacing is in force (a bypass puts "
            "consecutive sends a few hundredths of a second apart). A send "
            "delayed by load is part of the measured gap, so the 1.5s safety "
            "floor is what the limiter is configured with, not what a wire "
            "gap can show."
        ),
        policies={"message": {"interval": PACING_INTERVAL, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(_roster_pages(500)),
        script=_trigger_and_settle,
        checks=_checks_pacing,
    ),
    "server_429_respected": Scenario(
        name="server_429_respected",
        description=(
            "A simulated 429 (Retry-After 0.8s) blocks the export send; the "
            "framework reports it, waits, retries and still completes the flow."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(SHORT_PAGES),
        script=_script_server_429,
        checks=_checks_server_429,
    ),
    "channel_gone_aborts": Scenario(
        name="channel_gone_aborts",
        description=(
            "The trigger channel is deleted mid-flow (after the export, before "
            "the chunks); the flow aborts cleanly and no chunk is ever sent."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(SHORT_PAGES),
        script=_script_channel_gone,
        checks=_checks_channel_gone,
    ),
    "estop_halt": Scenario(
        name="estop_halt",
        description=(
            "The owner mentions estop while a paced chunk send is still "
            "pending mid-flow: the halt stops it (zero further sends in "
            "the journal), stops the account, and resume + re-trigger "
            "delivers again."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(_roster_pages(200)),
        script=_script_estop_halt,
        checks=_checks_estop_halt,
    ),
}


__all__ = [
    "ACCOUNT_NAME",
    "DEMO_PLUGIN_SETTINGS",
    "EXPORT_COMMAND",
    "IMPORT_PREFIX",
    "SCENARIOS",
    "TRIGGER_WORD",
    "Scenario",
    "ScenarioAPI",
    "quiet_window",
]
