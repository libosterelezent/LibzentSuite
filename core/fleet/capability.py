"""FleetTaskHandler — the plugin opt-in capability (PHASE 9, §5).

Plugins that want FLEET-MANAGED task flows expose ONE instance as
``fleet_handler`` on their PluginBase subclass. DEFAULT OFF: plugins
without it (ping and every simple plugin) are completely untouched.

The contract keeps ownership where the architecture puts it (§1/§5):
  * the plugin OWNS the flow shape — what one dispatch does;
  * the framework OWNS membership, fleet cooldowns, cross-account
    state, and the lifecycle — a handler NEVER runs private timer
    loops for scheduled work; the central scheduler drives it.

Handlers speak the generic vocabulary only (§10b): task definitions,
assignments, channel scopes. Game knowledge stays in the plugin (and
its parser pack).
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from .models import TaskDefinition, validate_channel_scope

__all__ = ["FleetTaskHandler", "handler_of", "definitions_of"]


class FleetTaskHandler:
    """One fleet-capable plugin's flow contract (§5).

    Lifecycle mapping:
      ``task_definitions``  declarations the framework registers when
                            the plugin activates on a group-managed
                            account (channel-scoped, D5)
      ``prepare``           run once before the first dispatch of an
                            assignment (load per-channel task config
                            from B3 data, warm caches); may return a
                            token passed to every execute
      ``execute``           ONE dispatch unit — must be short-lived
                            (submit work through the scheduler, await
                            its completion); REQUIRED
      ``suspend``           called when the assignment suspends (relay
                            handoff, member pause): stop producing new
                            dispatch units promptly
      ``recover``           called when a suspended assignment resumes
                            (possibly on a DIFFERENT account — handlers
                            must not assume account stickiness)
    """

    def task_definitions(self) -> Sequence[TaskDefinition]:
        """The plugin's declarations; validated eagerly (channel
        scope, field classes, metric hints)."""
        return ()

    async def prepare(self, ctx: Any, assignment: Any) -> Any:
        """Pre-dispatch setup; returns a token for execute()."""
        return None

    async def execute(self, ctx: Any, assignment: Any,
                      prepared: Any) -> None:
        """One dispatch unit. REQUIRED."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement execute()")

    async def suspend(self, ctx: Any, assignment: Any) -> None:
        """Stop producing dispatch units (drain-friendly)."""
        return None

    async def recover(self, ctx: Any, assignment: Any) -> None:
        """Resume producing dispatch units (may be a new account)."""
        return None


def handler_of(plugin: Any) -> Optional[FleetTaskHandler]:
    """The plugin's fleet capability, or None (the default). Only real
    FleetTaskHandler instances count — a stray attribute never mounts
    a plugin into the fleet."""
    handler = getattr(plugin, "fleet_handler", None)
    return handler if isinstance(handler, FleetTaskHandler) else None


def definitions_of(plugin: Any) -> list:
    """The plugin's validated task declarations ([] when not capable).
    Validation failures raise InvalidDefinition at MOUNT time — a bad
    declaration must fail loudly, not half-register."""
    handler = handler_of(plugin)
    if handler is None:
        return []
    declared = handler.task_definitions()
    definitions = []
    for definition in declared:
        if not isinstance(definition, TaskDefinition):
            raise ValueError(
                "task_definitions() must yield TaskDefinition objects, "
                f"got {type(definition).__name__}")
        # re-run the channel-scope validation explicitly so a subclass
        # that bypassed __post_init__ (e.g. object.__new__) is caught
        validate_channel_scope(definition.channel_scope)
        definitions.append(definition)
    return definitions
