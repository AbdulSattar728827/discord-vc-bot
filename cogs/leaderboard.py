"""
cogs/leaderboard.py — leaderboard with streaks
"""

import discord
from discord.ext import commands, tasks
import logging, asyncio
from datetime import datetime, timezone
from database import db

logger = logging.getLogger(__name__)

LEADERBOARD_CHANNEL_NAME = "🏆vc-leaderboard"
REFRESH_INTERVAL_MINUTES = 30

MEDAL  = ["🥇", "🥈", "🥉"]
COLORS = [0xFFD700, 0xC0C0C0, 0xCD7F32]

def fmt(seconds: float) -> str:
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def rank_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13: return f"{n}th"
    return {1:f"{n}st",2:f"{n}nd",3:f"{n}rd"}.get(n%10, f"{n}th")

def streak_display(streak: int) -> str:
    if streak <= 0:  return ""
    if streak >= 30: return f" 🔥🔥🔥{streak}d"
    if streak >= 7:  return f" 🔥🔥{streak}d"
    if streak >= 1:  return f" 🔥{streak}d"
    return ""


class LeaderboardCog(commands.Cog, name="Leaderboard"):

    def __init__(self, bot):
        self.bot      = bot
        self._pending: dict[int, asyncio.Task] = {}
        self._locks:   dict[int, asyncio.Lock] = {}
        self._ready   = False

    def cog_unload(self):
        if self.auto_refresh.is_running():
            self.auto_refresh.cancel()

    def _lock(self, gid):
        if gid not in self._locks:
            self._locks[gid] = asyncio.Lock()
        return self._locks[gid]

    @tasks.loop(minutes=REFRESH_INTERVAL_MINUTES)
    async def auto_refresh(self):
        if not self._ready:
            return
        for guild in self.bot.guilds:
            await self._update(guild)

    @auto_refresh.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _get_or_create_channel(self, guild):
        ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL_NAME)
        if ch:
            return ch
        try:
            ow = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False),
                guild.me:           discord.PermissionOverwrite(send_messages=True, manage_messages=True, embed_links=True),
            }
            ch = await guild.create_text_channel(LEADERBOARD_CHANNEL_NAME, overwrites=ow,
                                                   topic="🎙️ Live Voice Channel Activity Leaderboard")
            logger.info("[%s] Created #%s", guild.id, LEADERBOARD_CHANNEL_NAME)
        except discord.Forbidden:
            return None
        return ch

    async def _build_embeds(self, guild) -> list[discord.Embed]:
        board = await db.get_leaderboard_with_streaks(str(guild.id))
        now   = datetime.now(timezone.utc)

        if not board:
            e = discord.Embed(
                title="🎙️ Voice Channel Leaderboard",
                description="No voice activity yet.\nJoin a Voice Channel to appear here!",
                color=0x5865F2, timestamp=now,
            )
            e.set_footer(text=f"Updates every {REFRESH_INTERVAL_MINUTES} min • {guild.name}")
            return [e]

        embeds = []

        # Top-3 cards
        for i, (uid, secs, streak) in enumerate(board[:3]):
            member = guild.get_member(int(uid))
            name   = member.display_name if member else f"Unknown ({uid})"
            avatar = member.display_avatar.url if member else (guild.icon.url if guild.icon else None)
            e = discord.Embed(
                title=f"{MEDAL[i]}  {rank_suffix(i+1)} Place",
                description=f"**{name}**",
                color=COLORS[i], timestamp=now,
            )
            e.add_field(name="⏱️ Total Time", value=fmt(secs),         inline=True)
            e.add_field(name="🏅 Rank",        value=rank_suffix(i+1),  inline=True)
            if streak > 0:
                e.add_field(name="🔥 Streak", value=f"{streak} day(s)", inline=True)
            if avatar:
                e.set_thumbnail(url=avatar)
            embeds.append(e)

        # Full list with streaks
        rows = []
        for i, (uid, secs, streak) in enumerate(board):
            member = guild.get_member(int(uid))
            name   = member.display_name if member else f"Unknown ({uid})"
            medal  = MEDAL[i] if i < 3 else "▫️"
            s_tag  = streak_display(streak)
            rows.append(f"`{rank_suffix(i+1):>5}`  {medal}  **{name}**{s_tag}  —  {fmt(secs)}")

        for ci in range(0, len(rows), 20):
            sl = rows[ci:ci+20]
            e  = discord.Embed(
                title="📊 Full Leaderboard" if ci == 0 else f"📊 Leaderboard (#{ci+1}–#{ci+len(sl)})",
                description="\n".join(sl),
                color=0x5865F2, timestamp=now,
            )
            e.set_footer(text=f"Total members: {len(board)} • Updates every {REFRESH_INTERVAL_MINUTES} min • {guild.name}")
            embeds.append(e)

        return embeds

    async def _update(self, guild):
        async with self._lock(guild.id):
            channel = await self._get_or_create_channel(guild)
            if not channel:
                return
            embeds = await self._build_embeds(guild)
            if not embeds:
                return
            try:
                await channel.purge(limit=100, check=lambda m: m.author.id == guild.me.id)
            except Exception:
                pass
            try:
                for embed in embeds:
                    await channel.send(embed=embed)
                logger.info("[%s] Leaderboard posted.", guild.id)
            except discord.Forbidden:
                logger.error("[%s] Cannot post in #%s.", guild.id, LEADERBOARD_CHANNEL_NAME)

    @discord.app_commands.command(
        name="refresh_leaderboard",
        description="Force-refresh the VC leaderboard (admin only).",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def refresh_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._update(interaction.guild)
        await interaction.followup.send("✅ Leaderboard refreshed!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(LeaderboardCog(bot))
    logger.info("LeaderboardCog loaded.")
