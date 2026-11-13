"""ping plugin: sends a provided message to a chosen channel.

The first production drop-in: ``<prefix>ping <channel_id> <text...>``
through the paced pipeline, plus a dashboard form (channel + text +
Send) via the safe UI builder API. Named ``ping`` because the
framework reserves ``say`` as a retired RemoteControl verb.
"""

from .plugin import PingPlugin

__all__ = ["PingPlugin"]
