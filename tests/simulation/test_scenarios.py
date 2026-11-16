"""Scenario registry tests.

The full-stack runs (runner + all managers + the workload fixture
plugin) are written as runnable code — all scenario runs execute the
full production stack (managers, scheduler, limiter, rich event model)
over the in-memory world. Everything that does not need the other
managers (registry shape, world setups, pure checks) runs now.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

from adapters.simulator.runner import SimulationRunner
from adapters.simulator.scenarios import (
    ACCOUNT_NAME,
    DEMO_PLUGIN_SETTINGS,
    EXPORT_COMMAND,
    IMPORT_PREFIX,
    PACING_INTERVAL,
    SCENARIOS,
    TRIGGER_WORD,
    Scenario,
    quiet_window,
)
from adapters.simulator.world import (
    SYNTHETIC_ALPHA_ID,
    SYNTHETIC_GUILD_CHANNEL,
    SYNTHETIC_MUDAE_ID,
    SYNTHETIC_OWNER_ID,
    SimWorld,
)
from core.errors import SimulationError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: the workload fixture directory (the framework bundles no plugins; the
#: scenarios drive the soulcopy-like flow plugin that lives here)
SIM_PLUGINS = ROOT / "tests" / "fixtures" / "sim_plugins"

#: scenario names the contract requires at minimum
REQUIRED_SCENARIOS = (
    "soulcopy_basic",
    "soulcopy_multichunk",
    "pacing_respected",
    "server_429_respected",
    "channel_gone_aborts",
    "control_mention",
    "estop_halt",
)


def _fabricated_world() -> SimWorld:
    """A world in the post-flow state soulcopy_basic expects (hand-driven)."""
    world = SimWorld()
    SCENARIOS["soulcopy_basic"].world_setup(world)
    world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, EXPORT_COMMAND)
    world.deliver(
        SYNTHETIC_GUILD_CHANNEL,
        SYNTHETIC_ALPHA_ID,
        IMPORT_PREFIX + "Alice$Bob$Charlie$Dana$Eve$Finn",
    )
    return world


def _audit_out(ts_values, contents) -> list:
    """Fabricated outbound audit entries (alpha, direction 'out')."""
    return [
        {
            "account": ACCOUNT_NAME,
            "direction": "out",
            "action": "message",
            "ts": ts,
            "content": content,
        }
        for ts, content in zip(ts_values, contents)
    ]


class TestRegistryShape(unittest.TestCase):
    """The registry itself is well-formed (no managers needed)."""

    def test_all_required_scenarios_registered(self) -> None:
        for name in REQUIRED_SCENARIOS:
            self.assertIn(name, SCENARIOS)
        self.assertEqual(len(SCENARIOS), len(REQUIRED_SCENARIOS))

    def test_fields_valid(self) -> None:
        import inspect

        for name, scenario in SCENARIOS.items():
            self.assertIsInstance(scenario, Scenario)
            self.assertEqual(scenario.name, name)
            self.assertTrue(scenario.description.strip(), name)
            self.assertTrue(name.replace("_", "").isalnum())
            self.assertTrue(callable(scenario.world_setup), name)
            self.assertTrue(callable(scenario.checks), name)
            self.assertTrue(inspect.iscoroutinefunction(scenario.script), name)

    def test_policies_sane(self) -> None:
        """Intervals at/above the 1.5s floor; jitter 0 <= a <= b."""
        for name, scenario in SCENARIOS.items():
            self.assertIsInstance(scenario.policies, dict)
            for action, spec in scenario.policies.items():
                self.assertIn("interval", spec, f"{name}/{action}")
                self.assertGreaterEqual(float(spec["interval"]), 1.5)
                jitter = spec.get("jitter", [0.0, 0.0])
                self.assertEqual(len(jitter), 2)
                self.assertLessEqual(0.0, float(jitter[0]))
                self.assertLessEqual(float(jitter[0]), float(jitter[1]))
                self.assertLessEqual(float(jitter[1]), 5.0)

    def test_runner_lists_registry(self) -> None:
        entries = SimulationRunner(plugin_dir=str(SIM_PLUGINS)).list()
        self.assertEqual(
            sorted(entry["name"] for entry in entries),
            sorted(REQUIRED_SCENARIOS),
        )
        for entry in entries:
            self.assertTrue(entry["description"].strip())

    def test_unknown_scenario_raises_simulation_error(self) -> None:
        runner = SimulationRunner(plugin_dir=str(SIM_PLUGINS))
        with self.assertRaises(SimulationError) as caught:
            runner.scenario("does_not_exist")
        self.assertIn("unknown scenario", str(caught.exception))
        self.assertIn("soulcopy_basic", str(caught.exception))  # known list hint

    def test_demo_settings_and_quiet_window_consistent(self) -> None:
        self.assertIn("soulcopy", DEMO_PLUGIN_SETTINGS)
        read_timeout = float(
            DEMO_PLUGIN_SETTINGS["soulcopy"]["dm_read_timeout"]
        )
        self.assertGreaterEqual(read_timeout, 1.0)
        self.assertAlmostEqual(quiet_window(), read_timeout + 1.2)


class TestWorldSetups(unittest.TestCase):
    """Every scenario's world_setup builds a usable starting topology."""

    def test_setups_create_topology_and_responder(self) -> None:
        for name in REQUIRED_SCENARIOS:
            world = SimWorld()
            SCENARIOS[name].world_setup(world)
            self.assertIn(SYNTHETIC_OWNER_ID, world.users, name)
            self.assertIn(SYNTHETIC_MUDAE_ID, world.users, name)
            self.assertIn(SYNTHETIC_ALPHA_ID, world.users, name)
            self.assertTrue(world.channel_exists(SYNTHETIC_GUILD_CHANNEL), name)
            self.assertEqual(world.responder_id, SYNTHETIC_MUDAE_ID, name)
            self.assertIn(EXPORT_COMMAND, world.responses, name)
            self.assertTrue(world.responses[EXPORT_COMMAND], name)


