"""
cogs/stats.py — Personal VC stats slash command
Members can type /mystats to see their own VC statistics
"""

import discord
from discord.ext import commands
import logging
from datetime import datetime, timezone
from database import db

logger = logging.getLogger(__name__)

# Milestone titles matching tracker.py
MILESTONES = [
    (1,    "VC Newcomer",   "🥉"),
    (5,    "VC Regular",    "🥈"),
    (10,   "VC Dedicated",  "🥇"),
    (25,   "VC Elite",      "💎"),
    (50,   "VC Legend",     "👑"),
    (100,  "VC Monster",    "🔥"),
    (200,  "VC Obsessed",   "💀"),
    (500,  "VC Immortal",   "⚡"),
    (1000, "VC GOD",        "🌟"),
]

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

def get_current_title(total_hours: float) -> tuple[str, str]:
    """Get the highest milestone title achieved."""
    current = ("No Title Yet", "🎮")
    for hours, title, emoji in MILESTONES:
        if total_hours >= hours:
            current = (title, emoji)
    return current

def get_next_milestone(total_hours: float) -> tuple[int, str, str] | None:
    """Get the next milestone to achieve."""
    for hours, title, emoji in MILESTONES:
        if total_hours < hours:
            return (hours, title, emoji)
    return None


class StatsCog(commands.Cog, name="Stats"):

    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(
        name="mystats",
        description="See your personal VC statistics!",
    )
    async def mystats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        gid = str(interaction.guild.id)
        uid = str(interaction.user.id)

        # Get total time
        total_secs = await db.get_total(gid, uid)
        total_hours = total_secs / 3600

        # Get rank
        board = await db.get_leaderboard(gid)
        rank  = next((i+1 for i,(u,_) in enumerate(board) if u == uid), None)

        # Get session count
        session_count = await db.get_session_count(gid, uid)

        # Get current title
        title, title_emoji = get_current_title(total_hours)

        # Get next milestone
        next_ms = get_next_milestone(total_hours)

        # Build embed
        e = discord.Embed(
            title=f"📊 Your VC Stats",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url,
        )

        e.add_field(name="⏱️ Total VC Time",  value=fmt(total_secs),                          inline=True)
        e.add_field(name="🏅 Rank",            value=rank_suffix(rank) if rank else "Unranked", inline=True)
        e.add_field(name="🎮 Sessions",        value=str(session_count),                       inline=True)
        e.add_field(name=f"{title_emoji} Title", value=title,                                  inline=True)

        if total_secs > 0:
            avg = total_secs / session_count if session_count else 0
            e.add_field(name="📈 Avg Session",  value=fmt(avg),   inline=True)
        
        # Next milestone progress
        if next_ms:
            next_hours, next_title, next_emoji = next_ms
            next_secs    = next_hours * 3600
            remaining    = next_secs - total_secs
            progress_pct = min(int((total_secs / next_secs) * 100), 100)
            filled       = int(progress_pct / 10)
            bar          = "█" * filled + "░" * (10 - filled)

            e.add_field(
                name=f"🎯 Next: {next_emoji} {next_title} ({next_hours}h)",
                value=f"`{bar}` {progress_pct}%\n{fmt(remaining)} remaining",
                inline=False,
            )
        else:
            e.add_field(
                name="🌟 MAX RANK ACHIEVED",
                value="You are a **VC GOD** — the highest title!",
                inline=False,
            )

        e.set_footer(text=f"{interaction.guild.name} • Keep grinding!")

        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="whois",
        description="Check VC stats of any member!",
    )
    async def whois(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        gid = str(interaction.guild.id)
        uid = str(member.id)

        total_secs  = await db.get_total(gid, uid)
        total_hours = total_secs / 3600
        board       = await db.get_leaderboard(gid)
        rank        = next((i+1 for i,(u,_) in enumerate(board) if u == uid), None)
        session_count = await db.get_session_count(gid, uid)
        title, title_emoji = get_current_title(total_hours)
        next_ms = get_next_milestone(total_hours)

        e = discord.Embed(
            title=f"📊 VC Stats — {member.display_name}",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="⏱️ Total VC Time",    value=fmt(total_secs) if total_secs else "No time yet", inline=True)
        e.add_field(name="🏅 Rank",              value=rank_suffix(rank) if rank else "Unranked",        inline=True)
        e.add_field(name="🎮 Sessions",          value=str(session_count),                               inline=True)
        e.add_field(name=f"{title_emoji} Title", value=title,                                            inline=True)

        if next_ms:
            next_hours, next_title, next_emoji = next_ms
            next_secs    = next_hours * 3600
            remaining    = next_secs - total_secs
            progress_pct = min(int((total_secs / next_secs) * 100), 100)
            filled       = int(progress_pct / 10)
            bar          = "█" * filled + "░" * (10 - filled)
            e.add_field(
                name=f"🎯 Next: {next_emoji} {next_title} ({next_hours}h)",
                value=f"`{bar}` {progress_pct}%\n{fmt(remaining)} remaining",
                inline=False,
            )

        e.set_footer(text=f"{interaction.guild.name}")
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(StatsCog(bot))
    logger.info("StatsCog loaded.")
