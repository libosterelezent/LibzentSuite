"""CLI command-engine + REPL tests (fake ControlAPI; no real managers)."""
from __future__ import annotations

import io
import unittest
from typing import Any, List, Optional, Tuple

from interfaces.cli.commands import (
    HELP,
    QuitRequested,
    UsageError,
    format_account_info,
    format_accounts,
    format_audit,
    format_effective,
    format_history,
    format_logs,
    format_plugins,
    format_plugin_state,
    format_policies,
    format_profiles,
    format_simulation_report,
    format_tasks,
    format_warnings,
    help_text,
    run_command,
)
from interfaces.cli.repl import Repl, render_error, run_once


class FakeApi:
    """Duck-typed ControlAPI recording every call; canned payloads."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Tuple, dict]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    # ---- status
    async def status(self):
        self._record("status")
        return {
            "uptime_s": 12.5, "tasks": {"queued": 1, "running": 0, "completed": 3},
            "accounts": [{"name": "alpha", "status": "running", "user_id": 42,
                          "enabled": True, "profiles": ["roller"]}],
            "ratelimit": {"policies": [{"level": "global", "owner": None,
                                        "action": "message", "interval": 2.0,
                                        "jitter": [0.1, 0.5]}]},
            "warnings": [{"level": "warn", "code": "aggressive", "message": "fast"}],
        }

    # ---- accounts
    async def accounts_list(self):
        self._record("accounts_list")
        return [{"name": "alpha", "status": "running", "user_id": 42, "enabled": True,
                 "profiles": ["roller"], "latency_ms": 83.0, "uptime_s": 100.0}]

    async def accounts_add(self, payload):
        self._record("accounts_add", payload)
        return {"added": payload["name"], "profiles": payload.get("profiles", [])}

    async def accounts_remove(self, name):
        self._record("accounts_remove", name)
        return {"removed": name}

    async def accounts_info(self, name):
        self._record("accounts_info", name)
        return {
            "name": name,
            "status": {"connected": True, "state": "running", "latency_ms": 83.0,
                       "uptime_s": 8040.0},
            "identity": {"name": "Alpha#0", "discord_id": 42},
            "runtime": {"cached_guilds": 3, "cached_channels": 12, "cached_dms": 2,
                        "cache_source": "client cache (READY)"},
            "plugins": [{"name": "soulcopy", "enabled": True},
                        {"name": "roll", "enabled": False}],
            "profiles": ["collector"],
            "rate_limit": {"message": {"interval": 2.0, "jitter": [0.1, 0.5],
                                       "source": "inherited from global"}},
        }

    async def accounts_enable(self, name, enabled):
        self._record("accounts_enable", name, enabled)
        return {"name": name, "enabled": enabled}

    async def accounts_start(self, name=None):
        self._record("accounts_start", name)
        return {"started": name or "all"}

    async def accounts_stop(self, name=None):
        self._record("accounts_stop", name)
        return {"stopped": name or "all"}

    async def accounts_emergency_stop(self, name):
        self._record("accounts_emergency_stop", name)
        return {"account": name, "cancelled_tasks": 2}

    # ---- plugins
    async def plugins_manifests(self):
        self._record("plugins_manifests")
        return [{"name": "soulcopy", "version": "2.0.0", "description": "export",
                 "settings": [{"name": "trigger", "type": "str", "default": "slcopy",
                               "description": "word", "min": None, "max": None}]}]

    async def plugins_state(self, account):
        self._record("plugins_state", account)
        return [{"name": "soulcopy", "enabled": True, "loaded": True,
                 "settings": {"trigger": "slcopy"}, "last_error": None, "error_count": 0}]

    async def plugins_set_enabled(self, account, plugin, enabled):
        self._record("plugins_set_enabled", account, plugin, enabled)
        return {"account": account, "plugin": plugin, "enabled": enabled}

    async def plugins_settings(self, account):
        self._record("plugins_settings", account)
        return [{"name": "soulcopy", "settings": {"trigger": "slcopy"}}]

    async def plugins_set_setting(self, account, plugin, key, value):
        self._record("plugins_set_setting", account, plugin, key, value)
        return {"account": account, "plugin": plugin, "key": key, "value": value}

    # ---- profiles
    async def profiles_list(self):
        self._record("profiles_list")
        return [{"name": "roller", "description": "rolls", "plugins": ["roll"]}]

    async def profiles_create(self, name, plugins, description=""):
        self._record("profiles_create", name, plugins, description)
        return {"name": name, "plugins": plugins}

    async def profiles_delete(self, name):
        self._record("profiles_delete", name)
        return {"deleted": name, "existed": True}

    async def profiles_assign(self, account, profile):
        self._record("profiles_assign", account, profile)
        return {"account": account, "profiles": [profile]}

    async def profiles_unassign(self, account, profile):
        self._record("profiles_unassign", account, profile)
        return {"account": account, "profiles": []}

    # ---- ratelimit
    async def ratelimit_policies(self):
        self._record("ratelimit_policies")
        return [{"level": "global", "owner": None, "action": "message",
                 "interval": 2.0, "jitter": [0.1, 0.5]}]

    async def ratelimit_set(self, action, interval, jitter_min=0.0, jitter_max=0.0,
                            level="global", owner=None):
        self._record("ratelimit_set", action, interval, jitter_min, jitter_max,
                     level, owner)
        return {"action": action, "level": level, "owner": owner, "policy": "2.00s"}

    async def ratelimit_clear(self, level, owner, action):
        self._record("ratelimit_clear", level, owner, action)
        return {"cleared": True, "level": level, "owner": owner, "action": action}

    async def ratelimit_effective(self, action, account=None, plugin=None):
        self._record("ratelimit_effective", action, account, plugin)
        return {"action": action, "interval": 3.0, "jitter": [0.0, 1.0],
                "sources": [{"level": "account", "owner": "alpha", "interval": 3.0}]}

    async def ratelimit_stats(self):
        self._record("ratelimit_stats")
        return {"floor": 1.5, "grants": 10}

    async def ratelimit_warnings(self):
        self._record("ratelimit_warnings")
        return [{"level": "warn", "code": "aggressive", "message": "fast policy"}]

    async def ratelimit_set_floor(self, seconds):
        self._record("ratelimit_set_floor", seconds)
        return {"floor": seconds}

    async def ratelimit_lane(self):
        self._record("ratelimit_lane")
        return {"enabled": True, "prefixrate": 1.5, "paused": False,
                "waiting": 2, "grants": 9, "last_grant_age_s": 0.4,
                "held_off_seconds": 0.0}

    async def ratelimit_set_lane(self, enabled=None, prefixrate=None):
        self._record("ratelimit_set_lane", enabled, prefixrate)
        return {"enabled": bool(enabled) if enabled is not None else True,
                "prefixrate": prefixrate if prefixrate is not None else 1.5,
                "paused": False, "waiting": 0, "grants": 9,
                "last_grant_age_s": None, "held_off_seconds": 0.0}

    # ---- tasks
    async def tasks_list(self, status=None, account=None):
        self._record("tasks_list", status=status, account=account)
        return [{"id": 7, "account": "alpha", "plugin": "soulcopy", "action": "message",
                 "priority": 2, "status": "queued", "age_s": 1.2}]

    async def tasks_cancel(self, task_id):
        self._record("tasks_cancel", task_id)
        return {"cancelled": True, "task_id": task_id}

    async def tasks_cancel_matching(self, account=None, plugin=None):
        self._record("tasks_cancel_matching", account, plugin)
        return {"cancelled": 3}

    async def tasks_history(self, limit=50, account=None):
        self._record("tasks_history", limit, account)
        return [{"id": 7, "account": "alpha", "action": "message",
                 "status": "completed", "duration_s": 0.4}]

    # ---- simulation
    async def simulation_list(self):
        self._record("simulation_list")
        return [{"name": "soulcopy_basic", "description": "two pages"}]

    async def simulation_run(self, name):
        self._record("simulation_run", name)
        return {"name": name, "passed": True, "duration_s": 1.2, "failures": [],
                "log_lines": ["00:00.100 INFO core ready"],
                "interactions": [], "summary": "ok"}

    # ---- logs/audit
    async def logs_tail(self, n=100, account=None, severity=None):
        self._record("logs_tail", n, account, severity)
        return [{"ts": 1700000000.5, "severity": "INFO", "source": "core",
                 "message": "ready", "account": None, "plugin": None,
                 "task_id": None, "event": None}]

    async def audit_tail(self, n=100, account=None):
        self._record("audit_tail", n, account)
        return [{"ts": 1700000000.5, "account": "alpha", "direction": "out",
                 "action": "message", "channel_id": 500, "content": "$slud-s",
                 "wait_ms": 150.0, "jitter_ms": 300.0, "note": "plugin=soulcopy"}]

    # ---- system
    async def emergency_stop_all(self):
        self._record("emergency_stop_all")
        return {"cancelled_tasks": 5, "halted": True}

    async def shutdown(self):
        self._record("shutdown")


def called(api: FakeApi, name: str) -> Optional[Tuple[Tuple, dict]]:
    """Args/kwargs of the MOST RECENT call with this method name."""
    result: Optional[Tuple[Tuple, dict]] = None
    for cname, args, kwargs in api.calls:
        if cname == name:
            result = (args, kwargs)
    return result


class TestBasics(unittest.IsolatedAsyncioTestCase):
    async def test_help(self) -> None:
        text = await run_command(FakeApi(), "help")
        self.assertIn("accounts", text)
        self.assertIn("usage: accounts", text)

    async def test_empty_line(self) -> None:
        self.assertIsNone(await run_command(FakeApi(), "   "))

    async def test_quit_raises(self) -> None:
        with self.assertRaises(QuitRequested):
            await run_command(FakeApi(), "quit")
        with self.assertRaises(QuitRequested):
            await run_command(FakeApi(), "exit")

    async def test_unknown_command(self) -> None:
        text = await run_command(FakeApi(), "frobnicate")
        self.assertIn("unknown command", text)

    async def test_unclosed_quotes(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), 'accounts add "bob')


class TestAccountsGroup(unittest.IsolatedAsyncioTestCase):
    async def test_list(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts list")
        self.assertIn("alpha", text)
        self.assertIn("42", text)
        self.assertTrue(called(api, "accounts_list"))

    async def test_add_with_profiles(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts add bob roller sniper")
        args, _ = called(api, "accounts_add")
        self.assertEqual(args[0], {"name": "bob", "profiles": ["roller", "sniper"]})

    async def test_add_bare(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts add bob")
        args, _ = called(api, "accounts_add")
        self.assertEqual(args[0], {"name": "bob"})

    async def test_info_renders_sections(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts info alpha")
        self.assertTrue(called(api, "accounts_info"))
        for needle in ("Status", "Identity", "Runtime", "client cache", "Plugins",
                       "Profile", "Rate Limit", "inherited from global"):
            self.assertIn(needle, text)

    async def test_enable_on_off(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts enable alpha on")
        args, _ = called(api, "accounts_enable")
        self.assertEqual(args, ("alpha", True))
        await run_command(api, "accounts enable alpha off")
        args, _ = called(api, "accounts_enable")
        self.assertEqual(args, ("alpha", False))
        with self.assertRaises(UsageError):
            await run_command(api, "accounts enable alpha maybe")

    async def test_start_stop_all_and_named(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts start all")
        self.assertEqual(called(api, "accounts_start")[0], (None,))
        await run_command(api, "accounts start alpha")
        self.assertEqual(called(api, "accounts_start")[0], ("alpha",))
        await run_command(api, "accounts stop")
        self.assertEqual(called(api, "accounts_stop")[0], (None,))

    async def test_estop_account(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts estop alpha")
        self.assertEqual(called(api, "accounts_emergency_stop")[0], ("alpha",))

    async def test_wrong_arg_count(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts remove")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts info a b")

    async def test_unknown_subcommand(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts frobnicate")


class TestPluginsGroup(unittest.IsolatedAsyncioTestCase):
    async def test_list(self) -> None:
        api = FakeApi()
        text = await run_command(api, "plugins list")
        self.assertIn("soulcopy", text)
        self.assertIn("trigger", text)

    async def test_enable_disable(self) -> None:
        api = FakeApi()
        await run_command(api, "plugins enable alpha soulcopy")
        self.assertEqual(called(api, "plugins_set_enabled")[0],
                         ("alpha", "soulcopy", True))
        await run_command(api, "plugins disable alpha soulcopy")
        self.assertEqual(called(api, "plugins_set_enabled")[0],
                         ("alpha", "soulcopy", False))

    async def test_set(self) -> None:
        api = FakeApi()
        await run_command(api, "plugins set alpha soulcopy trigger go")
        self.assertEqual(called(api, "plugins_set_setting")[0],
                         ("alpha", "soulcopy", "trigger", "go"))


class TestProfilesGroup(unittest.IsolatedAsyncioTestCase):
    async def test_create_requires_plugins(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "profiles create roller")

    async def test_create_assign_unassign_delete(self) -> None:
        api = FakeApi()
        await run_command(api, "profiles create roller roll")
        self.assertEqual(called(api, "profiles_create")[0], ("roller", ["roll"], ""))
        await run_command(api, "profiles assign alpha roller")
        self.assertEqual(called(api, "profiles_assign")[0], ("alpha", "roller"))
        await run_command(api, "profiles unassign alpha roller")
        await run_command(api, "profiles delete roller")
        text = await run_command(api, "profiles list")
        self.assertIn("roller", text)


class TestRateLimitGroup(unittest.IsolatedAsyncioTestCase):
    async def test_set_global(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit set message 2.5")
        self.assertEqual(called(api, "ratelimit_set")[0],
                         ("message", 2.5, 0.0, 0.0, "global", None))

    async def test_set_jitter_and_level(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit set message 2.5 0.2 0.8 account alpha")
        self.assertEqual(called(api, "ratelimit_set")[0],
                         ("message", 2.5, 0.2, 0.8, "account", "alpha"))

    async def test_set_bad_number(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            await run_command(FakeApi(), "ratelimit set message x")
        self.assertIn("not a number", str(ctx.exception))

    async def test_set_bad_arg_count(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "ratelimit set message 2.5 0.2")

    async def test_set_bad_level(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "ratelimit set message 2.5 0 0 weird alpha")

    async def test_set_account_plugin_owner(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit set dm 4 0 0 account_plugin alpha/soulcopy")
        self.assertEqual(called(api, "ratelimit_set")[0][4], "account_plugin")
        self.assertEqual(called(api, "ratelimit_set")[0][5], "alpha/soulcopy")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(),
                              "ratelimit set dm 4 0 0 account_plugin alpha")

    async def test_clear_global_dash_owner(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit clear global - message")
        self.assertEqual(called(api, "ratelimit_clear")[0], ("global", None, "message"))

    async def test_effective_and_warnings(self) -> None:
        api = FakeApi()
        text = await run_command(api, "ratelimit effective message alpha soulcopy")
        self.assertEqual(called(api, "ratelimit_effective")[0],
                         ("message", "alpha", "soulcopy"))
        self.assertIn("account", text)
        await run_command(api, "ratelimit warnings")
        self.assertTrue(called(api, "ratelimit_warnings"))

    async def test_floor(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit floor 2.0")
        self.assertEqual(called(api, "ratelimit_set_floor")[0], (2.0,))

    async def test_lane_show(self) -> None:
        api = FakeApi()
        text = await run_command(api, "ratelimit lane")
        self.assertTrue(called(api, "ratelimit_lane"))
        self.assertIn("sequential sends", text)
        self.assertIn("active", text)

    async def test_lane_on_with_rate(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit lane on 2.5")
        self.assertEqual(called(api, "ratelimit_set_lane")[0], (True, 2.5))

    async def test_lane_on_bare_and_off(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit lane on")
        self.assertEqual(called(api, "ratelimit_set_lane")[0], (True, None))
        await run_command(api, "ratelimit lane off")
        self.assertEqual(called(api, "ratelimit_set_lane")[0], (False, None))

    async def test_lane_rate_zero_pauses(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit lane rate 0")
        self.assertEqual(called(api, "ratelimit_set_lane")[0], (None, 0.0))

    async def test_lane_bad_inputs(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "ratelimit lane sideways")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "ratelimit lane on x")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "ratelimit lane rate")


class TestTasksGroup(unittest.IsolatedAsyncioTestCase):
    async def test_list(self) -> None:
        api = FakeApi()
        text = await run_command(api, "tasks list")
        self.assertTrue(called(api, "tasks_list"))
        self.assertIn("soulcopy", text)

    async def test_cancel(self) -> None:
        api = FakeApi()
        await run_command(api, "tasks cancel 7")
        self.assertEqual(called(api, "tasks_cancel")[0], (7,))

    async def test_history(self) -> None:
        api = FakeApi()
        await run_command(api, "tasks history 5")
        self.assertEqual(called(api, "tasks_history")[0], (5, None))

    async def test_cancel_matching(self) -> None:
        api = FakeApi()
        await run_command(api, "tasks cancel-matching alpha soulcopy")
        self.assertEqual(called(api, "tasks_cancel_matching")[0],
                         ("alpha", "soulcopy"))


class TestSimulationLogsAudit(unittest.IsolatedAsyncioTestCase):
    async def test_simulation_list_and_run(self) -> None:
        api = FakeApi()
        text = await run_command(api, "simulation list")
        self.assertIn("soulcopy_basic", text)
        text = await run_command(api, "simulation run soulcopy_basic")
        self.assertTrue(called(api, "simulation_run"))
        self.assertIn("PASS", text.upper())

    async def test_simulation_run_needs_name(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            await run_command(FakeApi(), "simulation run")
        self.assertIn("soulcopy_basic", str(ctx.exception))  # lists available

    async def test_logs_and_audit(self) -> None:
        api = FakeApi()
        text = await run_command(api, "logs 5 alpha")
        self.assertEqual(called(api, "logs_tail")[0], (5, "alpha", None))
        self.assertIn("core", text)
        text = await run_command(api, "audit 5 alpha")
        self.assertEqual(called(api, "audit_tail")[0], (5, "alpha"))
        self.assertIn("$slud-s", text)
        self.assertIn("wait", text)

    async def test_estop_global(self) -> None:
        api = FakeApi()
        await run_command(api, "estop")
        self.assertTrue(called(api, "emergency_stop_all"))


class TestFormatters(unittest.TestCase):
    def test_format_accounts_empty(self) -> None:
        self.assertIn("no accounts", format_accounts([]))

    def test_format_account_info_marks_runtime_cache(self) -> None:
        text = format_account_info(FakeApi().accounts_info.__wrapped__ if False else {
            "name": "alpha",
            "status": {"connected": True, "state": "running", "latency_ms": 83.0,
                       "uptime_s": 8040.0},
            "identity": {"name": "Alpha#0", "discord_id": 42},
            "runtime": {"cached_guilds": 3, "cached_channels": 12, "cached_dms": 2,
                        "cache_source": "client cache (READY)"},
            "plugins": [{"name": "soulcopy", "enabled": True}],
            "profiles": ["collector"],
            "rate_limit": {"message": {"interval": 2.0, "jitter": [0.1, 0.5],
                                       "source": "inherited from global"}},
        })
        self.assertIn("client cache", text)
        self.assertIn("inherited from global", text)

    def test_format_warnings_icons(self) -> None:
        text = format_warnings([{"level": "warn", "code": "aggressive", "message": "m"}])
        self.assertIn("[!]", text)
        self.assertIn("aggressive", text)

    def test_format_simulation_report_failure(self) -> None:
        text = format_simulation_report({
            "name": "x", "passed": False, "duration_s": 1.0,
            "failures": ["chunk missing"], "log_lines": ["00:00.001 INFO core go"],
            "interactions": [], "summary": "failed",
        })
        self.assertIn("FAIL", text.upper())
        self.assertIn("chunk missing", text)


class TestRepl(unittest.IsolatedAsyncioTestCase):
    async def test_quit_calls_shutdown_every_path(self) -> None:
        for lines, marker in (
            (["quit"], "quit path"),
            ([""], "EOF path"),
            (["help", "status"], "last-command path continues then EOF"),
        ):
            api = FakeApi()
            out = io.StringIO()
            repl = Repl(api, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
            await repl.run()
            self.assertTrue(called(api, "shutdown"), marker)
            self.assertIn("bye.", out.getvalue())

    async def test_error_renders_one_line(self) -> None:
        class ExplodingApi(FakeApi):
            async def status(self):
                raise RuntimeError("boom\nwith newline")

        out = io.StringIO()
        repl = Repl(ExplodingApi(), stdin=io.StringIO("status\n"), stdout=out)
        await repl.run()
        self.assertIn("error: boom with newline", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    async def test_render_error_keeps_type_for_non_domain(self) -> None:
        self.assertIn("KeyError", render_error(KeyError("x")))

    async def test_run_once(self) -> None:
        api = FakeApi()
        out = io.StringIO()
        code = await run_once(api, ["accounts", "list"], stdout=out)
        self.assertEqual(code, 0)
        self.assertIn("alpha", out.getvalue())
        code = await run_once(api, [], stdout=out)
        self.assertEqual(code, 0)
        # a command that RAISES renders an error line and exits 1
        code = await run_once(api, ["ratelimit", "set", "message", "x"], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("error:", out.getvalue())
        code = await run_once(api, ["quit"], stdout=out)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
