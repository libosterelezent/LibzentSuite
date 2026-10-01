"""Simulator adapter: the offline half of the Discord boundary.

Exports:
  * SimWorld         — in-memory Discord world (users, channels, DMs,
    scripted responder, one-shot 429s, channel deletion)
  * SimClient        — adapters.base.DiscordClient implementation over the
    world (one per simulated account)
  * Scenario/SCENARIOS — full-stack scenario registry (spec §16)
  * SimulationRunner — builds a complete ephemeral stack per scenario run

Everything here runs offline with SYNTHETIC ids only — no network, no tokens.
"""
from __future__ import annotations

from .client import SimClient
from .runner import SimulationRunner
from .scenarios import SCENARIOS, Scenario, ScenarioAPI
from .world import SimWorld

__all__ = [
    "SimWorld",
    "SimClient",
    "Scenario",
    "ScenarioAPI",
    "SCENARIOS",
    "SimulationRunner",
]
