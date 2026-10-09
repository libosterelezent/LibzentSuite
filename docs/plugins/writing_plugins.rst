Writing a plugin
================

Structure (see ``plugins/roll/`` for the real minimal example):

.. code-block:: python

   from core.event_bus import Event, MessageReceived
   from core.plugin_base import PluginBase, PluginContext
   from core.priority import Priority
   from core.settings_manager import Setting


   class MyPlugin(PluginBase):
       name = "myplugin"
       version = "1.0.0"
       description = "What it does."
       settings = (
           Setting("trigger", "str", "go", "Owner command word."),
       )

       async def on_load(self, ctx: PluginContext) -> None:
           ctx.logger.info("loaded (trigger=%r)", ctx.settings["trigger"])

       async def handle_event(self, ctx: PluginContext, event: Event) -> None:
           if not isinstance(event, MessageReceived):
               return
           if event.content.strip() != ctx.settings["trigger"]:
               return
           if event.author_id not in ctx.services.credentials.owner_user_ids:
               return
           ctx.services.validate_channel(event.channel_id)  # TargetGone aborts
           rich = event.message                     # rich DTO (may be None)
            if rich is not None and rich.buttons:
                await ctx.services.click_button(
                    event.channel_id, event.message_id, rich.buttons[0].custom_id)
           else:
               await ctx.services.send_message(
                   event.channel_id, "done", priority=Priority.LOW)

Every send/react/click flows through the centralized scheduler and rate
limiter — pacing, the 1.5s floor, serialization, audit and cancellation
apply automatically. Catch :class:`core.errors.FlowBlocked` around
multi-step flows and log ONE clean abort line (see soulcopy).

Drop the package under ``plugins/``; it is discovered automatically and
runs on any account it is assigned to (``accounts plugin-add <account>
<plugin>`` — direct assignment, applied immediately).
