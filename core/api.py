"""CoreAPI — the ONE application surface both interfaces call (spec §13/§14).

CLI and dashboard are thin wrappers over these methods and NOTHING else, so
they can never drift apart in capability (parity by construction). This
facade is intentionally duck-typed over the managers: it stores whatever it
is given and delegates. Managers are constructed by core/bootstrap.py.

Method map (grouped like the dashboard navigation, spec §12):
  status            overview: accounts (+credential status, cooldown count)
                    + limits + tasks + warnings
  accounts_*        list/add/remove/info/enable/start/stop/emergency-stop
                    (+ account_setting_get/account_setting_set for the
                    framework per-account knobs, and
                    accounts_plugins_list/plugin_assign/plugin_unassign/
                    plugins_set — the DIRECT account→plugin capability
                    assignments, persisted through db.account_plugins;
                    accounts_plugin_profile(s) bind an ASSIGNED plugin
                    to a settings profile, accounts_effective_settings
                    merges defaults <- profile <- account overrides, and
                    accounts_channels snapshots the live client's text
                    channels)
  plugin_profiles_* list/create/update/delete of per-plugin setting
                    profiles (db.plugin_profiles, migration v7) with
                    settings validated against the plugin's Setting
                    schema and the reserved "channels" scope against
                    core/plugin_scopes
  credentials       set_credential / credential_status / import_env over the
                    encrypted CredentialService (values NEVER echoed back)
  cooldowns_*       list/clear persistent cooldowns (CooldownService)
  plugins_*         manifests/state/enable/settings/set-setting
                    (set-setting value null = plugins_clear_setting: the
                    account-layer override is DELETED, unmasking the lower
                    layers — not pinned to the schema default)
  profiles_*        list/create/delete/assign/unassign (RETAINED profile
                    infrastructure — bookkeeping only, NO effect on which
                    plugins an account runs; capability assignment lives
                    in accounts_plugin_*)
  groups_*          sync groups: list/get/create/update/delete + membership
                    (add/remove account, set/clear account) + snapshot;
                    membership persists through SyncGroupManager and the
                    runtime pacing scope through RateLimitManager
  ratelimit_*       policies/set/clear/effective/stats/warnings/set-floor
                    (+lane; every mutation persists through the db repos)
  tasks_*           list/cancel/cancel-matching/history
  simulation_*      list/run
  logs_tail / audit_tail
  emergency_stop_all / shutdown

Management audit: account, credential and emergency-stop mutations emit an
audit row via the recorder (``action="mgmt:*"``, ``direction="out"``, note
naming the calling surface); other management calls currently do not.
Credential material NEVER reaches the audit trail — account names and
counters only.

Manager migration note: the managers are moving to aiosqlite-backed async
persistence; call sites below tolerate BOTH sync and async manager results
through ``_resolved()`` so this facade keeps working at every step.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time as _time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .dashboard_ui.context import ActionContext, CheckContext
from .dashboard_ui.errors import UIError
from .errors import DatabaseError, PluginError, SyncGroupError
from . import plugin_scopes
from .rate_limit_manager.policies import parse_scope
from .rate_limit_manager import (
    KV_FLOOR_KEY,
    KV_LANE_KEY,
    KV_REMOVED_KEY,
)

if TYPE_CHECKING:  # duck-typed at runtime — no manager imports here
    from .account_manager import AccountManager
    from .audit import AuditRecorder
    from .cooldowns import CooldownService
    from .credential_service import CredentialService
    from .credentials import Credentials
    from .logging_service import LogService
    from .plugin_manager import PluginManager
    from .profile_manager import ProfileManager
    from .rate_limit_manager import RateLimitManager
    from .settings_manager import SettingsManager
    from .task_scheduler import TaskScheduler

LOG = logging.getLogger("libzent.api")

#: back-compat alias for the kv key persisting the send-lane configuration
#: (the canonical constants live in core.rate_limit_manager)
LANE_KV_KEY = KV_LANE_KEY


async def _resolved(outcome: Any) -> Any:
    """Await manager results when the manager went async (migration shim).

    Same tolerance pattern AccountManager uses for its collaborators: a
    method may return a plain value today and a coroutine after the async
    persistence switch — both shapes work through here.
    """
    if inspect.iscoroutine(outcome):
        return await outcome
    return outcome


def parse_bool(value: Any, field: str) -> bool:
    """Strict boolean parsing shared by every mutation surface.

    bool("false") == True silently did the OPPOSITE of the request once —
    JSON/CLI surfaces must carry real booleans; anything else is rejected.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"field '{field}' must be a boolean (true/false), got {value!r}")


