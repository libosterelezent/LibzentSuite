"""End-to-end integration of the direct Account→Plugins capability model.

Boots the REAL production stack (``core.bootstrap.build`` — the boot path
of main.py) over the fixture plugins (probe + spy) and a temporary
database, following the harness style of tests/integration/
test_account_persistence.py. Accounts are never STARTED (no Discord
client is needed): attachment happens at add/load time, which is exactly
the capability path under test.

Items (one test class each, numbered like the verification plan):
  1. persistence      — accounts_add(plugins=[...]) writes account_plugins
                        rows; accounts_list serves assigned_plugins sorted
  2. zero assignment  — plugins=[] attaches ZERO plugins (a spy on the
                        plugin manager's attach_account sees a CONCRETE
                        empty set, never the unrestricted None)
  3. only-those       — the attached set is EXACTLY the assigned set, a
                        strict subset of the discovered set
  4. mutation flows   — assign / unassign / unassign-missing (existed=
                        False, not an error) / re-assign
  5. assigned ≠ enabled — the enabled axis and the assignment axis never
                        write to each other
  6. restart          — full shutdown + rebuild on the same DB restores
                        identical assignments AND attachment sets
  7. profile decoupling — ProfileManager functional (CRUD + the retained
                        allowed_plugins) but NON-authoritative
  8. sync group isolation — group membership and plugin assignments are
                        independent tables/axes
  9. plugin settings  — settings keyed (account, plugin, key) survive
                        assignment changes
 10. validation       — unknown plugin (ValueError listing known), unknown
                        account (AccountError), legacy 'profiles' payload
                        key rejected

Plus the dashboard surface: a REAL DashboardServer over the REAL
ControlAPI driving the four plugin routes and the add-account payloads
over real HTTP (the style of tests/unit/test_dashboard_http.py).
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.bootstrap import build
from core.errors import AccountError
from interfaces.dashboard.server import DashboardServer

REPO_ROOT = Path(__file__).resolve().parents[2]
#: fixture plugins — the framework bundles none; probe/spy/echo carry the
#: capability-model assertions below (probe has the "trigger" setting;
#: config_demo is the dashboard-UI reference fixture)
REAL_PLUGINS = REPO_ROOT / "tests" / "fixtures" / "plugins"

PROBE, SPY = "probe", "spy"
DASH_TOKEN = "e2e-dash-token"


# ------------------------------------------------------------- HTTP helpers
def http_get(url: str, token: Optional[str] = DASH_TOKEN) -> Tuple[int, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def http_post(url: str, payload: Any,
              token: Optional[str] = DASH_TOKEN) -> Tuple[int, str]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def get_json(text: str) -> dict:
    return json.loads(text)


# ------------------------------------------------------------------ harness
class AccountPluginsE2EBase(unittest.IsolatedAsyncioTestCase):
    """Each test boots the real framework over its own temp dir + DB."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sm-plugins-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state" / "slaughter.db"

    async def boot(self) -> Tuple[Any, List[Tuple[str, Optional[Set[str]]]]]:
        """``await build(...)`` + an attach spy on the PluginManager.

        The spy records the EXACT allowed set the account manager hands
        the plugin manager (``attach_account(account, allowed)``) — the
        wire the invariant lives on. ``allowed=None`` would mean
        "unrestricted" (the dead profile-era default); it must never
        appear for an account.
        """
        fw = await build(
            env_file=str(self.root / "absent.env"),
            config_dir=str(self.root / "config"),  # no config -> defaults
            db_path=str(self.db_path),
            plugin_dir=str(REAL_PLUGINS),
            quiet=True,
        )
        attach_calls: List[Tuple[str, Optional[Set[str]]]] = []
        original = fw.plugins.attach_account

        async def _spy(account: str, allowed: Optional[Set[str]] = None):
            attach_calls.append(
                (account, None if allowed is None else set(allowed)))
            return await original(account, allowed)

        fw.plugins.attach_account = _spy
        self.addCleanup(self._shutdown, fw)
        return fw, attach_calls

    async def _shutdown(self, fw: Any) -> None:
        await fw.shutdown()

    # ------------------------------------------------------------ assertions
    @staticmethod
    async def attached(api: Any, account: str) -> Set[str]:
        """Plugin names the PluginManager currently has attached."""
        return {p["name"] for p in await api.plugins_state(account)}

    @staticmethod
    async def assigned(api: Any, account: str) -> List[str]:
        return list(await api.accounts_plugins_list(account))

    @staticmethod
    def last_attach(attach_calls: List[Tuple[str, Optional[Set[str]]]],
                    account: str) -> Optional[Set[str]]:
        """The allowed set of the account's most recent attach (None when
        the account was never attached)."""
        for name, allowed in reversed(attach_calls):
            if name == account:
                return allowed
        return None

    @staticmethod
    async def account_row(api: Any, name: str) -> Dict[str, Any]:
        rows = {r["name"]: r for r in await api.accounts_list()}
        return rows[name]