class TestChecksPure(unittest.TestCase):
    """Checks are pure functions over (audit, world): fabricate and verify."""

    def test_basic_passes_on_complete_flow(self) -> None:
        world = _fabricated_world()
        audit = _audit_out(
            [10.0, 11.8], [EXPORT_COMMAND, IMPORT_PREFIX + "Alice$Bob"]
        )
        self.assertEqual(SCENARIOS["soulcopy_basic"].checks(audit, world), [])

    def test_basic_fails_when_chunk_missing(self) -> None:
        world = SimWorld()
        SCENARIOS["soulcopy_basic"].world_setup(world)
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, EXPORT_COMMAND)
        audit = _audit_out([10.0], [EXPORT_COMMAND])
        failures = SCENARIOS["soulcopy_basic"].checks(audit, world)
        self.assertTrue(any("chunk" in f for f in failures))
        self.assertTrue(any("2 outbound" in f for f in failures))

    def test_basic_fails_on_multiple_chunks(self) -> None:
        world = _fabricated_world()
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                      IMPORT_PREFIX + "second")
        failures = SCENARIOS["soulcopy_basic"].checks([], world)
        self.assertTrue(any("1 chunk" in f for f in failures))

    def test_multichunk_passes_and_catches_oversize(self) -> None:
        world = SimWorld()
        SCENARIOS["soulcopy_multichunk"].world_setup(world)
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                      IMPORT_PREFIX + "a" * 100)
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                      IMPORT_PREFIX + "b" * 100)
        self.assertEqual(SCENARIOS["soulcopy_multichunk"].checks([], world), [])

        oversized = SimWorld()
        SCENARIOS["soulcopy_multichunk"].world_setup(oversized)
        oversized.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                          IMPORT_PREFIX + "x" * 2100)
        failures = SCENARIOS["soulcopy_multichunk"].checks([], oversized)
        self.assertTrue(any("2000" in f for f in failures))

    def test_pacing_pass_and_violation(self) -> None:
        check = SCENARIOS["pacing_respected"].checks
        good = _audit_out(
            [10.0, 11.7, 13.4, 15.1],
            [EXPORT_COMMAND, IMPORT_PREFIX + "a", IMPORT_PREFIX + "b",
             IMPORT_PREFIX + "c"],
        )
        self.assertEqual(check(good, SimWorld()), [])
        rushed = _audit_out(
            [10.0, 11.0, 13.0, 14.9],
            [EXPORT_COMMAND, IMPORT_PREFIX + "a", IMPORT_PREFIX + "b",
             IMPORT_PREFIX + "c"],
        )
        failures = check(rushed, SimWorld())
        self.assertTrue(any("pacing violated" in f for f in failures))
        self.assertLessEqual(
            PACING_INTERVAL, 1.7, "pacing scenario stays sim-cheap"
        )

    def test_pacing_needs_timestamps_and_three_sends(self) -> None:
        check = SCENARIOS["pacing_respected"].checks
        self.assertTrue(check([], SimWorld()))  # not enough sends
        no_ts = [{"account": ACCOUNT_NAME, "direction": "out", "content": "x"}
                 for _ in range(3)]
        failures = check(no_ts, SimWorld())
        self.assertTrue(any("timestamps" in f for f in failures))

    def test_server_429_pass_and_violations(self) -> None:
        scenario = SCENARIOS["server_429_respected"]
        world = SimWorld()
        scenario.world_setup(world)
        world.blocked_sends.append({"ts": 100.0, "retry_after": 0.8})
        export = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, EXPORT_COMMAND
        )
        export.ts = 100.9  # 0.9s later: >= retry_after - tolerance
        chunk = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, IMPORT_PREFIX + "x"
        )
        chunk.ts = 102.5
        self.assertEqual(scenario.checks([], world), [])

        too_fast = SimWorld()
        scenario.world_setup(too_fast)
        too_fast.blocked_sends.append({"ts": 100.0, "retry_after": 0.8})
        early = too_fast.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, EXPORT_COMMAND
        )
        early.ts = 100.1
        failures = scenario.checks([], too_fast)
        self.assertTrue(any("retry came" in f for f in failures))

        never_blocked = SimWorld()
        scenario.world_setup(never_blocked)
        failures = scenario.checks([], never_blocked)
        self.assertTrue(any("never fired" in f for f in failures))

    def test_channel_gone_pass_and_violations(self) -> None:
        scenario = SCENARIOS["channel_gone_aborts"]
        # pass: export went out, pages arrived, channel deleted, zero chunks
        world = SimWorld()
        scenario.world_setup(world)
        export = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, EXPORT_COMMAND
        )
        dm = world.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        world.deliver(dm, SYNTHETIC_MUDAE_ID, "Soulmates\n...", dm=True)
        world.channel_gone(SYNTHETIC_GUILD_CHANNEL)
        world.gone_channels[SYNTHETIC_GUILD_CHANNEL] = export.ts + 0.1
        self.assertEqual(scenario.checks([], world), [])

        # violation: a chunk landed despite the deletion
        leaked = SimWorld()
        scenario.world_setup(leaked)
        leaked.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, EXPORT_COMMAND
        )
        dm = leaked.dm_channel_for(SYNTHETIC_ALPHA_ID, SYNTHETIC_MUDAE_ID)
        leaked.deliver(dm, SYNTHETIC_MUDAE_ID, "page", dm=True)
        leaked.channel_gone(SYNTHETIC_GUILD_CHANNEL)
        leaked.deliver(dm, SYNTHETIC_ALPHA_ID, IMPORT_PREFIX + "leak", dm=True)
        failures = scenario.checks([], leaked)
        self.assertTrue(any("abort cleanly" in f for f in failures))

        # violation: flow never started (no pages) makes the test void
        empty = SimWorld()
        scenario.world_setup(empty)
        empty.channel_gone(SYNTHETIC_GUILD_CHANNEL)
        failures = scenario.checks([], empty)
        self.assertTrue(any("never started" in f for f in failures))

    def test_estop_halt_pass_and_violations(self) -> None:
        from adapters.simulator.scenarios import ESTOP_GRACE_S

        scenario = SCENARIOS["estop_halt"]
        audit = [{"account": "system", "direction": "out",
                  "action": "mgmt:estop", "ts": 100.0}]
        # pass: chunk one + halt + quiet + re-trigger + post-resume chunk
        world = SimWorld()
        scenario.world_setup(world)
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                      IMPORT_PREFIX + "one")
        retrigger = world.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "slcopy")
        retrigger.ts = 105.0
        world.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                      IMPORT_PREFIX + "two")  # after resume: fine
        self.assertEqual(scenario.checks(audit, world), [])

        # violation: a send after the halt (inside the re-trigger window)
        leaker = SimWorld()
        scenario.world_setup(leaker)
        leaker.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                       IMPORT_PREFIX + "one")
        late = leaker.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "post-halt send")
        late.ts = 102.0  # 2.0s past the estop: outside any in-flight grace
        retrigger = leaker.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "slcopy")
        retrigger.ts = 105.0
        failures = scenario.checks(audit, leaker)
        self.assertTrue(any("after the halt" in f for f in failures))

        # in-flight grace: a send INSIDE the grace window is not a leak
        grace = SimWorld()
        scenario.world_setup(grace)
        grace.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                      IMPORT_PREFIX + "one")
        racing = grace.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID, "mid-flight")
        racing.ts = 100.0 + ESTOP_GRACE_S / 2
        retrigger = grace.deliver(
            SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_OWNER_ID, "slcopy")
        retrigger.ts = 105.0
        grace.deliver(SYNTHETIC_GUILD_CHANNEL, SYNTHETIC_ALPHA_ID,
                      IMPORT_PREFIX + "two")
        self.assertEqual(scenario.checks(audit, grace), [])

        # violation: estop never executed
        self.assertTrue(any("never executed" in f for f in
                            scenario.checks([], SimWorld())))
        # violation: no re-trigger after the halt
        halted = SimWorld()
        scenario.world_setup(halted)
        failures = scenario.checks(audit, halted)
        self.assertTrue(any("re-triggered" in f for f in failures))



