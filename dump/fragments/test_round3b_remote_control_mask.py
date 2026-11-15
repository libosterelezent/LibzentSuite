# RETIRED 2026-11-15 from tests/unit/test_round3_reaudit_b.py (verbatim):
# pinned the retired RemoteControl._execute masking contract.

    async def test_remote_control_masks_internal_runtime_errors(self):
        """FIXED (round-3 re-audit): bare RuntimeError left _DOMAIN_ERRORS —
        non-domain internals ESCAPE _execute so _handle masks them; domain
        errors come back rendered as strings."""
        from core.remote_control import RemoteControl

        rc = RemoteControl.__new__(RemoteControl)  # only _execute's contract
        rc._handlers = {}
        rc._api_provider = lambda: None

        async def boom(api, args):
            raise RuntimeError("boom")

        rc._handlers["status"] = boom
        rc._logger = LOG
        with self.assertRaises(RuntimeError):
            # non-domain internals must ESCAPE _execute so _handle masks
            # them; domain errors come back rendered as strings
            await rc._execute("status")


