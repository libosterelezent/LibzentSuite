"""Full-stack integration tests: the PRODUCTION composition over the simulator.

These exercise core.bootstrap.build() — the exact boot path of main.py — with
only the Discord boundary replaced by the in-memory simulator (spec §9's
Testing adapter). Everything else (managers, scheduler, limiter, database,
audit, logging) is the real production code.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from adapters.simulator.client import SimClient
from adapters.simulator.world import SimWorld
from core.bootstrap import build
from core.credentials import Credentials
from core.errors import AccountError
from database.db import Database

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

OWNER_ID = 1001
PRIMARY_BOT_ID = 999_999
ALPHA_ID = 2001
GUILD_CHANNEL = 500

REPO_ROOT = Path(__file__).resolve().parents[2]
#: framework fixture plugins (probe/spy/echo) — the framework bundles none
FIXTURE_PLUGINS = REPO_ROOT / "tests" / "fixtures" / "plugins"
#: the full-stack WORKLOAD fixture (a exporter-like paced flow) driving
#: the pacing/estop e2e scenarios below
SIM_PLUGINS = REPO_ROOT / "tests" / "fixtures" / "sim_plugins"
PROBE = "probe"

FAST_SETTINGS = {
    "exporter": {
        "inter_chunk_delay": 0.0,
        # the exporter stops collecting DM pages after this much idle time,
        # whether or not the first one arrived. Under the gate's coverage leg
        # a 0.6s window closed first ("collected 0 soulmate page(s)") and the
        # flow sent no chunk. 3.0s is what the simulation scenarios give the
        # same fixture (adapters/simulator/scenarios.py).
        "dm_read_timeout": 3.0,
    }
}
FAST_POLICIES = {
    "global": {
        "message": {"interval": 1.5, "jitter": [0.0, 0.05]},
        "dm": {"interval": 1.5, "jitter": [0.0, 0.05]},
    },
    "accounts": {},
}
#: the message interval these tests configure. The pacing tests assert it
#: twice without a clock, then read the wire once:
#:
#: * the EFFECTIVE policy is exactly this interval: configuration, no clock;
#: * the pacer's own STAMPS are at least this interval apart: exact under any
#:   load, up to ``STAMP_EPSILON`` of rounding (see ``Stack.pacing_stamps``);
#: * no SEND reached the wire before the stamp that admitted it. The pacer
#:   stamps its spacing clock at the expected wire instant, so a send delayed
#:   by load leaves later than its stamp, never earlier. A measured gap
#:   between two sends has no such property: one send running 110ms later
#:   than the next left a 1.3896s gap under the gate's coverage leg, so
#:   none is asked of it.
POLICY_INTERVAL = FAST_POLICIES["global"]["message"]["interval"]
#: ``ratelimit_stats`` reports a stamp as its age, rounded to a millisecond
STAMP_EPSILON = 0.005

#: the pages the simulated primary bot DMs in answer to "$dump"
DUMP_PAGES = ["Soulmates\n---------\nAlice\nBob\nCharlie", "Dana\nEve\nFinn"]
#: enough names for a 50-character chunk budget to split into three chunks:
#: with the "$dump" request that is four sends, each chunk after the first
#: held by the pacer for a full interval after the one before
#: (test_pacing_enforced_between_sends)
MANY_NAMES = [f"name{i:04d}" for i in range(12)]
LONG_DUMP_PAGES = [
    "Soulmates\n---------\n" + "\n".join(MANY_NAMES[:6]),
    "\n".join(MANY_NAMES[6:]),
]

#: what a stalled exporter flow leaves in the log: its own progress lines
#: (plus everything the framework logged loudly; see Stack._flow_log_tail)
FLOW_LOG_MARKS = ("flow aborted", "soulmate", "chunk", "did not complete", "exporter")


def make_config_dir(settings: dict[str, dict[str, Any]] | None = None) -> Path:
    """A config directory for one stack: the fast fixture settings (with
    `settings` merged in per plugin), the fast policies, and no accounts."""
    root = Path(tempfile.mkdtemp(prefix="sm-config-"))
    overrides = settings or {}
    merged = {name: {**values, **overrides.get(name, {})} for name, values in FAST_SETTINGS.items()}
    (root / "global_settings.json").write_text(json.dumps(merged))
    (root / "rate_limits.json").write_text(json.dumps(FAST_POLICIES))
    (root / "accounts").mkdir()
    return root


class PacerStampAssertions:
    """The strict, clock-free pacing proofs shared by the pacing tests."""

    def assert_pacer_stamps_spaced(
        self: unittest.TestCase, stamps: list[float], jitters: list[float]
    ) -> None:
        """Consecutive pacer stamps are at least ``POLICY_INTERVAL`` apart.

        The stamps come from ``Stack.pacing_stamps``: the pacer's own
        spacing clock, which lateness cannot compress. A stamp is grant +
        that send's jitter, so a gap between stamps is the interval plus
        the later grant's wake-up lateness plus the later send's jitter.
        The jitter is known (``jitters``, from ``Stack.grant_jitters``) and
        is subtracted here; only the lateness is left to hide a shorter
        interval behind.
        """
        self.assertEqual(len(jitters), len(stamps))
        gaps = [
            b - a - jitter
            for (a, b), jitter in zip(itertools.pairwise(stamps), jitters[1:], strict=True)
        ]
        self.assertTrue(
            all(g >= POLICY_INTERVAL - STAMP_EPSILON for g in gaps),
            f"pacer stamps are closer than {POLICY_INTERVAL}s: gaps={[round(g, 3) for g in gaps]}",
        )

    def assert_no_send_before_its_stamp(self: unittest.TestCase, leads: list[float]) -> None:
        """No send reached the wire before the stamp that admitted it.

        ``leads`` are per send ``wire time - stamp``, from
        ``Stack.pacing_stamps``: never negative on a healthy system, however
        late load makes a send, and exact up to the noise of the clock
        offset (microseconds). A negative lead means the stamp sampled
        after that send is newer than the send itself: it left without
        waiting for the pacer, or the sampler missed its own send and read
        the next stamp.
        """
        self.assertTrue(
            all(lead >= -STAMP_EPSILON for lead in leads),
            f"send before its stamp: leads={[round(x, 3) for x in leads]}",
        )


class Stack:
    """A fully wired production framework over one simulated world
    (``stack = await Stack(...).boot()`` — build is async now)."""

    def __init__(
        self,
        db_path: str | None = None,
        extra_accounts: list[str] | None = None,
        plugin_dir: Path = FIXTURE_PLUGINS,
        settings: dict[str, dict[str, Any]] | None = None,
        dump_pages: list[str] | None = None,
    ) -> None:
        self.world = SimWorld(
            responder_id=PRIMARY_BOT_ID,
            responses={"$dump": list(dump_pages or DUMP_PAGES)},
            reply_delay=0.01,
        )
        self.world.add_user(OWNER_ID, "owner")
        self.world.add_user(PRIMARY_BOT_ID, "bot")
        self.world.add_user(ALPHA_ID, "alpha")
        self.world.add_user(2002, "beta")
        self.world.add_channel(GUILD_CHANNEL, "grind-channel")
        self.user_ids = {"alpha": ALPHA_ID, "beta": 2002}
        self.config_dir = make_config_dir(settings)
        self.credentials = Credentials.from_values(
            {
                "PRIMARY_BOT_ID": str(PRIMARY_BOT_ID),
                "OWNER_USER_IDS": str(OWNER_ID),
                "DISCORD_TOKEN_ALPHA": "sim-alpha",
                "DISCORD_TOKEN_BETA": "sim-beta",
            }
        )
        self.db = Database(db_path if db_path is not None else ":memory:")
        self._db_path = db_path
        self.framework: Any = None  # set by boot() (build is async)
        self._extra = extra_accounts
        self._plugin_dir = plugin_dir

    async def boot(self) -> Stack:
        """``build()`` is async now (aiosqlite database layer)."""
        # db_path must follow the database (PR review 2026-11-18):
        # build() locates the plugin-data side-car AND the credential
        # key from db_path's parent — leaving it on the default put
        # :memory: runs' side-cars in the repo root (a leaked
        # plugin-data.db + credentials.key) and file runs' in state/.
        sidecar_path = (
            self._db_path
            if self._db_path is not None
            else str(Path(self.config_dir) / "sidecars" / "libzent.db")
        )
        self.framework = await build(
            config_dir=str(self.config_dir),
            plugin_dir=str(self._plugin_dir),
            client_factory=self._client_factory,
            credentials=self.credentials,
            database=self.db,
            db_path=sidecar_path,
            quiet=True,
        )
        # Seed credentials for the sim accounts (database-only resolution —
        # the .env path no longer exists at runtime). We inject directly
        # into the service's memory + DB cache to avoid generating a
        # persistent key file for tests.
        cs = getattr(self.framework, "credential_service", None)
        if cs is not None:
            for name in ("alpha", "beta"):
                token = self.credentials.token_for(name)
                if token:
                    cs._memory[name] = token
                    cs._db_accounts.add(name)
                    cs._runtime_sources[name] = "db"
        return self

    def _client_factory(self, name: str, token: str) -> SimClient:
        return SimClient(self.world, self.user_ids[name], seed=hash(name) % 1000)

    async def start(self) -> None:
        # capability model: attach every DISCOVERED plugin explicitly (the
        # old stack attached all plugins by default; zero assignments now
        # means zero plugins, so these whole-stack flow tests spell it out)
        names = [m["name"] for m in await self.framework.api.plugins_manifests()]
        for name in self._extra or ["alpha"]:
            await self.framework.api.accounts_add({"name": name, "plugins": names})
        await self.framework.api.accounts_start()

    async def stop(self) -> None:
        await self.framework.shutdown()

    async def owner_says(self, content: str, account: str = "alpha") -> None:
        self.world.deliver(GUILD_CHANNEL, OWNER_ID, content)

    def outbound(self, channel: int, authors: set[int]) -> list[float]:
        """Journal timestamps of the messages `authors` sent to `channel`,
        oldest first (the journal is what actually hit the wire)."""
        return sorted(m.ts for m in self.world.history.get(channel, []) if m.author_id in authors)

    async def wait_for_outbound(
        self, channel: int, count: int, account: str = "alpha", limit: float = 30.0
    ) -> None:
        """Return once `account` has `count` messages in the channel's journal.

        Waits for the OUTCOME the caller will assert on, never for silence.
        The export flow is legitimately quiet in the trigger channel while
        the plugin exchanges DM pages and then waits out the pacing interval
        before its chunk goes out; under load that silence outlasts any
        fixed quiet window, and the window would return before the chunk.
        """
        await self._wait_for_sends(channel, {self.user_ids[account]}, count, limit, account)

    async def pacing_stamps(
        self,
        key: str,
        channel: int,
        authors: set[int],
        sends: int,
        limit: float = 30.0,
        *,
        first: int = 1,
        wire_floor: float | None = None,
    ) -> tuple[list[float], list[float]]:
        """The pacer's stamps for `key` and each send's lead over its stamp,
        one entry per send, oldest first. ``first`` is the 1-based journal
        index the sampled window starts at (a re-measurement window; 1
        by default).

        When the pacer admits a send it stamps the key's clock at grant +
        jitter, and it admits the next send on that key no sooner than one
        interval after that stamp: consecutive stamps are a full interval
        apart under ANY load. Lateness delays a send, never the distance
        between stamps, so this is the spacing proof a wire gap cannot be.

        Each stamp is sampled after its send lands in the journal, within
        one poll of it. ``ratelimit_stats`` reports the stamp as its age at
        a clock read taken inside the call, so the limiter's clock is
        wrapped for the sampler's duration to record those readings, and
        each stamp is rebuilt from the very read that produced its age.
        Nothing is estimated: a thread the OS deschedules anywhere in the
        sampler cannot move a stamp, in either direction.

        The lead is ``send time - stamp``: zero or more for a send that
        waited for the pacer, since it left at or after the stamp that
        admitted it. The journal times a send on the wall clock and the
        pacer stamps on the monotonic one; the two are put on one scale by
        an offset from the narrowest of a few back-to-back probes
        (``_narrowest_wall_offset``), which bounds the lead's error at
        microseconds.

        One limit, stated and detected: the pacer keeps a single stamp per
        key and a later grant overwrites it, so a sample taken a whole
        interval after its send's own grant would read that later stamp.
        That takes a send or a poll more than an interval late (209ms is
        the most measured). The sampler refuses to record a stamp that
        does not advance: two sends matched to one grant mean it fell
        behind, and that is reported as exactly that, not as a pacing
        violation. A per-send record would need the pacer to expose it.
        """
        clock = self.framework.limiter._clock
        readings: list[float] = []
        original_now = clock.now

        def recording_now() -> float:
            """The limiter's clock while the sampler runs: it records every reading."""
            value = original_now()
            readings.append(value)
            return value

        stamps: list[float] = []
        leads: list[float] = []
        clock.now = recording_now
        try:
            for count in range(first, first + sends):
                landed = await self._wait_for_sends(channel, authors, count, limit, key)
                before = len(readings)
                ages = (await self.framework.api.ratelimit_stats())["last_fire_ages"]
                if key not in ages:
                    raise AssertionError(
                        f"{key}: send {count} landed, but the pacer never stamped this key"
                    )
                if len(readings) == before:
                    raise AssertionError(f"{key}: ratelimit_stats read no clock")
                stamp = readings[-1] - ages[key]
                if stamps and stamp <= stamps[-1]:
                    # Two samples matched one grant. EITHER the sampler's
                    # poll fell more than an interval behind (an honesty
                    # limit of this MEASUREMENT, retried) OR the two sends
                    # were granted so close that the later grant preceded
                    # the first sample's read (a REAL violation - it must
                    # not launder through a retry; review round 6). The
                    # WIRE gap between the two sends discriminates: paced
                    # sends stay a near-full interval apart on the wire
                    # however late the grants read; violated sends are close.
                    if (
                        wire_floor is not None
                        and (landed[count - 1] - landed[count - 2]) < wire_floor
                    ):
                        raise AssertionError(
                            f"{key}: sends {count - 1} and {count} hit the wire "
                            f"{landed[count - 1] - landed[count - 2]:.3f}s apart while "
                            "sharing one grant - a pacing violation, not a sampling limit"
                        )
                    raise AssertionError(
                        f"{key}: the sampler fell behind between send {count - 1} and "
                        f"send {count}: both matched the same grant (stamp {stamp:.3f}); "
                        "pacing cannot be judged for this run"
                    )
                stamps.append(stamp)
                leads.append(landed[count - 1] - (stamp + self._narrowest_wall_offset()))
        finally:
            clock.now = original_now
        return stamps, leads

    async def pacing_stamps_with_retry(
        self,
        key: str,
        channel: int,
        authors: set[int],
        sends: int,
        resubmit: Callable[[int], Awaitable[None]],
        limit: float = 30.0,
        attempts: int = 3,
        wire_floor: float | None = None,
    ) -> tuple[list[float], list[float], int]:
        """pacing_stamps with a re-measurement path for the sampler's own limit.

        "pacing cannot be judged" is the sampler's honesty limit - a poll
        more than an interval late means the sample fell behind (multi-
        second callback stalls on a busy desktop; observed in-gate
        2026-11-25) - and it is NOT a verdict about pacing. On that
        specific error the flow is re-triggered and the FRESH window is
        measured. ``wire_floor`` (passed through to pacing_stamps) keeps
        a REAL violation from laundering through the retry: sends that
        hit the wire closer than the floor while sharing a grant raise
        a non-retryable violation.

        ``resubmit`` receives the 1-based journal index the fresh window
        starts at - it must wait out any in-flight flow before
        re-triggering (the exporter ignores a trigger mid-flow; review
        round 6).

        Returns ``(stamps, leads, first)``: ``first`` is the 1-based
        journal index the returned window starts at, so jitter slices
        and send-count asserts line up with the window actually
        measured.
        """
        first = 1
        for attempt in range(attempts):
            try:
                stamps, leads = await self.pacing_stamps(
                    key, channel, authors, sends, limit, first=first, wire_floor=wire_floor
                )
                return stamps, leads, first
            except AssertionError as exc:
                if "pacing cannot be judged" not in str(exc) or attempt == attempts - 1:
                    raise
                first += sends
                await resubmit(first)
        raise AssertionError(f"{key}: unreachable - the loop returns or raises")

    @staticmethod
    def _narrowest_wall_offset(probes: int = 5) -> float:
        """``wall clock - monotonic clock``, from the narrowest of `probes`
        back-to-back readings of both.

        The two clocks tick together, so their difference is constant. What
        can corrupt it is a thread descheduled between reading one and the
        other; keeping the probe with the smallest span makes that unlikely,
        and bounds the error by half of that span.
        """
        best_span = float("inf")
        best_offset = 0.0
        for _ in range(probes):
            early = time.monotonic()
            wall = time.time()
            late = time.monotonic()
            span = late - early
            if span < best_span:
                best_span = span
                best_offset = wall - (early + late) / 2
        return best_offset

    def grant_jitters(self, accounts: set[str], action: str, sends: int) -> list[float]:
        """The grant jitter each of the first `sends` tasks of `accounts`/`action`
        drew, in seconds, in start order.

        The scheduler records a task's jitter and start time before it invokes
        the sender, so every task whose send already landed carries both. Its
        finish time is not usable here: a send is in the journal before its
        task is.
        """
        rows = [
            r
            for r in self.framework.scheduler.tasks()
            if r["account"] in accounts and r["action"] == action and r["started_at"] is not None
        ]
        rows.sort(key=lambda r: (r["started_at"], r["id"]))
        return [r["grant_jitter_ms"] / 1000.0 for r in rows[:sends]]

    async def _wait_for_sends(
        self, channel: int, authors: set[int], count: int, limit: float, who: str
    ) -> list[float]:
        """Journal times of the first `count` sends, once that many have landed.

        Polls the journal. When `limit` runs out first, it fails naming `who`
        and quoting the framework's newest log lines.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        while loop.time() < deadline:
            landed = self.outbound(channel, authors)
            if len(landed) >= count:
                return landed
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"{who} never reached {count} outbound messages in channel {channel}"
            f"{await self._flow_log_tail()}"
        )

    async def _flow_log_tail(self) -> str:
        """Newest flow-relevant log lines, to explain a stalled flow in the failure."""
        logs = await self.framework.api.logs_tail(n=200)
        lines = [
            f"[{e.get('severity')}] {e.get('message')}"
            for e in logs
            if str(e.get("severity", "")).upper() in ("WARNING", "ERROR", "CRITICAL")
            or any(mark in str(e.get("message", "")) for mark in FLOW_LOG_MARKS)
        ]
        return "".join(f"\n  log: {line[:200]}" for line in lines[-6:])


class TestFullStackWorkloadFlow(PacerStampAssertions, unittest.IsolatedAsyncioTestCase):
    """The production boot path driving the WORKLOAD fixture (a paced
    multi-step flow plugin under tests/fixtures/sim_plugins — the
    framework itself bundles no plugins)."""

    async def test_flow_end_to_end(self) -> None:
        stack = await Stack(plugin_dir=SIM_PLUGINS).boot()
        await stack.start()
        try:
            await stack.owner_says("export")
            # the request and the chunk are the account's two sends here
            await stack.wait_for_outbound(GUILD_CHANNEL, 2)
            history = stack.world.messages_of(GUILD_CHANNEL)
            # export request + one chunk (pages fit a single chunk)
            self.assertIn("export", history[0])
            chunks = [m for m in history if m.startswith("$exporter ")]
            self.assertEqual(len(chunks), 1)
            self.assertIn("Alice", chunks[0])

            # outbound audit trail with pacing data (queued persistence):
            # wait for BOTH rows the assertion below reads — one per send
            audit: list[dict[str, Any]] = []
            for _ in range(200):
                audit = await stack.framework.api.audit_tail(n=20)
                rows = [
                    e
                    for e in audit
                    if e.get("direction") == "out" and e.get("action") in ("message", "dm")
                ]
                if len(rows) >= 2:
                    break
                await asyncio.sleep(0.15)
            outs = rows
            self.assertGreaterEqual(len(outs), 2)
            self.assertTrue(all("plugin=exporter" in (e.get("note") or "") for e in outs))
            # task history persisted with statuses
            hist = await stack.framework.api.tasks_history(limit=10)
            self.assertTrue(hist)
            self.assertTrue(all(h["status"] == "completed" for h in hist))

            # structured logging saw the lifecycle. The audit row a send
            # leaves behind is written BEFORE the TaskCompleted event is
            # published, so the audit poll above is not a barrier for it:
            # wait for the event itself, bounded, then assert.
            logs: list[dict[str, Any]] = []
            for _ in range(100):
                logs = await stack.framework.api.logs_tail(n=50)
                if any(entry.get("event") == "TaskCompleted" for entry in logs):
                    break
                await asyncio.sleep(0.05)
            events = [entry.get("event") for entry in logs if entry.get("event")]
            self.assertIn("TaskCompleted", events)
            self.assertIn("AccountConnected", events)
        finally:
            await stack.stop()

    async def test_pacing_enforced_between_sends(self) -> None:
        stack = await Stack(
            plugin_dir=SIM_PLUGINS,
            settings={"exporter": {"max_chunk_length": 50}},
            dump_pages=LONG_DUMP_PAGES,
        ).boot()
        await stack.start()
        try:
            alpha = {stack.user_ids["alpha"]}
            await stack.owner_says("export")

            # "$dump", then three chunks. Every chunk after the first is held
            # by the pacer for a full interval after the one before, however
            # long the DM phase took, which is why this reads a flow of four
            # sends and not a request and a single chunk.
            async def rerun_export(total: int) -> None:
                # wait out the in-flight flow first: the exporter ignores a
                # trigger while its copy flow runs (review round 6) - a
                # dropped re-trigger means the fresh window never arrives
                await stack.wait_for_outbound(GUILD_CHANNEL, total - 1, "alpha")
                await stack.owner_says("export")

            # "pacing cannot be judged" is the sampler's honesty limit (a
            # poll more than an interval late on a busy desktop, observed
            # in-gate 2026-11-25), not a pacing verdict: re-run the flow
            # and measure the fresh window; a REAL spacing violation
            # never takes that path.
            stamps, leads, first = await stack.pacing_stamps_with_retry(
                "alpha:message",
                GUILD_CHANNEL,
                alpha,
                4,
                rerun_export,
                # the wire floor: interval minus the lateness budget (0.45s,
                # matching scenarios.py's PACING_TOLERANCE)
                wire_floor=POLICY_INTERVAL - 0.45,
            )
            sent = stack.world.messages_of(GUILD_CHANNEL)
            self.assertEqual(
                sum(1 for m in sent if m.startswith("$exporter ")),
                3 * ((first + 3) // 4),  # one flow (3 chunks) per attempt
            )
            # STRICT, and no clock involved: what paces these sends is the
            # effective policy, and it is exactly the configured interval
            effective = await stack.framework.api.ratelimit_effective("message", "alpha")
            self.assertEqual(effective["interval"], POLICY_INTERVAL)
            # STRICT, on the pacer's own clock: consecutive stamps on the key
            # are a full interval apart whatever the load (Stack.pacing_stamps)
            jitters = stack.grant_jitters({"alpha"}, "message", sends=first + 3)[first - 1 :]
            self.assert_pacer_stamps_spaced(stamps, jitters)
            # STRICT, on the wire: no send left before the pacer admitted it
            self.assert_no_send_before_its_stamp(leads)
        finally:
            await stack.stop()


class TestPluginAssignmentGating(unittest.IsolatedAsyncioTestCase):
    """Direct account→plugin assignments gate the attached set (the
    profile-mediated model is gone; profiles are retained bookkeeping)."""

    async def test_assignment_limits_plugin_set(self) -> None:
        stack = await Stack(extra_accounts=["alpha", "beta"]).boot()
        api = stack.framework.api
        await api.accounts_add({"name": "alpha", "plugins": [PROBE]})
        await api.accounts_add({"name": "beta"})  # zero assignments
        await api.accounts_start()
        try:
            alpha_plugins = {p["name"] for p in await api.plugins_state("alpha")}
            beta_plugins = {p["name"] for p in await api.plugins_state("beta")}
            self.assertEqual(alpha_plugins, {PROBE})  # assigned only
            self.assertEqual(beta_plugins, set())  # zero = ZERO (not "all")
            self.assertEqual(await api.accounts_plugins_list("beta"), [])
        finally:
            await stack.stop()


class TestPersistenceRoundtrip(unittest.IsolatedAsyncioTestCase):
    async def test_state_survives_reboot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-state-") as tmp:
            db_path = str(Path(tmp) / "libzent.db")
            stack = await Stack(db_path=db_path).boot()
            api = stack.framework.api
            # capability: DIRECT plugin assignment (the old "profiles" key
            # no longer exists on accounts_add)
            await api.accounts_add({"name": "alpha", "plugins": [PROBE]})
            await api.ratelimit_set("message", 4.0, 0.1, 0.9, level="account", owner="alpha")
            await api.plugins_set_enabled("alpha", PROBE, False)
            await api.plugins_set_setting("alpha", PROBE, "trigger", "go")
            await stack.stop()

            # second boot on the same DB restores everything
            stack2 = await Stack(db_path=db_path).boot()
            api2 = stack2.framework.api
            try:
                accounts = await api2.accounts_list()
                self.assertEqual([a["name"] for a in accounts], ["alpha"])
                self.assertEqual(accounts[0]["assigned_plugins"], [PROBE])
                policies = await api2.ratelimit_policies()
                override = [
                    p for p in policies if p["level"] == "account" and p["owner"] == "alpha"
                ]
                self.assertEqual(len(override), 1)
                self.assertEqual(override[0]["interval"], 4.0)
                states = {p["name"]: p for p in await api2.plugins_state("alpha")}
                self.assertFalse(states[PROBE]["enabled"])  # plugin state
                self.assertEqual(states[PROBE]["settings"]["trigger"], "go")
            finally:
                await stack2.stop()


class TestPluginToggleFailsClosed(unittest.IsolatedAsyncioTestCase):
    """PR review 2026-11-18: the toggle validates BEFORE any state moves.
    reconcile_plugin_cogs no-ops silently on an unknown account, so
    without the up-front checks an unknown name produced a success echo
    plus an audit row for an account that does not exist."""

    async def test_unknown_account_rejected_without_audit(self) -> None:
        stack = await Stack().boot()
        try:
            with self.assertRaises(AccountError):
                await stack.framework.api.plugins_set_enabled("ghost", PROBE, False)
            audit = await stack.framework.api.audit_tail(n=50, account="ghost")
            self.assertEqual(audit, [])
        finally:
            await stack.stop()

    async def test_unknown_plugin_rejected(self) -> None:
        stack = await Stack().boot()
        try:
            await stack.framework.api.accounts_add({"name": "alpha", "plugins": [PROBE]})
            with self.assertRaises(ValueError):
                await stack.framework.api.plugins_set_enabled("alpha", "no-such-plugin", False)
        finally:
            await stack.stop()


class TestEmergencyStop(unittest.IsolatedAsyncioTestCase):
    async def test_global_estop_halts_everything(self) -> None:
        stack = await Stack().boot()
        await stack.start()
        try:
            out = await stack.framework.api.emergency_stop_all()
            self.assertTrue(out["halted"])
            accounts = await stack.framework.api.accounts_list()
            self.assertTrue(all(a["status"] == "stopped" for a in accounts))
        finally:
            await stack.stop()


class TestApiCallTrimming(unittest.IsolatedAsyncioTestCase):
    """Anti-flag guarantees: resolved entities cost ONE REST call per account
    lifetime (persistent across restarts), and typing is opt-in."""

    async def test_dm_resolved_once_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sm-dm-") as tmp:
            db_path = str(Path(tmp) / "libzent.db")
            stack = await Stack(db_path=db_path).boot()
            api = stack.framework.api
            await api.accounts_add({"name": "alpha"})
            await api.accounts_start()
            try:
                # resolve the primary bot DM twice in this boot: ONE client call
                services = stack.framework.accounts.services_for("alpha", PROBE)
                cid1 = await services.open_dm(PRIMARY_BOT_ID)
                cid2 = await services.open_dm(PRIMARY_BOT_ID)
                self.assertEqual(cid1, cid2)
                client = stack.framework.accounts.get("alpha").runtime.client
                self.assertEqual(client.open_dm_calls, [PRIMARY_BOT_ID])
            finally:
                await stack.stop()

            # second boot on the same DB: still exactly ONE call ever
            stack2 = await Stack(db_path=db_path).boot()
            api2 = stack2.framework.api
            # alpha was restored from the database by the AccountManager
            names = [a["name"] for a in await api2.accounts_list()]
            self.assertIn("alpha", names)
            await api2.accounts_start()
            try:
                services2 = stack2.framework.accounts.services_for("alpha", PROBE)
                cid3 = await services2.open_dm(PRIMARY_BOT_ID)
                self.assertEqual(cid3, cid1)
                client2 = stack2.framework.accounts.get("alpha").runtime.client
                self.assertEqual(client2.open_dm_calls, [])  # DB-served
            finally:
                await stack2.stop()

    async def test_typing_indicator_opt_in(self) -> None:
        stack = await Stack().boot()
        api = stack.framework.api
        await api.accounts_add({"name": "alpha"})
        await stack.framework.accounts.start("alpha")
        try:
            client = stack.framework.accounts.get("alpha").runtime.client
            settings = stack.framework.settings
            # default: typing_probability 0.0 -> zero typing calls, zero extra API
            await stack.framework.accounts.send_hook(
                SimpleNamespace(
                    account="alpha", action="message", channel_id=GUILD_CHANNEL, content="x"
                )
            )
            self.assertEqual(client.typing_calls, [])
            # opted in at 1.0 -> every guild send fires typing first
            await settings.set_account_setting("alpha", "typing_probability", 1.0)
            await stack.framework.accounts.send_hook(
                SimpleNamespace(
                    account="alpha", action="message", channel_id=GUILD_CHANNEL, content="y"
                )
            )
            self.assertEqual(len(client.typing_calls), 1)
        finally:
            await stack.stop()


class TestEmergencyStopResume(unittest.IsolatedAsyncioTestCase):
    """REGRESSION: the global estop message says 'restart manually' — the
    resume path must actually exist. estop halts everything; system_resume
    re-arms the scheduler so plugin sends work again."""

    async def test_estop_then_resume_restores_sending(self) -> None:
        stack = await Stack().boot()
        api = stack.framework.api
        await api.accounts_add({"name": "alpha"})
        await api.accounts_start()
        try:
            out = await api.emergency_stop_all()
            self.assertTrue(out["halted"])
            # halted: every submit is rejected
            from core.errors import SchedulerError

            with self.assertRaises(SchedulerError):
                stack.framework.scheduler.submit(
                    "alpha", PROBE, "message", channel_id=GUILD_CHANNEL, content="x"
                )
            # resume: scheduler re-armed, sends flow again
            resumed = await api.system_resume()
            self.assertTrue(resumed["resumed"])
            await api.accounts_start()  # accounts restart is explicit
            scheduler = stack.framework.scheduler
            scheduler.submit("alpha", PROBE, "message", channel_id=GUILD_CHANNEL, content="back")
            for _ in range(100):
                await asyncio.sleep(0.05)
                if "back" in stack.world.messages_of(GUILD_CHANNEL):
                    break
            self.assertIn("back", stack.world.messages_of(GUILD_CHANNEL))
        finally:
            await stack.stop()


class TestGroupRateScope(PacerStampAssertions, unittest.IsolatedAsyncioTestCase):
    """Per-account/group scope over the production bootstrap: accounts in
    one sync_group serialize their API activity on a shared clock."""

    async def test_group_members_serialize(self) -> None:
        stack = await Stack().boot()
        api = stack.framework.api
        group = "grinders"
        await api.accounts_add({"name": "alpha", "workspace": group})
        await api.accounts_add({"name": "beta", "workspace": group})
        self.assertEqual(
            (await api.ratelimit_stats())["groups"],
            {"alpha": "grinders", "beta": "grinders"},
        )
        await api.accounts_start()
        try:
            scheduler = stack.framework.scheduler

            async def submit_round(_total: int) -> None:  # the scheduler queues - no in-flight flow
                for account in ("alpha", "beta", "alpha", "beta"):
                    scheduler.submit(
                        account,
                        PROBE,
                        "message",
                        channel_id=GUILD_CHANNEL,
                        content=f"hi-{account}",
                    )

            await submit_round(1)
            members = set(stack.user_ids.values())
            # sample the shared clock's stamp as each send lands, to a
            # deadline rather than a fixed count of polls: four paced sends
            # are three 1.5s intervals (~t=4.5s) and instrumentation load
            # can stretch that — the sampler returns the moment the last
            # lands. "pacing cannot be judged" is the sampler's honesty
            # limit — a poll more than an interval late (multi-second
            # callback stalls on a busy desktop, observed in-gate
            # 2026-11-25) — not a pacing verdict: the retry re-measures a
            # fresh window; a REAL spacing violation never takes that path.
            stamps, leads, first = await stack.pacing_stamps_with_retry(
                f"group:{group}:message",
                GUILD_CHANNEL,
                members,
                4,
                submit_round,
                # the wire floor: interval minus the lateness budget (0.45s,
                # matching scenarios.py's PACING_TOLERANCE)
                wire_floor=POLICY_INTERVAL - 0.45,
            )
            await asyncio.sleep(0.3)
            history = [m for m in stack.world.messages_of(GUILD_CHANNEL) if m.startswith("hi-")]
            self.assertEqual(len(history), first + 3)  # four sends per attempt
            # STRICT, no clock: every member is paced at the configured
            # interval on its own (account, message) scope — the shared group
            # clock is what serializes the members against each other
            for account in stack.user_ids:
                effective = await stack.framework.api.ratelimit_effective("message", account)
                self.assertEqual(effective["interval"], POLICY_INTERVAL)
            # STRICT, on the pacer's own clock: consecutive stamps on the
            # SHARED clock are a full interval apart (Stack.pacing_stamps)
            jitters = stack.grant_jitters(set(stack.user_ids), "message", sends=first + 3)[
                first - 1 :
            ]
            self.assert_pacer_stamps_spaced(stamps, jitters)
            # STRICT, on the wire: no send left before the pacer admitted it
            self.assert_no_send_before_its_stamp(leads)
        finally:
            await stack.stop()


if __name__ == "__main__":
    unittest.main()
