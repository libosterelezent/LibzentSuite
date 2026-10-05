"""Scenario definitions for the offline simulation suite (spec §16).

Each :class:`Scenario` bundles everything the runner needs:
  * ``policies``  — rate policies applied BEFORE the run (interval + jitter);
    the runner clamps every interval to the 1.5s safety floor — simulations
    never configure unsafe pacing,
  * ``world_setup`` — builds the synthetic topology (users, the trigger
    channel, the scripted Mudae responder and its pages),
  * ``script``     — drives the built stack (usually: the owner types the
    soulcopy trigger, then the world settles),
  * ``checks``     — pure post-mortem over (audit dicts, world) returning
    human-readable failure strings ([] == pass).

All ids are SYNTHETIC (module constants in adapters.simulator.world); real
Discord ids/tokens only ever live in .env.

Documented decisions (contract ambiguities — see INTEGRATION.md ground rules):
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
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_MUDAE_ID,
    SYNTHETIC_OWNER_ID,
    SimWorld,
    WorldMessage,
)

#: the single account every scenario runs (synthetic ids only)
ACCOUNT_NAME = "alpha"
#: Mudae command the soulcopy plugin sends to request the export
EXPORT_COMMAND = "$slud-s"
#: prefix the plugin puts on every produced chunk (soulcopy textutils)
IMPORT_PREFIX = "$soulcopy "
#: owner trigger word that starts the soulcopy flow (plugin default)
TRIGGER_WORD = "slcopy"
#: pacing used by pacing_respected and its verification tolerance (seconds)
PACING_INTERVAL = 1.6
PACING_TOLERANCE = 0.05
#: simulated 429 wait and the tolerance when verifying the retry gap
RETRY_AFTER = 0.8
RETRY_TOLERANCE = 0.3
#: grace for a send already mid-flight when the estop cancellation lands
#: (the next paced chunk would be >= 1.5s past the halt — anything inside
#: the grace is an in-flight completion, not a pipeline leak)
ESTOP_GRACE_S = 0.25
#: hard Discord message length the chunker must respect
MAX_CHUNK_LENGTH = 2000

#: prefix the self_command scenario drives (mirror of the live layer's
#: SELF_COMMAND_PREFIX; "sm!" is the .env.example default). Declared per
#: scenario (Scenario.self_command_prefix) — the runner threads it into
#: the sim clients exactly like the live factory threads it from
#: credentials, and adds the account's own synthetic id to OWNER_USER_IDS
#: (the live layer's documented requirement for the owner check).
SELF_COMMAND_PREFIX = "sm!"
#: synthetic stranger driving the isolation half of self_command
SELF_COMMAND_STRANGER_ID = 4242

#: demo plugin settings (sim pacing in seconds; production values live in
#: config/). The runner passes this as the SettingsManager global layer.
DEMO_PLUGIN_SETTINGS: Dict[str, Dict[str, Any]] = {
    "soulcopy": {
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
    return float(DEMO_PLUGIN_SETTINGS["soulcopy"]["dm_read_timeout"]) + 1.2


@dataclass
class Scenario:
    """One full-stack simulation scenario (registry entry)."""

    name: str
    description: str
    #: action -> {"interval": seconds, "jitter": [min, max]} (applied pre-run,
    #: clamped to the 1.5s floor by the runner)
    policies: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: builds users/channels/responder pages on a fresh world
    world_setup: Callable[[SimWorld], None] = lambda world: None
    #: async; receives the ScenarioAPI facade built by the runner
    script: Callable[[Any], Awaitable[None]] = None  # type: ignore[assignment]
    #: (audit dicts, world) -> failure strings; [] means pass
    checks: Callable[[List[dict], SimWorld], List[str]] = None  # type: ignore[assignment]
    #: self-command prefix this scenario exercises (None = the runner
    #: falls back to the credentials' SELF_COMMAND_PREFIX, default "")
    self_command_prefix: Optional[str] = None


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
    #: synthetic Mudae responder user id
    mudae_id: int
    #: (channel_id, content, author_id) -> deliver an inbound message
    owner_says: Callable[[int, str, int], None]
    #: fresh list of the collected audit dicts (normalized)
    audit_entries: Callable[[], List[dict]]
    #: plugin settings overrides the runner applied (global layer)
    settings_overrides: Dict[str, Dict[str, Any]]
    #: the mention-prefix RemoteControl bridge (control_mention scenario)
    remote: Any = None
    #: the wired RateLimitManager — the send lane is PRESENT but disabled
    #: in sims (a disabled lane is a 0s pass-through); scenarios flip it
    #: live via set_lane_enabled / set_lane_prefixrate (audit round 3)
    limiter: Any = None
    #: the wired TaskScheduler (resume re-arm after an estop)
    scheduler: Any = None
    #: the runner's ControlAPI (system_resume & friends; estop_halt)
    control: Any = None
    #: the wired LifecycleGovernor (production-parity lifecycle pacing,
    #: round-4) — scenarios may assert on stats()/retry_after()
    lifecycle: Any = None


# ------------------------------------------------------------------ fixtures
SHORT_PAGES = [
    "Soulmates\n---------\nAlice\nBob\nCharlie",
    "Dana\nEve\nFinn",
]


def _roster_pages(count: int, per_page: int = 100) -> List[str]:
    """Synthetic name roster split into Mudae-style DM pages (no header)."""
    names = [f"sim-user-{i:04d}" for i in range(count)]
    return [
        "\n".join(names[i:i + per_page]) for i in range(0, count, per_page)
    ]


def _base_world(pages: List[str], reply_delay: float = 0.01) -> Callable[[SimWorld], None]:
    """world_setup factory: owner + mudae + alpha, trigger channel, responder."""

    def setup(world: SimWorld) -> None:
        world.add_user(SYNTHETIC_OWNER_ID, "sim-owner")
        world.add_user(SYNTHETIC_MUDAE_ID, "mudae")
        world.add_user(SYNTHETIC_ALPHA_ID, "alpha")
        world.add_channel(SYNTHETIC_GUILD_CHANNEL, "grind-channel")
        world.responder_id = SYNTHETIC_MUDAE_ID
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
def _outbound(audit: List[dict], account: str = ACCOUNT_NAME) -> List[dict]:
    """Audit entries of one account's outbound sends, oldest first."""
    entries = [
        entry
        for entry in audit
        if str(entry.get("direction", "")) == "out"
        and entry.get("account", account) == account
    ]
    return sorted(entries, key=lambda entry: float(entry.get("ts") or 0.0))


