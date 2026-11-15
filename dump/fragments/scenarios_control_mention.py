# RETIRED 2026-11-15 from adapters/simulator/scenarios.py (verbatim,
# extracted from the last committed state): the control_mention
# scenario (script + checks + registry entry) and the ScenarioAPI.remote
# seam — retired with the RemoteControl mention bridge. The live
# registry keeps its other six scenarios. See dump/README.md.

    #: the mention-prefix RemoteControl bridge (control_mention scenario)
    remote: Any = None



# ------------------------------------------------------------------ registry
async def _script_control_mention(api: ScenarioAPI) -> None:
    """Owner drives @account commands; a stranger tries and gets nothing."""
    mention = f"<@{api.account_user_id}>"
    api.owner_says(api.channel_id, f"{mention} status", api.owner_id)
    await api.world.wait_for_quiet(api.channel_id, quiet=0.6, limit=15.0)
    # non-owner mentions the same account: must be ignored completely
    stranger = 4242
    api.world.add_user(stranger, "stranger")
    api.owner_says(api.channel_id, f"{mention} estop", stranger)
    await api.world.wait_for_quiet(api.channel_id, quiet=0.6, limit=15.0)


def _checks_control_mention(audit: list[dict[str, Any]], world: SimWorld) -> list[str]:
    failures: list[str] = []
    out = [
        e
        for e in audit
        if e.get("direction") == "out" and e.get("note", "").find("plugin=control") >= 0
    ]
    if not out:
        failures.append("owner mention produced no reply (expected a 'status' answer)")
    history = world.messages_of(SYNTHETIC_GUILD_CHANNEL)
    replies = [m for m in history if "uptime" in m or "tasks" in m]
    if not replies:
        failures.append("no status reply text landed in the guild channel")
    # the stranger's estop must NOT have produced a reply or halted anything
    estop_replies = [m for m in history if "EMERGENCY STOP" in m]
    if estop_replies:
        failures.append("non-owner mention was answered — owner gating broken")
    stranger_msgs = [
        m
        for m in world.journal
        if m.channel_id == SYNTHETIC_GUILD_CHANNEL and m.content.endswith("estop")
    ]
    if not stranger_msgs:
        failures.append("stranger mention never landed - owner-gating check is void")
        return failures
    stranger_ts = stranger_msgs[-1].ts
    later = [
        m
        for m in world.journal
        if m.channel_id == SYNTHETIC_GUILD_CHANNEL
        and m.author_id == SYNTHETIC_ALPHA_ID
        and m.ts > stranger_ts
    ]
    if later:
        failures.append(
            f"account replied after a non-owner mention "
            f"({len(later)} message(s)) - owner gating broken"

    "control_mention": Scenario(
        name="control_mention",
        description=(
            "The owner manages the fleet from Discord by MENTIONING the "
            "account as the command prefix (@account status, estop); replies "
            "arrive through the paced pipeline and non-owners are ignored."
        ),
        policies={"message": {"interval": 1.5, "jitter": [0.0, 0.05]}},
        world_setup=_base_world(SHORT_PAGES),
        script=_script_control_mention,
        checks=_checks_control_mention,
    ),
