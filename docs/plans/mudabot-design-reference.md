# Mudabot reference — the reactive wait and the cog–client binding

A documented copy of the two structures from Mudabot that
[client-capability-adoption.md](client-capability-adoption.md) measures
against — and only those. Mudabot itself is a sibling checkout at
`D:\Workspace\Mudabot`; it is not part of this repository and is not a git
repository, so the excerpts are pinned by content hash under
[Provenance](#provenance). Nothing else from it is adopted.

## What is adopted, and what is not

| Structure | Mudabot's shape | What this repo takes |
|---|---|---|
| Reactive wait | `wait_for(event, timeout=..., check=<predicate>)` with predicates written over raw messages and embeds | The predicate expressiveness — through `expect`, a send-coupled window. `wait_for`'s subscribe-at-call-time behavior is the flaw Track B must not inherit. |
| Cog–client binding | Every cog is constructed with the client and reaches everything through `self.bot` | A cog receives exactly one client, its own account's (Track C, shipped). The client is never a cross-account service locator. |
| Everything else | Pacing, scheduling, profile storage, the game flows | Not adopted. |

## 1. The cog–client binding

Each cog is a `commands.Cog` whose constructor takes the client, and the
extension entry point hands it over:

```python
class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

# ... at the bottom of every cog module
async def setup(bot):
    await bot.add_cog(Settings(bot))
```

All five cogs follow this exactly (`def __init__(self, bot)` and
`async def setup(bot)`), and each resolves channels and identity through
the one handle:

```python
channel = self.bot.get_channel(channel_id)
user = self.bot.user
```

## 2. The reactive wait

### 2.1 Call shape and inventory

```python
message = await self.bot.wait_for('message', timeout=timeout, check=check)
```

Nineteen call sites across six cog modules:

| Module | `message` | `reaction_add` | Total |
|---|---|---|---|
| `cogs/checks.py` | 1 | 1 | 2 |
| `cogs/roll_profile.py` | 3 | 1 | 4 |
| `cogs/kl_profile.py` | 5 | 1 | 6 |
| `cogs/daily.py` | 1 | 1 | 2 |
| `cogs/pokeslots.py` | 1 | 0 | 1 |
| `cogs/snipe.py` | 3 | 1 | 4 |
| **Total** | **14** | **5** | **19** |

### 2.2 The two predicate shapes

Every `check` is a closure over the flow's bound context (a channel, the
target bot's id, sometimes the invoking user). There are only two shapes.

**A message predicate** — is this the channel I asked in, from the bot I
asked, saying one of the things this flow treats as an answer:

```
message.channel == <bound channel>
and message.author.id == <target bot id>
and ( <family predicate> or <family predicate> ... )
```

**A reaction predicate** — did the target bot react, with this emoji, on
the message I just sent:

```
reaction.message.id == <sent message>.id
and str(reaction.emoji) == <emoji>
and user.id == <target bot id>
```

The generic helpers show both in full, including the convention that a
timeout is an answer too (`False`, never an exception):

```python
class Checks:
    def __init__(self, bot):
        self.bot = bot

    async def response(self, profile, timeout: float = 4.0):
        try:
            def check(message: discord.Message) -> bool:
                return (
                    message.channel == profile.channel and
                    message.author.id == MUDAE_ID and
                    CardMessages.is_us_warning(message)
                )
            await self.bot.wait_for('message', timeout=timeout, check=check)
            return True
        except asyncio.TimeoutError:
            return False

    async def confirmation(self, msg, timeout: float = 2.0):
        try:
            def check(reaction, user) -> bool:
                return (
                    reaction.message.id == msg.id and
                    str(reaction.emoji) == '✅' and
                    user.id == MUDAE_ID
                )
            await self.bot.wait_for('reaction_add', timeout=timeout, check=check)
            return True
        except asyncio.TimeoutError:
            return False
```

### 2.3 Reading an answer: `flatten_text`

Mudae answers inside embeds, where `content` is empty. One helper folds a
message into a single string so a family predicate can match without
caring where the text sat; the no-embeds case short-circuits because it
runs for every waiter:

```python
def flatten_text(message: discord.Message) -> str:
    if not getattr(message, "embeds", None):
        return getattr(message, "content", "") or ""
    parts = [getattr(message, "content", "") or ""]
    for embed in message.embeds:
        parts.append(getattr(embed, "title", None) or "")
        parts.append(getattr(embed, "description", None) or "")
        for field in getattr(embed, "fields", None) or ():
            parts.append(getattr(field, "value", None) or "")
    return "\n".join(p for p in parts if p)
```

### 2.4 The family predicates

One namespace of static predicates per command family, all in
`cogs/checks.py` ("single source of truth for every cog" — three cogs
each kept a diverging copy before). Each predicate is a boolean over a
message, optionally bound to the invoking user so an answer meant for
somebody else is not adopted:

| Namespace | Predicates over a message |
|---|---|
| `CardMessages` | `is_roulette_pass(message, user=None)`, `is_us`, `is_us_warning`, `is_limit(embeds)` |
| `KakeralootResponses` | `is_confirm`, `is_pinWarn`, `out_of_scrap`, `is_succeeded`, `is_rolls` |
| `KakeralootDisplayMessage` | `is_pass(message, user=None)`, `is_no_usage` |
| `ScrapResponses` | `is_confirm(message, user=None)`, `scrap_no_server` |
| `MudaeStateResponses` | `is_rt_locked`, `is_rt_cooldown`, `is_mu_claim_state` |
| `BasicMessageCheck` | `check_daily_cooldown_warn`, `check_pokeslot_cooldown_warn`, `is_awaiting_answer` |

The parsing under them is written for hostile input: digit groups are
bounded (`\d{1,3}`) so a hostile message cannot cause catastrophic
backtracking, and `int()` on a capture goes through a helper that returns
`None` instead of raising into the caller.

### 2.5 The fields the predicates read

The surface a predicate touches — the fields an `expect` check must be
able to see on the frozen DTO:

| Object | Fields |
|---|---|
| message | `id`, `channel`, `author.id`, `content`, `embeds` |
| embed | `title`, `description`, `footer.text`, `fields[].value` |
| reaction | `message.id`, `str(emoji)` |
| user | `id`, `name` (the invoking account, when an answer is bound by name) |
| client (binding) | `user`, `get_channel(id)` |

## Provenance

Observed 2026-11-23 at `D:\Workspace\Mudabot`. SHA-256, first 16 hex
digits:

| File | Lines | SHA-256 |
|---|---|---|
| `main.py` | 261 | `999f098f0d83366e` |
| `cogs/checks.py` | 362 | `d9e87bb935899b0d` |
| `cogs/roll_profile.py` | 517 | `dcea5a6e1500c394` |
| `cogs/kl_profile.py` | 677 | `9b4688c5c8eea29a` |
| `cogs/daily.py` | 325 | `973b7cbae7e69aa9` |
| `cogs/pokeslots.py` | 300 | `28f50eac4710c655` |
| `cogs/snipe.py` | 604 | `c350845f84156a68` |

The excerpts are shortened (each helper's generic error branch is
omitted) and otherwise verbatim.
