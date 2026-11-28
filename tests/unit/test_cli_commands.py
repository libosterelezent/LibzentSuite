"""CLI command-engine + REPL tests (fake ControlAPI; no real managers)."""

from __future__ import annotations

import asyncio
import io
import unittest
from types import SimpleNamespace as NS
from typing import Any, ClassVar

from libzentsuite.interfaces.cli.commands import (
    HELP,
    QuitRequested,
    UsageError,
    format_account_info,
    format_account_plugins,
    format_accounts,
    format_audit,
    format_history,
    format_lane,
    format_logs,
    format_policies,
    format_simulation_report,
    format_warnings,
    run_command,
)
from libzentsuite.interfaces.cli.repl import Repl, render_error, run_once


class FakeApi:
    """Duck-typed ControlAPI recording every call; canned payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    # ---- status
    async def status(self) -> dict[str, Any]:
        self._record("status")
        return {
            "uptime_s": 12.5,
            "tasks": {"queued": 1, "running": 0, "completed": 3},
            "accounts": [
                {
                    "name": "alpha",
                    "status": "running",
                    "user_id": 42,
                    "enabled": True,
                    "assigned_plugins": ["roll"],
                }
            ],
            "ratelimit": {
                "policies": [
                    {
                        "level": "global",
                        "owner": None,
                        "action": "message",
                        "interval": 2.0,
                        "jitter": [0.1, 0.5],
                    }
                ]
            },
            "warnings": [{"level": "warn", "code": "aggressive", "message": "fast"}],
        }

    # ---- accounts
    async def accounts_list(self) -> list[dict[str, Any]]:
        self._record("accounts_list")
        return [
            {
                "name": "alpha",
                "status": "running",
                "user_id": 42,
                "enabled": True,
                "assigned_plugins": ["exporter", "roll"],
                "latency_ms": 83.0,
                "uptime_s": 100.0,
            }
        ]

    async def accounts_add(self, payload: dict[str, Any], surface: str = "api") -> dict[str, Any]:
        self._record("accounts_add", payload, surface=surface)
        return {
            "added": payload["name"],
            "plugins": sorted(payload.get("plugins", [])),
            "credential": "db" if payload.get("credential") else None,
        }

    # ---- account plugin assignments (direct account→plugin capabilities)
    async def accounts_plugins_list(self, account: str) -> list[str]:
        self._record("accounts_plugins_list", account)
        return ["exporter", "roll"]

    async def accounts_plugin_assign(
        self, account: str, plugin: str, surface: str = "api"
    ) -> dict[str, Any]:
        self._record("accounts_plugin_assign", account, plugin, surface=surface)
        return {"account": account, "plugin": plugin}

    async def accounts_plugin_unassign(
        self, account: str, plugin: str, surface: str = "api"
    ) -> dict[str, Any]:
        self._record("accounts_plugin_unassign", account, plugin, surface=surface)
        return {"account": account, "plugin": plugin, "existed": True}

    async def accounts_remove(self, name: str, surface: str = "api") -> dict[str, Any]:
        self._record("accounts_remove", name, surface=surface)
        return {"removed": name}

    async def accounts_set_credential(
        self, name: str, credential: str, surface: str = "api"
    ) -> dict[str, Any]:
        self._record("accounts_set_credential", name, surface=surface)
        return {"account": name, "credential": "db"}

    async def accounts_credential_status(self) -> list[dict[str, Any]]:
        self._record("accounts_credential_status")
        return [{"name": "alpha", "credential": "db"}, {"name": "beta", "credential": "missing"}]

    async def credentials_import_env(
        self, overwrite: bool = False, surface: str = "api"
    ) -> dict[str, Any]:
        self._record("credentials_import_env", overwrite=overwrite, surface=surface)
        return {"imported": ["alpha"], "skipped": ["beta"], "failed": {}}

    async def cooldowns_list(self, account: str | None = None) -> list[dict[str, Any]]:
        self._record("cooldowns_list", account)
        return [
            {
                "account": "alpha",
                "plugin": "roll",
                "cooldown_key": "vote",
                "remaining_s": 90.0,
                "source": "plugin",
                "reason": "",
            }
        ]

    async def cooldowns_clear(
        self, account: str, plugin: str, cooldown_key: str | None = None, surface: str = "api"
    ) -> dict[str, Any]:
        self._record("cooldowns_clear", account, plugin, cooldown_key, surface=surface)
        return {"account": account, "plugin": plugin, "cooldown_key": cooldown_key, "cleared": 1}

    async def accounts_info(self, name: str) -> dict[str, Any]:
        self._record("accounts_info", name)
        return {
            "name": name,
            "status": {
                "connected": True,
                "state": "running",
                "latency_ms": 83.0,
                "uptime_s": 8040.0,
            },
            "identity": {"name": "Alpha#0", "discord_id": 42},
            "runtime": {
                "cached_guilds": 3,
                "cached_channels": 12,
                "cached_dms": 2,
                "cache_source": "client cache (READY)",
            },
            "plugins": [{"name": "exporter", "enabled": True}, {"name": "roll", "enabled": False}],
            "rate_limit": {
                "message": {
                    "interval": 2.0,
                    "jitter": [0.1, 0.5],
                    "source": "inherited from global",
                }
            },
        }

    async def accounts_enable(self, name: str, enabled: bool) -> dict[str, Any]:
        self._record("accounts_enable", name, enabled)
        return {"name": name, "enabled": enabled}

    async def accounts_start(self, name: str | None = None, surface: str = "api") -> dict[str, Any]:
        self._record("accounts_start", name, surface=surface)
        return {"started": name or "all"}

    async def accounts_stop(self, name: str | None = None, surface: str = "api") -> dict[str, Any]:
        self._record("accounts_stop", name, surface=surface)
        return {"stopped": name or "all"}

    async def accounts_emergency_stop(self, name: str, surface: str = "api") -> dict[str, Any]:
        self._record("accounts_emergency_stop", name, surface=surface)
        return {"account": name, "cancelled_tasks": 2}

    # ---- plugins
    async def plugins_manifests(self) -> list[dict[str, Any]]:
        self._record("plugins_manifests")
        return [
            {
                "name": "exporter",
                "version": "2.0.0",
                "description": "export",
                "settings": [
                    {
                        "name": "trigger",
                        "type": "str",
                        "default": "export",
                        "description": "word",
                        "min": None,
                        "max": None,
                    }
                ],
            }
        ]

    async def plugins_state(self, account: str) -> list[dict[str, Any]]:
        self._record("plugins_state", account)
        return [
            {
                "name": "exporter",
                "enabled": True,
                "loaded": True,
                "settings": {"trigger": "export"},
                "last_error": None,
                "error_count": 0,
            }
        ]

    async def plugins_set_enabled(self, account: str, plugin: str, enabled: bool) -> dict[str, Any]:
        self._record("plugins_set_enabled", account, plugin, enabled)
        return {"account": account, "plugin": plugin, "enabled": enabled}

    async def plugins_settings(self, account: str) -> list[dict[str, Any]]:
        self._record("plugins_settings", account)
        return [{"name": "exporter", "settings": {"trigger": "export"}}]

    async def plugins_set_setting(
        self, account: str, plugin: str, key: str, value: Any
    ) -> dict[str, Any]:
        self._record("plugins_set_setting", account, plugin, key, value)
        return {"account": account, "plugin": plugin, "key": key, "value": value}

    # ---- ratelimit
    async def ratelimit_policies(self) -> list[dict[str, Any]]:
        self._record("ratelimit_policies")
        return [
            {
                "level": "global",
                "owner": None,
                "action": "message",
                "interval": 2.0,
                "jitter": [0.1, 0.5],
            }
        ]

    async def ratelimit_set(
        self,
        action: str,
        interval: float,
        jitter_min: float = 0.0,
        jitter_max: float = 0.0,
        level: str = "global",
        owner: str | None = None,
    ) -> dict[str, Any]:
        self._record("ratelimit_set", action, interval, jitter_min, jitter_max, level, owner)
        return {"action": action, "level": level, "owner": owner, "policy": "2.00s"}

    async def ratelimit_clear(self, level: str, owner: str | None, action: str) -> dict[str, Any]:
        self._record("ratelimit_clear", level, owner, action)
        return {"cleared": True, "level": level, "owner": owner, "action": action}

    async def ratelimit_effective(
        self, action: str, account: str | None = None, plugin: str | None = None
    ) -> dict[str, Any]:
        self._record("ratelimit_effective", action, account, plugin)
        return {
            "action": action,
            "interval": 3.0,
            "jitter": [0.0, 1.0],
            "sources": [{"level": "account", "owner": "alpha", "interval": 3.0}],
        }

    async def ratelimit_stats(self) -> dict[str, Any]:
        self._record("ratelimit_stats")
        return {"floor": 1.5, "grants": 10}

    async def ratelimit_warnings(self) -> list[dict[str, Any]]:
        self._record("ratelimit_warnings")
        return [{"level": "warn", "code": "aggressive", "message": "fast policy"}]

    async def ratelimit_set_floor(self, seconds: float) -> dict[str, Any]:
        self._record("ratelimit_set_floor", seconds)
        return {"floor": seconds}

    async def ratelimit_lane(self) -> dict[str, Any]:
        self._record("ratelimit_lane")
        return {
            "enabled": True,
            "prefixrate": 1.5,
            "paused": False,
            "waiting": 2,
            "grants": 9,
            "last_grant_age_s": 0.4,
            "held_off_seconds": 0.0,
        }

    async def ratelimit_set_lane(
        self, enabled: bool | None = None, prefixrate: float | None = None
    ) -> dict[str, Any]:
        self._record("ratelimit_set_lane", enabled, prefixrate)
        return {
            "enabled": bool(enabled) if enabled is not None else True,
            "prefixrate": prefixrate if prefixrate is not None else 1.5,
            "paused": False,
            "waiting": 0,
            "grants": 9,
            "last_grant_age_s": None,
            "held_off_seconds": 0.0,
        }

    # ---- tasks
    async def tasks_list(
        self, status: str | None = None, account: str | None = None
    ) -> list[dict[str, Any]]:
        self._record("tasks_list", status=status, account=account)
        return [
            {
                "id": 7,
                "account": "alpha",
                "plugin": "exporter",
                "action": "message",
                "priority": 2,
                "status": "queued",
                "age_s": 1.2,
            }
        ]

    async def tasks_cancel(self, task_id: int) -> dict[str, Any]:
        self._record("tasks_cancel", task_id)
        return {"cancelled": True, "task_id": task_id}

    async def tasks_cancel_matching(
        self, account: str | None = None, plugin: str | None = None
    ) -> dict[str, Any]:
        self._record("tasks_cancel_matching", account, plugin)
        return {"cancelled": 3}

    async def tasks_history(
        self, limit: int = 50, account: str | None = None
    ) -> list[dict[str, Any]]:
        self._record("tasks_history", limit, account)
        return [
            {
                "id": 7,
                "account": "alpha",
                "action": "message",
                "status": "completed",
                "duration_s": 0.4,
            }
        ]

    # ---- simulation
    async def simulation_list(self) -> list[dict[str, Any]]:
        self._record("simulation_list")
        return [{"name": "export_basic", "description": "two pages"}]

    async def simulation_run(self, name: str) -> dict[str, Any]:
        self._record("simulation_run", name)
        return {
            "name": name,
            "passed": True,
            "duration_s": 1.2,
            "failures": [],
            "log_lines": ["00:00.100 INFO core ready"],
            "interactions": [],
            "summary": "ok",
        }

    # ---- logs/audit
    async def logs_tail(
        self,
        n: int = 100,
        account: str | None = None,
        severity: str | None = None,
        without_account: bool = False,
    ) -> list[dict[str, Any]]:
        self._record("logs_tail", n, account, severity, without_account)
        return [
            {
                "ts": 1700000000.5,
                "severity": "INFO",
                "source": "core",
                "message": "ready",
                "account": None,
                "plugin": None,
                "task_id": None,
                "event": None,
            }
        ]

    async def audit_tail(self, n: int = 100, account: str | None = None) -> list[dict[str, Any]]:
        self._record("audit_tail", n, account)
        return [
            {
                "ts": 1700000000.5,
                "account": "alpha",
                "direction": "out",
                "action": "message",
                "channel_id": 500,
                "content": "$dump",
                "wait_ms": 150.0,
                "jitter_ms": 300.0,
                "note": "plugin=exporter",
            }
        ]

    # ---- system
    async def emergency_stop_all(self, surface: str = "api") -> dict[str, Any]:
        self._record("emergency_stop_all", surface=surface)
        return {"cancelled_tasks": 5, "halted": True}

    async def system_resume(self) -> dict[str, Any]:
        self._record("system_resume")
        return {"resumed": True}

    async def shutdown(self) -> None:
        self._record("shutdown")


def called(api: FakeApi, name: str) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
    """Args/kwargs of the MOST RECENT call with this method name."""
    result: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    for cname, args, kwargs in api.calls:
        if cname == name:
            result = (args, kwargs)
    return result


class TestBasics(unittest.IsolatedAsyncioTestCase):
    async def test_help(self) -> None:
        text = await run_command(FakeApi(), "help")
        assert text is not None
        self.assertIn("accounts", text)
        # the overview is grouped BRIEFS — the flood moved behind focus
        self.assertIn("observability:", text)
        self.assertNotIn("usage: accounts", text)

    async def test_empty_line(self) -> None:
        self.assertIsNone(await run_command(FakeApi(), "   "))

    async def test_quit_raises(self) -> None:
        with self.assertRaises(QuitRequested):
            await run_command(FakeApi(), "quit")
        with self.assertRaises(QuitRequested):
            await run_command(FakeApi(), "exit")

    async def test_unknown_command(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            await run_command(FakeApi(), "frobnicate")
        self.assertIn("unknown command", str(ctx.exception))
        self.assertIn("try 'help'", str(ctx.exception))

    async def test_unclosed_quotes(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), 'accounts add "bob')


class TestAccountsGroup(unittest.IsolatedAsyncioTestCase):
    async def test_list(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts list")
        assert text is not None
        self.assertIn("alpha", text)
        self.assertIn("42", text)
        # capabilities render from assigned_plugins (not legacy profiles)
        self.assertIn("plugins: exporter, roll", text)
        self.assertNotIn("profiles:", text)
        self.assertTrue(called(api, "accounts_list"))

    async def test_add_with_plugins(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts add bob roll exporter")
        recorded = called(api, "accounts_add")
        assert recorded is not None
        args, _ = recorded
        # the CLI preserves the caller's order (sorted on display only)
        self.assertEqual(args[0], {"name": "bob", "plugins": ["roll", "exporter"]})

    async def test_add_bare(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts add bob")
        recorded = called(api, "accounts_add")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args[0], {"name": "bob"})

    async def test_add_never_sends_profiles(self) -> None:
        # the backend rejects a 'profiles' payload key (unknown-key
        # ValueError) — the CLI must never build one
        api = FakeApi()
        await run_command(api, "accounts add bob roller")
        recorded = called(api, "accounts_add")
        assert recorded is not None
        args, _ = recorded
        self.assertNotIn("profiles", args[0])
        self.assertEqual(args[0]["plugins"], ["roller"])

    async def test_info_renders_sections(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts info alpha")
        assert text is not None
        self.assertTrue(called(api, "accounts_info"))
        for needle in (
            "Status",
            "Identity",
            "Runtime",
            "client cache",
            "Plugins",
            "Rate Limit",
            "inherited from global",
        ):
            self.assertIn(needle, text)

    async def test_enable_on_off(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts enable alpha on")
        recorded = called(api, "accounts_enable")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args, ("alpha", True))
        await run_command(api, "accounts enable alpha off")
        recorded = called(api, "accounts_enable")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args, ("alpha", False))
        with self.assertRaises(UsageError):
            await run_command(api, "accounts enable alpha maybe")

    async def test_start_stop_all_and_named(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts start all")
        recorded = called(api, "accounts_start")
        assert recorded is not None
        self.assertEqual(recorded[0], (None,))
        await run_command(api, "accounts start alpha")
        recorded = called(api, "accounts_start")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha",))
        await run_command(api, "accounts stop")
        recorded = called(api, "accounts_stop")
        assert recorded is not None
        self.assertEqual(recorded[0], (None,))

    async def test_estop_account(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts estop alpha")
        recorded = called(api, "accounts_emergency_stop")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha",))

    async def test_wrong_arg_count(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts remove")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts info a b")

    async def test_unknown_subcommand(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts frobnicate")


class TestAccountPluginCommands(unittest.IsolatedAsyncioTestCase):
    """accounts plugins|plugin-add|plugin-remove (direct capabilities)."""

    async def test_plugins_list_happy(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts plugins alpha")
        assert text is not None
        recorded = called(api, "accounts_plugins_list")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha",))
        self.assertIn("roll", text)
        self.assertIn("exporter", text)
        # sorted rendering
        self.assertLess(text.index("exporter"), text.index("roll"))

    async def test_plugins_list_empty(self) -> None:
        class EmptyApi(FakeApi):
            async def accounts_plugins_list(self, account: str) -> list[str]:
                self._record("accounts_plugins_list", account)
                return []

        text = await run_command(EmptyApi(), "accounts plugins alpha")
        assert text is not None
        self.assertIn("no plugins assigned", text)
        self.assertIn("plugin-add", text)

    async def test_plugins_list_unknown_account_error(self) -> None:
        from libzentsuite.core.errors import AccountError

        class GhostApi(FakeApi):
            async def accounts_plugins_list(self, account: str) -> list[str]:
                raise AccountError(f"unknown account {account!r}")

        # the engine bubbles domain errors; the surface renders one line
        with self.assertRaises(AccountError):
            await run_command(GhostApi(), "accounts plugins ghost")
        out = io.StringIO()
        code = await run_once(GhostApi(), ["accounts", "plugins", "ghost"], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("error: unknown account 'ghost'", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    async def test_plugin_add_happy(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts plugin-add alpha roll")
        assert text is not None
        recorded = called(api, "accounts_plugin_assign")
        assert recorded is not None
        args, kwargs = recorded
        self.assertEqual(args, ("alpha", "roll"))
        self.assertEqual(kwargs.get("surface"), "cli")
        self.assertIn("plugin added", text)
        self.assertIn("plugin=roll", text)

    async def test_plugin_add_unknown_plugin_error(self) -> None:
        class TypoApi(FakeApi):
            async def accounts_plugin_assign(
                self, account: str, plugin: str, surface: str = "api"
            ) -> dict[str, Any]:
                raise ValueError(f"unknown plugin(s) [{plugin!r}] (known plugins: ['roll'])")

        with self.assertRaises(ValueError) as ctx:
            await run_command(TypoApi(), "accounts plugin-add alpha nosuch")
        self.assertIn("known plugins", str(ctx.exception))
        out = io.StringIO()
        code = await run_once(TypoApi(), ["accounts", "plugin-add", "alpha", "nosuch"], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("error: unknown plugin", out.getvalue())

    async def test_plugin_remove_happy(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts plugin-remove alpha roll")
        assert text is not None
        recorded = called(api, "accounts_plugin_unassign")
        assert recorded is not None
        args, kwargs = recorded
        self.assertEqual(args, ("alpha", "roll"))
        self.assertEqual(kwargs.get("surface"), "cli")
        self.assertIn("plugin removed", text)
        self.assertIn("existed=true", text)

    async def test_plugin_remove_idempotent_message(self) -> None:
        class GoneApi(FakeApi):
            async def accounts_plugin_unassign(
                self, account: str, plugin: str, surface: str = "api"
            ) -> dict[str, Any]:
                self._record("accounts_plugin_unassign", account, plugin, surface=surface)
                return {"account": account, "plugin": plugin, "existed": False}

        api = GoneApi()
        text = await run_command(api, "accounts plugin-remove alpha roll")
        assert text is not None
        self.assertTrue(called(api, "accounts_plugin_unassign"))
        self.assertIn("was not assigned to alpha", text)
        self.assertIn("nothing to do", text)
        self.assertNotIn("existed=false", text)

    async def test_usage_errors(self) -> None:
        for line in (
            "accounts plugins",
            "accounts plugins a b",
            "accounts plugin-add alpha",
            "accounts plugin-add a b c",
            "accounts plugin-remove alpha",
            "accounts plugin-remove a b c",
        ):
            with self.subTest(line=line):
                with self.assertRaises(UsageError) as ctx:
                    await run_command(FakeApi(), line)
                self.assertIn("usage: accounts plugin", str(ctx.exception))

    async def test_help_documents_new_commands(self) -> None:
        focused = await run_command(FakeApi(), "help accounts")
        assert focused is not None
        self.assertIn("add <name> [plugin ...]", focused)
        self.assertIn("plugins <account>", focused)
        self.assertIn("plugin-add <account> <plugin>", focused)
        self.assertIn("plugin-remove <account> <plugin>", focused)


class TestPluginsGroup(unittest.IsolatedAsyncioTestCase):
    async def test_list(self) -> None:
        api = FakeApi()
        text = await run_command(api, "plugins list")
        assert text is not None
        self.assertIn("exporter", text)
        self.assertIn("trigger", text)

    async def test_enable_disable(self) -> None:
        api = FakeApi()
        await run_command(api, "plugins enable alpha exporter")
        recorded = called(api, "plugins_set_enabled")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha", "exporter", True))
        await run_command(api, "plugins disable alpha exporter")
        recorded = called(api, "plugins_set_enabled")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha", "exporter", False))

    async def test_set(self) -> None:
        api = FakeApi()
        await run_command(api, "plugins set alpha exporter trigger go")
        recorded = called(api, "plugins_set_setting")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha", "exporter", "trigger", "go"))


class TestRateLimitGroup(unittest.IsolatedAsyncioTestCase):
    async def test_set_global(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit set message 2.5")
        recorded = called(api, "ratelimit_set")
        assert recorded is not None
        self.assertEqual(recorded[0], ("message", 2.5, 0.0, 0.0, "global", None))

    async def test_set_jitter_and_level(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit set message 2.5 0.2 0.8 account alpha")
        recorded = called(api, "ratelimit_set")
        assert recorded is not None
        self.assertEqual(recorded[0], ("message", 2.5, 0.2, 0.8, "account", "alpha"))

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
        await run_command(api, "ratelimit set dm 4 0 0 account_plugin alpha/exporter")
        recorded = called(api, "ratelimit_set")
        assert recorded is not None
        self.assertEqual(recorded[0][4], "account_plugin")
        recorded = called(api, "ratelimit_set")
        assert recorded is not None
        self.assertEqual(recorded[0][5], "alpha/exporter")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "ratelimit set dm 4 0 0 account_plugin alpha")

    async def test_clear_global_dash_owner(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit clear global - message")
        recorded = called(api, "ratelimit_clear")
        assert recorded is not None
        self.assertEqual(recorded[0], ("global", None, "message"))

    async def test_effective_and_warnings(self) -> None:
        api = FakeApi()
        text = await run_command(api, "ratelimit effective message alpha exporter")
        assert text is not None
        recorded = called(api, "ratelimit_effective")
        assert recorded is not None
        self.assertEqual(recorded[0], ("message", "alpha", "exporter"))
        self.assertIn("account", text)
        await run_command(api, "ratelimit warnings")
        self.assertTrue(called(api, "ratelimit_warnings"))

    async def test_floor(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit floor 2.0")
        recorded = called(api, "ratelimit_set_floor")
        assert recorded is not None
        self.assertEqual(recorded[0], (2.0,))

    async def test_lane_show(self) -> None:
        api = FakeApi()
        text = await run_command(api, "ratelimit lane")
        assert text is not None
        self.assertTrue(called(api, "ratelimit_lane"))
        self.assertIn("sequential sends", text)
        self.assertIn("active", text)

    async def test_lane_on_with_rate(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit lane on 2.5")
        recorded = called(api, "ratelimit_set_lane")
        assert recorded is not None
        self.assertEqual(recorded[0], (True, 2.5))

    async def test_lane_on_bare_and_off(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit lane on")
        recorded = called(api, "ratelimit_set_lane")
        assert recorded is not None
        self.assertEqual(recorded[0], (True, None))
        await run_command(api, "ratelimit lane off")
        recorded = called(api, "ratelimit_set_lane")
        assert recorded is not None
        self.assertEqual(recorded[0], (False, None))

    async def test_lane_rate_zero_pauses(self) -> None:
        api = FakeApi()
        await run_command(api, "ratelimit lane rate 0")
        recorded = called(api, "ratelimit_set_lane")
        assert recorded is not None
        self.assertEqual(recorded[0], (None, 0.0))

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
        assert text is not None
        self.assertTrue(called(api, "tasks_list"))
        self.assertIn("exporter", text)

    async def test_cancel(self) -> None:
        api = FakeApi()
        await run_command(api, "tasks cancel 7")
        recorded = called(api, "tasks_cancel")
        assert recorded is not None
        self.assertEqual(recorded[0], (7,))

    async def test_history(self) -> None:
        api = FakeApi()
        await run_command(api, "tasks history 5")
        recorded = called(api, "tasks_history")
        assert recorded is not None
        self.assertEqual(recorded[0], (5, None))

    async def test_cancel_matching(self) -> None:
        api = FakeApi()
        await run_command(api, "tasks cancel-matching alpha exporter")
        recorded = called(api, "tasks_cancel_matching")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha", "exporter"))


class TestSimulationLogsAudit(unittest.IsolatedAsyncioTestCase):
    async def test_simulation_list_and_run(self) -> None:
        api = FakeApi()
        text = await run_command(api, "simulation list")
        assert text is not None
        self.assertIn("export_basic", text)
        text = await run_command(api, "simulation run export_basic")
        assert text is not None
        self.assertTrue(called(api, "simulation_run"))
        self.assertIn("PASS", text.upper())

    async def test_simulation_run_needs_name(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            await run_command(FakeApi(), "simulation run")
        self.assertIn("export_basic", str(ctx.exception))  # lists available

    async def test_logs_and_audit(self) -> None:
        api = FakeApi()
        text = await run_command(api, "logs 5 alpha")
        assert text is not None
        recorded = called(api, "logs_tail")
        assert recorded is not None
        self.assertEqual(recorded[0], (5, "alpha", None, False))
        self.assertIn("core", text)
        text = await run_command(api, "audit 5 alpha")
        assert text is not None
        recorded = called(api, "audit_tail")
        assert recorded is not None
        self.assertEqual(recorded[0], (5, "alpha"))
        self.assertIn("$dump", text)
        self.assertIn("wait", text)

    async def test_estop_global(self) -> None:
        api = FakeApi()
        await run_command(api, "estop")
        self.assertTrue(called(api, "emergency_stop_all"))

    async def test_estop_resume(self) -> None:
        api = FakeApi()
        await run_command(api, "estop resume")
        self.assertTrue(called(api, "system_resume"))
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "estop sideways")


class TestCredentialsAndCooldowns(unittest.IsolatedAsyncioTestCase):
    """accounts credential / credentials [status|import-env] / cooldowns."""

    async def test_accounts_credential_roundtrip(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts credential alpha tok-1234")
        assert text is not None
        # the fake deliberately records ONLY the name — never the secret
        recorded = called(api, "accounts_set_credential")
        assert recorded is not None
        args, kwargs = recorded
        self.assertEqual(args, ("alpha",))
        self.assertEqual(kwargs.get("surface"), "cli")
        self.assertIn("credential stored", text)
        self.assertIn("credential=db", text)
        self.assertNotIn("tok-1234", text)  # the value is NEVER echoed
        self.assertIn("shell history", text)  # the warning is always shown
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts credential alpha")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "accounts credential a b c")

    async def test_accounts_credential_in_help(self) -> None:
        focused = await run_command(FakeApi(), "help accounts")
        assert focused is not None
        self.assertIn("credential <name> <token>", focused)
        self.assertIn("shell history", focused)
        creds = await run_command(FakeApi(), "help credentials")
        assert creds is not None
        self.assertIn("credentials status|import-env", creds)
        cool = await run_command(FakeApi(), "help cooldowns")
        assert cool is not None
        self.assertIn("cooldowns [account]", cool)

    async def test_credentials_status(self) -> None:
        api = FakeApi()
        text = await run_command(api, "credentials status")
        assert text is not None
        self.assertTrue(called(api, "accounts_credential_status"))
        self.assertIn("alpha", text)
        self.assertIn("db (encrypted)", text)
        self.assertIn("missing", text)
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "credentials status extra")

    async def test_credentials_import_env(self) -> None:
        api = FakeApi()
        text = await run_command(api, "credentials import-env")
        assert text is not None
        recorded = called(api, "credentials_import_env")
        assert recorded is not None
        self.assertEqual(recorded[0], ())
        recorded = called(api, "credentials_import_env")
        assert recorded is not None
        self.assertEqual(recorded[1].get("overwrite"), False)
        self.assertIn("imported: alpha", text)
        self.assertIn("skipped : beta", text)
        text = await run_command(api, "credentials import-env --overwrite")
        assert text is not None
        recorded = called(api, "credentials_import_env")
        assert recorded is not None
        self.assertEqual(recorded[1].get("overwrite"), True)
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "credentials import-env --force")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "credentials import-env --overwrite x")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "credentials frobnicate")

    async def test_cooldowns_list_all_and_one(self) -> None:
        api = FakeApi()
        text = await run_command(api, "cooldowns")
        assert text is not None
        recorded = called(api, "cooldowns_list")
        assert recorded is not None
        self.assertEqual(recorded[0], (None,))
        self.assertIn("alpha", text)
        self.assertIn("roll", text)
        self.assertIn("1m 30s", text)
        await run_command(api, "cooldowns alpha")
        recorded = called(api, "cooldowns_list")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha",))
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "cooldowns alpha beta")

    async def test_cooldowns_clear_one_and_all(self) -> None:
        api = FakeApi()
        await run_command(api, "cooldowns clear alpha roll vote")
        recorded = called(api, "cooldowns_clear")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha", "roll", "vote"))
        recorded = called(api, "cooldowns_clear")
        assert recorded is not None
        self.assertEqual(recorded[1].get("surface"), "cli")
        await run_command(api, "cooldowns clear alpha roll")
        recorded = called(api, "cooldowns_clear")
        assert recorded is not None
        self.assertEqual(recorded[0], ("alpha", "roll", None))
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "cooldowns clear alpha")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "cooldowns clear a b c d")

    async def test_accounts_mutation_surfaces_are_clipped(self) -> None:
        api = FakeApi()
        await run_command(api, "accounts add bob")
        recorded = called(api, "accounts_add")
        assert recorded is not None
        self.assertEqual(recorded[1].get("surface"), "cli")
        await run_command(api, "accounts remove bob")
        recorded = called(api, "accounts_remove")
        assert recorded is not None
        self.assertEqual(recorded[1].get("surface"), "cli")
        await run_command(api, "accounts start bob")
        recorded = called(api, "accounts_start")
        assert recorded is not None
        self.assertEqual(recorded[1].get("surface"), "cli")
        await run_command(api, "estop")
        recorded = called(api, "emergency_stop_all")
        assert recorded is not None
        self.assertEqual(recorded[1].get("surface"), "cli")


class TestFormatters(unittest.TestCase):
    def test_format_accounts_empty(self) -> None:
        self.assertIn("no accounts", format_accounts([]))

    def test_format_accounts_renders_assigned_plugins(self) -> None:
        text = format_accounts(
            [
                {
                    "name": "alpha",
                    "status": "running",
                    "user_id": 42,
                    "enabled": True,
                    "assigned_plugins": ["exporter", "roll"],
                    "profiles": ["roller"],
                }
            ]
        )
        self.assertIn("plugins: exporter, roll", text)
        self.assertNotIn("profiles:", text)  # legacy profiles stay out

    def test_format_account_plugins_sorted_and_empty(self) -> None:
        text = format_account_plugins("alpha", ["exporter", "roll"])
        self.assertEqual(text.splitlines(), ["  exporter", "  roll"])
        empty = format_account_plugins("alpha", [])
        self.assertIn("no plugins assigned", empty)
        self.assertIn("accounts plugin-add alpha", empty)

    def test_format_account_info_marks_runtime_cache(self) -> None:
        text = format_account_info(
            {
                "name": "alpha",
                "status": {
                    "connected": True,
                    "state": "running",
                    "latency_ms": 83.0,
                    "uptime_s": 8040.0,
                },
                "identity": {"name": "Alpha#0", "discord_id": 42},
                "runtime": {
                    "cached_guilds": 3,
                    "cached_channels": 12,
                    "cached_dms": 2,
                    "cache_source": "client cache (READY)",
                },
                "plugins": [{"name": "exporter", "enabled": True}],
                "rate_limit": {
                    "message": {
                        "interval": 2.0,
                        "jitter": [0.1, 0.5],
                        "source": "inherited from global",
                    }
                },
            }
        )
        self.assertIn("client cache", text)
        self.assertIn("inherited from global", text)

    def test_format_warnings_icons(self) -> None:
        text = format_warnings([{"level": "warn", "code": "aggressive", "message": "m"}])
        self.assertIn("[!]", text)
        self.assertIn("aggressive", text)

    def test_format_simulation_report_failure(self) -> None:
        text = format_simulation_report(
            {
                "name": "x",
                "passed": False,
                "duration_s": 1.0,
                "failures": ["chunk missing"],
                "log_lines": ["00:00.001 INFO core go"],
                "interactions": [],
                "summary": "failed",
            }
        )
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
            async def status(self) -> dict[str, Any]:
                raise RuntimeError("boom\nwith newline")

        out = io.StringIO()
        repl = Repl(ExplodingApi(), stdin=io.StringIO("status\n"), stdout=out)
        await repl.run()
        # bare RuntimeError is NOT a domain error (it would mask internal
        # failures): the type name stays, the message is collapsed to
        # ONE line and no traceback leaks
        self.assertIn("error: RuntimeError: boom with newline", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())
        self.assertNotIn("\nboom", out.getvalue())

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

    async def test_run_once_quotes_argv(self) -> None:
        api = FakeApi()
        code = await run_once(api, ["accounts", "add", "bob two"], stdout=io.StringIO())
        self.assertEqual(code, 0)
        recorded = called(api, "accounts_add")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args[0], {"name": "bob two"})

    async def test_stop_event_ends_repl_at_prompt(self) -> None:
        import os

        api = FakeApi()
        out = io.StringIO()
        read_fd, write_fd = os.pipe()  # never delivers a line: blocks
        try:
            stop = asyncio.Event()
            repl = Repl(api, stdin=os.fdopen(read_fd, "r"), stdout=out, stop_event=stop)
            asyncio.get_running_loop().call_later(0.1, stop.set)
            await asyncio.wait_for(repl.run(), timeout=5)
        finally:
            os.close(write_fd)
        self.assertTrue(called(api, "shutdown"))
        self.assertIn("bye.", out.getvalue())
        self.assertFalse(repl.running)

    async def test_stop_event_set_before_run_exits_immediately(self) -> None:
        api = FakeApi()
        out = io.StringIO()
        stop = asyncio.Event()
        stop.set()
        repl = Repl(api, stdin=io.StringIO(""), stdout=out, stop_event=stop)
        await asyncio.wait_for(repl.run(), timeout=5)
        self.assertTrue(called(api, "shutdown"))
        self.assertIn("bye.", out.getvalue())

    async def test_repl_survives_unicode_garbage(self) -> None:
        api = FakeApi()
        out = io.StringIO()

        class BombsOnReadline(io.StringIO):
            # text-mode readline returns str at runtime, but typeshed's
            # _IOBase base declares bytes — unresolvable at the type level
            def readline(self, size: int | None = -1) -> str:  # type: ignore[override]  # simulates undecodable input bytes
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

        repl = Repl(api, stdin=BombsOnReadline(), stdout=out)
        await asyncio.wait_for(repl.run(), timeout=5)
        self.assertIn("bye.", out.getvalue())
        self.assertTrue(called(api, "shutdown"))


class TestHelpCommand(unittest.IsolatedAsyncioTestCase):
    """'help <cmd>' focus, alias handling, and registry drift checks."""

    async def test_help_bare_is_full_text(self) -> None:
        text = await run_command(FakeApi(), "help")
        assert text is not None
        # every group header present, every command appears exactly once
        for group in (
            "general",
            "accounts",
            "plugins",
            "workspaces",
            "operations",
            "observability",
            "development",
        ):
            self.assertIn(f"{group}:", text)
        for name in HELP:
            self.assertIn(name, text)
        # the overview does NOT flood: no usage signatures in the table
        self.assertNotIn("usage: accounts", text)

    async def test_help_focus_for_every_command(self) -> None:
        for name in sorted(HELP):
            text = await run_command(FakeApi(), f"help {name}")
            assert text is not None
            self.assertIn(name, text)
            self.assertIn(HELP[name][2], text)

    async def test_help_focus_shows_usage(self) -> None:
        text = await run_command(FakeApi(), "help ratelimit")
        assert text is not None
        self.assertIn("usage: ratelimit", text)
        self.assertIn("floor", text)
        with self.assertRaises(UsageError) as ctx:
            await run_command(FakeApi(), "help tasks cancel")
        self.assertIn("usage: help [command]", str(ctx.exception))

    async def test_help_exit_alias(self) -> None:
        text = await run_command(FakeApi(), "help exit")
        assert text is not None
        self.assertIn("graceful shutdown", text)

    async def test_help_unknown_and_extra(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            await run_command(FakeApi(), "help frobnicate")
        self.assertIn("unknown command", str(ctx.exception))
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "help accounts list")

    async def test_profiles_group(self) -> None:
        class ProfilesApi(FakeApi):
            async def account_plugin_profiles(
                self, account: str, plugin: str
            ) -> list[dict[str, Any]]:
                return [
                    {"name": "main", "schema_version": 2, "description": "primary"},
                    {"name": "alt", "schema_version": 1, "description": ""},
                ]

            async def account_plugin_profile_active(
                self, account: str, plugin: str
            ) -> dict[str, Any]:
                return {"account": account, "plugin": plugin, "active": "main"}

        out = await run_command(ProfilesApi(), "profiles alpha exporter list")
        assert out is not None
        self.assertIn("main", out)
        self.assertIn("alt", out)
        out = await run_command(ProfilesApi(), "profiles alpha exporter active")
        assert out is not None
        self.assertEqual(out, "active profile: main")
        with self.assertRaises(UsageError):
            await run_command(ProfilesApi(), "profiles alpha exporter frobnicate")

    def test_help_registry_no_drift(self) -> None:
        self.assertEqual(
            set(HELP),
            {
                "help",
                "status",
                "accounts",
                "credentials",
                "cooldowns",
                "plugins",
                "profiles",
                "workspaces",
                "ratelimit",
                "tasks",
                "simulation",
                "log",
                "global-log",
                "logs",
                "audit",
                "estop",
                "quit",
            },
        )
        for name, (sig, brief, detail) in HELP.items():
            self.assertTrue(brief, f"HELP[{name!r}] has no brief")
            self.assertTrue(detail, f"HELP[{name!r}] has no detail")
            self.assertIsInstance(sig, str)


class TestHostileParsing(unittest.IsolatedAsyncioTestCase):
    """Fuzz: hostile argument shapes only ever produce text, None,
    QuitRequested or UsageError — never another exception, never a
    traceback at the REPL."""

    HOSTILE: ClassVar[list[str]] = [
        "accounts add",
        "accounts add '' extra",
        'accounts add "my acc"',
        "accounts add 泥棒#42",
        "accounts add ' spaced '",
        "accounts remove",
        "accounts info a b",
        "accounts credential a",
        "accounts credential a b c",
        "accounts enable a",
        "accounts enable a on extra",
        "accounts enable a maybe",
        "accounts start a b",
        "accounts estop",
        "accounts frobnicate",
        "accounts plugins",
        "accounts plugins a b",
        "accounts plugin-add a",
        "accounts plugin-add a b c",
        "accounts plugin-remove",
        "accounts plugin-remove a b c d",
        "ratelimit set message abc",
        "ratelimit set message 2.5 0.2",
        "ratelimit set message nan",
        "ratelimit set message inf",
        "ratelimit set message 1e999",
        "ratelimit set message -inf",
        "ratelimit set message 2.5 nan nan",
        "ratelimit set message 2.5 0.2 0.8",
        "ratelimit clear global",
        "ratelimit clear account - message",
        "ratelimit clear weird x y",
        "ratelimit floor abc",
        "ratelimit floor nan",
        "ratelimit floor",
        "ratelimit floor 1e999",
        "ratelimit lane on 2.5 extra",
        "ratelimit lane off now",
        "ratelimit lane rate",
        "ratelimit lane on x",
        "ratelimit lane sideways",
        "ratelimit effective",
        "ratelimit effective a b c d",
        "ratelimit warnings extra",
        "ratelimit show extra",
        "tasks cancel notanumber",
        "tasks cancel 7 extra",
        "tasks cancel ''",
        "tasks history -3",
        "tasks history abc",
        "tasks history 5 extra",
        "tasks history 999999999999999999999",
        "tasks list a b",
        "tasks cancel-matching a b c",
        "logs -5",
        "logs --5",
        "logs 5 a b",
        "logs 0x10",
        "audit -1",
        "plugins state",
        "plugins set a p k",
        "plugins enable a",
        "credentials",
        "credentials import-env --force",
        "credentials import-env --overwrite --overwrite",
        "cooldowns a b",
        "cooldowns clear a",
        "cooldowns clear a b c d",
        "cooldowns ''",
        "cooldowns clear '' roll",
        "simulation run",
        "simulation run a b",
        "simulation list extra",
        "estop resume now",
        "estop sideways",
        "status extra",
        "help a b",
        "",
        "   ",
        "\t",
        "\x00",
        "\x01\x02",
        "accounts\x01add bob",
        "'unclosed",
        '"unclosed',
        "HELP",
        "Accounts LIST",
        "frobnicate",
        "ACCOUNTS ADD bob",
    ]

    async def test_hostile_lines_never_escape_the_contract(self) -> None:
        api = FakeApi()
        for line in self.HOSTILE:
            with self.subTest(line=line):
                try:
                    result = await run_command(api, line)
                except (QuitRequested, UsageError):
                    continue
                self.assertIsInstance(result, (str, type(None)))

    async def test_hostile_lines_through_repl_no_traceback(self) -> None:
        lines = [ln for ln in self.HOSTILE if ln.strip()] + [""]
        out = io.StringIO()
        repl = Repl(FakeApi(), stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
        await asyncio.wait_for(repl.run(), timeout=15)
        text = out.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertIn("bye.", text)
        self.assertIn("error: unknown command 'frobnicate'", text)

    async def test_quoting_round_trip(self) -> None:
        api = FakeApi()
        await run_command(api, 'accounts add "my acc" roller')
        recorded = called(api, "accounts_add")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args[0], {"name": "my acc", "plugins": ["roller"]})
        await run_command(api, "accounts add 'bob two'")
        recorded = called(api, "accounts_add")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args[0], {"name": "bob two"})

    async def test_unicode_account_name_round_trip(self) -> None:
        api = FakeApi()
        text = await run_command(api, "accounts info 泥棒")
        assert text is not None
        recorded = called(api, "accounts_info")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args, ("泥棒",))
        self.assertIn("泥棒", text)

    async def test_numbers_and_negatives(self) -> None:
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "logs -5")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "audit -1")
        with self.assertRaises(UsageError):
            await run_command(FakeApi(), "tasks history -1")
        api = FakeApi()
        await run_command(api, "logs 0")
        recorded = called(api, "logs_tail")
        assert recorded is not None
        # the alias routes no-account calls to the framework view
        self.assertEqual(recorded[0], (0, None, None, True))


class TestStopEventRaces(unittest.IsolatedAsyncioTestCase):
    """Round-2: the stop event vs long commands, EOF, and pending input."""

    async def test_stop_event_during_long_command(self) -> None:
        class SlowStatusApi(FakeApi):
            async def status(self) -> dict[str, Any]:
                await asyncio.sleep(0.05)
                # deterministic mid-command stop (was call_later(0.02),
                # which raced the command's start under instrumentation
                # load). The sleep(0) yields so the repl's stop_wait
                # future RESOLVES before its loop-top check — the exact
                # promptless-exit interleaving the test pins.
                stop.set()
                await asyncio.sleep(0)
                return await super().status()

        api = SlowStatusApi()
        out = io.StringIO()
        stop = asyncio.Event()
        repl = Repl(api, stdin=io.StringIO("status\n"), stdout=out, stop_event=stop)
        await asyncio.wait_for(repl.run(), timeout=5)
        text = out.getvalue()
        self.assertTrue(called(api, "status"))
        self.assertTrue(called(api, "shutdown"))
        self.assertIn("uptime:", text)
        self.assertIn("bye.", text)
        self.assertNotIn("sm> bye.", text)

    async def test_stop_before_run_discards_pending_input(self) -> None:
        api = FakeApi()
        out = io.StringIO()
        stop = asyncio.Event()
        stop.set()
        repl = Repl(api, stdin=io.StringIO("status\nhelp\n"), stdout=out, stop_event=stop)
        await asyncio.wait_for(repl.run(), timeout=5)
        text = out.getvalue()
        self.assertFalse(called(api, "status"))
        self.assertTrue(called(api, "shutdown"))
        self.assertIn("bye.", text)
        self.assertNotIn("uptime:", text)

    async def test_eof_and_stop_same_tick(self) -> None:
        class StopSignalApi(FakeApi):
            def __init__(self) -> None:
                super().__init__()
                self.stop: asyncio.Event | None = None

            async def status(self) -> dict[str, Any]:
                result = await super().status()
                if self.stop is not None:
                    self.stop.set()
                return result

        api = StopSignalApi()
        out = io.StringIO()
        stop = asyncio.Event()
        api.stop = stop
        repl = Repl(api, stdin=io.StringIO("status\n"), stdout=out, stop_event=stop)
        await asyncio.wait_for(repl.run(), timeout=5)
        text = out.getvalue()
        self.assertTrue(called(api, "shutdown"))
        self.assertEqual(text.count("bye."), 1)
        self.assertIn("uptime:", text)

    async def test_keyboard_interrupt_mid_command_exits_cleanly(self) -> None:
        class InterruptingApi(FakeApi):
            async def status(self) -> dict[str, Any]:
                raise KeyboardInterrupt

        api = InterruptingApi()
        out = io.StringIO()
        repl = Repl(api, stdin=io.StringIO("status\n"), stdout=out)
        await asyncio.wait_for(repl.run(), timeout=5)
        text = out.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertIn("bye.", text)
        self.assertTrue(called(api, "shutdown"))
        self.assertFalse(repl.running)


class TestRunOnceExitCodes(unittest.IsolatedAsyncioTestCase):
    """Every failure mode maps to the right process exit code."""

    async def test_success_and_estop_are_zero(self) -> None:
        api = FakeApi()
        out = io.StringIO()
        self.assertEqual(await run_once(api, ["accounts", "list"], stdout=out), 0)
        self.assertIn("alpha", out.getvalue())
        self.assertEqual(await run_once(api, ["estop"], stdout=out), 0)
        self.assertEqual(await run_once(api, [], stdout=io.StringIO()), 0)

    async def test_bad_args_exit_one(self) -> None:
        out = io.StringIO()
        code = await run_once(FakeApi(), ["ratelimit", "set", "message", "abc"], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("error:", out.getvalue())

    async def test_unknown_command_exit_one(self) -> None:
        out = io.StringIO()
        code = await run_once(FakeApi(), ["frobnicate"], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("unknown command", out.getvalue())

    async def test_runtime_error_exit_one(self) -> None:
        class BoomApi(FakeApi):
            async def status(self) -> dict[str, Any]:
                raise RuntimeError("boom")

        out = io.StringIO()
        self.assertEqual(await run_once(BoomApi(), ["status"], stdout=out), 1)
        # bare RuntimeError is NOT domain: the type name stays (internal
        # failures must not mask as curated error text), no traceback
        self.assertIn("error: RuntimeError: boom", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    async def test_failing_estop_exit_one(self) -> None:
        from libzentsuite.core.errors import AccountError

        class HaltApi(FakeApi):
            async def emergency_stop_all(self, surface: str = "api") -> dict[str, Any]:
                raise AccountError("already halted")

        out = io.StringIO()
        self.assertEqual(await run_once(HaltApi(), ["estop"], stdout=out), 1)
        self.assertIn("error: already halted", out.getvalue())

    async def test_flags_after_subcommand_pass_through(self) -> None:
        api = FakeApi()
        out = io.StringIO()
        code = await run_once(
            api,
            ["ratelimit", "set", "message", "2.5", "0.2", "0.8", "account", "alpha"],
            stdout=out,
        )
        self.assertEqual(code, 0)
        recorded = called(api, "ratelimit_set")
        assert recorded is not None
        self.assertEqual(recorded[0], ("message", 2.5, 0.2, 0.8, "account", "alpha"))
        code = await run_once(api, ["credentials", "import-env", "--overwrite"], stdout=out)
        self.assertEqual(code, 0)
        recorded = called(api, "credentials_import_env")
        assert recorded is not None
        self.assertEqual(recorded[1].get("overwrite"), True)

    async def test_shell_metachar_tokens_survive(self) -> None:
        api = FakeApi()
        code = await run_once(api, ["accounts", "add", "bob;rm -rf /"], stdout=io.StringIO())
        self.assertEqual(code, 0)
        recorded = called(api, "accounts_add")
        assert recorded is not None
        args, _ = recorded
        self.assertEqual(args[0], {"name": "bob;rm -rf /"})

    async def test_output_is_deterministic_and_ansi_free(self) -> None:
        outs = []
        for _ in range(2):
            out = io.StringIO()
            await run_once(FakeApi(), ["status"], stdout=out)
            outs.append(out.getvalue())
        self.assertEqual(outs[0], outs[1])
        self.assertNotIn("\x1b", outs[0])


class TestOutputVolumeAndFormatting(unittest.TestCase):
    """Huge payloads render bounded, deterministic, ANSI-free text."""

    def _history_rows(self, count: int) -> list[dict[str, Any]]:
        return [
            {
                "id": i,
                "status": "completed",
                "account": "alpha",
                "plugin": "exporter",
                "action": "message",
                "duration_s": 0.25,
            }
            for i in range(count)
        ]

    def test_format_history_ten_thousand_rows(self) -> None:
        text = format_history(self._history_rows(10_000))
        self.assertEqual(len(text.splitlines()), 10_000)

    def test_pure_formatters_emit_no_ansi(self) -> None:
        text = format_history(self._history_rows(3))
        self.assertNotIn("\x1b", text)
        text = format_logs(
            [{"ts": 1700000000.5, "severity": "INFO", "source": "core", "message": "ready"}]
        )
        self.assertNotIn("\x1b", text)
        text = format_audit(
            [
                {
                    "ts": 1700000000.5,
                    "account": "alpha",
                    "direction": "out",
                    "action": "message",
                    "channel_id": 5,
                    "content": "$sl",
                    "wait_ms": 10.0,
                    "jitter_ms": 20.0,
                }
            ]
        )
        self.assertNotIn("\x1b", text)

    def test_format_lane_paused_and_caps(self) -> None:
        paused = format_lane(
            {
                "enabled": True,
                "paused": True,
                "prefixrate": 0.0,
                "waiting": 3,
                "grants": 1,
                "last_grant_age_s": None,
            }
        )
        self.assertIn("PAUSED", paused)
        self.assertIn("never", paused)
        capped = format_lane(
            {
                "enabled": True,
                "paused": False,
                "prefixrate": 86400.0,
                "waiting": 0,
                "grants": 2,
                "last_grant_age_s": 4.0,
            }
        )
        self.assertIn("86400s", capped)
        self.assertNotIn("86400.0s", capped)

    def test_format_policies_jitter_rendering(self) -> None:
        text = format_policies(
            [
                {
                    "level": "global",
                    "owner": None,
                    "action": "message",
                    "interval": 2.0,
                    "jitter": [0.1, 0.5],
                }
            ]
        )
        self.assertIn("2.00s +0.10..0.50s", text)


class TestFloorCapDisplay(unittest.IsolatedAsyncioTestCase):
    """'ratelimit floor' round-1 cap (86400) renders correctly end to end."""

    async def test_floor_at_cap_displays_bare(self) -> None:
        from libzentsuite.core.errors import RatePolicyError

        class CappedFloorApi(FakeApi):
            async def ratelimit_set_floor(self, seconds: float) -> dict[str, Any]:
                if seconds > 86400.0:
                    raise RatePolicyError("safety floor exceeds the one-day cap (86400s)")
                return {"floor": min(float(seconds), 86400.0)}

        api = CappedFloorApi()
        text = await run_command(api, "ratelimit floor 86400")
        assert text is not None
        self.assertIn("floor=86400", text)
        self.assertNotIn("86400.0", text)
        text = await run_command(api, "ratelimit floor 3600")
        assert text is not None
        self.assertIn("floor=3600", text)
        with self.assertRaises(RatePolicyError):
            await run_command(api, "ratelimit floor 90000")
        with self.assertRaises(UsageError):  # non-finite rejected at the CLI
            await run_command(api, "ratelimit floor nan")


if __name__ == "__main__":
    unittest.main()


class ColumnSeparationTests(unittest.TestCase):
    def test_overlong_cell_never_runs_into_the_next_column(self) -> None:
        from libzentsuite.interfaces.cli.commands import _col

        self.assertEqual(_col("ab", 5), "ab   ")
        self.assertEqual(_col("a-very-long-account-name", 8), "a-very-long-account-name ")


class LiveLogWindowTests(unittest.IsolatedAsyncioTestCase):
    """The live-log contract (owner rule 2026-11-17): --live switches the
    interface to the log window; any input line returns to the CLI; a
    one-shot invocation without an input feed is a usage error, never a
    silent fall-back to flooding."""

    class _LiveApi(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.subscribed: list[object] = []

        def logs_service(self) -> Any:
            outer = self

            class _Svc:
                def subscribe(self, cb: Any) -> Any:
                    outer.subscribed.append(cb)

                    def _unsub() -> None:
                        outer.subscribed.remove(cb)

                    return _unsub

            return _Svc()

    async def test_live_requires_the_repl_feed(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            await run_command(LiveLogWindowTests._LiveApi(), "log alpha --live")
        self.assertIn("REPL", str(ctx.exception))

    async def test_live_window_renders_tail_and_returns_on_any_line(self) -> None:
        """Observable rendering: the seeded tail AND a live-streamed row
        appear on the configured stream; the subscription tears down."""
        api = LiveLogWindowTests._LiveApi()
        out = io.StringIO()

        async def feed() -> str:
            # hold the window open until the subscription is REGISTERED,
            # push one synthetic row through it, then return
            while not api.subscribed:
                await asyncio.sleep(0)
            push_row = api.subscribed[0]
            assert callable(push_row)
            push_row(
                NS(
                    as_dict=lambda: {
                        "ts": 1.0,
                        "severity": "INFO",
                        "source": "test",
                        "message": "live-row",
                        "account": "alpha",
                        "plugin": None,
                        "event": None,
                    }
                )
            )

            # explicit synchronization on the OBSERVABLE (PR review:
            # no yield loops, no wall-clock sleeps): wait until the row
            # has actually rendered to the window's stream, then return
            async def _row_rendered() -> None:
                while "live-row" not in out.getvalue():
                    await asyncio.sleep(0)

            await asyncio.wait_for(_row_rendered(), timeout=5.0)
            return "q"

        summary = await run_command(api, "log alpha --live", input_feed=feed, out=out)
        assert summary is not None
        self.assertIn("live log closed", summary)
        text = out.getvalue()
        self.assertIn("live log:", text)  # the header
        self.assertIn("live-row", text)  # the streamed row RENDERED
        self.assertEqual(api.subscribed, [])  # unsubscribed on exit

    async def test_live_flag_parses_in_any_position_and_keeps_args(self) -> None:
        api = LiveLogWindowTests._LiveApi()

        async def feed() -> str:
            return ""

        await run_command(api, "log alpha -f 5", input_feed=feed)
        recorded = called(api, "logs_tail")
        assert recorded is not None
        # the initial tail kept account + limit; --live/-f stripped
        self.assertEqual(recorded[0][0:2], (5, "alpha"))

    async def test_global_log_live_routes_to_the_framework_view(self) -> None:
        api = LiveLogWindowTests._LiveApi()

        async def feed() -> str:
            return "q"

        summary = await run_command(api, "global-log --live", input_feed=feed)
        assert summary is not None
        self.assertIn("live log closed", summary)
        recorded = called(api, "logs_tail")
        assert recorded is not None
        self.assertTrue(recorded[0][3])  # without_account: the suite's rows
