"""Real Discord adapter: the live-client half of the adapter boundary.

Exports:
  * RealDiscordClient — adapters.base.DiscordClient over a lazily imported
    user-account-capable Discord library (discord.py-self)
  * RealClientFactory — adapters.base.ClientFactory building live clients

The ``discord`` import happens INSIDE connect(), so importing this package is
always safe offline (simulator, tests, CLI dry-runs).
"""

from __future__ import annotations

from .client import RealClientFactory, RealDiscordClient

__all__ = ["RealClientFactory", "RealDiscordClient"]
