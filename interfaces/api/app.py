"""The FastAPI core API — routers over the existing ControlAPI.

The interface pivot's phase 2: Pydantic request/response models, native
WebSocket for live events, every route a thin wrapper over the ONE
facade. Authorization rides the token seam (the API token from env;
per-account command authorization stays the AccessManager's domain —
the REST surface is operator-facing).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core.api import ControlAPI

__all__ = ["build_app"]

_api_key = APIKeyHeader(name="X-API-Token", auto_error=False)


class AccountCreate(BaseModel):
    name: str
    token_ref: str | None = None
    workspace: str | None = None
    enabled: bool = True
    plugins: list[str] = Field(default_factory=list)


class WorkspaceCreate(BaseModel):
    name: str
    description: str = ""


def build_app(api: ControlAPI, *, token: str | None = None) -> FastAPI:
    """Wire the routers over ONE ControlAPI instance."""
    app = FastAPI(title="LibzentSuite", version="1.0.0", docs_url="/api/docs")

    def _auth(key: str | None = Depends(_api_key)) -> None:
        if token is not None and key != token:
            raise HTTPException(status_code=401, detail="invalid token")

    # ------------------------------------------------------------ system
    @app.get("/api/status", dependencies=[Depends(_auth)])
    async def status() -> dict[str, Any]:
        return await api.status()

    @app.post("/api/estop", dependencies=[Depends(_auth)])
    async def estop() -> dict[str, Any]:
        return await api.emergency_stop_all(surface="api")

    @app.post("/api/resume", dependencies=[Depends(_auth)])
    async def resume() -> dict[str, Any]:
        return await api.system_resume()

    # ---------------------------------------------------------- accounts
    @app.get("/api/accounts", dependencies=[Depends(_auth)])
    async def accounts_list() -> list[dict[str, Any]]:
        return await api.accounts_list()

    @app.post("/api/accounts", dependencies=[Depends(_auth)], status_code=201)
    async def accounts_add(payload: AccountCreate) -> dict[str, Any]:
        return await api.accounts_add(payload.model_dump(), surface="api")

    @app.get("/api/accounts/{name}", dependencies=[Depends(_auth)])
    async def accounts_info(name: str) -> dict[str, Any]:
        return await api.accounts_info(name)

    @app.delete("/api/accounts/{name}", dependencies=[Depends(_auth)])
    async def accounts_remove(name: str) -> dict[str, Any]:
        return await api.accounts_remove(name, surface="api")

    @app.post("/api/accounts/{name}/start", dependencies=[Depends(_auth)])
    async def accounts_start(name: str) -> dict[str, Any]:
        return await api.accounts_start(name)

    @app.post("/api/accounts/{name}/stop", dependencies=[Depends(_auth)])
    async def accounts_stop(name: str) -> dict[str, Any]:
        return await api.accounts_stop(name)

    # -------------------------------------------------------- workspaces
    @app.get("/api/workspaces", dependencies=[Depends(_auth)])
    async def workspaces_list() -> list[dict[str, Any]]:
        return await api.workspaces_list()

    @app.post("/api/workspaces", dependencies=[Depends(_auth)], status_code=201)
    async def workspaces_create(payload: WorkspaceCreate) -> dict[str, Any]:
        return await api.workspaces_create(payload.model_dump(), surface="api")

    @app.get("/api/workspaces/{name}", dependencies=[Depends(_auth)])
    async def workspaces_get(name: str) -> dict[str, Any]:
        return await api.workspaces_get(name)

    @app.delete("/api/workspaces/{name}", dependencies=[Depends(_auth)])
    async def workspaces_delete(name: str, detach_members: bool = False) -> dict[str, Any]:
        return await api.workspaces_delete(name, detach_members, surface="api")

    @app.post("/api/workspaces/{name}/members/{account}", dependencies=[Depends(_auth)])
    async def workspaces_add_member(name: str, account: str) -> dict[str, Any]:
        return await api.workspaces_add_account(account, name, surface="api")

    @app.delete("/api/workspaces/{name}/members/{account}", dependencies=[Depends(_auth)])
    async def workspaces_remove_member(name: str, account: str) -> dict[str, Any]:
        return await api.workspaces_remove_account(account, surface="api")

    @app.post("/api/workspaces/{name}/pause", dependencies=[Depends(_auth)])
    async def workspaces_pause(name: str) -> dict[str, Any]:
        return await api.workspaces_pause(name, surface="api")

    @app.post("/api/workspaces/{name}/resume", dependencies=[Depends(_auth)])
    async def workspaces_resume(name: str) -> dict[str, Any]:
        return await api.workspaces_resume(name, surface="api")

    @app.get("/api/workspaces/{name}/tasks", dependencies=[Depends(_auth)])
    async def workspaces_tasks(name: str, status: str | None = None) -> list[dict[str, Any]]:
        return await api.workspaces_tasks(workspace=name, status=status)

    # ------------------------------------------------------------ plugins
    @app.get("/api/plugins", dependencies=[Depends(_auth)])
    async def plugins_list() -> list[dict[str, Any]]:
        return await api.plugins_manifests()

    @app.get("/api/accounts/{name}/plugins", dependencies=[Depends(_auth)])
    async def account_plugins(name: str) -> list[str]:
        return await api.accounts_plugins_list(name)

    @app.post("/api/accounts/{name}/plugins/{plugin}", dependencies=[Depends(_auth)])
    async def plugin_assign(name: str, plugin: str) -> dict[str, Any]:
        await api.accounts_plugin_assign(name, plugin, surface="api")
        return {"assigned": plugin}

    @app.delete("/api/accounts/{name}/plugins/{plugin}", dependencies=[Depends(_auth)])
    async def plugin_unassign(name: str, plugin: str) -> dict[str, Any]:
        await api.accounts_plugin_unassign(name, plugin, surface="api")
        return {"unassigned": plugin}

    # ------------------------------- account-owned profiles (P5 surface)
    @app.get("/api/accounts/{name}/plugins/{plugin}/profiles", dependencies=[Depends(_auth)])
    async def account_profiles(name: str, plugin: str) -> list[dict[str, Any]]:
        return await api.account_plugin_profiles(name, plugin)

    @app.post(
        "/api/accounts/{name}/plugins/{plugin}/profiles/{profile}",
        dependencies=[Depends(_auth)],
    )
    async def account_profile_create(
        name: str, plugin: str, profile: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = body or {}
        return await api.account_plugin_profile_create(
            name,
            plugin,
            profile,
            payload.get("description", ""),
            payload.get("settings"),
            schema_version=int(payload.get("schema_version", 1)),
            surface="api",
        )

    @app.get(
        "/api/accounts/{name}/plugins/{plugin}/profiles/{profile}",
        dependencies=[Depends(_auth)],
    )
    async def account_profile_get(name: str, plugin: str, profile: str) -> dict[str, Any] | None:
        return await api.account_plugin_profile_get(name, plugin, profile)

    @app.patch(
        "/api/accounts/{name}/plugins/{plugin}/profiles/{profile}",
        dependencies=[Depends(_auth)],
    )
    async def account_profile_update(
        name: str, plugin: str, profile: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = body or {}
        return await api.account_plugin_profile_update(
            name,
            plugin,
            profile,
            description=payload.get("description"),
            settings=payload.get("settings"),
            schema_version=payload.get("schema_version"),
            surface="api",
        )

    @app.delete(
        "/api/accounts/{name}/plugins/{plugin}/profiles/{profile}",
        dependencies=[Depends(_auth)],
    )
    async def account_profile_delete(name: str, plugin: str, profile: str) -> dict[str, Any]:
        return await api.account_plugin_profile_delete(name, plugin, profile, surface="api")

    # ------------------------------------------------------------- tasks
    @app.get("/api/tasks", dependencies=[Depends(_auth)])
    async def tasks_list(status: str | None = None) -> list[dict[str, Any]]:
        return await api.tasks_list(status=status)

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(_auth)])
    async def task_cancel(task_id: int) -> dict[str, Any]:
        return await api.tasks_cancel(task_id)

    # -------------------------------------------------------------- logs
    @app.get("/api/logs", dependencies=[Depends(_auth)])
    async def logs_tail(limit: int = 50, account: str | None = None) -> list[dict[str, Any]]:
        return await api.logs_tail(limit, account=account)

    @app.get("/api/audit", dependencies=[Depends(_auth)])
    async def audit_tail(limit: int = 50, account: str | None = None) -> list[dict[str, Any]]:
        return await api.audit_tail(limit, account=account)

    # -------------------------------------------------------- websocket
    @app.websocket("/api/ws")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        # the live-event bridge lands with the bus wiring (the W4
        # reconciler already publishes; the WS consumer follows)
        try:
            while True:
                # poll ControlAPI's status snapshot at a gentle cadence —
                # the live-event subscription lands with the bus wiring
                # (the W4 reconciler already publishes; the bridge follows)
                await asyncio.sleep(2.0)
                snapshot = await api.status()
                await ws.send_json({"kind": "status", "data": snapshot})
        except WebSocketDisconnect:
            return
        except Exception:
            with contextlib_suppress():
                await ws.close()

    def contextlib_suppress() -> Any:
        import contextlib

        return contextlib.suppress(Exception)

    return app
