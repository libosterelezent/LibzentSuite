"""Live testing suite package — opt-in, REAL Discord, never auto-discovered.

Run:  python3 tests_live/live_suite.py [--send]
Requires SLAUGHTER_LIVE_TOKEN + SLAUGHTER_LIVE_CHANNEL env vars.

Deliberately OUTSIDE tests/ so `unittest discover` never hits the network.
"""
