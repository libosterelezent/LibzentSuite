import discord
import asyncio
import random
from discord.ext import commands

class Autocopy(commands.Cog):
  def __init__(self, bot):
    self.bot = bot
    self.max_chunk_length = 2000
    self.mudae_id = 432610292342587392
    
  @commands.command()
  async def slcopy(self, ctx):
    try:
        await asyncio.sleep(1)
        mudae_bot = await self.bot.fetch_user(self.mudae_id)
        dm_channel = mudae_bot.dm_channel or await mudae_bot.create_dm()
    
        await asyncio.sleep(0.5)
        task = asyncio.create_task(self.read_dm(dm_channel))
        await ctx.send("$slud-s")
        messages = await task
        
        chunks = self.process_and_split_messages(messages)
        for chunk in chunks:
          await asyncio.sleep(10)
          await ctx.send(chunk)
    
      except Exception as e:
        print(f"Error in slcopy: {e}")

  @commands.command()
  async def close(self, ctx):
    await self.bot.close()
    
  async def read_dm(self, dm_channel):
      """Collects messages from DM until no new ones arrive for timeout period."""
      messages = []
      try:
        while True:
          msg = await self.bot.wait_for(
            "message",
            timeout=10,  # Adjust as needed
            check=lambda m: m.author.id == self.mudae_id 
            and m.channel == dm_channel
            )
          messages.append(msg.content)
      except asyncio.TimeoutError:
        print("No new messages received, stopping listener.")
      return messages
    
  def process_and_split_messages(self, messages):
      """Processes messages: removes unwanted lines, replaces line breaks with `$`, and splits into chunks."""
      full_text = ""
    
      for msg_obj in messages:
        lines = msg_obj.split("\n")
        if "Soulmates" in lines[0]:
          lines = lines[2:]
        
        full_text += "$".join(lines) + "$"
        
      chunks = []
      current_chunk = ""
    
      for part in full_text.split("$"):
        formatted_part = part + "$"
          
        if len(current_chunk) + len(formatted_part) + len("$soulcopy ") > self.max_chunk_length:
          chunks.append(f"$soulcopy {current_chunk.strip('$')}")
          current_chunk = ""
    
        current_chunk += formatted_part
    
      if current_chunk:
        chunks.append(f"$soulcopy {current_chunk.strip('$')}")
    
      return chunks
      
async def setup(bot):
    await bot.add_cog(Autocopy(bot))
