# RETIRED 2026-11-15 from tests/unit/test_ping_plugin.py (verbatim):
# the COMMANDS-era tests. The _ping BEHAVIOR tests stay; the command
# face returns as a COG (roadmap). See dump/README.md.


    async def test_command_declared_with_grammar(self) -> None:
        spec = PingPlugin.COMMANDS[0]
        self.assertEqual(spec.name, "ping")
        self.assertEqual(
            [a.name for a in spec.args], ["channel_id", "text"])
        self.assertTrue(spec.args[1].greedy)

    async def test_ping_sends_through_services(self) -> None:
        reply = await self.plugin._ping(self.ctx, 555, "hello there")
        self.assertEqual(self.services.sent, [(555, "hello there")])
        self.assertIn("sent to", reply)
        self.assertEqual(len(self.plugin._sent), 1)

    async def test_blocked_channel_aborts_clean_no_raise(self) -> None:
        self.services.blocked = True
        reply = await self.plugin._ping(self.ctx, 555, "hello")
        self.assertIn("unusable", reply)
        self.assertEqual(self.plugin._sent, [])

    async def test_cmd_requires_channel_and_text(self) -> None:
        cmd = _cmd_ctx(self.services)
        reply = await PingPlugin._cmd_ping(self.plugin, cmd, [])
        self.assertIn("usage", reply)

    async def test_cmd_rejects_non_numeric_channel(self) -> None:
        cmd = _cmd_ctx(self.services)
        reply = await PingPlugin._cmd_ping(self.plugin, cmd,
                                           ["abc", "hi"])
        self.assertIn("number", reply)

    async def test_cmd_happy_path(self) -> None:
        cmd = _cmd_ctx(self.services)
        reply = await PingPlugin._cmd_ping(
            self.plugin, cmd, ["555", "hello", "world"])
        self.assertIn("sent to", reply)
        self.assertEqual(self.services.sent,
                         [(555, "hello world")])

    async def test_command_name_avoids_reserved_verbs(self) -> None:
        """`ping` must not collide with the framework's reserved names
        (the retired `say` collision is why this plugin was renamed)."""
        from core.plugin_manager.commands import FRAMEWORK_COMMANDS
        reserved = {s.name for s in FRAMEWORK_COMMANDS}
        self.assertNotIn(PingPlugin.COMMANDS[0].name, reserved)

