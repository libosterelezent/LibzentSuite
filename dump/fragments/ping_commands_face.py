# RETIRED 2026-11-15 from plugins/ping/plugin.py (verbatim): the
# COMMANDS-era face. The ping BEHAVIOR (_ping) stays; the command
# returns as a COG once the host injects the paced facade.

    async def _cmd_ping(self, cmd: CommandContext, args: list[str]) -> str:
        """``ping <channel_id> <text...>`` handler (bound per account)."""
        if not cmd.services or len(args) < 2:
            return "usage: ping <channel_id> <text...>"
        try:
            channel_id = int(args[0])
        except ValueError:
            return "usage: ping <channel_id> <text...> (id must be a number)"
        # reuse the executor's context: same services facade as events
        return await self._ping(cmd, channel_id, " ".join(args[1:]))

