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
                    assignments, persisted through db.account_plugins)
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

from .rate_limit_manager import (
    KV_FLOOR_KEY,
    KV_LANE_KEY,
    KV_REMOVED_KEY,
    MAX_PREFIXRATE,
)
from .errors import SyncGroupError

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
    from .sync_group_manager import SyncGroupManager
    from .task_scheduler import TaskScheduler

LOG = logging.getLogger("slaughtermudae.api")

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
        ("db"|"env"|None) and the number of active persistent cooldowns —
        both filled in ONLY when the manager snapshot lacks them, so a
        manager that already knows never gets second-guessed.
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
            if "cooldowns" not in account and self._cooldowns is not None:
                try:
                    # FINAL-PHASE (F3): memory-only count (the mirror is
                    # boot-restored + mutation-written; resync absorbs
                    # external edits) — no per-second DB scan per account
                    account["cooldowns"] = self._cooldowns.active_count_for(name)
                except Exception:
                    account["cooldowns"] = 0
        return accounts

    def _credential_state(self, name: str) -> Optional[str]:
        """'db' | 'env' | 'missing' | None (no service wired). Never a value."""
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
        """
        out = await _resolved(self._accounts.remove_account(name))
        self._audit_mgmt(name, "mgmt:account.removed", surface)
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
        """Set a policy. Enforces the safety floor: < 1.5s/action is REJECTED.

        Engine mutation first (memory takes effect immediately), then the
        awaited repository write — best-effort exactly like the old engine
        path: a failing DB write is logged, never a failed API call, and
        pacing safety never depends on the database.
        """
        action = (action or "").strip()
        if owner is not None:
            owner = owner.strip() or None
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
        if owner is not None:
            owner = owner.strip() or None
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
        """Raise the pacing floor (can never drop below 1.5s; persisted)."""
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

    async def _persist_lane(self, lane: Dict[str, Any]) -> None:
        """Persist the lane config under the ``ratelimit:lane`` kv key."""
        kv = getattr(self._database, "kv", None) \
            if self._database is not None else None
        if kv is None:
            return
        try:
            await kv.set(LANE_KV_KEY, {
                "enabled": bool(lane.get("enabled", False)),
                "prefixrate": float(lane.get("prefixrate", 0.0) or 0.0),
            })
        except Exception as exc:
            LOG.warning("could not persist send-lane configuration: %s", exc)

    async def _persist_floor(self, floor: float) -> None:
        """Persist the raised floor (kv): a restart must not silently revert
        a post-incident safety measure to the 1.5s default."""
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

    @staticmethod
    def _parse_lane_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        raise ValueError(f"field 'enabled' must be a boolean, got {value!r}")

    @staticmethod
    def _parse_lane_number(value: Any) -> float:
        """Validate prefixrate FULLY (type + engine range) BEFORE any lane
        mutation — the engine's own RatePolicyError used to land after
        ``enabled`` had already been applied (partial mutation)."""
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"field 'prefixrate' must be a number, got {value!r}") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError(f"field 'prefixrate' must be finite, got {value!r}")
        if number < 0:
            raise ValueError(f"field 'prefixrate' must be >= 0 (0 pauses the lane), got {value!r}")
        if number > MAX_PREFIXRATE:
            raise ValueError(
                f"field 'prefixrate' {number:g}s exceeds the "
                f"{MAX_PREFIXRATE:g}s cap")
        return number

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
