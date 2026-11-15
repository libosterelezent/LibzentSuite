# RETIRED 2026-11-15 from core/api.py (verbatim): the dashboard-era UI
# surface (_ui_account_context + dashboard_ui_page). The registry class
# retired with the dashboard (2026-11-13); on every real stack these
# could only return None (audit drift #7). See dump/README.md.

    # ------------------------------------------------------- UI extension

    # ------------------------------------------- dashboard UI extension (D)
    async def _ui_account_context(self, plugin: str, account: str) -> tuple[str, Any]:
        """Validate the dashboard's account selection for a plugin UI tree.

        Delegates to the manager's ``active_instance_for`` — the documented
        dashboard validation seam: it raises PluginError naming the exact
        problem (unknown account, plugin not assigned, disabled, not
        active), the same fail-closed verdicts the dispatch surface
        enforces. Returns the account with its services facade (None when
        the account is not actively wired).
        """
        self._plugins.active_instance_for(plugin, account)
        return account, self._plugins.services_for(account, plugin)

    async def dashboard_ui_page(
        self, plugin: str, page_id: str, account: str | None = None
    ) -> dict[str, Any] | None:
        """One page's full serialized component tree; None when the
        plugin has no UI or the page id is unknown (route -> 404).

        Visibility is applied to the RENDERED definition: subtrees
        whose (own or ancestor) check denies are pruned from the tree
        before it crosses the wire — the same fail-closed verdicts the
        dispatch/data surfaces enforce server-side. ``account`` (the
        validated dashboard selection) reaches account-dependent checks.
        """
        registry = self._plugins.ui_registry
        if registry is None:
            return None  # no UI system wired on this stack (dashboard retired)
        if account is not None:
            account, _services = await self._ui_account_context(plugin, account)
        layout = registry.get_layout(plugin)
        if not layout:
            return None
        for page in layout.get("children") or []:
            if page.get("id") == page_id:
                tree = await registry.visible_tree(plugin, page, account=account)
                return tree if isinstance(tree, dict) else None
        return None

