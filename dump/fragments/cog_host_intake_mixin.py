# RETIRED 2026-11-15 from core/plugin_manager/cog_host.py (verbatim) +
# core/plugin_manager/intake_cog.py (whole module, git-mv'd to dump/):
# the C1 intake mixin shape — the adapter wires its own event
# handlers directly, so only tests ever constructed it (audit §1).

class FrameworkIntakeCog:
    """C1's framework-side event wiring shape ( lands with the adapter
    integration in C1's wiring commit): the ONE framework listener —
    audit + bus publication — with plugin listeners running alongside
    it, never behind it. Implemented as a mixin the adapter builds its
    intake cog from; the dispatch order guarantee comes from the
    library: every listener is an independently scheduled task."""
