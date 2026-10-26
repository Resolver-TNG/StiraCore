"""Discord entry point: python -m stiracore.integrations.discord_bot."""

from __future__ import annotations

import asyncio
import logging
import os

from .runtime import DIMENSIONS, ICONS, ChatSessions, chunks

logger = logging.getLogger(__name__)
COLORS = {"normal": 0x2ECC71, "warning": 0xF1C40F, "critical": 0xE74C3C, "coherence": 0xF1C40F}


def state_embed(discord, status):
    level = status["breaker"]
    embed = discord.Embed(
        title="Neural State",
        description=f"{ICONS.get(level, '⚪')} Breaker: {level.upper()}",
        color=COLORS.get(level, 0x95A5A6),
    )
    for name in DIMENSIONS:
        value = status["neural_state"][name]
        filled = round(max(0.0, min(1.0, value)) * 10)
        embed.add_field(name=name, value=f"{'█' * filled}{'░' * (10 - filled)} {value:.2f}")
    return embed


def create_client(discord, sessions: ChatSessions):
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    # One offloaded NXP turn at a time per channel; other channels and the gateway
    # heartbeat remain responsive while a model is running.
    locks: dict[str, asyncio.Lock] = {}

    @client.event
    async def on_message(message):
        if message.author.bot:
            return
        text = message.content.strip()
        if not text:
            return
        key = f"{getattr(message.guild, 'id', 'dm')}:{message.channel.id}"
        async with locks.setdefault(key, asyncio.Lock()):
            session = await asyncio.to_thread(sessions.get, key)
            if text == "!status":
                status = await asyncio.to_thread(session.status)
            else:
                result = await asyncio.to_thread(session.runner, text)
                for part in chunks(result.text, 2000):
                    await message.channel.send(part)
                status = result.after
            await message.channel.send(embed=state_embed(discord, status))
            # Presence is global: it reflects the most recently active conversation.
            level = status["breaker"]
            presence = {
                "normal": discord.Status.online,
                "warning": discord.Status.idle,
                "coherence": discord.Status.idle,
                "critical": discord.Status.dnd,
            }
            await client.change_presence(
                status=presence.get(level, discord.Status.idle),
                activity=discord.Game(name=f"{ICONS.get(level, '⚪')} {level.upper()}"),
            )

    return client


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("Discord skipped: set DISCORD_BOT_TOKEN.")
        return 0
    try:
        import discord
    except ImportError:
        logger.warning(
            'Discord skipped: install the optional extra with pip install "stiracore[discord]".'
        )
        return 0
    create_client(discord, ChatSessions("discord")).run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