class TestFullStackRuns(unittest.IsolatedAsyncioTestCase):
    """End-to-end: runner builds the whole stack and the scenario passes.

    Requires database.db + core.rate_limit_manager + core.task_scheduler +
    core.audit + core.plugin_manager + core.profile_manager +
    core.account_manager AND the workload fixture under
    tests/fixtures/sim_plugins (the framework bundles no plugins).
    """

    async def _run(self, name: str) -> dict:
        runner = SimulationRunner(plugin_dir=str(SIM_PLUGINS))
        report = await runner.run(name)
        for key in ("name", "passed", "duration_s", "failures", "log_lines",
                    "interactions", "summary"):
            self.assertIn(key, report)
        if not report["passed"]:
            tail = "\n".join(report["log_lines"][-40:])
            self.fail(
                f"{name} failed: {report['failures']}\nlog tail:\n{tail}"
            )
        return report

    async def test_soulcopy_basic(self) -> None:
        await self._run("soulcopy_basic")

    async def test_soulcopy_multichunk(self) -> None:
        await self._run("soulcopy_multichunk")

    async def test_pacing_respected(self) -> None:
        await self._run("pacing_respected")

    async def test_server_429_respected(self) -> None:
        await self._run("server_429_respected")

    async def test_channel_gone_aborts(self) -> None:
        await self._run("channel_gone_aborts")

    async def test_estop_halt(self) -> None:
        await self._run("estop_halt")

    async def test_report_carries_evidence(self) -> None:
        """A passing run leaves interactions and spec-§9 log lines behind."""
        report = await self._run("soulcopy_basic")
        self.assertTrue(report["interactions"])
        self.assertTrue(report["log_lines"])
        for line in report["log_lines"]:
            self.assertRegex(line, r"^\d+\.\d{3}s (INFO|WARNING|ERROR|DEBUG) ")
        self.assertIn("passed", report["summary"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