class ControlAPI:
    """High-level operations over every manager (JSON-safe in/out)."""

    def __init__(
        self,
        *,
        accounts: "AccountManager",
        plugins: "PluginManager",
        profiles: "ProfileManager",
        settings: "SettingsManager",
        limiter: "RateLimitManager",
        scheduler: "TaskScheduler",
        logs: "LogService",
        audit: Optional["AuditRecorder"] = None,
        simulation: Any = None,          # adapters.simulator.runner.SimulationRunner
        credentials: Optional["Credentials"] = None,
        database: Any = None,
        credentials_svc: Optional["CredentialService"] = None,
        cooldowns: Optional["CooldownService"] = None,
        sync_groups: Any = None,
        access: Any = None,              # core.access_manager.AccessManager
        started_at: float = 0.0,
        reconcile_interval_s: float = 30.0,
    ) -> None:
        self._accounts = accounts
        self._plugins = plugins
        self._profiles = profiles
        self._settings = settings
        self._limiter = limiter
        self._scheduler = scheduler
        self._logs = logs
        self._audit = audit
        self._simulation = simulation
        self._credentials = credentials
        self._database = database
        self._credentials_svc = credentials_svc
        self._cooldowns = cooldowns
        self._sync_groups = sync_groups
        self._access = access
        # NOTE: started_at is accepted (core/bootstrap passes it) but the
        # value is never read — uptime derives from _booted_at below; kept
        # for the bootstrap call-site contract.
        self._started_at = started_at
        self._booted_at = _time.time()
        # FINAL-PHASE (F3): authoritative reconciliation cadence (bounds
        # externally-edited state; 0 disables — tests/sims want determinism)
        self._reconcile_interval_s = float(reconcile_interval_s)
        self._last_reconcile = -1e9  # first status() call reconciles
        #: serializes the cleared-policy tombstone read-modify-write: two
        #: concurrent ratelimit_clear calls raced the kv read/write and
        #: lost tombstones, resurrecting cleared defaults on the next boot
        self._tombstone_lock = asyncio.Lock()

    # ---------------------------------------------------------------- status
    async def status(self) -> Dict[str, Any]:
        """Overview snapshot: accounts + rate limits + tasks + warnings.

        Account rows are enriched with their credential status
        ("db"|"missing"|None) and the number of active persistent
        cooldowns — both filled in ONLY when the manager snapshot lacks
        them, so a manager that already knows never gets second-guessed.
        """
        await self._maybe_reconcile()  # FINAL-PHASE (F3): bounded staleness
        return {
            "accounts": await self._enrich_accounts(await self._accounts.accounts()),
            "ratelimit": self._limiter.stats(),
            "tasks": self._scheduler.stats(),
            "warnings": self._limiter.warnings(),
            "uptime_s": round(_time.time() - self._booted_at, 1),
        }

    async def _maybe_reconcile(self) -> None:
        """FINAL-PHASE (F3): periodic authoritative reconciliation.

        In-process mutations are memory-immediate; this 30s pass bounds
        divergence from OUT-of-band DB edits (a second process, manual
        sqlite) so the memory-first status poll stays honest without
        polling SQLite per second.
        """
        now = _time.monotonic()
        if now - self._last_reconcile < self._reconcile_interval_s:
            return
        self._last_reconcile = now
        try:
            reconcile = getattr(self._accounts, "reconcile_records", None)
            if callable(reconcile):
                await reconcile()
            if self._cooldowns is not None:
                resync = getattr(self._cooldowns, "resync", None)
                if callable(resync):
                    await resync()
        except Exception:
            LOG.debug("status reconciliation failed", exc_info=True)

    async def _enrich_accounts(
        self, accounts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Add 'credential' + 'cooldowns' to manager status dicts (in place)."""
        if not isinstance(accounts, list):
            return accounts
        for account in accounts:
            if not isinstance(account, dict) or not account.get("name"):
                continue
            name = account["name"]
            if "credential" not in account:
                account["credential"] = self._credential_state(name)
        if self._cooldowns is not None:
            try:
                # FINAL-PHASE (F3): memory-only counts (the mirror is
                # boot-restored + mutation-written; resync absorbs
                # external edits) — ONE O(C) pass for all accounts, not
                # a per-account scan of every row (O(N×C) at the 1 Hz
                # status poll)
                counts = self._cooldowns.active_counts_all()
            except Exception:
                counts = {}
            for account in accounts:
                if isinstance(account, dict) and account.get("name") \
                        and "cooldowns" not in account:
                    account["cooldowns"] = counts.get(account["name"], 0)
        return accounts

    def _credential_state(self, name: str) -> Optional[str]:
        """'db' | 'missing' | None (no service wired). Never a value.

        ('env' no longer exists at runtime — the encrypted store is the
        sole source and .env tokens only enter via import-env.)
        """
        if self._credentials_svc is None:
            return None
        try:
            return self._credentials_svc.status(name)
        except Exception:
            return None

    # -------------------------------------------------------------- accounts
    async def accounts_list(self) -> List[Dict[str, Any]]:
        """Every account: name, discord id (when running), status, profiles,
        credential status and active-cooldown count."""
        return await self._enrich_accounts(await self._accounts.accounts())

    async def accounts_add(
        self, payload: Dict[str, Any], surface: str = "api"
    ) -> Dict[str, Any]:
        """Register + persist an account; optionally store its credential.

        payload: {name (required), token_ref?, sync_group?, enabled?,
                  plugins?: [names], credential?: str}. Unknown keys are
        rejected (typo trap) — ``profiles`` is no longer accepted: profile
        assignment stopped controlling account capabilities, so the key is
        gone rather than silently ignored.

        ``credential`` (the token VALUE) is handed to the encrypted
        CredentialService AFTER the account row exists — never into the
        accounts table, never into any response payload, and storing it
        performs NO network activity (saving is always separate from
        connecting, spec §16).
        """
        if not isinstance(payload, dict) or not payload.get("name"):
            raise ValueError("account payload needs at least {'name': ...}")
        allowed = {"name", "token_ref", "sync_group", "enabled", "plugins",
                   "credential"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown account payload keys: {sorted(unknown)}")
        plugins = payload.get("plugins") or []
        if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
            raise ValueError(f"'plugins' must be a list of strings, got {plugins!r}")
        self._validate_plugins_known(plugins)
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("field 'enabled' must be a boolean")
        credential = payload.get("credential")
        if credential is not None:
            if not isinstance(credential, str) or not credential.strip():
                raise ValueError("field 'credential' must be a non-empty string")
            credential = credential.strip()
            if self._credentials_svc is None:
                raise RuntimeError(
                    "a credential was supplied but credential storage is not "
                    "configured on this api"
                )
        out = await _resolved(self._accounts.add_account(
            payload["name"],
            token_ref=payload.get("token_ref"),
            enabled=enabled,
            sync_group=payload.get("sync_group"),
            plugins=plugins or None,
        ))
        name = out.get("name", payload["name"]) if isinstance(out, dict) else payload["name"]
        self._audit_mgmt(name, "mgmt:account.created", surface)
        if credential is not None:
            # TOCTOU: an interleaved remove_account between the add above
            # and the store below would re-create an orphan credential row
            # for a dead account — re-check the registration first
            await _resolved(self._accounts.get(name))  # AccountError if gone
            await self._credentials_svc.store(name, credential)
            self._audit_mgmt(name, "mgmt:credential.stored", surface)
            if isinstance(out, dict):
                out["credential"] = self._credential_state(name)
        return out

    async def accounts_remove(self, name: str, surface: str = "api") -> Dict[str, Any]:
        """Remove a STOPPED account from runtime + database (cascade).

        Cascade (spec §32): the account's stored credential and every one
        of its persistent cooldowns are deleted afterwards, each guarded —
        the removal itself must never fail because a service is unwired.

        ABORTED removals (the manager detected a same-named account was
        re-created mid-cascade) skip the whole cascade AND the removed
        audit row: those backstops are name-keyed, so running them would
        delete the NEW account's credential/cooldown/access state.
        """
        out = await _resolved(self._accounts.remove_account(name))
        if isinstance(out, dict) and (
            out.get("recreated") or not out.get("removed", True)
        ):
            # the manager preserved a re-created same-named account —
            # cascading now would destroy ITS state
            self._audit_mgmt(name, "mgmt:account.remove-aborted", surface,
                             extra="recreated=true")
            return out
        self._audit_mgmt(name, "mgmt:account.removed", surface)
        # Access row cascade: clear the admin/perms/prefix row so the
        # Admin page doesn't show deleted accounts
        access = getattr(self, "_access", None)
        if access is not None:
            try:
                await access.forget_account(name)
            except Exception:
                LOG.exception("access cascade failed for %r", name)
        # Backstop cascade (idempotent with manager.remove_account's own
        # guarded cascade): covers stacks where the MANAGER is not wired
        # with the credential service / cooldown service (direct
        # ControlAPI use, partial test/sim stacks). Both deletes are
        # no-ops when the manager cascade already ran.
        if self._credentials_svc is not None:
            try:
                await self._credentials_svc.delete(name)
            except Exception:
                LOG.exception("credential cascade failed for %r", name)
        if self._cooldowns is not None:
            try:
                await self._cooldowns.delete_account(name)
            except Exception:
                LOG.exception("cooldown cascade failed for %r", name)
        return out

    async def accounts_info(self, name: str) -> Dict[str, Any]:
        """Account details page (spec §8): status/identity/runtime/plugins/rate."""
        out = await _resolved(self._accounts.details(name))
        if isinstance(out, dict) and "credential" not in out:
            out["credential"] = self._credential_state(out.get("name", name))
        return out

    async def accounts_enable(self, name: str, enabled: bool) -> Dict[str, Any]:
        return await _resolved(
            self._accounts.set_enabled(name, parse_bool(enabled, "enabled"))
        )

    async def accounts_start(
        self, name: Optional[str] = None, surface: str = "api"
    ) -> Dict[str, Any]:
        """Start one account (None = every enabled account)."""
        out = await self._accounts.start(name) if name else await self._accounts.start_all()
        self._audit_mgmt(name or "system", "mgmt:account.started", surface)
        return out

    async def accounts_stop(
        self, name: Optional[str] = None, surface: str = "api"
    ) -> Dict[str, Any]:
        """Stop one account (None = all) — graceful."""
        out = await self._accounts.stop(name) if name else await self._accounts.stop_all()
        self._audit_mgmt(name or "system", "mgmt:account.stopped", surface)
        return out

    async def accounts_emergency_stop(
        self, name: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Account-scoped emergency stop: cancel its tasks NOW + disconnect."""
        out = await _resolved(self._accounts.emergency_stop(name))
        self._audit_mgmt(name, "mgmt:account.estop", surface)
        return out

    # ----------------------------------------------------- account plugins
    # Direct account→plugin assignments (the capability source since the
    # profile-mediated model was removed). Every mutation persists through
    # the account manager (which keeps its in-memory record authoritative
    # and re-runs the attach), so a RUNNING account's plugin set follows
    # instantly — no next-start semantics.
    async def accounts_plugins_list(self, account: str) -> List[str]:
        """Sorted plugin names assigned to the account."""
        return await _resolved(self._accounts.assigned_plugins(account))

    async def accounts_plugin_assign(
        self, account: str, plugin: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Assign ONE plugin to an account (idempotent; re-attaches NOW).

        Raises:
            AccountError: unknown account.
            ValueError: unknown plugin (the discovered set is listed —
                typo trap, same convention as the sync-group managers).
        """
        self._validate_account_known(account)
        self._validate_plugins_known([plugin])
        await _resolved(self._accounts.assign_plugin(account, plugin))
        self._audit_mgmt(account, "mgmt:account.plugin_assigned", surface,
                         extra=f"plugin={plugin}")
        return {"account": account, "plugin": plugin}

    async def accounts_plugin_unassign(
        self, account: str, plugin: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Remove ONE plugin assignment (idempotent; re-attaches NOW).

        ``existed`` reports the pre-state (False when the plugin was not
        assigned — not an error).
        """
        self._validate_account_known(account)
        existed = bool(await _resolved(
            self._accounts.unassign_plugin(account, plugin)))
        self._audit_mgmt(account, "mgmt:account.plugin_unassigned", surface,
                         extra=f"plugin={plugin} existed={existed}")
        return {"account": account, "plugin": plugin, "existed": existed}

    async def accounts_plugins_set(
        self, account: str, plugins: List[str], surface: str = "api"
    ) -> Dict[str, Any]:
        """REPLACE the account's whole assignment set (re-attaches NOW).

        ALL names are validated BEFORE anything is written: an unknown
        name rejects the whole call (all-or-nothing), never a partial set.
        """
        if not isinstance(plugins, (list, tuple)) or not all(
                isinstance(p, str) for p in plugins):
            raise ValueError(f"'plugins' must be a list of strings, got {plugins!r}")
        self._validate_account_known(account)
        self._validate_plugins_known(list(plugins))
        stored = list(await _resolved(
            self._accounts.set_account_plugins(account, list(plugins))))
        self._audit_mgmt(account, "mgmt:account.plugin_set", surface,
                         extra=f"plugins={len(stored)}")
        return {"account": account, "plugins": stored}

    def _validate_account_known(self, account: str) -> None:
        """AccountError when the account is not registered (typo trap)."""
        self._accounts.get(account)

    def _validate_plugins_known(self, plugins: List[str]) -> None:
        """Reject unknown plugin names up front with the discovered list.

        The discovered/installed set comes from the plugin manager (the
        same source ``plugins_manifests`` serves); a failure listing it
        fails CLOSED (every name unknown) — capability assignment must
        not degrade into accepting typos.
        """
        known = self._known_plugin_names()
        unknown = [p for p in plugins if p not in known]
        if unknown:
            raise ValueError(
                f"unknown plugin(s) {unknown} (known plugins: {known})"
            )

    def _known_plugin_names(self) -> List[str]:
        """Sorted discovered plugin names (typo-trap source of truth)."""
        try:
            names = getattr(self._plugins, "names", None)
            if callable(names):
                return sorted(set(names() or []))
            return sorted({m.get("name") for m in self._plugins.manifests()
                           if isinstance(m, dict) and m.get("name")})
        except Exception:
            LOG.exception("could not list discovered plugins")
            return []

    def _plugin_schema(self, plugin: str) -> tuple:
        """Setting schema of one plugin, duck-typed over the manager.

        Prefers the plugin manager's ``_schema_for`` (the SAME source the
        set-setting path validates against — parity by construction); a
        stub manager without it falls back to rebuilding Setting objects
        from the manifests. Unknown plugins raise PluginError.
        """
        getter = getattr(self._plugins, "_schema_for", None)
        if callable(getter):
            return tuple(getter(plugin))
        from .settings_manager import Setting
        for manifest in self._plugins.manifests():
            if not (isinstance(manifest, dict) and manifest.get("name") == plugin):
                continue
            settings = []
            for entry in manifest.get("settings") or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                settings.append(Setting(
                    entry["name"], entry.get("type", "str"),
                    entry.get("default"), entry.get("description", ""),
                    min_value=entry.get("min"), max_value=entry.get("max"),
                ))
            return tuple(settings)
        raise ValueError(f"unknown plugin {plugin!r} (no schema available)")

    def _validate_profile_settings(
        self, plugin: str, settings: Any
    ) -> Dict[str, Any]:
        """Validate a profile's settings dict against the plugin's schema.

        Same validation path as /api/plugins/set-setting: unknown keys
        are rejected (typo trap, the known keys listed), values are
        type-checked through each Setting's own ``validate``. The
        RESERVED ``channels`` key (the channel-scope convention) is
        validated by core.plugin_scopes instead and stored normalized.
        Returns the cleaned settings dict ({} for None).
        """
        if settings is None:
            return {}
        if not isinstance(settings, dict):
            raise ValueError(f"'settings' must be an object, got {settings!r}")
        defs = {s.name: s for s in self._plugin_schema(plugin)}
        reserved = plugin_scopes.CHANNELS_KEY
        unknown = sorted(k for k in settings if k != reserved and k not in defs)
        if unknown:
            raise ValueError(
                f"unknown setting key(s) {unknown} for plugin {plugin!r} "
                f"(known: {sorted(defs)}; reserved: [{reserved!r}])"
            )
        cleaned: Dict[str, Any] = {}
        for key, value in settings.items():
            if key == reserved:
                continue
            cleaned[key] = defs[key].validate(value)  # SettingsValidationError
        if reserved in settings:
            # the single scope validator (later phases reuse it verbatim)
            cleaned[reserved] = plugin_scopes.validate(settings.get(reserved))
        return cleaned

    # ----------------------------------------------------- plugin profiles
    # Per-plugin named setting bundles (migration v7) + the account-side
    # bindings. Persistence is schema-agnostic; THIS layer validates
    # settings against the plugin's Setting schema (plugin_profiles_*)
    # and enforces "binding requires assignment" (accounts_plugin_profile).
    def _profile_repo(self) -> Any:
        """PluginProfilesRepository, or None on stacks without a database."""
        return getattr(self._database, "plugin_profiles", None) \
            if self._database is not None else None

    def _require_profile_repo(self) -> Any:
        """Honest failure on stacks built without profile storage."""
        repo = self._profile_repo()
        if repo is None:
            raise RuntimeError(
                "plugin profiles are not configured on this api")
        return repo

    def _binding_repo(self) -> Any:
        """AccountPluginsRepository (bindings), or None without a database."""
        return getattr(self._database, "account_plugins", None) \
            if self._database is not None else None

    async def plugin_profiles_list(
        self, plugin: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Every profile (or one plugin's), plugin-then-name order.

        Each row: plugin, name, description, settings, created_at,
        updated_at (plus the row id).
        """
        repo = self._profile_repo()
        if repo is None:
            return []
        return [record.to_dict()
                for record in await _resolved(repo.list(plugin))]

    async def plugin_profile_create(
        self,
        plugin: str,
        name: str,
        description: str = "",
        settings: Optional[Dict[str, Any]] = None,
        surface: str = "api",
    ) -> Dict[str, Any]:
        """Create one plugin profile (settings validated against schema).

        Raises:
            ValueError: unknown plugin (discovered set listed), bad
                name/description, unknown settings key, bad value type,
                or a malformed channels scope.
            DatabaseError: the (plugin, name) pair already exists.
        """
        repo = self._require_profile_repo()
        if not isinstance(name, str) or not name.strip():
            raise ValueError("profile name must be a non-empty string")
        if not isinstance(description, str):
            raise ValueError("field 'description' must be a string")
        self._validate_plugins_known([plugin])
        cleaned = self._validate_profile_settings(plugin, settings)
        try:
            record = await repo.create(
                plugin, name.strip(), description, cleaned)
        except DatabaseError as exc:
            # duplicate (plugin, name) is a client conflict, not a 500 —
            # mirror the update path's conversion
            if "already exists" in str(exc):
                raise ValueError(
                    f"profile {name.strip()!r} already exists for plugin "
                    f"{plugin!r}") from exc
            raise
        self._audit_mgmt(
            "system", "mgmt:plugin_profile.created", surface,
            extra=f"plugin={plugin} profile={record.name}")
        return record.to_dict()

    async def plugin_profile_update(
        self,
        plugin: str,
        name: str,
        *,
        name_new: Optional[str] = None,
        description: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        surface: str = "api",
    ) -> Dict[str, Any]:
        """Update name/description/settings of one profile.

        At least one field is required; a rename refuses an existing
        (plugin, name) pair, rewrites bound accounts' binding pointers in
        the SAME transaction (repository cascade) and refreshes the
        account manager's memory records afterwards.
        """
        repo = self._require_profile_repo()
        if name_new is None and description is None and settings is None:
            raise ValueError(
                "profile update needs at least one of "
                "{'name_new', 'description', 'settings'}"
            )
        if name_new is not None and (
            not isinstance(name_new, str) or not name_new.strip()
        ):
            raise ValueError("field 'name_new' must be a non-empty string")
        if description is not None and not isinstance(description, str):
            raise ValueError("field 'description' must be a string")
        self._validate_plugins_known([plugin])
        current = await repo.get(plugin, name)
        if current is None:
            known = [record.name for record in await repo.list(plugin)]
            raise ValueError(
                f"unknown profile {name!r} for plugin {plugin!r} "
                f"(known: {known})")
        cleaned = None if settings is None \
            else self._validate_profile_settings(plugin, settings)
        try:
            updated = await repo.update(
                plugin, name, name_new=name_new, description=description,
                settings=cleaned)
        except DatabaseError as exc:
            if "already exists" in str(exc):
                raise ValueError(str(exc)) from exc  # 400, not a db fault
            raise
        if updated is None:  # vanished mid-update (never silent)
            raise ValueError(
                f"profile {name!r} of plugin {plugin!r} disappeared mid-update")
        if updated.name != current.name:
            # the rename cascade moved every bound account's pointer in
            # the DB — the in-memory records must follow (F3 pattern)
            await self._refresh_all_plugin_profiles()
        await self._refresh_live_settings(None, plugin)
        self._audit_mgmt(
            "system", "mgmt:plugin_profile.updated", surface,
            extra=f"plugin={plugin} profile={updated.name}")
        return updated.to_dict()

    async def plugin_profile_delete(
        self, plugin: str, name: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Delete one profile; its account bindings cascade to NULL.

        Idempotent: deleting a missing profile reports ``existed=False``
        and raises nothing.
        """
        repo = self._require_profile_repo()
        existed = bool(await repo.delete(plugin, name))
        if existed:  # the cascade NULLed bound accounts' bindings in the DB
            await self._refresh_all_plugin_profiles()
            await self._refresh_live_settings(None, plugin)
        self._audit_mgmt(
            "system", "mgmt:plugin_profile.deleted", surface,
            extra=f"plugin={plugin} profile={name} existed={existed}")
        return {"plugin": plugin, "profile": name, "existed": existed}

    async def accounts_plugin_profile(
        self,
        account: str,
        plugin: str,
        profile: Optional[str],
        surface: str = "api",
    ) -> Dict[str, Any]:
        """Bind (or clear: profile=None) the account's profile for a plugin.

        Binding REQUIRES the plugin to be assigned to the account (an
        assignment-less binding could never take effect); the profile
        must belong to that plugin. The assignment is unchanged, so NO
        plugin re-attach happens — only the in-memory binding record and
        an audit row are written.
        """
        repo = self._require_profile_repo()
        if self._binding_repo() is None:
            raise RuntimeError(
                "plugin profile storage is not configured on this api")
        self._validate_account_known(account)
        self._validate_plugins_known([plugin])
        if profile is not None and (
            not isinstance(profile, str) or not profile.strip()
        ):
            raise ValueError("field 'profile' must be a non-empty string or null")
        assigned = await self._assigned_plugins(account)
        if plugin not in assigned:
            raise ValueError(
                f"plugin {plugin!r} is not assigned to account {account!r} "
                f"(assigned: {assigned}; bind a profile only for an "
                f"assigned plugin)"
            )
        if profile is not None:
            record = await repo.get(plugin, profile)
            if record is None:
                known = [row.name for row in await repo.list(plugin)]
                raise ValueError(
                    f"unknown profile {profile!r} for plugin {plugin!r} "
                    f"(known: {known})")
        await self._binding_repo().set_profile(account, plugin, profile)
        self._set_account_plugin_profile(account, plugin, profile)
        await self._refresh_live_settings(account, plugin)
        self._audit_mgmt(
            account, "mgmt:account.plugin_profile", surface,
            extra=f"plugin={plugin} profile={profile or '-'}")
        return {"account": account, "plugin": plugin, "profile": profile}

    async def accounts_plugin_profiles(
        self, account: str
    ) -> Dict[str, Optional[str]]:
        """``{plugin: profile name or None}`` for every ASSIGNED plugin."""
        self._validate_account_known(account)
        assigned = await self._assigned_plugins(account)
        bindings: Dict[str, Optional[str]] = {}
        binding_repo = self._binding_repo()
        reader = getattr(binding_repo, "bindings", None)
        if callable(reader):
            bindings = dict(await reader(account) or {})
        return {plugin: bindings.get(plugin) for plugin in assigned}

    async def accounts_effective_settings(
        self, account: str, plugin: str
    ) -> Dict[str, Any]:
        """Effective settings for one (account, plugin), fully merged.

        Merge order (highest wins): plugin Setting defaults <- the global
        layer <- the bound
        profile's settings (minus the reserved ``channels`` key) <- the
        account's per-plugin overrides (plugin_settings rows). The
        profile's channel scope rides along under ``"channels"`` (None =
        no restriction). Requires the plugin to be assigned to the
        account, like every account-scoped plugin setting surface.
        """
        self._validate_account_known(account)
        self._validate_plugins_known([plugin])
        assigned = await self._assigned_plugins(account)
        if plugin not in assigned:
            raise ValueError(
                f"plugin {plugin!r} is not assigned to account {account!r} "
                f"(assigned: {assigned})"
            )
        reserved = plugin_scopes.CHANNELS_KEY
        merged: Dict[str, Any] = {
            setting.name: setting.validate(setting.default)
            for setting in self._plugin_schema(plugin)
        }
        # the GLOBAL layer sits between the defaults and the profile —
        # exactly where SettingsManager.resolve applies it, so this view
        # agrees with what the running plugin actually uses
        layers_view = getattr(self._settings, "layers_view", None)
        if callable(layers_view):
            try:
                global_layer = (layers_view().get("global") or {}).get(plugin) or {}
            except Exception:
                global_layer = {}
            for key, value in global_layer.items():
                if key != reserved:
                    merged[key] = value
        profile_name: Optional[str] = None
        scope: Optional[Dict[str, Any]] = None
        binding_repo = self._binding_repo()
        profile_repo = self._profile_repo()
        bindings_reader = getattr(binding_repo, "bindings", None)
        if callable(bindings_reader):
            profile_name = (await bindings_reader(account) or {}).get(plugin)
        if profile_name is not None and profile_repo is not None:
            record = await profile_repo.get(plugin, profile_name)
            if record is not None:  # dangling binding (out-of-band edit): skip
                for key, value in (record.settings or {}).items():
                    if key != reserved:
                        merged[key] = value
                raw_scope = (record.settings or {}).get(reserved)
                scope = plugin_scopes.validate(raw_scope) \
                    if raw_scope is not None else None
        if self._database is not None:  # per-account overrides (highest)
            overrides = await self._database.plugins.settings(account, plugin) \
                or {}
            for key, value in overrides.items():
                if key != reserved:  # the scope is profile-owned
                    merged[key] = value
        merged[reserved] = scope
        return merged

    async def accounts_channels(self, account: str) -> List[Dict[str, Any]]:
        """The account's visible TEXT channels, from the live client cache.

        ``[{"guild_id", "guild_name", "channels": [{"id", "name",
        "type"}]}]`` (guild name then channel name order, ids as
        strings). [] for offline/unstarted accounts — never raises; the
        UI offers manual channel-ID entry as the fallback (Phase 2).
        Memory-only read of the client's READY cache: zero API calls.
        """
        self._validate_account_known(account)
        snapshot = getattr(self._accounts, "channels_snapshot", None)
        if not callable(snapshot):
            return []
        try:
            return list(await _resolved(snapshot(account)) or [])
        except Exception:
            LOG.exception("channel snapshot failed for %r", account)
            return []

    async def _assigned_plugins(self, account: str) -> List[str]:
        """Sorted assigned plugins, memory-first via the account manager
        (repository bindings' keys as the duck-typed fallback)."""
        reader = getattr(self._accounts, "assigned_plugins", None)
        if callable(reader):
            return sorted(set(await _resolved(reader(account)) or []))
        binding_repo = self._binding_repo()
        fallback = getattr(binding_repo, "bindings", None)
        if not callable(fallback):
            return []
        return sorted((await fallback(account) or {}))

    def _set_account_plugin_profile(
        self, account: str, plugin: str, profile: Optional[str]
    ) -> None:
        """Propagate ONE binding change into the manager's memory record
        (FINAL-PHASE F3 pattern, same as _refresh_account_group): the
        status poll serves Account.plugin_profiles, but the repository
        owns the persisted value."""
        try:
            account_obj = getattr(self._accounts, "_accounts", {}).get(account)
            if account_obj is not None and hasattr(account_obj, "plugin_profiles"):
                account_obj.plugin_profiles[plugin] = profile
        except Exception:
            LOG.debug("plugin profile memory refresh failed for %r", account,
                      exc_info=True)

    async def _refresh_live_settings(self, account: Optional[str] = None,
                                     plugin: Optional[str] = None) -> None:
        """Push a profile-layer change into RUNNING plugins' settings."""
        refresh = getattr(self._plugins, "refresh_settings", None)
        if not callable(refresh):
            return
        try:
            out = refresh(account, plugin)
            if inspect.iscoroutine(out):
                await out
        except Exception:
            LOG.debug("live settings refresh failed", exc_info=True)

    async def _refresh_all_plugin_profiles(self) -> None:
        """Re-read EVERY account's bindings into the memory records.

        Rename/delete cascades rewrite MANY accounts' binding pointers in
        one repository transaction; one bulk re-read afterwards keeps the
        memory-first status poll honest without per-account queries.
        """
        binding_repo = self._binding_repo()
        reader = getattr(binding_repo, "all_bindings", None)
        if not callable(reader):
            return
        try:
            bindings = await reader() or {}
        except Exception:
            LOG.debug("plugin profile binding re-read failed", exc_info=True)
            return
        for name, bound in bindings.items():
            try:
                account_obj = getattr(self._accounts, "_accounts", {}).get(name)
                if account_obj is not None and hasattr(account_obj, "plugin_profiles"):
                    account_obj.plugin_profiles = dict(bound)
            except Exception:
                LOG.debug("plugin profile memory refresh failed for %r", name,
                          exc_info=True)

    # ------------------------------------------------------ account settings
    async def account_setting_get(self, name: str) -> Dict[str, Any]:
        """Resolved framework account settings (reconnect policy...).

        Facade over SettingsManager.account_settings — validated against
        the ACCOUNT_SETTINGS schema (stored junk is warned about and
        ignored by the manager, never echoed raw).
        """
        settings = await _resolved(self._settings.account_settings(name))
        return {"account": name, "settings": dict(settings or {})}

    async def account_setting_set(
        self, name: str, key: str, value: Any, surface: str = "api"
    ) -> Dict[str, Any]:
        """Set ONE framework account setting (validated + persisted).

        Raises SettingsValidationError (a ValueError) for unknown keys or
        bad values — surfaces render it like any other 400. Emits
        SettingChanged on success; the account manager picks reconnect
        changes up on the next reconnect decision.
        """
        validated = await _resolved(
            self._settings.set_account_setting(name, key, value)
        )
        self._audit_mgmt(name, "mgmt:account.setting", surface,
                         extra=f"key={key}")
        return {"account": name, "key": key, "value": validated}

    # ----------------------------------------------------------- credentials
    async def accounts_set_credential(
        self, name: str, credential: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Store (replace) one account's credential — encrypt-at-rest only.

        No network activity whatsoever. Unknown accounts raise the
        manager's AccountError; the response carries the account name and
        the new credential STATUS, never the value.
        """
        await _resolved(self._accounts.get(name))  # AccountError when unknown
        if self._credentials_svc is None:
            raise RuntimeError("credential storage is not configured on this api")
        if not isinstance(credential, str) or not credential.strip():
            raise ValueError("credential must be a non-empty string")
        await self._credentials_svc.store(name, credential)
        self._audit_mgmt(name, "mgmt:credential.stored", surface)
        return {"account": name, "credential": self._credential_state(name)}

    async def accounts_credential_status(self) -> List[Dict[str, Any]]:
        """[{name, credential}] for every account — display-safe, no values."""
        out: List[Dict[str, Any]] = []
        for account in await self._accounts.accounts():
            name = account.get("name") if isinstance(account, dict) else None
            if not name:
                continue
            out.append({"name": name, "credential": self._credential_state(name)})
        return out

    async def credentials_import_env(
        self, overwrite: bool = False, surface: str = "api"
    ) -> Dict[str, Any]:
        """MIGRATION-ONLY: explicit one-shot import of legacy .env tokens.

        Reads each registered account's ``DISCORD_TOKEN_<NAME>`` value from
        the .env file and stores it encrypted in the database. This is the
        ONLY path by which .env tokens enter the system — normal runtime
        credential resolution NEVER reads .env. Skips accounts that already
        have a stored DB credential unless ``overwrite``. Returns account
        NAMES only (never token values).
        """
        overwrite = parse_bool(overwrite, "overwrite")
        if self._credentials_svc is None:
            raise RuntimeError("credential storage is not configured on this api")
        report = await self._credentials_svc.import_from_env(
            await self._env_account_tokens(), overwrite
        )
        self._audit_mgmt(
            "system", "mgmt:credential.imported", surface,
            extra=(
                f"imported={len(report.get('imported') or [])} "
                f"skipped={len(report.get('skipped') or [])} "
                f"failed={len(report.get('failed') or {})}"
            ),
        )
        return report

    async def _env_account_tokens(self) -> Dict[str, str]:
        """account -> token mapping from the legacy .env view (no DB writes).

        Enumerates REGISTERED accounts and resolves each
        ``DISCORD_TOKEN_<NAME>`` entry (the migration-only accessor).
        """
        if self._credentials is None:
            return {}
        credentials = self._credentials
        # the boot snapshot is FROZEN: a .env edited after boot must still
        # be importable from a long-lived dashboard/REPL (re-read on demand)
        source_path = getattr(credentials, "source_path", None)
        if callable(source_path):
            try:
                path = source_path()
            except Exception:
                path = None
            if path:
                try:
                    from .credentials import Credentials as _Creds
                    fresh = _Creds.from_file(str(path))
                    if fresh is not None:
                        credentials = fresh
                except Exception:
                    pass  # unreadable now: fall back to the boot snapshot
        token_for = getattr(credentials, "token_for", None)
        if not callable(token_for):
            return {}
        tokens: Dict[str, str] = {}
        for account in await self._accounts.accounts():
            name = account.get("name") if isinstance(account, dict) else None
            if not name:
                continue
            token = token_for(name)
            if token:
                tokens[name] = token
        return tokens

    # ------------------------------------------------------------- cooldowns
    async def cooldowns_list(
        self, account: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Active persistent cooldowns of one account (None = every account).

        JSON-safe rows: account/plugin/cooldown_key, the REMAINING seconds
        (absolute expiry minus now — restart-correct), source and reason.
        """
        if self._cooldowns is None:
            return []
        if account is not None:
            names: List[str] = [account]
        else:
            names = [
                row.get("name")
                for row in await self._accounts.accounts()
                if isinstance(row, dict) and row.get("name")
            ]
        now = _time.time()
        rows: List[Dict[str, Any]] = []
        for name in names:
            if not name:
                continue
            for row in await self._cooldowns.active_for(name):
                expires_at = float(row.get("expires_at", 0.0) or 0.0)
                rows.append({
                    "account": row.get("account", name),
                    "plugin": row.get("plugin"),
                    "cooldown_key": row.get("cooldown_key"),
                    "remaining_s": round(max(0.0, expires_at - now), 1),
                    "source": row.get("source"),
                    "reason": row.get("reason") or "",
                })
        rows.sort(key=lambda r: r["remaining_s"])  # soonest-expiring first
        return rows

    async def cooldowns_clear(
        self,
        account: str,
        plugin: str,
        cooldown_key: Optional[str] = None,
        surface: str = "api",
    ) -> Dict[str, Any]:
        """Clear ONE cooldown (``cooldown_key`` given) or ALL of a plugin."""
        if self._cooldowns is None:
            raise RuntimeError("cooldown service is not configured on this api")
        for label, value in (("account", account), ("plugin", plugin)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"cooldown {label} must be a non-empty string")
        if cooldown_key is not None and (
            not isinstance(cooldown_key, str) or not cooldown_key
        ):
            raise ValueError("cooldown_key must be a non-empty string when given")
        cleared = 0
        if cooldown_key:
            cleared = 1 if await self._cooldowns.clear(account, plugin, cooldown_key) else 0
        else:
            for row in await self._cooldowns.active_for(account, plugin=plugin):
                key = row.get("cooldown_key")
                if key and await self._cooldowns.clear(account, plugin, key):
                    cleared += 1
        self._audit_mgmt(
            account, "mgmt:cooldown.cleared", surface,
            extra=f"plugin={plugin} key={cooldown_key or '*'} cleared={cleared}",
        )
        return {
            "account": account,
            "plugin": plugin,
            "cooldown_key": cooldown_key,
            "cleared": cleared,
        }

    # --------------------------------------------------------------- plugins
    async def plugins_manifests(self) -> List[Dict[str, Any]]:
        """Installed plugins incl. their settings schema (auto-generated UIs)."""
        return self._plugins.manifests()

    async def plugins_state(self, account: str) -> List[Dict[str, Any]]:
        """Per-account plugin runtime state incl. resolved settings + errors."""
        return self._plugins.states(account)

    async def plugins_reload(self, plugin: str,
                             surface: str = "api") -> Dict[str, Any]:
        """Hot-reload one plugin (detach → re-import → re-attach).

        The reserved name ``"__rescan__"`` re-scans the plugin directory
        instead (no single plugin targeted) — the Plugins page's rescan
        button.
        """
        # no names() pre-check: an UNLOADED plugin is not in the discovered
        # set yet must stay reloadable; the manager re-scans and rejects
        # genuinely unknown names itself
        try:
            out = self._plugins.reload_plugin(plugin)
            if inspect.iscoroutine(out):
                out = await out
        except PluginError as exc:
            if str(exc).startswith("unknown plugin"):
                raise ValueError(f"unknown plugin {plugin!r}") from exc
            raise
        self._audit_mgmt("system", "mgmt:plugin.reloaded", surface,
                         extra=f"plugin={plugin}")
        return out

    async def plugins_unload(self, plugin: str,
                             surface: str = "api") -> Dict[str, Any]:
        """Unload a plugin (detach everywhere, unregister UI; assignments
        stay in the DB so reload restores them)."""
        out = self._plugins.unload_plugin(plugin)
        if inspect.iscoroutine(out):
            out = await out
        self._audit_mgmt("system", "mgmt:plugin.unloaded", surface,
                         extra=f"plugin={plugin}")
        return out

    async def plugins_set_enabled(
        self, account: str, plugin: str, enabled: bool
    ) -> Dict[str, Any]:
        """Enable/disable a plugin on an account (lifecycle hooks fire)."""
        await self._plugins.set_enabled(account, plugin, parse_bool(enabled, "enabled"))
        return {"account": account, "plugin": plugin, "enabled": enabled}

    async def plugins_settings(self, account: str) -> List[Dict[str, Any]]:
        """Resolved settings per plugin for one account."""
        return [
            {"name": state["name"], "settings": state["settings"]}
            for state in self._plugins.states(account)
        ]

    async def plugins_set_setting(
        self, account: str, plugin: str, key: str, value: Any
    ) -> Dict[str, Any]:
        """Change ONE plugin setting for ONE account (validated + persisted).

        ``value=None`` means RESET: the account-layer override is DELETED
        (:meth:`plugins_clear_setting`), unmasking the global layer /
        defaults — a null must not pin the schema default over a non-default
        global value (the dashboard set-setting route forwards null as-is).
        """
        if value is None:
            return await self.plugins_clear_setting(account, plugin, key)
        out = await self._plugins.set_setting(account, plugin, key, value)
        return {"account": account, "plugin": plugin, "key": key, "value": out}

    async def plugins_clear_setting(
        self, account: str, plugin: str, key: str
    ) -> Dict[str, Any]:
        """Reset ONE plugin setting: DELETE the account-layer override.

        Delegates to PluginManager.clear_setting when it exists (it also
        refreshes the running plugin's live settings view); otherwise
        falls back to SettingsManager.clear_plugin_setting (persistence +
        memory layers + SettingChanged; the live runtime view catches up
        on the next boot/resolve). Returns the newly-effective value.
        """
        clear = getattr(self._plugins, "clear_setting", None)
        if callable(clear):
            effective = await _resolved(clear(account, plugin, key))
        else:
            effective = await _resolved(
                self._settings.clear_plugin_setting(account, plugin, key)
            )
        return {
            "account": account, "plugin": plugin, "key": key,
            "cleared": True, "value": effective,
        }

    # -------------------------------------------------------------- profiles
    async def profiles_list(self) -> List[Dict[str, Any]]:
        return await _resolved(self._profiles.profiles())

    async def profiles_create(
        self, name: str, plugins: List[str], description: str = ""
    ) -> Dict[str, Any]:
        if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
            raise ValueError("'plugins' must be a list of plugin names")
        return await _resolved(self._profiles.set_profile(name, plugins, description))

    async def profiles_delete(self, name: str) -> Dict[str, Any]:
        return await _resolved(self._profiles.delete_profile(name))

    async def profiles_assign(self, account: str, profile: str) -> Dict[str, Any]:
        """Grant a profile (bookkeeping ONLY — no capability effect).

        Profiles no longer decide which plugins an account runs: that is
        the direct account→plugin assignment (accounts_plugin_assign /
        accounts_plugins_set). The grant is persisted and mirrored into
        the account record, and nothing re-attaches.
        """
        out = await _resolved(self._profiles.assign(account, profile))
        self._refresh_account_profiles(account)  # FINAL-PHASE (F3)
        return out

    async def profiles_unassign(self, account: str, profile: str) -> Dict[str, Any]:
        """Revoke a profile grant (bookkeeping ONLY — no capability effect;
        assignment-based attachment is untouched by profile changes)."""
        out = await _resolved(self._profiles.unassign(account, profile))
        self._refresh_account_profiles(account)  # FINAL-PHASE (F3)
        return out

    def _refresh_account_profiles(self, account: str) -> None:
        """Propagate a profile-assignment change into the manager's memory
        record (FINAL-PHASE F3): ProfileManager owns its cache but never
        touched AccountManager.record.profiles — the memory-first status
        poll must not go stale the moment profiles change."""
        try:
            manager = self._accounts
            account_obj = getattr(manager, "_accounts", {}).get(account)
            if account_obj is None:
                return
            cache = getattr(manager._profile_manager, "_assignment_cache", None)
            if isinstance(cache, dict):
                account_obj.record.profiles = sorted(
                    cache.get(account, []))
        except Exception:
            LOG.debug("profile memory refresh failed for %r", account,
                      exc_info=True)

    # ------------------------------------------------------------ sync groups
    async def groups_list(self) -> List[Dict[str, Any]]:
        """Every sync group with its member count (sorted by name)."""
        if self._sync_groups is None:
            return []
        return await _resolved(self._sync_groups.list_groups())

    async def groups_get(self, group: Any) -> Dict[str, Any]:
        """One group by id or name (SyncGroupError when unknown)."""
        self._require_groups()
        out = await _resolved(self._sync_groups.get_group(group))
        if out is None:
            raise SyncGroupError(f"unknown sync group {group!r}")
        return out

    async def groups_create(
        self, payload: Dict[str, Any], surface: str = "api"
    ) -> Dict[str, Any]:
        """Create a group (may have zero members).

        payload: {name (required), description?, enabled?}. Unknown keys are
        rejected (typo trap, same convention as accounts_add).
        """
        self._require_groups()
        if not isinstance(payload, dict) or not payload.get("name"):
            raise ValueError("group payload needs at least {'name': ...}")
        allowed = {"name", "description", "enabled"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown group payload keys: {sorted(unknown)}")
        description = payload.get("description", "")
        if not isinstance(description, str):
            raise ValueError("field 'description' must be a string")
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("field 'enabled' must be a boolean")
        out = await _resolved(self._sync_groups.create_group(
            payload["name"], description, enabled=enabled))
        self._audit_mgmt("system", "mgmt:group.created", surface,
                         extra=f"group={out.get('name')}")
        return out

    async def groups_update(
        self, group: Any, payload: Dict[str, Any], surface: str = "api"
    ) -> Dict[str, Any]:
        """Update name/description/enabled; a rename moves every member's
        runtime pacing scope with it (no stale group:<old> mapping)."""
        self._require_groups()
        if not isinstance(payload, dict) or not payload:
            raise ValueError(
                "group update payload needs at least one of "
                "{'name', 'description', 'enabled'}"
            )
        allowed = {"name", "description", "enabled"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown group payload keys: {sorted(unknown)}")
        if "description" in payload and not isinstance(payload["description"], str):
            raise ValueError("field 'description' must be a string")
        if "enabled" in payload:
            payload = dict(payload)
            payload["enabled"] = parse_bool(payload["enabled"], "enabled")
        out = await _resolved(self._sync_groups.update_group(group, **payload))
        # A rename rewrites every member's pointer in the repository AND the
        # limiter, but never Account.record — propagate the new name into
        # the memory records the 1 Hz poll serves (same F3 pattern as the
        # membership endpoints below; idempotent for non-rename updates).
        for member in await _resolved(
                self._sync_groups.get_group_members(out["name"])):
            self._refresh_account_group(member, out.get("name"))
        self._audit_mgmt("system", "mgmt:group.updated", surface,
                         extra=f"group={out.get('name')}")
        return out

    async def groups_delete(
        self, group: Any, detach_members: bool = False, surface: str = "api"
    ) -> Dict[str, Any]:
        """Delete a group (accounts are NEVER deleted). Refuses while
        members remain unless detach_members is explicitly True."""
        self._require_groups()
        detach_members = parse_bool(detach_members, "detach_members")
        out = await _resolved(
            self._sync_groups.delete_group(group, detach_members=detach_members)
        )
        # Detached members' Account.records still name the deleted group —
        # clear them (the repo + limiter are already clean) so the account
        # cards / group picker show Independent, not a dangling group name.
        for member in out.get("detached") or []:
            self._refresh_account_group(member, None)
        self._audit_mgmt(
            "system", "mgmt:group.deleted", surface,
            extra=f"group={out.get('deleted')} detached={len(out.get('detached') or [])}",
        )
        return out

    async def groups_add_account(
        self, group: Any, account: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Put an account in a group (replaces any previous membership)."""
        self._require_groups()
        out = await _resolved(
            self._sync_groups.add_account_to_group(group, account))
        self._refresh_account_group(account, out.get("group"))
        self._audit_mgmt(account, "mgmt:group.member_added", surface,
                         extra=f"group={out.get('group')}")
        return out

    async def groups_remove_account(
        self, group: Any, account: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Remove an account from one group (back to per-account pacing)."""
        self._require_groups()
        out = await _resolved(
            self._sync_groups.remove_account_from_group(group, account))
        if out.get("existed"):
            self._refresh_account_group(account, None)
        self._audit_mgmt(account, "mgmt:group.member_removed", surface,
                         extra=f"group={out.get('group')}")
        return out

    async def groups_set_account(
        self, account: str, group: Any, surface: str = "api"
    ) -> Dict[str, Any]:
        """Set/replace an account's group (None -> G and G1 -> G2)."""
        self._require_groups()
        out = await _resolved(self._sync_groups.set_account_group(account, group))
        self._refresh_account_group(account, out.get("group"))
        self._audit_mgmt(account, "mgmt:group.member_set", surface,
                         extra=f"group={out.get('group')}")
        return out

    async def groups_clear_account(
        self, account: str, surface: str = "api"
    ) -> Dict[str, Any]:
        """Remove the account from any group (group -> None)."""
        self._require_groups()
        out = await _resolved(self._sync_groups.clear_account_group(account))
        self._refresh_account_group(account, None)
        self._audit_mgmt(account, "mgmt:group.member_cleared", surface)
        return out

    async def groups_snapshot(self, group: Any) -> Dict[str, Any]:
        """Group snapshot: identity/metadata + members + runtime pacing
        info. Member rows are enriched with the account manager's live
        status (derived — no duplicate runtime registry)."""
        self._require_groups()
        snapshot = await _resolved(self._sync_groups.snapshot(group))
        members = snapshot.pop("members", [])
        by_name = {
            row.get("name"): row
            for row in await self._accounts.accounts()
            if isinstance(row, dict) and row.get("name")
        }
        snapshot["members"] = [
            {
                "name": name,
                "status": (by_name.get(name) or {}).get("status"),
                "enabled": (by_name.get(name) or {}).get("enabled"),
            }
            for name in members
        ]
        return snapshot

    def _require_groups(self) -> None:
        """Honest failure on stacks built without the group manager."""
        if self._sync_groups is None:
            raise RuntimeError(
                "sync group manager is not configured on this api")

    def _refresh_account_group(self, account: str, group: Optional[str]) -> None:
        """Propagate a membership change into the account manager's memory
        record (FINAL-PHASE F3 pattern, same as _refresh_account_profiles):
        the 1 Hz dashboard poll reads record.sync_group, and the sync-group
        manager owns the persisted value but never touches Account.record.
        """
        try:
            account_obj = getattr(self._accounts, "_accounts", {}).get(account)
            if account_obj is not None:
                account_obj.record.sync_group = group
        except Exception:
            LOG.debug("group memory refresh failed for %r", account,
                      exc_info=True)

    # ------------------------------------------------------- UI extension
    async def ui_stats(self, account: str) -> List[Dict[str, Any]]:
        """Per-account Home-tab stat cards from every enabled plugin."""
        return await _resolved(self._plugins.ui_stats(account))

    async def ui_panels(self) -> List[Dict[str, Any]]:
        """Plugin-contributed sidebar panels (framework-level)."""
        return await _resolved(self._plugins.ui_panels())

    # ------------------------------------------- dashboard UI extension (D)
    async def dashboard_ui_navigation(self) -> List[Dict[str, Any]]:
        """Plugin pages for the sidebar, VISIBILITY-CHECKED.

        Same fail-closed contract as page retrieval: a page whose check
        (or an ancestor's) denies is excluded from the sidebar, not just
        from direct reads. Checks run with account=None here (the nav
        exists before any account is selected); page checks that depend
        on an account should allow None to stay reachable, and gate the
        account-dependent parts with component checks instead.
        """
        registry = self._plugins.ui_registry
        nav: List[Dict[str, Any]] = []
        for plugin_name in registry.list_plugins():
            layout = registry.get_layout(plugin_name)
            if not layout:
                continue
            for page in (layout.get("children") or []):
                if page.get("type") != "page":
                    continue
                page_id = page.get("id", "")
                if not await registry.check_visibility(
                        plugin_name, page_id,
                        CheckContext(plugin=plugin_name,
                                     component_id=page_id,
                                     component_type="page")):
                    continue
                nav.append({
                    "plugin": plugin_name,
                    "page_id": page_id,
                    "title": page.get("title", plugin_name),
                    "icon": page.get("icon", ""),
                    "order": page.get("order", 0),
                    "account_scoped": page.get("account_scoped", False),
                })
        return sorted(nav, key=lambda n: (n.get("order", 0), n["plugin"]))

    async def dashboard_ui_page(self, plugin: str,
                                page_id: str,
                                account: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """One page's full serialized component tree; None when the
        plugin has no UI or the page id is unknown (route -> 404).

        Visibility is applied to the RENDERED definition: subtrees
        whose (own or ancestor) check denies are pruned from the tree
        before it crosses the wire — the same fail-closed verdicts the
        dispatch/data surfaces enforce server-side. ``account`` (the
        validated dashboard selection) reaches account-dependent checks.
        """
        registry = self._plugins.ui_registry
        if account is not None:
            account, _services = await self._ui_account_context(plugin, account)
        layout = registry.get_layout(plugin)
        if not layout:
            return None
        for page in (layout.get("children") or []):
            if page.get("id") == page_id:
                return await registry.visible_tree(plugin, page,
                                                   account=account)
        return None

    async def _ui_account_context(self, plugin: str,
                                  account: Optional[str]):
        """Validate a browser-supplied account for UI dispatch.

        Returns (account, services) — services is None only for
        account-less (plugin-wide) dispatch. The account is validated
        against the plugin manager (known, assigned, enabled, active);
        the services facade comes from the account's own wiring. Never
        guessed, never fabricated.
        """
        if account is None:
            return None, None
        from core.errors import PluginError
        try:
            self._plugins.active_instance_for(plugin, account)
        except PluginError as exc:
            raise UIError(
                f"invalid account context for plugin {plugin!r}: {exc}") from exc
        services = self._plugins.services_for(account, plugin)
        if services is None:
            raise UIError(
                f"account {account!r} has no active services wiring — "
                f"start the account before account-scoped UI actions")
        return account, services

    async def dashboard_ui_action(self, plugin: str, component_id: str,
                                  payload: Dict[str, Any],
                                  surface: str = "api",
                                  account: Optional[str] = None) -> Dict[str, Any]:
        """Dispatch a registered component action (visibility-checked).

        Raises UIError (a ValueError -> honest 400 at the interface) for
        unknown plugin/component, hidden components, missing actions and
        callback failures. ``surface`` names the calling interface in the
        audit vocabulary (api/dashboard) — reserved for mgmt audit wiring.
        """
        registry = self._plugins.ui_registry
        account, services = await self._ui_account_context(plugin, account)
        context = ActionContext(plugin=plugin, component_id=component_id,
                                account=account, services=services)
        result = await registry.dispatch_action(
            plugin, component_id, payload, context)
        return {"success": result.success, "message": result.message,
                "data": result.data, "field_errors": result.field_errors,
                "refresh": result.refresh, "modal": result.modal}

    async def dashboard_ui_data(self, plugin: str,
                                component_id: str,
                                account: Optional[str] = None) -> Any:
        """Fetch a data-provider result (visibility-checked, JSON-safe).

        Raises UIError when the plugin/component is unknown, hidden, or
        declares no provider (the interface distinguishes that from a
        payload fault: it maps to 404, not 400). ``account`` is the
        validated dashboard selection for account-scoped pages."""
        registry = self._plugins.ui_registry
        account, _services = await self._ui_account_context(plugin, account)
        # the outer context carries the validated account; the registry
        # pins a precise per-component CheckContext per provider call
        context = CheckContext(plugin=plugin, component_id=component_id,
                               component_type="", account=account,
                               services=_services)
        return await registry.get_data(plugin, component_id, context)

    # ------------------------------------------------------ admin access
    async def admin_overview(self) -> Dict[str, Any]:
        """The admin panel's full view: admin ids, mudae bot id, and every
        account's access row.

        Ids serialize as STRINGS: Discord snowflakes exceed JavaScript
        Number.MAX_SAFE_INTEGER, so JSON numbers get rounded by the
        browser's JSON.parse before any code runs."""
        access = self._require_access()
        rows = await access.all_account_rows()
        return {
            "admins": [str(i) for i in await access.admin_ids()],
            "mudae_bot_id": str(await access.mudae_bot_id())
            if await access.mudae_bot_id() is not None else None,
            "accounts": rows,
        }

    async def admin_set_ids(self, ids: List[int],
                            surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        out = {"admins": await access.set_admin_ids(ids)}
        self._audit_mgmt("system", "mgmt:admin.ids_set", surface,
                         extra=f"count={len(out['admins'])}")
        return out

    async def admin_add_id(self, user_id: int,
                           surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        out = {"admins": await access.add_admin(user_id)}
        self._audit_mgmt("system", "mgmt:admin.added", surface,
                         extra=f"user_id={user_id}")
        return out

    async def admin_remove_id(self, user_id: int,
                              surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        out = {"admins": await access.remove_admin(user_id)}
        self._audit_mgmt("system", "mgmt:admin.removed", surface,
                         extra=f"user_id={user_id}")
        return out

    async def admin_account_flag(self, account: str, is_admin: bool,
                                 surface: str = "api") -> Dict[str, Any]:
        self._require_access()
        out = await _resolved(self._access.set_admin_flag(account, is_admin))
        self._audit_mgmt(account, "mgmt:admin.flag_set", surface,
                         extra=f"is_admin={is_admin}")
        return out

    async def admin_perms(self, account: str,
                           surface: str = "api") -> Dict[str, Any]:
        """Read the per-account command-permission list.

        ``surface`` is reserved (mgmt audit vocabulary): reads are not
        audited - only the perms_set/add/remove mutations are.
        Ids serialize as strings (snowflake-safe transport).
        """
        access = self._require_access()
        return {"account": account,
                "allowed_user_ids": [str(i) for i in
                                     await access.allowed_user_ids(account)]}

    async def admin_perms_set(self, account: str, ids: List[int],
                              surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        out = {"account": account,
               "allowed_user_ids": await access.set_allowed_user_ids(
                   account, ids)}
        self._audit_mgmt(account, "mgmt:admin.perms_set", surface,
                         extra=f"count={len(out['allowed_user_ids'])}")
        return out

    async def admin_perms_add(self, account: str, user_id: int,
                              surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        out = {"account": account,
               "allowed_user_ids": await access.add_allowed(account, user_id)}
        self._audit_mgmt(account, "mgmt:admin.perm_added", surface,
                         extra=f"user_id={user_id}")
        return out

    async def admin_perms_remove(self, account: str, user_id: int,
                                 surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        out = {"account": account,
               "allowed_user_ids": await access.remove_allowed(
                   account, user_id)}
        self._audit_mgmt(account, "mgmt:admin.perm_removed", surface,
                         extra=f"user_id={user_id}")
        return out

    async def admin_prefix(self, account: str,
                            surface: str = "api") -> Dict[str, Any]:
        """Read the per-account self-command prefix configuration.

        ``surface`` is reserved (mgmt audit vocabulary): reads are not
        audited — only admin_prefix_set is.
        """
        access = self._require_access()
        return {"account": account,
                **await access.prefix_config(account)}

    async def admin_prefix_set(self, account: str, mode: str,
                               symbol: str = "",
                               surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        out = {"account": account,
               **await access.set_prefix_config(account, mode, symbol)}
        self._audit_mgmt(account, "mgmt:admin.prefix_set", surface,
                         extra=f"mode={mode} symbol={symbol!r}")
        return out

    async def admin_mudae_id(self,
                              surface: str = "api") -> Dict[str, Any]:
        """Read the framework-level Mudae bot id.

        ``surface`` is reserved (mgmt audit vocabulary): reads are not
        audited — only admin_mudae_id_set is.
        """
        access = self._require_access()
        return {"mudae_bot_id": await access.mudae_bot_id()}

    async def admin_mudae_id_set(self, user_id,
                                 surface: str = "api") -> Dict[str, Any]:
        access = self._require_access()
        if user_id is not None and not str(user_id).strip().lstrip("-").isdigit():
            raise ValueError("mudae bot id must be an integer (or null)")
        await access.set_mudae_bot_id(
            int(user_id) if user_id is not None else None)
        self._audit_mgmt("system", "mgmt:admin.mudae_id_set", surface,
                         extra=f"value={user_id}")
        return {"mudae_bot_id": await access.mudae_bot_id()}

    async def admin_forget_account(self, account: str,
                                   surface: str = "api") -> Dict[str, Any]:
        """Remove an account's access row (admin/perms/prefix) — cleanup
        for accounts deleted before the cascade existed, or manual prune."""
        access = self._require_access()
        await access.forget_account(account)
        self._audit_mgmt(account, "mgmt:admin.forgot", surface)
        return {"forgotten": account}

    def _require_access(self) -> Any:
        access = getattr(self, "_access", None)
        if access is None:
            raise RuntimeError(
                "admin access store not wired on this api")
        return access

    # ------------------------------------------------------------- rate limits
    async def ratelimit_policies(self) -> List[Dict[str, Any]]:
        """Every stored policy with its level/owner scope."""
        return self._limiter.policies()

    async def ratelimit_set(
        self,
        action: str,
        interval: float,
        jitter_min: float = 0.0,
        jitter_max: float = 0.0,
        level: str = "global",
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set a policy. Enforces the safety floor: an interval at or below the
        configured floor is REJECTED (see the Policies page for the value).

        Engine mutation first (memory takes effect immediately), then the
        awaited repository write — best-effort exactly like the old engine
        path: a failing DB write is logged, never a failed API call, and
        pacing safety never depends on the database.
        """
        action = (action or "").strip()
        # normalize EXACTLY like the engine (parse_scope) so the persisted
        # row / tombstone key is the one the engine stores — 'alpha / foo'
        # and 'alpha/foo' must not be two different DB rows
        if isinstance(owner, str):
            owner = owner.strip() or None
        level, owner = parse_scope(level, owner)
        policy = self._limiter.set_policy_values(
            action, interval, jitter_min, jitter_max, level, owner
        )
        await self._persist_policy(level, owner, policy)
        await self._write_removed_defaults(level, owner, action, removed=False)
        return {"action": action, "level": level, "owner": owner, "policy": str(policy)}

    async def ratelimit_clear(
        self, level: str, owner: Optional[str], action: str
    ) -> Dict[str, Any]:
        # normalize FIRST: the engine strips internally and the repo delete
        # must target the same key or the row silently survives a "clear"
        action = (action or "").strip()
        if isinstance(owner, str):
            owner = owner.strip() or None
        level, owner = parse_scope(level, owner)
        removed = self._limiter.clear_policy(level, owner, action)
        await self._forget_policy(level, owner, action)
        await self._write_removed_defaults(level, owner, action, removed=True)
        return {"cleared": removed, "level": level, "owner": owner, "action": action}

    async def ratelimit_effective(
        self, action: str, account: Optional[str] = None, plugin: Optional[str] = None
    ) -> Dict[str, Any]:
        """What actually paces this (account, plugin, action) right now."""
        return self._limiter.effective(action, account, plugin)

    async def ratelimit_stats(self) -> Dict[str, Any]:
        """Visualization data: policies, cooldowns, last fires, counters."""
        return self._limiter.stats()

    async def ratelimit_warnings(self) -> List[Dict[str, Any]]:
        """Safety review of the current configuration."""
        return self._limiter.warnings()

    async def ratelimit_set_floor(self, seconds: float) -> Dict[str, Any]:
        """Raise the pacing floor (never below MIN_ACTION_INTERVAL; persisted)."""
        floor = self._limiter.set_floor(seconds)
        await self._persist_floor(floor)
        return {"floor": floor}

    async def ratelimit_lane(self) -> Dict[str, Any]:
        """Sequential send lane state (REMOVED — returns stub for compat)."""
        return {"enabled": False, "prefixrate": 0, "removed": True,
                "reason": "fleet send lane removed: sync groups + per-account "
                           "pacing replace it without throttling single accounts"}

    async def ratelimit_set_lane(
        self, enabled: Any = None, prefixrate: Any = None
    ) -> Dict[str, Any]:
        """Lane configuration (REMOVED — accepts and returns stub for compat)."""
        return {"enabled": False, "prefixrate": 0, "removed": True,
                "note": "fleet send lane removed; setting ignored"}
    async def _persist_policy(self, level: str, owner: Optional[str],
                              policy: Any) -> None:
        """Best-effort awaited policy write (pacing never depends on the DB)."""
        repo = getattr(self._database, "policies", None) \
            if self._database is not None else None
        if repo is None:
            return
        try:
            await repo.set(level, owner, policy.action, policy.interval,
                           policy.jitter_min, policy.jitter_max)
        except Exception as exc:
            LOG.warning("could not persist rate policy %r at %s/%s: %s "
                        "(policy is still active in memory)",
                        policy.action, level, owner or "-", exc)

    async def _forget_policy(self, level: str, owner: Optional[str],
                             action: str) -> None:
        """Best-effort awaited policy delete."""
        repo = getattr(self._database, "policies", None) \
            if self._database is not None else None
        if repo is None:
            return
        try:
            await repo.delete(level, owner, action)
        except Exception as exc:
            LOG.warning("could not delete rate policy %r at %s/%s: %s",
                        action, level, owner or "-", exc)

    async def _persist_floor(self, floor: float) -> None:
        """Persist the raised floor (kv): a restart must not silently revert
        a post-incident safety measure to the configured default floor."""
        kv = getattr(self._database, "kv", None) \
            if self._database is not None else None
        if kv is None:
            return
        try:
            await kv.set(KV_FLOOR_KEY, float(floor))
        except Exception as exc:
            LOG.warning("could not persist rate-limit floor: %s", exc)

    async def _write_removed_defaults(
        self, level: str, owner: Optional[str], action: str, *, removed: bool
    ) -> None:
        """Tombstone bookkeeping: a CLEARED first-boot default must never be
        re-seeded from the config file on the next boot; an explicit SET
        clears the tombstone again.

        The read-modify-write is serialized by ``_tombstone_lock``: two
        concurrent clears raced the kv get/set and each rewrote the list
        without the other's entry — lost tombstones resurrected cleared
        defaults on boot.
        """
        kv = getattr(self._database, "kv", None) \
            if self._database is not None else None
        if kv is None:
            return
        key = f"{level}|{owner or ''}|{action}"
        async with self._tombstone_lock:
            try:
                raw = await kv.get(KV_REMOVED_KEY) or []
                entries = list(raw) if isinstance(raw, list) else []
                if removed and key not in entries:
                    entries.append(key)
                if not removed and key in entries:
                    entries.remove(key)
                await kv.set(KV_REMOVED_KEY, entries)
                sync = getattr(self._limiter, "set_removed_defaults", None)
                if callable(sync):
                    sync(entries)
            except Exception as exc:
                LOG.warning("could not persist cleared-policy tombstone: %s", exc)

    # ------------------------------------------------------------------ tasks
    async def tasks_list(
        self, status: Optional[str] = None, account: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Queued/running tasks (and recent ones) with optional filters."""
        return self._scheduler.tasks(status=status, account=account)

    async def tasks_cancel(self, task_id: int) -> Dict[str, Any]:
        return {"cancelled": self._scheduler.cancel(int(task_id)), "task_id": task_id}

    async def tasks_cancel_matching(
        self, account: Optional[str] = None, plugin: Optional[str] = None
    ) -> Dict[str, Any]:
        """Emergency-stop helper: cancel every matching task."""
        return {"cancelled": self._scheduler.cancel_matching(account=account, plugin=plugin)}

    async def tasks_history(self, limit: int = 50, account: Optional[str] = None) -> List[Dict[str, Any]]:
        # scheduler.history() is async now (aiosqlite repo reads)
        return await _resolved(self._scheduler.history(limit=limit, account=account))

    # ------------------------------------------------------------- simulation
    async def simulation_list(self) -> List[Dict[str, Any]]:
        if self._simulation is None:
            return []
        return self._simulation.list()

    async def simulation_run(self, name: str) -> Dict[str, Any]:
        """Run one scenario offline; returns the full timed report."""
        if self._simulation is None:
            raise RuntimeError("simulation runner not configured")
        return await self._simulation.run(name)

    # ------------------------------------------------------------------- logs
    async def logs_tail(
        self, n: int = 100, account: Optional[str] = None, severity: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Structured log tail (dashboard Logs page + CLI 'logs')."""
        return self._logs.tail(n, account=account, severity=severity)

    def logs_service(self) -> "LogService":
        """The live service (dashboard WS subscribes to this)."""
        return self._logs

    def audit_recorder(self) -> Optional["AuditRecorder"]:
        """The live audit recorder (dashboard WS subscription point)."""
        return self._audit

    async def audit_tail(
        self, n: int = 100, account: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if self._audit is None:
            return []
        # AuditRecorder.history() is async now (aiosqlite repo reads)
        return await _resolved(self._audit.history(limit=n, account=account))

    # ---------------------------------------------------------------- system
    @property
    def dashboard_token(self) -> Optional[str]:
        return self._credentials.dashboard_token if self._credentials else None

    async def emergency_stop_all(self, surface: str = "api") -> Dict[str, Any]:
        """GLOBAL emergency stop: cancel all tasks, stop all accounts, halt."""
        cancelled = self._scheduler.cancel_matching()
        await self._scheduler.stop(drain=False)
        stopped = await self._accounts.stop_all()
        self._logs.error("system", f"GLOBAL EMERGENCY STOP executed ({cancelled} task(s) cancelled)")
        self._audit_mgmt("system", "mgmt:estop", surface,
                         extra=f"cancelled={cancelled}")
        return {"cancelled_tasks": cancelled, "accounts": stopped, "halted": True}

    async def system_resume(self) -> Dict[str, Any]:
        """Resume after a GLOBAL emergency stop.

        Re-arms the scheduler (stop() left it terminal — every task had
        already reached a final state, so nothing stale carries over) and
        returns fresh status. Accounts stay stopped on purpose: restart the
        ones you want with accounts_start. The resume path exists because
        emergency_stop_all's contract says 'restart manually' — without it
        the halt was irreversible until process restart.
        """
        self._scheduler.start()
        self._logs.info("system", "resume after emergency stop: scheduler re-armed")
        return {"resumed": True, "status": await self.status()}

    async def shutdown(self) -> Dict[str, Any]:
        """Graceful shutdown of the scheduler + accounts (quit/dashboard).

        Does NOT close the database: the persistence queues (logs/audit)
        are owned by Framework.shutdown(), whose ordered teardown
        (stop_persistence BEFORE db.close) is the pinned contract —
        closing here would drain queued records into a dead DB and
        silently lose them. When no framework runs the teardown (direct
        ControlAPI use in tests), callers own the database.
        """
        await self._scheduler.stop(drain=True)
        await self._accounts.stop_all()
        return {"shutdown": True}

    # ------------------------------------------------------- mgmt audit trail
    def _audit_mgmt(self, account: str, action: str, surface: str = "",
                    extra: str = "") -> None:
        """One management audit row — value-free by construction (spec audit).

        The note names the calling surface ('cli' / 'dashboard' / 'api')
        plus counters; credential MATERIAL never reaches this trail, only
        account names. Never raises: auditing must not break management.
        """
        if self._audit is None:
            return
        note = f"surface={surface or 'api'}"
        if extra:
            note += f" {extra}"
        try:
            self._audit.record(
                account=str(account or "system"),
                direction="out",
                action=action,
                note=note,
            )
        except Exception:
            LOG.debug("management audit write failed", exc_info=True)


__all__ = ["ControlAPI", "parse_bool", "LANE_KV_KEY"]
