# RETIRED 2026-11-15 from core/credentials.py (verbatim) — the gate
# read only by the retired mention bridge; see dump/README.md

    @property
    def remote_control_enabled(self) -> bool:
        """Mention-prefix management from Discord (REMOTE_CONTROL=0 opts out).

        FAIL CLOSED: an absent key keeps the documented default (on);
        a present-but-empty or unrecognized value DISABLES the gate —
        the old parse failed OPEN (anything unrecognized meant enabled).
        """
        global _remote_control_warned
        raw = self._values.get("REMOTE_CONTROL")
        if raw is None:
            return True  # documented default: enabled
        value = raw.strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off", ""):
            return False
        if not _remote_control_warned:
            _remote_control_warned = True
            LOG.warning(
                "REMOTE_CONTROL=%r is not a recognized value (expected "
                "1/true/yes/on or 0/false/no/off) — remote control is "
                "DISABLED (fail closed)",
                raw[:40],
            )
        return False