def _delivered_chunks(world: SimWorld) -> List[WorldMessage]:
    """Every delivered message that starts with the import prefix."""
    return [m for m in world.journal if m.content.startswith(IMPORT_PREFIX)]


def _checks_self_command(audit: List[dict], world: SimWorld) -> List[str]:
    """self_command: exactly ONE flow, driven by the own prefixed trigger.

    Evidence: exactly one inbound audit row with the BARE trigger word
    authored by the account itself (the synthetic re-fire crossing the
    boundary), exactly one export command + one chunk in the journal
    (exactly one flow), and NOTHING sent by the account after the
    stranger's identical prefixed command landed.
    """
    failures: List[str] = []
    refires = [
        entry
        for entry in audit
        if str(entry.get("direction", "")) == "in"
        and entry.get("content") == TRIGGER_WORD
        and f"author={SYNTHETIC_ALPHA_ID}" in str(entry.get("note", ""))
    ]
    if len(refires) != 1:
        failures.append(
            f"expected exactly 1 bare-trigger re-fire authored by the account, "
            f"saw {len(refires)}"
        )
    exports = [
        m for m in world.journal
        if m.author_id == SYNTHETIC_ALPHA_ID and m.content == EXPORT_COMMAND
    ]
    if len(exports) != 1:
        failures.append(
            f"expected exactly 1 export command from the self-command flow, "
            f"saw {len(exports)}"
        )
    chunks = _delivered_chunks(world)
    if len(chunks) != 1:
        failures.append(
            f"expected exactly 1 chunk from the self-command flow, saw {len(chunks)}"
        )
    strangers = [
        m for m in world.journal
        if m.author_id == SELF_COMMAND_STRANGER_ID
        and m.content == SELF_COMMAND_PREFIX + TRIGGER_WORD
    ]
    if not strangers:
        failures.append(
            "stranger prefixed command never landed - isolation check is void"
        )
        return failures
    stranger_ts = strangers[-1].ts
    late = [
        m for m in world.journal
        if m.author_id == SYNTHETIC_ALPHA_ID and m.ts > stranger_ts
    ]
    if late:
        failures.append(
            f"{len(late)} account send(s) after the stranger's prefixed "
            f"command - self-command isolation broken"
        )
    return failures


