"""
cogs/tracker.py — VC time tracker with milestones
"""

import discord
from discord.ext import commands
import logging
from datetime import datetime, timezone
from database import db

logger = logging.getLogger(__name__)

# ── Milestone definitions ──────────────────────────────────────────────────────
MILESTONES = [
    (1,    "VC Newcomer",   "🥉", "Welcome to the grind!"),
    (5,    "VC Regular",    "🥈", "Getting serious!"),
    (10,   "VC Dedicated",  "🥇", "True dedication!"),
    (25,   "VC Elite",      "💎", "You're elite!"),
    (50,   "VC Legend",     "👑", "An absolute legend!"),
    (100,  "VC Monster",    "🔥", "Unstoppable monster!"),
    (200,  "VC Obsessed",   "💀", "Completely obsessed!"),
    (500,  "VC Immortal",   "⚡", "You are immortal!"),
    (1000, "VC GOD",        "🌟", "You are a VC GOD!"),
]
MILESTONE_HOURS = [m[0] for m in MILESTONES]

MILESTONES_CHANNEL = "🎉vc-milestones"

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


class TrackerCog(commands.Cog, name="Tracker"):

    def __init__(self, bot):
        self.bot = bot
        self._sessions: dict[str, dict[str, datetime]] = {}

    # ── On startup: record anyone already in VC ────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            gid = str(guild.id)
            for vc in guild.voice_channels:
                for member in vc.members:
                    if member.bot:
                        continue
                    uid = str(member.id)
                    self._sessions.setdefault(gid, {})[uid] = now
                    logger.info("[%s] Found %s already in #%s on startup",
                                gid, member.display_name, vc.name)

    # ── Leaderboard data accessor ──────────────────────────────────────────────

    async def get_leaderboard(self, guild_id: str) -> list[tuple[str, float]]:
        return await db.get_leaderboard(guild_id)

    # ── Milestones channel ─────────────────────────────────────────────────────

    async def _get_or_create_milestones_channel(self, guild: discord.Guild):
        ch = discord.utils.get(guild.text_channels, name=MILESTONES_CHANNEL)
        if ch:
            return ch
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, add_reactions=False
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True
                ),
            }
            ch = await guild.create_text_channel(
                MILESTONES_CHANNEL,
                overwrites=overwrites,
                topic="🎉 VC Milestone Achievements — celebrate your fellow members!",
            )
            logger.info("[%s] Created #%s", guild.id, MILESTONES_CHANNEL)
        except discord.Forbidden:
            return None
        return ch

    async def _check_and_post_milestones(self, guild, member, total_secs):
        total_hours = total_secs / 3600
        gid = str(guild.id)
        uid = str(member.id)

        # Get already achieved milestones
        achieved = await db.get_achieved_milestones(gid, uid)

        for hours, title, emoji, message in MILESTONES:
            if hours in achieved:
                continue  # Already posted this milestone
            if total_hours >= hours:
                # New milestone reached!
                await db.save_milestone(gid, uid, hours)

                ch = await self._get_or_create_milestones_channel(guild)
                if not ch:
                    continue

                e = discord.Embed(
                    title=f"{emoji} Milestone Unlocked!",
                    description=(
                        f"**{member.mention}** just reached **{hours} hours** in Voice Channels!\n\n"
                        f"🏷️ New Title: **{title}**\n"
                        f"💬 *{message}*"
                    ),
                    color=0xFFD700,
                    timestamp=datetime.now(timezone.utc),
                )
                e.set_thumbnail(url=member.display_avatar.url)
                e.set_footer(text=f"Total VC Time: {fmt(total_secs)}")

                try:
                    await ch.send(embed=e)
                    logger.info("[%s] Milestone posted for %s: %dh (%s)",
                                guild.id, member.display_name, hours, title)
                except Exception as ex:
                    logger.error("[%s] Failed to post milestone: %s", guild.id, ex)

    # ── Admin logs channel ─────────────────────────────────────────────────────

    async def _get_or_create_logs_channel(self, guild: discord.Guild):
        ch = discord.utils.get(guild.text_channels, name="🔒vc-logs")
        if ch:
            return ch
        try:
            admin_role = discord.utils.find(lambda r: r.permissions.administrator, guild.roles)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
            }
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
            ch = await guild.create_text_channel("🔒vc-logs", overwrites=overwrites,
                                                  topic="🔒 Admin-only VC session logs")
            logger.info("[%s] Created #🔒vc-logs", guild.id)
        except discord.Forbidden:
            return None
        return ch

    async def _post_log_embed(self, guild, member, channel_name,
                               join_time, leave_time, duration, rank):
        ch = await self._get_or_create_logs_channel(guild)
        if not ch:
            return
        total = await db.get_total(str(guild.id), str(member.id))
        e = discord.Embed(title="📋 VC Session Log", color=0x57F287, timestamp=leave_time)
        e.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        e.add_field(name="👤 Member",        value=member.mention,                         inline=True)
        e.add_field(name="🏅 Rank",          value=rank_suffix(rank),                      inline=True)
        e.add_field(name="🎙️ Channel",      value=f"#{channel_name}",                     inline=True)
        e.add_field(name="🕐 Joined",        value=f"<t:{int(join_time.timestamp())}:T>",  inline=True)
        e.add_field(name="🕐 Left",          value=f"<t:{int(leave_time.timestamp())}:T>", inline=True)
        e.add_field(name="⏱️ Session",       value=fmt(duration),                          inline=True)
        e.add_field(name="📊 Total VC Time", value=fmt(total),                             inline=True)
        e.set_footer(text=f"User ID: {member.id}")
        try:
            await ch.send(embed=e)
        except Exception as ex:
            logger.error("[%s] Failed to post log: %s", guild.id, ex)

    # ── Voice state ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                     before: discord.VoiceState,
                                     after:  discord.VoiceState):
        if member.bot:
            return

        gid = str(member.guild.id)
        uid = str(member.id)
        now = datetime.now(timezone.utc)

        joined = before.channel is None and after.channel is not None
        left   = before.channel is not None and after.channel is None
        moved  = (before.channel and after.channel and
                  before.channel.id != after.channel.id)

        if joined or moved:
            if moved:
                await self._finalise(gid, uid, member, before.channel, now)
            self._sessions.setdefault(gid, {})[uid] = now
            logger.info("[%s] %s joined #%s", gid, member.display_name,
                        after.channel.name if after.channel else "?")

        elif left:
            await self._finalise(gid, uid, member, before.channel, now)

        # Leaderboard refreshes every 30 minutes automatically

    async def _finalise(self, gid, uid, member, channel, leave_time):
        join_time = self._sessions.get(gid, {}).pop(uid, None)
        if join_time is None:
            logger.warning("[%s] %s left but had no join time recorded",
                           gid, member.display_name)
            return

        duration = (leave_time - join_time).total_seconds()
        if duration < 1:
            return

        # Save to database
        await db.add_time(gid, uid, duration)

        # Get updated total and rank
        board = await db.get_leaderboard(gid)
        rank  = next((i+1 for i,(u,_) in enumerate(board) if u == uid), len(board))
        total = await db.get_total(gid, uid)

        # Save log
        await db.add_log(
            gid, uid, member.display_name,
            channel.name if channel else "Unknown",
            join_time, leave_time,
            round(duration, 2), fmt(duration),
            rank_suffix(rank), fmt(total)
        )

        logger.info("[%s] %s left #%s | session %s | total %s | rank %s",
                    gid, member.display_name,
                    channel.name if channel else "?",
                    fmt(duration), fmt(total), rank_suffix(rank))

        # Check milestones
        await self._check_and_post_milestones(member.guild, member, total)

        await self._post_log_embed(
            member.guild, member,
            channel.name if channel else "Unknown",
            join_time, leave_time, duration, rank,
        )


async def setup(bot):
    await bot.add_cog(TrackerCog(bot))
    logger.info("TrackerCog loaded.")
