# RETIRED 2026-11-15 from core/plugin_manager/manager.py (verbatim):
# the hub-era self-command dispatch path — no production caller since
# the cog surface carries commands; see dump/README.md.

    # ---------------------------------------------------------- self-commands
    def command_entries(self, account: str) -> list[tuple[CommandSpec, str]]:
        """``(CommandSpec, plugin name)`` pairs for the account's plugins.

        Collection rule (visibility = assigned ∧ enabled): every runtime
        the account holds that is enabled and instantiated contributes its
        class's ``COMMANDS``. Handlers declared as plain plugin-class
        methods are BOUND to the per-account instance here (one instance
        per (account, plugin) pair — handlers reach plugin internals like
        any lifecycle hook); an empty ``category`` defaults to the plugin's
        display name.

        Collision policy: deterministic manifest order (plugin name sort);
        the FIRST registration of a command name wins, later collisions
        are dropped with a warning naming both plugins.
        """
        runtimes = self._accounts.get(account, {})
        out: list[tuple[CommandSpec, str]] = []
        taken: dict[str, str] = {}
        for plugin_name in sorted(runtimes):  # manifest-sort determinism
            runtime = runtimes[plugin_name]
            if not runtime.enabled or runtime.instance is None:
                continue
            specs = getattr(type(runtime.instance), "COMMANDS", ()) or ()
            for declared in specs:
                if declared.name in taken:
                    self._logger.warning(
                        "self-command %r declared by plugin %r collides with "
                        "%r — first registration wins, the later one is dropped",
                        declared.name,
                        plugin_name,
                        taken[declared.name],
                    )
                    continue
                taken[declared.name] = plugin_name
                spec = replace(
                    declared,
                    handler=self._bind_handler(declared.handler, runtime.instance),
                    category=declared.category or plugin_name,
                )
                out.append((spec, plugin_name))
        return out

    def commands_for(self, account: str) -> list[CommandSpec]:
        """The account's plugin-declared CommandSpecs (bound handlers,
        defaulted categories, collisions resolved) — spec-only view of
        :meth:`command_entries`."""
        return [spec for spec, _plugin in self.command_entries(account)]

    @staticmethod
    def _bind_handler(handler: Any, instance: Any) -> Any:
        """Bind a plain plugin-class method to the per-account instance.

        A handler declared as a method (``async def _cmd(self, ctx, args)``
        referenced bare in the class-level ``COMMANDS`` tuple) arrives as
        an unbound function; descriptor-binding gives it ``self`` so it
        reaches the plugin's own state like a lifecycle hook. The class-
        body provenance check (walking the MRO) keeps FREE functions and
        lambdas — which already have the right signature — untouched.
        """
        if isinstance(handler, types.FunctionType):
            for klass in type(instance).__mro__:
                if handler in vars(klass).values():
                    return handler.__get__(instance, klass)
        return handler

    def _sync_commands(self, account: str) -> None:
        """Re-publish the account's self-command surface.

        C4: the cog host manages registration (add_cog/remove_cog);
        this method is a deliberate no-op kept for the callers that
        still reference it — retire when the last caller drops."""
        return

    # ------------------------------------------------------------ execution
    async def execute_self_command(
        self,
        account: str,
        name: str,
        args: list[str],
        channel_id: int,
        *,
        guild_id: int | None = None,
        invoker_id: int | None = None,
        raw: str = "",
    ) -> None:
        """Run one self-command and reply in-channel through the pipeline.

        Fully error-contained (a crashing handler must never kill the
        account or its Bot): the handler's string return value, its usage
        errors (``ValueError`` — including ``SettingsValidationError``)
        and the internal-error fallback are ALL sent back through the
        per-plugin services facade — the same paced pipeline plugin sends
        use — never a raw client send.

        The command gate mirrors the trigger-word path exactly: the
        invoking Discord user id must pass AccessManager.can_invoke —
        the manager's own ``_access_manager`` (bootstrap seam) first,
        the hub's bootstrap-registered AccessManager second, and the
        credentials view's owner ids (``services.credentials``) only as
        the legacy last resort for direct-construction stacks with no
        access manager anywhere. Non-owners and UNKNOWN invokers
        (``invoker_id=None``) are ignored with no reply — can_invoke
        fails closed.

        FRAMEWORK commands (plugin="selfcmd": help/pg/estop/…) run even
        when the account's plugins are deactivated — their handlers use
        the hub's api_provider, not ctx.services, and an undeliverable
        reply is logged instead of dropped (SAFETY: `estop` always runs).
        """
        entry = self._find_command(account, name)
        if entry is None:
            self._logger.debug("unknown self-command %r on %s", name, account)
            return
        spec, plugin = entry.spec, entry.plugin
        factory = self._services.get(account)
        services = factory(plugin) if factory is not None else None
        if services is None and plugin != FRAMEWORK_PLUGIN:
            # Plugin verbs need their paced pipeline. FRAMEWORK verbs do
            # not (their handlers talk to the ControlAPI through the
            # hub's api_provider): estop must run even on an account
            # whose plugins are deactivated — only the REPLY needs the
            # pipeline, and _send_reply logs it when there is none.
            self._logger.warning(
                "self-command %r on %s dropped: %s",
                name,
                account,
                "plugins not activated" if factory is None else "no services facade",
            )
            return
        # THE command gate: AccessManager.can_invoke (per-account
        # permission list + admin bypass, fail-closed — an unknown/None
        # invoker DENIES). The manager's own ``_access_manager``
        # (bootstrap seam) wins; when it is absent the hub's
        # bootstrap-registered AccessManager backs it up (covers
        # framework verbs like estop on a deactivated account, where
        # there are no facade credentials to read either). The
        # credentials view's owner ids are the LEGACY last resort for
        # direct-construction stacks (simulator/tests) with NO access
        # manager anywhere.
        access = getattr(self, "_access_manager", None)
        if access is None:
            access = getattr(self.command_hub, "access", None)
        allowed = False
        if access is not None:
            try:
                allowed = await access.can_invoke(account, invoker_id)
            except Exception:
                self._logger.exception(
                    "access check failed for %r on %s — failing closed", invoker_id, account
                )
                allowed = False
        else:
            owners = getattr(getattr(services, "credentials", None), "owner_user_ids", ()) or ()
            allowed = invoker_id in set(owners)
        if not allowed:
            self._logger.info(
                "self-command %r on %s ignored: invoker %s not allowed", name, account, invoker_id
            )
            return
        ctx = CommandContext(
            account=account,
            plugin=plugin,
            services=services,
            logger=logging.getLogger(f"libzent.{account}.{plugin}.command"),
            channel_id=int(channel_id or 0),
            guild_id=guild_id,
            settings_api=self._settings_api(account, plugin),
            raw=raw,
            parsed={},
            invoker_id=invoker_id,
        )
        try:
            if spec.handler is None:
                raise PluginError(f"command {spec.name!r} declares no handler")
            if spec.args:
                # declarative ArgSpecs: parse/convert/validate BEFORE the
                # handler — failures render the auto-usage line below
                ctx.parsed.update(parse_command_args(spec, list(args or [])))
            reply = await spec.handler(ctx, list(args or []))
        except asyncio.CancelledError:
            raise
        except (ValueError, SettingsValidationError) as exc:
            # usage error: render the usage line + the reason back
            usage = f"{spec.name} {spec.usage or usage_line(spec)}".strip()
            reply = f"usage: {usage}\n{exc}" if str(exc) else f"usage: {usage}"
        except Exception:
            ctx.logger.exception("self-command %s crashed on %s", name, account)
            reply = f"error: {name} failed internally (see logs)"
        if not reply:
            return
        await self._send_reply(services, channel_id, str(reply), ctx.logger)

    async def _send_reply(
        self, services: Any, channel_id: int, text: str, logger: logging.Logger
    ) -> None:
        """One paced reply (NORMAL priority — interactive, single message)."""
        if len(text) > MAX_REPLY_CHARS:
            text = text[: MAX_REPLY_CHARS - 20] + "\n… (truncated)"
        if services is None:
            # framework verb (estop!) on an account with no active plugin
            # pipeline: the reply CANNOT be delivered in-band — log it at
            # ERROR with the full text so the operator's answer survives
            # (the retired remote control's undeliverable-reply contract)
            logger.error(
                "self-command reply undeliverable (no services facade) — reply was: %s", text
            )
            return
        try:
            await services.send_message(int(channel_id), text, priority=Priority.NORMAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            # the reply is best-effort exactly like the remote-control
            # bridge: log it loudly so the operator's answer survives
            logger.error("self-command reply undeliverable — reply was: %s", text)

    def _find_command(self, account: str, name: str) -> RegisteredCommand | None:
        """Registry lookup from THIS manager's own view (never the hub's —
        the hub may be rebound by a newer manager in tests): framework
        verbs (reserved) first, then the account's plugin-declared specs."""
        from .commands import FRAMEWORK_COMMANDS, FRAMEWORK_PLUGIN

        for spec in FRAMEWORK_COMMANDS:
            if spec.name == name:
                return RegisteredCommand(spec=spec, plugin=FRAMEWORK_PLUGIN)
        for spec, plugin in self.command_entries(account):
            if spec.name == name:
                return RegisteredCommand(spec=spec, plugin=plugin)
        return None

    # ------------------------------------------------- command settings api
    def _settings_api(self, account: str, plugin: str) -> Any:
        """The validated settings surface for command handlers (cached
        per call site is unnecessary — construction is trivial)."""
        return _ManagerSettingsApi(self, account, plugin)