def _checks_basic(audit: List[dict], world: SimWorld) -> List[str]:
    """soulcopy_basic: 2 pages -> >= 2 outbound sends and one prefixed chunk."""
    failures: List[str] = []
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


def _checks_multichunk(audit: List[dict], world: SimWorld) -> List[str]:
    """soulcopy_multichunk: >= 2 chunks, each <= 2000 chars incl. prefix."""
    failures: List[str] = []
    chunks = _delivered_chunks(world)
    if len(chunks) < 2:
        failures.append(
            f"long roster should produce >= 2 chunks, saw {len(chunks)}"
        )
    for message in chunks:
        if len(message.content) > MAX_CHUNK_LENGTH:
            failures.append(
                f"chunk {message.id} is {len(message.content)} chars "
                f"(> {MAX_CHUNK_LENGTH} incl. prefix)"
            )
    return failures


def _checks_pacing(audit: List[dict], world: SimWorld) -> List[str]:
    """pacing_respected: every outbound gap >= interval - tolerance."""
    failures: List[str] = []
    outbound = _outbound(audit)
    if len(outbound) < 3:
        failures.append(
            f"expected >= 3 outbound sends to measure pacing, saw {len(outbound)}"
        )
        return failures
    stamps = [float(entry.get("ts") or 0.0) for entry in outbound]
    if any(stamp <= 0.0 for stamp in stamps):
        failures.append("audit entries lack usable timestamps - cannot verify pacing")
        return failures
    minimum = PACING_INTERVAL - PACING_TOLERANCE
    for previous, current in zip(stamps, stamps[1:]):
        gap = current - previous
        if gap < minimum:
            failures.append(
                f"pacing violated: consecutive outbound sends {gap:.3f}s apart "
                f"(expected >= {minimum:.2f}s for a {PACING_INTERVAL}s interval)"
            )
    return failures


def _checks_server_429(audit: List[dict], world: SimWorld) -> List[str]:
    """server_429_respected: blocked send retried AFTER the server wait."""
    failures: List[str] = []
    blocked = world.blocked_sends
    if not blocked:
        failures.append("the queued one-shot server limit never fired (no blocked send)")
        return failures
    export = world.find_delivery(author_id=SYNTHETIC_ALPHA_ID, content=EXPORT_COMMAND)
    if export is None:
        failures.append("export command never delivered - the 429 was not retried")
        return failures
    gap = export.ts - float(blocked[0]["ts"])
    minimum = RETRY_AFTER - RETRY_TOLERANCE
    if gap < minimum:
        failures.append(
            f"retry came {gap:.3f}s after the 429 (expected >= {minimum:.2f}s)"
        )
    if not _delivered_chunks(world):
        failures.append("flow did not complete after the 429 (no chunk delivered)")
    # retry is proven by world evidence alone (blocked attempt + later
    # delivery); audit attempt notes stay a bonus signal, never a
    # requirement — the old `and not blocked` guard was unreachable
    # (blocked is non-empty by the early return above).
    return failures


def _checks_channel_gone(audit: List[dict], world: SimWorld) -> List[str]:
    """channel_gone_aborts: mid-flow deletion stops the flow with no chunks."""
    failures: List[str] = []
    if world.channel_exists(SYNTHETIC_GUILD_CHANNEL):
        failures.append("trigger channel still exists - deletion never happened")
    dm_channel = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
    if not world.messages_of(dm_channel):
        failures.append("no Mudae DM pages arrived - the flow never started (test is void)")
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


def _estop_halt_ts(audit: List[dict]) -> Optional[float]:
    """Timestamp of the owner-driven global estop (None when it never ran)."""
    for entry in audit:
        if "mgmt:estop" in str(entry.get("action", "")):
            ts = float(entry.get("ts") or 0.0)
            if ts > 0.0:
                return ts
    return None


