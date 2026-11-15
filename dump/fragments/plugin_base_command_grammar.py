# RETIRED 2026-11-15 from core/plugin_base.py (verbatim): the hub-internal
# command grammar (CommandContext/ArgSpec/CommandSpec/parse_command_args/
# usage_line/CommandSettingsApi) — the cog surface replaced it; the audit
# confirmed zero production readers. See dump/README.md.

@dataclass(frozen=True)
class CommandContext:
    """Everything a self-command handler may touch at runtime.

    Built per invocation by the plugin manager. ``services`` is the SAME
    per-(account, plugin) facade plugin code uses, so a handler's sends
    flow through the paced pipeline with identical guarantees — there is
    deliberately NO raw client access here. ``channel_id``/``guild_id``
    describe where the command was invoked (DM commands have
    ``guild_id=None``); ``raw`` is the verbatim argument remainder after
    the command word.
    """

    #: account name the command was invoked on
    account: str
    #: plugin name the command belongs to ("selfcmd" for framework verbs)
    plugin: str
    #: per-(account, plugin) services facade (paced sends, credentials...);
    #: None ONLY for framework verbs, which run without a plugin facade
    services: PluginServices | None
    #: pre-scoped logger: libzent.<account>.<plugin>
    logger: logging.Logger
    #: channel the command was invoked in
    channel_id: int = 0
    #: guild of the invoking channel (None for DMs)
    guild_id: int | None = None
    #: validated settings surface (None on stripped stacks)
    settings_api: CommandSettingsApi | None = None
    #: verbatim argument remainder (whitespace-normalized by the parser)
    raw: str = ""
    #: the invoking Discord user id (None when unknown) — admin-gated
    #: handlers check it against the AccessManager
    invoker_id: int | None = None
    #: Reserved: the converged cog path parses typed kwargs natively
    #: (the library's converters); this field is vestigial.
    parsed: dict[str, Any] = field(default_factory=dict)



@dataclass(frozen=True)
class ArgSpec:
    """HUB-INTERNAL: one positional argument of a framework verb.

    No longer the plugin-facing grammar (plugins declare typed params
    on cog callbacks); this shape serves the framework verb
    definitions inside the hub until C3 migrates them to cogs."""

    name: str
    kind: Any = str
    required: bool = True
    default: Any = None
    greedy: bool = False
    choices: tuple[Any, ...] | None = None


def _convert_arg(spec: ArgSpec, token: str) -> Any:
    """HUB-INTERNAL: coerce one token to the ArgSpec's kind."""
    if spec.kind is str or (callable(spec.kind) is False and spec.kind is None):
        return token
    if spec.kind in (int, float):
        try:
            return spec.kind(token)
        except (TypeError, ValueError):
            raise ValueError(
                f"<{spec.name}> must be a valid {spec.kind.__name__}, got {token!r}"
            ) from None
    if callable(spec.kind):
        try:
            return spec.kind(token)
        except ValueError:
            raise ValueError(f"<{spec.name}>: {token!r} is not valid") from None
    raise ValueError(
        f"<{spec.name}> has an unsupported kind {spec.kind!r} (use str, int, float or a callable)"
    )


@dataclass(frozen=True)
class CommandSpec:
    """HUB-INTERNAL: one framework verb definition.

    The plugin-facing COMMANDS contract is RETIRED (plugins declare
    COG classes); this shape defines the hub's own verbs until C3
    migrates them to the FrameworkVerbsCog."""

    name: str
    help: str
    usage: str = ""
    category: str = ""
    args: tuple[ArgSpec, ...] = ()
    handler: Callable[..., Any] | None = None


def parse_command_args(spec: CommandSpec, argv: list[str]) -> dict[str, Any]:
    """HUB-INTERNAL: parse one framework verb invocation's positional
    arguments against its ArgSpec grammar. Raises ValueError with a
    usage-ready message on arity/type/choice failures."""
    out: dict[str, Any] = {}
    rest = list(argv)
    for arg in spec.args:
        if arg.greedy:
            out[arg.name] = list(rest)
            rest = []
            break
        if not rest:
            if arg.required:
                raise ValueError(f"missing <{arg.name}>")
            out[arg.name] = arg.default
            continue
        token = rest.pop(0)
        if arg.choices is not None and token not in arg.choices:
            raise ValueError(f"<{arg.name}> must be one of {list(arg.choices)}, got {token!r}")
        out[arg.name] = _convert_arg(arg, token)
    if rest:
        raise ValueError(f"unexpected extra argument(s): {' '.join(rest)}")
    return out


def usage_line(spec: CommandSpec) -> str:
    """HUB-INTERNAL: render a framework verb's usage hint."""
    parts = [f"<{a.name}>" if a.required else f"[{a.name}]" for a in spec.args]
    if any(a.greedy for a in spec.args):
        parts.append("...")
    return " ".join(parts)


__all__ = [
    "ArgSpec",
    "CommandContext",
    "CommandSettingsApi",
    "CommandSpec",
    "PluginBase",
    "PluginContext",
    "PluginServices",
    "parse_command_args",
    "usage_line",
]


# --- also retired: CommandSettingsApi (the command-only settings view) ---
class CommandSettingsApi(Protocol):
    """Settings surface a self-command handler may read/mutate."""

    def get(self, plugin: str, key: str, default: Any = None) -> Any: ...

    def set(self, plugin: str, key: str, value: Any) -> Any: ...

    def clear(self, plugin: str, key: str) -> Any: ...
