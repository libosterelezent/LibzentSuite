"""Adapters: the Discord boundary of the framework.

adapters.base      — DiscordClient protocol + DiscordMessage (the contract)
adapters.discord   — real client over a lazily imported user-account fork
adapters.simulator — full in-memory Discord world for offline runs/tests
"""