# --------------------------------------------------------- 1 + 2 + 3 (boot)
class TestAssignmentBootSemantics(AccountPluginsE2EBase):

    async def test_1_add_persists_rows_and_lists_sorted(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        out = await api.accounts_add(
            {"name": "alpha", "plugins": [SPY, PROBE, SPY]})
        # add_account's own answer carries the canonical (sorted, deduped) set
        self.assertEqual(out["assigned_plugins"], [PROBE, SPY])
        # rows persist in the account_plugins table (queried DIRECTLY)
        self.assertEqual(
            await fw.database.account_plugins.plugins_of("alpha"),
            [PROBE, SPY],
        )
        # and the surfaced account row serves them sorted
        row = await self.account_row(api, "alpha")
        self.assertEqual(row["assigned_plugins"], [PROBE, SPY])

    async def test_2_zero_assignments_attach_zero_plugins(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        await api.accounts_add({"name": "beta", "plugins": []})
        self.assertEqual(await self.assigned(api, "beta"), [])
        # the plugin manager attached ZERO plugins for beta — and the
        # allowed set it received was a CONCRETE empty set, not the
        # unrestricted None of the profile era
        self.assertEqual(await self.attached(api, "beta"), set())
        self.assertIsNotNone(self.last_attach(attach_calls, "beta"))
        self.assertEqual(self.last_attach(attach_calls, "beta"), set())
        # repo truth agrees: no rows for beta anywhere
        self.assertEqual(
            await fw.database.account_plugins.plugins_of("beta"), [])
        row = await self.account_row(api, "beta")
        self.assertEqual(row["assigned_plugins"], [])

    async def test_3_attached_set_is_exactly_the_assigned_set(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        discovered = {m["name"] for m in await api.plugins_manifests()}
        self.assertEqual(discovered,
                         {"probe", "spy", "echo", "config_demo"})  # sanity

        await api.accounts_add(
            {"name": "alpha", "plugins": [PROBE, SPY]})
        await api.accounts_add({"name": "gamma", "plugins": [PROBE]})

        # alpha: exactly its two; gamma: exactly its one — a STRICT subset
        # of the discovered set, so attachment is assignment-driven, not
        # discovery-driven (the "all plugins" default is gone)
        self.assertEqual(await self.attached(api, "alpha"), {PROBE, SPY})
        self.assertEqual(self.last_attach(attach_calls, "alpha"),
                         {PROBE, SPY})
        self.assertEqual(await self.attached(api, "gamma"), {PROBE})
        self.assertEqual(self.last_attach(attach_calls, "gamma"), {PROBE})
        self.assertNotEqual(await self.attached(api, "gamma"), discovered)


# ------------------------------------------------------------ 4 (mutation)
class TestMutationFlows(AccountPluginsE2EBase):

    async def test_4_assign_unassign_missing_reassign(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        await api.accounts_add(
            {"name": "alpha", "plugins": [PROBE, SPY]})
        await api.accounts_add({"name": "beta", "plugins": []})

        # assign spy to beta -> beta attached set grows NOW
        out = await api.accounts_plugin_assign("beta", SPY)
        self.assertEqual(out, {"account": "beta", "plugin": SPY})
        self.assertEqual(await self.assigned(api, "beta"), [SPY])
        self.assertEqual(await self.attached(api, "beta"), {SPY})

        # unassign probe from alpha -> alpha attached set shrinks NOW
        out = await api.accounts_plugin_unassign("alpha", PROBE)
        self.assertTrue(out["existed"])
        self.assertEqual(await self.assigned(api, "alpha"), [SPY])
        self.assertEqual(await self.attached(api, "alpha"), {SPY})

        # unassign a relationship that does not exist: existed=False, no
        # error, nothing else moves
        out = await api.accounts_plugin_unassign("alpha", PROBE)
        self.assertFalse(out["existed"])
        self.assertEqual(await self.assigned(api, "alpha"), [SPY])
        self.assertEqual(await self.attached(api, "alpha"), {SPY})

        # re-assign probe: back to the full set
        await api.accounts_plugin_assign("alpha", PROBE)
        self.assertEqual(await self.assigned(api, "alpha"), [PROBE, SPY])
        self.assertEqual(await self.attached(api, "alpha"), {PROBE, SPY})

        # repository truth matches the api view at every step's end
        self.assertEqual(
            await fw.database.account_plugins.all_assignments(),
            {"alpha": [PROBE, SPY], "beta": [SPY]},
        )


# --------------------------------------------------- 5 (assigned ≠ enabled)
class TestAssignedVersusEnabled(AccountPluginsE2EBase):

    @staticmethod
    def _enabled_map(states: List[Dict[str, Any]]) -> Dict[str, bool]:
        return {s["name"]: s["enabled"] for s in states}

    async def test_5_axes_are_independent(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        await api.accounts_add(
            {"name": "alpha", "plugins": [PROBE, SPY]})

        # (a) disabling a plugin never touches its assignment...
        await api.plugins_set_enabled("alpha", SPY, False)
        states = self._enabled_map(await api.plugins_state("alpha"))
        self.assertFalse(states[SPY])
        self.assertEqual(await self.assigned(api, "alpha"), [PROBE, SPY])
        # ...and re-enabling neither
        await api.plugins_set_enabled("alpha", SPY, True)
        states = self._enabled_map(await api.plugins_state("alpha"))
        self.assertTrue(states[SPY])
        self.assertEqual(await self.assigned(api, "alpha"), [PROBE, SPY])

        # (b) an assignment change never touches the enabled axis: probe's
        # persisted disabled state survives an unassign → re-assign cycle
        await api.plugins_set_enabled("alpha", PROBE, False)
        await api.accounts_plugin_unassign("alpha", PROBE)
        self.assertEqual(await self.assigned(api, "alpha"), [SPY])
        # persisted per-(account, plugin) state kept the disabled flag
        self.assertFalse(await fw.database.plugins.state("alpha", PROBE))
        await api.accounts_plugin_assign("alpha", PROBE)
        states = self._enabled_map(await api.plugins_state("alpha"))
        self.assertEqual(set(states), {PROBE, SPY})
        self.assertFalse(states[PROBE])     # still disabled after round-trip
        self.assertTrue(states[SPY])  # untouched by any of this


# ------------------------------------------------------------ 6 (restart)
class TestRestartRestoresAssignments(AccountPluginsE2EBase):

    async def test_6_rebuild_restores_assignments_and_attachment(self) -> None:
        # ---------------------------------------------------------- boot 1
        fw, _ = await self.boot()
        api = fw.api
        await api.accounts_add(
            {"name": "alpha", "plugins": [PROBE, SPY]})
        await api.accounts_add({"name": "beta", "plugins": []})
        # mutate past the initial state so restore cannot pass by luck
        await api.accounts_plugin_assign("beta", SPY)
        await api.accounts_plugin_unassign("alpha", PROBE)
        before_assigned = {"alpha": await self.assigned(api, "alpha"),
                           "beta": await self.assigned(api, "beta")}
        before_attached = {"alpha": await self.attached(api, "alpha"),
                           "beta": await self.attached(api, "beta")}
        self.assertEqual(before_assigned,
                         {"alpha": [SPY], "beta": [SPY]})
        await fw.shutdown()

        # ---------------------------------------------------------- boot 2
        # same DB path: build() runs AccountManager.load() (the bulk
        # assignment restore) before anything attaches
        fw2, attach_calls2 = await self.boot()
        api2 = fw2.api
        try:
            rows = {r["name"]: r for r in await api2.accounts_list()}
            self.assertEqual(sorted(rows), ["alpha", "beta"])
            self.assertEqual(rows["alpha"]["assigned_plugins"],
                             before_assigned["alpha"])
            self.assertEqual(rows["beta"]["assigned_plugins"],
                             before_assigned["beta"])
            self.assertEqual(await self.assigned(api2, "alpha"),
                             before_assigned["alpha"])
            self.assertEqual(await self.assigned(api2, "beta"),
                             before_assigned["beta"])
            # attachment sets identical too — restored FROM the rows
            self.assertEqual(await self.attached(api2, "alpha"),
                             before_attached["alpha"])
            self.assertEqual(await self.attached(api2, "beta"),
                             before_attached["beta"])
            self.assertEqual(
                await fw2.database.account_plugins.all_assignments(),
                {"alpha": [SPY], "beta": [SPY]},
            )
            # the in-memory mirror the attach path feeds from was bulk-
            # restored with CONCRETE sets (the spy wraps only after
            # build() returns, and load() attaches inside build)
            for name in ("alpha", "beta"):
                mirror = fw2.accounts.get(name).assigned_plugins
                self.assertEqual(sorted(mirror), before_assigned[name])
            # any post-boot mutation re-attaches from that mirror alone
            await api2.accounts_plugin_unassign("beta", SPY)
            self.assertEqual(self.last_attach(attach_calls2, "beta"), set())
            self.assertIsNotNone(self.last_attach(attach_calls2, "beta"))
        finally:
            await fw2.shutdown()


# ---------------------------------------------------- 7 (profile decoupling)
class TestProfileDecoupling(AccountPluginsE2EBase):

    async def test_7_profile_grants_never_move_attachment(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        await api.accounts_add({"name": "alpha", "plugins": [PROBE]})

        # profile CRUD works (retained infrastructure)
        await api.profiles_create("copier", [PROBE, SPY], "both")
        listed = await api.profiles_list()
        self.assertEqual([p["name"] for p in listed], ["copier"])
        self.assertEqual(listed[0]["plugins"], [PROBE, SPY])

        # granting the profile to alpha is bookkeeping ONLY
        await api.profiles_assign("alpha", "copier")
        row = await self.account_row(api, "alpha")
        self.assertEqual(row["profiles"], ["copier"])  # grant recorded
        # ...and attachment is UNCHANGED by the grant
        self.assertEqual(await self.attached(api, "alpha"), {PROBE})
        self.assertEqual(await self.assigned(api, "alpha"), [PROBE])
        self.assertEqual(self.last_attach(attach_calls, "alpha"), {PROBE})

        # the retained ProfileManager.allowed_plugins still answers (and
        # would grant BOTH) — the account capability path just never asks
        self.assertEqual(fw.profiles.allowed_plugins("alpha"),
                         {PROBE, SPY})

        # unassigning the profile works and moves nothing either
        await api.profiles_unassign("alpha", "copier")
        row = await self.account_row(api, "alpha")
        self.assertEqual(row["profiles"], [])
        self.assertEqual(await self.attached(api, "alpha"), {PROBE})


# -------------------------------------------------- 8 (sync group isolation)
class TestSyncGroupIsolation(AccountPluginsE2EBase):

    async def test_8_groups_and_assignments_are_independent(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        await api.accounts_add(
            {"name": "alpha", "plugins": [PROBE, SPY]})

        # group move does not change assignments
        await api.groups_create({"name": "grinders"})
        await api.groups_set_account("alpha", "grinders")
        row = await self.account_row(api, "alpha")
        self.assertEqual(row["sync_group"], "grinders")
        self.assertEqual(row["assigned_plugins"], [PROBE, SPY])
        self.assertEqual(await self.attached(api, "alpha"),
                         {PROBE, SPY})

        # assignment change does not change the group
        await api.accounts_plugins_set("alpha", [PROBE])
        row = await self.account_row(api, "alpha")
        self.assertEqual(row["sync_group"], "grinders")
        self.assertEqual(row["assigned_plugins"], [PROBE])
        snapshot = await api.groups_snapshot("grinders")
        self.assertEqual([m["name"] for m in snapshot["members"]], ["alpha"])
        self.assertEqual(snapshot["member_count"], 1)


# ------------------------------------------------------ 9 (plugin settings)
class TestPluginSettingsIndependence(AccountPluginsE2EBase):

    async def test_9_settings_survive_assignment_changes(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        await api.accounts_add(
            {"name": "alpha", "plugins": [PROBE, SPY]})
        await api.plugins_set_setting("alpha", PROBE, "trigger", "$go")

        # a DIFFERENT plugin's assignment churn leaves this setting alone
        await api.accounts_plugin_unassign("alpha", PROBE)
        await api.accounts_plugin_assign("alpha", PROBE)
        settings = {p["name"]: p["settings"]
                    for p in await api.plugins_settings("alpha")}
        self.assertEqual(settings[PROBE]["trigger"], "$go")
        self.assertEqual(
            await fw.database.plugins.setting("alpha", PROBE, "trigger"),
            "$go",
        )

        # even the plugin's OWN assignment round-trip keeps it (settings
        # are keyed (account, plugin, key), independently of assignments)
        await api.accounts_plugin_unassign("alpha", SPY)
        self.assertEqual(
            await fw.database.plugins.setting("alpha", PROBE, "trigger"),
            "$go",
        )
        await api.accounts_plugin_assign("alpha", SPY)
        settings = {p["name"]: p["settings"]
                    for p in await api.plugins_settings("alpha")}
        self.assertEqual(settings[PROBE]["trigger"], "$go")


# ---------------------------------------------------------- 10 (validation)
class TestValidationErrors(AccountPluginsE2EBase):

    async def test_10_unknown_plugin_account_and_profiles_key(self) -> None:
        fw, attach_calls = await self.boot()
        api = fw.api
        await api.accounts_add({"name": "alpha", "plugins": [PROBE]})

        # unknown plugin: ValueError LISTS the known plugins (typo trap)
        with self.assertRaises(ValueError) as ctx:
            await api.accounts_plugin_assign("alpha", "nosuchplugin")
        message = str(ctx.exception)
        self.assertIn("nosuchplugin", message)
        self.assertIn("known plugins", message)
        self.assertIn(PROBE, message)
        self.assertIn(SPY, message)
        # nothing was written by the failed call
        self.assertEqual(await self.assigned(api, "alpha"), [PROBE])

        # unknown account: the AccountError family (typo trap)
        for call in (
            lambda: api.accounts_plugin_assign("ghost", PROBE),
            lambda: api.accounts_plugin_unassign("ghost", PROBE),
            lambda: api.accounts_plugins_set("ghost", [PROBE]),
            lambda: api.accounts_plugins_list("ghost"),
        ):
            with self.assertRaises(AccountError):
                await call()

        # accounts_add rejects the legacy 'profiles' key as unknown
        with self.assertRaises(ValueError) as ctx:
            await api.accounts_add(
                {"name": "delta", "profiles": ["copier"]})
        self.assertIn("profiles", str(ctx.exception))
        self.assertNotIn("delta", [r["name"] for r in await api.accounts_list()])


# ------------------------------------------------- C (dashboard over stack)
class TestDashboardPluginRoutes(AccountPluginsE2EBase):
    """A REAL DashboardServer over the REAL ControlAPI + real DB."""

    async def asyncSetUp(self) -> None:
        self.fw, self.attach_calls = await self.boot()
        self.api = self.fw.api
        await self.api.accounts_add(
            {"name": "alpha", "plugins": [PROBE, SPY]})
        await self.api.accounts_add({"name": "beta", "plugins": []})
        self.server = DashboardServer(
            self.api, token=DASH_TOKEN, port=0).start()
        self.base = f"http://127.0.0.1:{self.server.bound_port}"

    async def asyncTearDown(self) -> None:
        self.server.stop()
        await self.fw.shutdown()

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    async def test_plugin_routes_roundtrip_against_real_api(self) -> None:
        # GET list (query-string account)
        status, body = await asyncio.to_thread(
            http_get, self.url(f"/api/accounts/plugins?account=alpha"))
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"], [PROBE, SPY])

        # assign: beta gains probe NOW (through the whole stack)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/assign"),
            {"account": "beta", "plugin": PROBE})
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["ok"])
        self.assertEqual(await self.attached(self.api, "beta"), {PROBE})

        # unassign: existed=True the first time...
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/unassign"),
            {"account": "beta", "plugin": PROBE})
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["data"]["existed"])
        # ...existed=False (still 200) when the relationship is absent
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/unassign"),
            {"account": "beta", "plugin": PROBE})
        self.assertEqual(status, 200)
        self.assertFalse(get_json(body)["data"]["existed"])
        self.assertEqual(await self.attached(self.api, "beta"), set())

        # set: whole-set replacement (all-or-nothing)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/set"),
            {"account": "alpha", "plugins": [SPY]})
        self.assertEqual(status, 200)
        self.assertEqual(get_json(body)["data"]["plugins"], [SPY])
        self.assertEqual(await self.attached(self.api, "alpha"), {SPY})

    async def test_plugin_routes_400_on_unknown_names(self) -> None:
        # unknown plugin -> honest 400 listing the known plugins
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/assign"),
            {"account": "alpha", "plugin": "nosuchplugin"})
        self.assertEqual(status, 400)
        error = get_json(body)["error"]
        self.assertIn("known plugins", error)
        self.assertIn(SPY, error)

        # unknown account -> honest 400 (AccountError family)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/assign"),
            {"account": "ghost", "plugin": PROBE})
        self.assertEqual(status, 400)
        self.assertIn("ghost", get_json(body)["error"])

        # set with ONE unknown name rejects the WHOLE call (all-or-nothing)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/set"),
            {"account": "alpha", "plugins": [PROBE, "nosuchplugin"]})
        self.assertEqual(status, 400)
        self.assertEqual(await self.assigned(self.api, "alpha"),
                         [PROBE, SPY])  # untouched

        # missing required field -> 400 naming the field
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/plugins/assign"),
            {"account": "alpha"})
        self.assertEqual(status, 400)
        self.assertIn("'plugin'", get_json(body)["error"])

    async def test_status_rows_carry_assigned_plugins(self) -> None:
        status, body = await asyncio.to_thread(
            http_get, self.url("/api/status"))
        self.assertEqual(status, 200)
        rows = {a["name"]: a for a in get_json(body)["data"]["accounts"]}
        self.assertEqual(rows["alpha"]["assigned_plugins"], [PROBE, SPY])
        self.assertEqual(rows["beta"]["assigned_plugins"], [])

    async def test_add_account_plugins_ok_profiles_rejected(self) -> None:
        # plugins payload accepted end-to-end (row + attachment)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"),
            {"name": "gamma", "plugins": [PROBE]})
        self.assertEqual(status, 200)
        self.assertTrue(get_json(body)["ok"])
        self.assertEqual(await self.attached(self.api, "gamma"), {PROBE})
        self.assertEqual(await self.assigned(self.api, "gamma"), [PROBE])

        # profiles payload rejected with 400 (unknown key — the profile
        # era's payload shape is gone, not silently ignored)
        status, body = await asyncio.to_thread(
            http_post, self.url("/api/accounts/add"),
            {"name": "delta", "profiles": ["copier"]})
        self.assertEqual(status, 400)
        self.assertIn("profiles", get_json(body)["error"])
        self.assertNotIn(
            "delta", [r["name"] for r in await self.api.accounts_list()])


if __name__ == "__main__":
    unittest.main()
