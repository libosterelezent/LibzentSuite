"""DashboardUIRegistry — the central registry for plugin dashboard UIs.

Stage C of the dashboard UI API. Manages the whole server-side lifetime
of a plugin's UI:

  * layout attachment  — validate the tree, freeze it, extract the
    action callbacks / visibility checks / data providers, store the
    JSON-safe snapshot;
  * action dispatch    — visibility-checked, fail-closed invocation of
    component callbacks with normalized :class:`ActionResult` output;
  * data providers     — visibility-checked, JSON-safe, bounded results;
  * visibility         — walks the component's FULL ancestor chain,
    evaluating every check (sync or async); any False or any raised
    error hides the component (fail-closed). ``visible_tree`` applies
    the same verdicts to the SERIALIZED page tree, so denied subtrees
    are pruned from what the frontend renders, not merely blocked at
    dispatch;
  * lifecycle cleanup  — detach unfreezes the tree and drops every
    registration; idempotent.

Plugin-scoped: two plugins can use the same component id safely — every
map is keyed ``plugin_name -> component_id -> ...``.

SYNC/ASYNC SPLIT (design decision): ``attach``/``detach``/``get_layout``/
``list_plugins`` are plain SYNCHRONOUS methods — they only validate,
freeze and index in-memory structures, no I/O — so the sync
``PluginBase.initialize_dashboard_ui`` hook can call them directly via
``attach_dashboard_ui`` with no event-loop gymnastics. The dispatch
surface (``dispatch_action``/``get_data``/``check_visibility``) is async
because it invokes PLUGIN callables, which may be coroutine functions.

Pure core: stdlib + the builder package only. Nothing here imports from
adapters/, interfaces/, database/ or the dashboard itself (Stage D's
routes call INTO this registry).
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import math
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from .builders import Layout, UIContainer
from .context import ActionContext, ActionResult, CheckContext
from .errors import TreeValidationError, UIError

LOG = logging.getLogger("slaughtermudae.dashboard_ui.registry")

#: per-list cap on data-provider results (bounded payloads crossing the wire)
MAX_DATA_ITEMS = 500
#: max nesting depth accepted in data-provider results
MAX_DATA_DEPTH = 8

_JSON_SCALARS = (str, int, float, bool, type(None))


class DashboardUIRegistry:
    """Central registry for plugin-attached dashboard UIs.

    Manages: layout attachment/validation, action callback registration,
    visibility check dispatch (incl. page-tree pruning), data provider
    registration, lifecycle cleanup. Plugin-scoped: two plugins can use
    the same component ID safely.
    """

    def __init__(self) -> None:
        # plugin_name -> component_id -> action callback
        self._actions: Dict[str, Dict[str, Callable]] = {}
        # plugin_name -> component_id -> visibility check callable
        self._checks: Dict[str, Dict[str, Callable]] = {}
        # plugin_name -> component_id -> data provider callable
        self._providers: Dict[str, Dict[str, Callable]] = {}
        # plugin_name -> serialized (JSON-safe) layout snapshot
        self._layouts: Dict[str, dict] = {}
        # plugin_name -> frozen builder tree (kept for re-serialization)
        self._builders: Dict[str, Layout] = {}
        # plugin_name -> component_id -> node (the dispatch index)
        self._nodes: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------ attachment
    def attach(self, plugin_name: str, layout: Layout) -> dict:
        """Validate, freeze and register ``layout`` for ``plugin_name``.

        Returns the serialized (JSON-safe) layout snapshot. Raises
        :class:`TreeValidationError` on an invalid tree and
        :class:`UIError` on a non-Layout argument or a layout that is
        already attached elsewhere. Re-attach replaces: the OLD layout
        survives untouched until the NEW one has validated (validate
        BEFORE detach), then the old registrations are dropped.

        Synchronous by design — pure in-memory work, callable directly
        from the sync ``initialize_dashboard_ui`` plugin hook.
        """
        if not isinstance(plugin_name, str) or not plugin_name:
            raise UIError(
                f"plugin_name must be a non-empty str, got {plugin_name!r}")
        if not isinstance(layout, Layout):
            raise UIError(
                f"attach() expects a Layout instance, got {type(layout).__name__}")
        if layout._frozen:
            raise UIError(
                "layout is already attached (frozen) — build a fresh tree "
                "instead of re-attaching a live one")
        errors = layout.validate()
        if errors:
            raise TreeValidationError(
                f"invalid dashboard UI layout for plugin {plugin_name!r}:\n"
                + "\n".join(f"  - {e}" for e in errors))
        # replacement: only now that the new tree has validated is it safe
        # to drop the old one (a failed validation keeps the old layout)
        if plugin_name in self._layouts:
            self.detach(plugin_name)
        layout._freeze()
        nodes: Dict[str, Any] = {}
        actions: Dict[str, Callable] = {}
        checks: Dict[str, Callable] = {}
        providers: Dict[str, Callable] = {}
        queue: deque = deque([layout])
        while queue:
            node = queue.popleft()
            cid = node.get_id()
            nodes[cid] = node
            check = node.get_check()
            if check is not None:
                checks[cid] = check
            action = getattr(node, "_action", None)
            if action is not None:
                actions[cid] = action
            submit = getattr(node, "_submit_action", None)  # Form
            if submit is not None:
                actions[cid] = submit
            source = getattr(node, "_data_source", None)  # ListView/Table
            if source is not None:
                providers[cid] = source
            if isinstance(node, UIContainer):
                queue.extend(node._children)
        self._nodes[plugin_name] = nodes
        self._actions[plugin_name] = actions
        self._checks[plugin_name] = checks
        self._providers[plugin_name] = providers
        self._builders[plugin_name] = layout
        snapshot = layout.to_dict()
        self._layouts[plugin_name] = snapshot
        LOG.info(
            "attached dashboard UI for plugin %r (%d component(s), "
            "%d action(s), %d check(s), %d provider(s))",
            plugin_name, len(nodes), len(actions), len(checks), len(providers))
        return copy.deepcopy(snapshot)

    def detach(self, plugin_name: str) -> bool:
        """Unfreeze and remove every registration for ``plugin_name``.

        Idempotent: detaching an unknown plugin is a no-op returning
        False. Returns True when something was removed.
        """
        removed = self._layouts.pop(plugin_name, None) is not None
        builder = self._builders.pop(plugin_name, None)
        if builder is not None:
            self._unfreeze(builder)
        self._nodes.pop(plugin_name, None)
        self._actions.pop(plugin_name, None)
        self._checks.pop(plugin_name, None)
        self._providers.pop(plugin_name, None)
        if removed:
            LOG.info("detached dashboard UI for plugin %r", plugin_name)
        return removed

    @staticmethod
    def _unfreeze(layout: Layout) -> None:
        """Release the frozen flag across the whole tree (detach path)."""
        queue: deque = deque([layout])
        while queue:
            node = queue.popleft()
            node._frozen = False
            if isinstance(node, UIContainer):
                queue.extend(node._children)

    def get_layout(self, plugin_name: str) -> Optional[dict]:
        """The stored JSON-safe snapshot (a copy — callers may mutate it);
        None when the plugin has no UI registered."""
        snapshot = self._layouts.get(plugin_name)
        return copy.deepcopy(snapshot) if snapshot is not None else None

    def list_plugins(self) -> List[str]:
        """Plugin names with an active UI (sorted for determinism)."""
        return sorted(self._layouts)

    # ------------------------------------------------------------- dispatch
    async def dispatch_action(
        self,
        plugin_name: str,
        component_id: str,
        payload: dict,
        context: ActionContext,
    ) -> ActionResult:
        """Invoke one component's action callback (visibility-checked).

        Raises :class:`UIError` when the plugin/component is unknown, the
        component is not visible, the component declares no action, the
        payload is not a dict, or the callback itself fails (non-UIError
        failures are wrapped so callers always catch UIError).
        """
        node = self._find_node(plugin_name, component_id)
        account = getattr(context, "account", None)
        if not await self.check_visibility(
                plugin_name, component_id,
                self._check_context(plugin_name, node, account)):
            raise UIError(
                f"component {component_id!r} of plugin {plugin_name!r} "
                f"is not available")
        callback = self._actions.get(plugin_name, {}).get(component_id)
        if callback is None:
            raise UIError(
                f"component {component_id!r} of plugin {plugin_name!r} "
                f"declares no action")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise UIError(
                f"action payload must be a dict, got {type(payload).__name__}")
        action_context = ActionContext(
            plugin=plugin_name,
            component_id=component_id,
            account=account,
            services=getattr(context, "services", None),
        )
        try:
            raw = await self._invoke(callback, action_context, payload)
        except asyncio.CancelledError:
            raise
        except UIError:
            raise
        except Exception as exc:
            LOG.warning(
                "action %r of plugin %r failed: %s: %s",
                component_id, plugin_name, type(exc).__name__, exc,
                exc_info=True)
            raise UIError(
                f"action {component_id!r} of plugin {plugin_name!r} "
                f"failed: {type(exc).__name__}: {exc}") from exc
        return self._normalize_result(raw, plugin_name, component_id)

    async def get_data(
        self,
        plugin_name: str,
        component_id: str,
        context: CheckContext,
    ) -> Any:
        """Fetch one component's data (visibility-checked, JSON-safe).

        The provider is called with a :class:`CheckContext` pinned to the
        component. Results must be JSON-safe (dict/list/str/int/float/
        bool/None); lists are capped at :data:`MAX_DATA_ITEMS` entries.
        Raises :class:`UIError` when the plugin/component is unknown, the
        component is not visible, no provider is registered, or the
        result is not JSON-safe.
        """
        node = self._find_node(plugin_name, component_id)
        account = getattr(context, "account", None)
        if not await self.check_visibility(
                plugin_name, component_id,
                self._check_context(plugin_name, node, account)):
            raise UIError(
                f"component {component_id!r} of plugin {plugin_name!r} "
                f"is not available")
        provider = self._providers.get(plugin_name, {}).get(component_id)
        if provider is None:
            raise UIError(
                f"component {component_id!r} of plugin {plugin_name!r} "
                f"declares no data provider")
        check_context = self._check_context(plugin_name, node, account)
        try:
            raw = await self._invoke(provider, check_context)
        except asyncio.CancelledError:
            raise
        except UIError:
            raise
        except Exception as exc:
            LOG.warning(
                "data provider %r of plugin %r failed: %s: %s",
                component_id, plugin_name, type(exc).__name__, exc,
                exc_info=True)
            raise UIError(
                f"data provider {component_id!r} of plugin {plugin_name!r} "
                f"failed: {type(exc).__name__}: {exc}") from exc
        return self._bound_json_safe(raw, plugin_name, component_id)

    async def check_visibility(
        self,
        plugin_name: str,
        component_id: str,
        context: CheckContext,
    ) -> bool:
        """Evaluate the component's FULL ancestor chain of checks.

        Walks from the component UP to the layout root; every ancestor
        with a check AND the component's own check must allow. Default is
        allow (no check = True); any check returning a falsey value or
        RAISING hides the component (fail-closed — the error is logged,
        never surfaced). Unknown plugin/component is False. Sync and
        async checks are both supported.
        """
        allowed = await self._chain_allowed(
            plugin_name,
            self._lookup_node(plugin_name, component_id),
            getattr(context, "account", None),
            cache={})
        return bool(allowed)

    def _lookup_node(self, plugin_name: str, component_id: str) -> Any:
        """Builder node for one id; None when unknown (denies closed)."""
        if not self._nodes.get(plugin_name):
            LOG.debug("visibility check for unknown plugin %r — closed",
                      plugin_name)
            return None
        node = self._nodes[plugin_name].get(component_id)
        if node is None:
            LOG.debug(
                "visibility check for unknown component %r of plugin %r — closed",
                component_id, plugin_name)
        return node

    async def _chain_allowed(
        self,
        plugin_name: str,
        node: Any,
        account: Optional[str],
        cache: Dict[int, bool],
    ) -> bool:
        """Ancestor-chain verdict for one builder node (root-first).

        Semantics IDENTICAL to :meth:`check_visibility` — every
        ancestor with a check and the node's own check must allow,
        fail-closed on falsey returns and raised errors. ``cache``
        memoizes per-node verdicts so a page-wide prune invokes each
        check at most once (``visible_tree`` shares one cache).
        """
        if node is None:
            return False
        chain: List[Any] = []
        seen: set[int] = set()
        walker: Any = node
        while walker is not None and id(walker) not in seen:
            seen.add(id(walker))
            chain.append(walker)
            walker = walker.get_parent()
        for candidate in reversed(chain):
            key = id(candidate)
            if key in cache:
                if not cache[key]:
                    return False
                continue
            check = candidate.get_check()
            if check is None:
                cache[key] = True
                continue
            check_context = self._check_context(
                plugin_name, candidate, account)
            try:
                allowed = await self._invoke(check, check_context)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning(
                    "visibility check for %r of plugin %r raised — "
                    "failing closed",
                    candidate.get_id(), plugin_name, exc_info=True)
                cache[key] = False
                return False
            if not allowed:
                cache[key] = False
                return False
            cache[key] = True
        return True

    async def visible_tree(
        self,
        plugin_name: str,
        node: dict,
        account: Optional[str] = None,
    ) -> Optional[dict]:
        """A copy of a serialized ``node`` with every visibility-denied
        subtree pruned (``None`` when the node itself is denied).

        Page serialization calls this so checks apply to what the
        frontend RENDERS, not only to server-side requests: a denied
        component (own check or any ancestor's) never crosses the
        wire. Each check runs at most once per call (one shared
        memoization cache across the whole tree walk); verdicts match
        :meth:`check_visibility` exactly. The input dict is never
        mutated. Unknown plugin → ``None`` (fail-closed).
        """
        if not self._nodes.get(plugin_name):
            return None
        cache: Dict[int, bool] = {}
        return await self._visible_copy(plugin_name, node, account, cache)

    async def _visible_copy(
        self,
        plugin_name: str,
        node: Any,
        account: Optional[str],
        cache: Dict[int, bool],
    ) -> Optional[dict]:
        if not isinstance(node, dict):
            return node
        builder = self._lookup_node(plugin_name, node.get("id"))
        if not await self._chain_allowed(plugin_name, builder, account, cache):
            return None
        if "children" in node:
            out = dict(node)
            kept: list = []
            for child in node["children"]:
                copy = await self._visible_copy(
                    plugin_name, child, account, cache)
                if copy is not None:
                    kept.append(copy)
            out["children"] = kept
            return out
        return dict(node)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _check_context(plugin_name: str, node: Any,
                       account: Optional[str]) -> CheckContext:
        """A CheckContext describing one node (the check's own component)."""
        return CheckContext(
            plugin=plugin_name,
            component_id=node.get_id(),
            component_type=getattr(node, "_type_name", type(node).__name__),
            account=account,
        )

    def _find_node(self, plugin_name: str, component_id: str) -> Any:
        """Node lookup with precise errors (dispatch paths raise, they do
        not fail closed — the caller distinguishes 404 from hidden)."""
        nodes = self._nodes.get(plugin_name)
        if not nodes:
            raise UIError(f"no dashboard UI registered for plugin {plugin_name!r}")
        node = nodes.get(component_id)
        if node is None:
            raise UIError(
                f"unknown component id {component_id!r} for plugin "
                f"{plugin_name!r}")
        return node

    @staticmethod
    async def _invoke(fn: Callable, *args) -> Any:
        """Call a plugin callable; await it when it returns an awaitable
        (sync and async checks/actions/providers are both first-class)."""
        result = fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return result

    @staticmethod
    def _normalize_result(raw: Any, plugin_name: str,
                          component_id: str) -> ActionResult:
        """Coerce a callback's return value into an ActionResult.

        Accepted shapes: ActionResult (as-is), None (plain success), str
        (success + message), dict (either a result-shaped dict or a data
        payload). Anything else becomes ``data`` on a successful result.
        """
        if isinstance(raw, ActionResult):
            return raw
        if raw is None:
            return ActionResult(success=True)
        if isinstance(raw, str):
            return ActionResult(success=True, message=raw)
        if isinstance(raw, dict):
            keys = {"success", "message", "data", "field_errors", "refresh"}
            if "success" in raw or "message" in raw:
                try:
                    return ActionResult(
                        success=bool(raw.get("success", True)),
                        message=str(raw.get("message", "")),
                        data=raw.get("data"),
                        field_errors=dict(raw.get("field_errors") or {}),
                        refresh=list(raw.get("refresh") or ()),
                    )
                except Exception:
                    pass  # malformed shape: fall through to data payload
            return ActionResult(success=True, data=raw)
        if isinstance(raw, bool):
            return ActionResult(success=raw)
        return ActionResult(success=True, data=raw)

    def _bound_json_safe(self, raw: Any, plugin_name: str,
                         component_id: str) -> Any:
        """Validate/cap a provider result so only JSON crosses the wire.

        Lists are truncated at :data:`MAX_DATA_ITEMS` (with a warning);
        anything non-JSON-safe raises :class:`UIError`.
        """

        def validate(value: Any, path: str, depth: int) -> Any:
            if isinstance(value, _JSON_SCALARS):
                if isinstance(value, float) and not math.isfinite(value):
                    raise UIError(
                        f"data provider {component_id!r} of plugin "
                        f"{plugin_name!r} returned a non-finite float "
                        f"at {path}")
                return value
            if isinstance(value, dict):
                if depth >= MAX_DATA_DEPTH:
                    raise UIError(
                        f"data provider {component_id!r} of plugin "
                        f"{plugin_name!r} exceeded the nesting depth "
                        f"limit ({MAX_DATA_DEPTH}) at {path}")
                out = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise UIError(
                            f"data provider {component_id!r} of plugin "
                            f"{plugin_name!r} returned a non-str dict key "
                            f"at {path}: {key!r}")
                    out[key] = validate(item, f"{path}.{key}", depth + 1)
                return out
            if isinstance(value, (list, tuple)):
                if depth >= MAX_DATA_DEPTH:
                    raise UIError(
                        f"data provider {component_id!r} of plugin "
                        f"{plugin_name!r} exceeded the nesting depth "
                        f"limit ({MAX_DATA_DEPTH}) at {path}")
                items = list(value)
                if len(items) > MAX_DATA_ITEMS:
                    LOG.warning(
                        "data provider %r of plugin %r returned %d items "
                        "— truncated to %d",
                        component_id, plugin_name, len(items), MAX_DATA_ITEMS)
                    items = items[:MAX_DATA_ITEMS]
                return [validate(item, f"{path}[{i}]", depth + 1)
                        for i, item in enumerate(items)]
            raise UIError(
                f"data provider {component_id!r} of plugin {plugin_name!r} "
                f"returned a non-JSON-safe value at {path}: "
                f"{type(value).__name__}")

        return validate(raw, "$", 0)


__all__ = ["DashboardUIRegistry", "MAX_DATA_ITEMS", "MAX_DATA_DEPTH"]
