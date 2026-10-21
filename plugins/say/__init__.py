"""say plugin: sends a provided message to a chosen channel.

The first production drop-in: ``<prefix>say <channel_id> <text...>``
through the paced pipeline, plus a dashboard form (channel + text +
Send) via the safe UI builder API.
"""
from .plugin import SayPlugin

__all__ = ["SayPlugin"]
