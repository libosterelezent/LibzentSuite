# Module `core.bootstrap`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `9c9ed1475e17ee886158b10ea539a924e304ac87`. Regenerate; do not hand-edit.

**Path:** `core/bootstrap.py`

```
Composition root: builds the ENTIRE framework from config + credentials.

Construction order (the only place that knows how the managers interconnect):

  Credentials -> Config -> Database(await connect) -> LogService(+db sink)
    -> EventBus -> RateLimitManager(+config policies, await load_persisted)
    -> SyncGroupManager(await load) -> SettingsManager(await load)
    -> AuditRecorder
    -> TaskScheduler(late-bound sender) -> PluginManager(discover + await
    persist_metadata) -> ProfileManager(await load)
    -> AccountManager(await load; restores persisted accounts + delegates
       group membership restore to SyncGroupManager)
    -> CredentialService(+refresh)/CooldownService(await restore)/
       PluginStateService wired into the managers
    -> sender bound (accounts.send_hook) -> scheduler.start()
    -> SimulationRunner -> ControlAPI

``build`` is ASYNC (``framework = await build(...)``): the database layer
is aiosqlite, so the connection+migrations and every boot-time repository
read (settings, policies, profiles, accounts, cooldown restore, plugin
metadata) are awaited here before any manager serves traffic.

The sender cycle (scheduler needs accounts.send_hook; accounts needs the
scheduler for submits/cancels) is broken with a late-bound holder — the same
seam the simulation runner uses.

``build_simulation`` produces a fully in-memory stack over the simulator for
tests, `main.py --sim` and the dashboard's Simulation page.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `Framework` | class | yes | The fully wired stack (managers + api), built by :func:`build`. |

### `Framework`

- `async def shutdown(self) -> None`
  - Graceful: drain the scheduler, stop accounts, detach logging

## Functions

- `async def build(*, env_file: str='.env', config_dir: str='config', db_path: str='state/slaughter.db', plugin_dir: str='plugins', client_factory: Optional[Callable[[str, str], Any]]=None, simulation_runner: Any=None, credentials: Optional[Credentials]=None, database: Any=None, quiet: bool=False, credential_key_path: Optional[Path]=None) -> Framework` — side effects: `network`
  - Compose the live framework (the boot flow of ``main.py``).
