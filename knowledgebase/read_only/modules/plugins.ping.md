# Module `plugins.ping`

> GENERATED REFERENCE — READ ONLY. Produced by `scripts/gen_knowledgebase.py` from static AST inspection.
> Commit: `eac8ca4b565e98b3d2a77cbad76de12f6df3a4f1`. Regenerate; do not hand-edit.

**Path:** `plugins\ping\__init__.py`

```
ping plugin: sends a provided message to a chosen channel.

The first production drop-in: ``<prefix>ping <channel_id> <text...>``
through the paced pipeline, plus a dashboard form (channel + text +
Send) via the safe UI builder API. Named ``ping`` because the
framework reserves ``say`` as a retired RemoteControl verb.
```
