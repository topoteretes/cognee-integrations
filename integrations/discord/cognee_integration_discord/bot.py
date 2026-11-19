"""discord.py wiring: slash commands + message ingestion over MemoryService.

This is the only module that imports discord.py; it stays a thin cog so the
behavior (in ``service.py``) is tested without a live bot. Slash commands defer
first because cognify/recall exceed Discord's ~3s ack window.
"""

from __future__ import annotations

from .adapter import ChatMemoryAdapter, CogneeHttpAdapter
from .config import BotConfig
from .service import MemoryService


def build_cog(service: MemoryService):
    """Build the Discord cog bound to a MemoryService.

    discord.py is imported lazily so the package (and its tests) import without
    the optional dependency installed.
    """
    import discord
    from discord import app_commands
    from discord.ext import commands

    class CogneeMemory(commands.Cog):
        def __init__(self, memory: MemoryService) -> None:
            self.memory = memory

        @app_commands.command(
            name="cognee-enable", description="Enable cognee memory capture in this channel"
        )
        @app_commands.checks.has_permissions(manage_guild=True)
        async def enable(self, interaction: discord.Interaction) -> None:
            self.memory.enable_channel(interaction.guild_id, interaction.channel_id)
            await interaction.response.send_message(
                "Cognee memory is now enabled in this channel.", ephemeral=True
            )

        @app_commands.command(
            name="cognee-disable", description="Stop cognee memory capture in this channel"
        )
        @app_commands.checks.has_permissions(manage_guild=True)
        async def disable(self, interaction: discord.Interaction) -> None:
            self.memory.disable_channel(interaction.guild_id, interaction.channel_id)
            await interaction.response.send_message(
                "Cognee memory capture disabled in this channel.", ephemeral=True
            )

        @app_commands.command(
            name="cognee-ask", description="Ask a question against this server's memory"
        )
        async def ask(self, interaction: discord.Interaction, question: str) -> None:
            await interaction.response.defer(thinking=True)
            result = await self.memory.answer(
                interaction.guild_id, interaction.channel_id, question
            )
            await interaction.followup.send(result.text)

        @app_commands.command(
            name="cognee-forget", description="Forget everything cognee holds for this server"
        )
        @app_commands.checks.has_permissions(manage_guild=True)
        async def forget(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True, ephemeral=True)
            await self.memory.forget_guild(interaction.guild_id)
            await interaction.followup.send("Forgot this server's cognee memory.", ephemeral=True)

        @commands.Cog.listener()
        async def on_message(self, message: discord.Message) -> None:
            if message.author.bot or message.guild is None:
                return
            await self.memory.ingest_message(
                message.guild.id,
                message.channel.id,
                message.id,
                str(message.author),
                message.content,
            )

    return CogneeMemory(service)


def run(config: BotConfig | None = None, adapter: ChatMemoryAdapter | None = None) -> None:
    """Start the Discord bot (blocking). Requires discord.py + DISCORD_BOT_TOKEN."""
    import discord
    from discord.ext import commands

    config = config or BotConfig.from_env()
    if adapter is None:
        adapter = CogneeHttpAdapter(config.cognee_base_url, config.cognee_api_key or None)
    service = MemoryService(adapter)

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        await bot.add_cog(build_cog(service))
        await bot.tree.sync()

    bot.run(config.discord_token)
