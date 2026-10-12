# Module `adapters.base`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `e30911d798609b426fb1dd4042e3d01f93102752`. Regenerate; do not hand-edit.

**Path:** `adapters/base.py`

```
Discord client abstraction (spec §9): ONE interface, two implementations.

.. code-block:: text

      DiscordClient (protocol)
          /                    Real Client          Simulator
    (adapters/discord)  (adapters/simulator)

The framework (account manager, scheduler sender hook, plugins) only ever
talks to this protocol — never to a concrete library — which is what makes
full offline simulation possible.

Implementations must provide lifecycle (``connect``/``disconnect``),
messaging (``send_message``, ``send_dm``, ``open_dm``, ``fetch_user``),
observability (``user_id``, ``latency_ms``, ``cached_snapshot`` — in-memory
view only, NEVER a network fetch), and callbacks (``on_message``,
``on_disconnect`` — assigned by the account manager before connect).

Server-provided rate limits: when Discord answers 429 the client raises
:class:`core.errors.ServerRateLimit` (``retry_after``, ``global_``). The scheduler reports it
to the rate limit manager and retries after backoff — the framework always
respects server-provided rate-limit information.
```

## Classes

| Symbol | Kind | Public | Purpose (first doc line) |
|---|---|---|---|
| `DiscordClient` | protocol | yes | The minimal surface every adapter must implement. |
| `ClientFactory` | protocol | yes | Builds a DiscordClient for one account (real or simulated). |

### `DiscordClient`

- `async def connect(self) -> None`
- `async def disconnect(self) -> None`
- `async def send_message(self, channel_id: int, content: str) -> DiscordMessage`
- `async def send_dm(self, user_id: int, content: str) -> DiscordMessage`
- `async def open_dm(self, user_id: int) -> int`
- `async def fetch_user(self, user_id: int) -> Any`
- `async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> Any`
  - React to a message (kakerareact capability). Raises the shared
- `async def click_button(self, channel_id: int, message_id: int, custom_id: str) -> Any`
  - Click a component by custom_id (kakeraloot capability). Resolves
- `def cached_snapshot(self) -> dict`
  - In-memory view: {"available": bool, "user": {"id","name"},

### `ClientFactory`