def _checks_estop_halt(audit: List[dict], world: SimWorld) -> List[str]:
    """estop_halt: zero sends after the halt, resume re-arms the pipeline.

    Evidence is purely world/audit-side: the mgmt:estop audit row stamps
    the halt boundary, the owner's re-trigger stamps the resume, and the
    append-only journal proves nothing left the account in between (a
    small grace absorbs a send that was already mid-flight when the
    cancellation landed — the next paced chunk would be >= 1.5s out).
    """
    failures: List[str] = []
    estop_ts = _estop_halt_ts(audit)
    if estop_ts is None:
        return ["the owner estop never executed (no mgmt:estop audit entry)"]
    retriggers = [
        m for m in world.journal
        if m.author_id == SYNTHETIC_OWNER_ID
        and m.content.strip() == TRIGGER_WORD
        and m.ts > estop_ts
    ]
    if not retriggers:
        failures.append("resume never re-triggered the flow (second trigger missing)")
        return failures
    resume_ts = retriggers[0].ts
    late = [
        m for m in world.journal
        if m.author_id == SYNTHETIC_ALPHA_ID
        and estop_ts + ESTOP_GRACE_S < m.ts < resume_ts
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
async def _script_self_command(api: ScenarioAPI) -> None:
    """The account's OWN prefixed message drives a full soulcopy flow.

    The human behind the account types ``sm!slcopy`` from their own
    Discord client: the message lands in the world authored by the
    account itself and the sim's command layer re-fires it as the bare
    trigger word (same pass-through set + DTO shape as the live layer's
    self_bot commands). A STRANGER typing the exact same prefixed command
    must start nothing — self_bot semantics make only the account's own
    messages invocable.
    """
    api.owner_says(
        api.channel_id,
        SELF_COMMAND_PREFIX + TRIGGER_WORD,
        api.account_user_id,
    )
    await api.world.wait_for_quiet(
        api.channel_id, quiet=quiet_window(), limit=60.0
    )
    # isolation: same prefix, same trigger, WRONG author -> no flow
    api.world.add_user(SELF_COMMAND_STRANGER_ID, "stranger")
    api.owner_says(
        api.channel_id,
        SELF_COMMAND_PREFIX + TRIGGER_WORD,
        SELF_COMMAND_STRANGER_ID,
    )
    await api.world.wait_for_quiet(api.channel_id, quiet=2.0, limit=10.0)


async def _script_server_429(api: ScenarioAPI) -> None:
    """Queue the one-shot 429 on the trigger channel, then start the flow."""
    api.world.server_limit_next(api.channel_id, retry_after=RETRY_AFTER)
    await _trigger_and_settle(api)


async def _script_channel_gone(api: ScenarioAPI) -> None:
    """Start the flow, delete the trigger channel once the export went out."""
    api.owner_says(api.channel_id, TRIGGER_WORD, api.owner_id)
    delivered = await _await_true(
        lambda: api.world.find_delivery(
            author_id=api.account_user_id, content=EXPORT_COMMAND
        )
        is not None,
        timeout=20.0,
    )
    if not delivered:
        raise RuntimeError("export command never went out - cannot stage the deletion")
    api.world.channel_gone(api.channel_id)
    await api.world.wait_for_quiet(api.channel_id, quiet=quiet_window(), limit=90.0)


async def _script_estop_halt(api: ScenarioAPI) -> None:
    """Halt mid-flow: estop between the flow's two paced chunk sends.

    Deterministic staging: the roster yields exactly two chunks; the owner
    mentions estop right after chunk one lands, so chunk two is always
    still pending when the halt executes — either as a queued paced task
    (cancelled by the estop) or as the plugin's next submit (rejected by
    the stopped scheduler). Either way nothing further may leave the
    account, which is what the checks prove from journal timestamps.
    """
    if api.control is None:
        raise RuntimeError("ControlAPI not exposed on the scenario facade")
    mention = f"<@{api.account_user_id}>"
    api.owner_says(api.channel_id, TRIGGER_WORD, api.owner_id)
    flowing = await _await_true(
        lambda: _delivered_chunks(api.world) != [], timeout=20.0
    )
    if not flowing:
        raise RuntimeError("no chunk delivered before the estop - cannot stage the halt")
    # owner mentions the account with estop: RemoteControl -> ControlAPI
    # .emergency_stop_all() (cancel tasks, stop scheduler, stop accounts)
    api.owner_says(api.channel_id, f"{mention} estop", api.owner_id)
    stopped = await _await_true(
        lambda: str(getattr(
            api.accounts.get(ACCOUNT_NAME).runtime, "status", ""
        ).value) == "stopped",
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


# ------------------------------------------------------------------ registry
async def _script_control_mention(api: "ScenarioAPI") -> None:
    """Owner drives @account commands; a stranger tries and gets nothing."""
    mention = f"<@{api.account_user_id}>"
    api.owner_says(api.channel_id, f"{mention} status", api.owner_id)
    await api.world.wait_for_quiet(api.channel_id, quiet=0.6, limit=15.0)
    # non-owner mentions the same account: must be ignored completely
    stranger = 4242
    api.world.add_user(stranger, "stranger")
    api.owner_says(api.channel_id, f"{mention} estop", stranger)
    await api.world.wait_for_quiet(api.channel_id, quiet=0.6, limit=15.0)


def _checks_control_mention(audit: List[dict], world: SimWorld) -> List[str]:
    failures: List[str] = []
    out = [
        e for e in audit
        if e.get("direction") == "out" and e.get("note", "").find("plugin=control") >= 0
    ]
    if not out:
        failures.append("owner mention produced no reply (expected a 'status' answer)")
    history = world.messages_of(SYNTHETIC_GUILD_CHANNEL)
    replies = [m for m in history if "uptime" in m or "tasks" in m]
    if not replies:
        failures.append("no status reply text landed in the guild channel")
    # the stranger's estop must NOT have produced a reply or halted anything
    estop_replies = [m for m in history if "EMERGENCY STOP" in m]
    if estop_replies:
        failures.append("non-owner mention was answered — owner gating broken")
    stranger_msgs = [
        m for m in world.journal
        if m.channel_id == SYNTHETIC_GUILD_CHANNEL and m.content.endswith("estop")
    ]
    if not stranger_msgs:
        failures.append(
            "stranger mention never landed - owner-gating check is void"
        )
        return failures
    stranger_ts = stranger_msgs[-1].ts
    later = [
        m for m in world.journal
        if m.channel_id == SYNTHETIC_GUILD_CHANNEL
        and m.author_id == SYNTHETIC_ALPHA_ID and m.ts > stranger_ts
    ]
    if later:
        failures.append(
            f"account replied after a non-owner mention "
            f"({len(later)} message(s)) - owner gating broken"
        )
    return failures



SCENARIOS: Dict[str, Scenario] = {
    "soulcopy_basic": Scenario(
        name="soulcopy_basic",
        description=(
            "Owner triggers slcopy; Mudae answers with 2 short pages and the "
            "plugin delivers a single $soulcopy chunk back to the channel."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(SHORT_PAGES),
        script=_trigger_and_settle,
        checks=_checks_basic,
    ),
    "soulcopy_multichunk": Scenario(
        name="soulcopy_multichunk",
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
            "audit trail is at least the interval (minus a 0.05s tolerance)."
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
    "control_mention": Scenario(
        name="control_mention",
        description=(
            "The owner manages the fleet from Discord by MENTIONING the "
            "account as the command prefix (@account status, estop); replies "
            "arrive through the paced pipeline and non-owners are ignored."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(SHORT_PAGES),
        script=_script_control_mention,
        checks=_checks_control_mention,
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
    "self_command": Scenario(
        name="self_command",
        description=(
            "With SELF_COMMAND_PREFIX set, the account's own 'sm!slcopy' "
            "message re-fires as the bare trigger and drives exactly one "
            "full soulcopy flow; a stranger typing the same prefixed "
            "command starts nothing (self_bot isolation)."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(SHORT_PAGES),
        script=_script_self_command,
        checks=_checks_self_command,
        self_command_prefix=SELF_COMMAND_PREFIX,
    ),
}


__all__ = [
    "Scenario",
    "ScenarioAPI",
    "SCENARIOS",
    "ACCOUNT_NAME",
    "EXPORT_COMMAND",
    "IMPORT_PREFIX",
    "TRIGGER_WORD",
    "DEMO_PLUGIN_SETTINGS",
    "SELF_COMMAND_PREFIX",
    "SELF_COMMAND_STRANGER_ID",
    "quiet_window",
]
