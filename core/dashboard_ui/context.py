"""Callback context types for the dashboard UI API (Stage C).

Three small frozen dataclasses define the contract between the
framework and plugin-declared callables (visibility checks, action
callbacks, data providers):

  * :class:`CheckContext`  — handed to visibility checks and data
    providers. Deliberately carries NO viewer identity: the current
    dashboard authenticates with ONE shared bearer token, not per-user
    identity, so a "who is looking" field would be a lie plugins could
    not trust. Checks key off plugin/component/account only. When the
    dashboard grows real per-user auth this type gains a viewer field
    and every existing check keeps working (additive change).
  * :class:`ActionContext` — handed to action callbacks; additionally
    carries the plugin's services facade so actions can send, persist
    state and read settings exactly like lifecycle hooks do.
  * :class:`ActionResult`  — the normalized, JSON-safe result every
    action callback returns (or is coerced into by the registry).

Pure stdlib; no imports from adapters/, interfaces/, database/ or the
dashboard itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class CheckContext:
    """Trusted context for visibility checks — fail-closed.

    Built by the registry (never by client input): ``plugin`` and
    ``component_id``/``component_type`` describe the component the
    check guards; ``account`` is the dashboard's currently selected
    account, when relevant.

    LIMITATION (documented deliberately): there is NO viewer identity
    here. The dashboard's auth model is a single shared token — anyone
    holding it sees everything — so per-viewer visibility cannot be
    enforced server-side today. Checks express *state* conditions (a
    feature flag, an account being linked, a flow being idle), not
    *who* is asking. Adding per-user auth later means extending this
    dataclass, which is backward-compatible for every existing check.
    """

    #: owning plugin name (the layout the component lives in)
    plugin: str
    #: the component being checked
    component_id: str
    #: serialization type of the component ("page", "button", ...)
    component_type: str
    #: selected account, when relevant (None = no account context)
    account: Optional[str] = None
    #: the account's services facade, when an account context exists
    services: Any = None


@dataclass(frozen=True)
class ActionContext:
    """Context passed to action callbacks.

    ``services`` is the plugin's per-(account, plugin) services facade
    (paced sends, cooldowns, state, settings) — the SAME surface
    lifecycle hooks use, so an action can do anything a hook can,
    through the same paced pipeline. It is None on stripped-down test
    stacks; callbacks must degrade gracefully.
    """

    #: owning plugin name
    plugin: str
    #: the component whose action fires
    component_id: str
    #: selected account, when relevant (None = no account context)
    account: Optional[str] = None
    #: the plugin's services facade (paced sends, settings, state)
    services: Any = None


@dataclass(frozen=True)
class ActionResult:
    """Normalized action result — JSON-safe.

    Action callbacks may return None, a plain string, a dict, or an
    ActionResult; the registry coerces everything into this shape
    before it crosses the wire:

      * ``success``    — the action's outcome flag;
      * ``message``    — one human-readable line for the dashboard toast;
      * ``data``       — arbitrary JSON-safe payload for the frontend;
      * ``field_errors`` — per-field validation errors (form renders
        these inline; maps field id -> error message);
      * ``refresh``    — component ids the frontend should re-fetch
        data for (the registry does not push; it only names targets);
      * ``modal``      — open the named Modal after the action (the
        context-modal pattern: the action records which row we are
        looking at server-side, refreshes the modal's components, and
        the frontend shows the dialog).
    """

    success: bool
    message: str = ""
    data: Any = None
    field_errors: Dict[str, str] = field(default_factory=dict)
    refresh: List[str] = field(default_factory=list)
    modal: Optional[str] = None


__all__ = ["CheckContext", "ActionContext", "ActionResult"]
