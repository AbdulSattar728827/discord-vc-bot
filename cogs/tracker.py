"""
cogs/tracker.py — VC time tracker with correct cumulative totals
"""

import discord
from discord.ext import commands
import json, os, logging
from datetime import datetime, timezone

logger     = logging.getLogger(__name__)
STATS_FILE = "data/vc_stats.json"
LOGS_FILE  = "data/vc_logs.json"

def _load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def _save(path, data):
    os.makedirs("data", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

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
        self.bot      = bot
        self._sessions: dict[str, dict[str, datetime]] = {}
        self.stats: dict[str, dict[str, float]] = _load(STATS_FILE, {})
        self.logs:  dict[str, list]             = _load(LOGS_FILE,  {})

    def save(self):
        _save(STATS_FILE, self.stats)
        _save(LOGS_FILE,  self.logs)

    def get_total(self, guild_id: str, user_id: str) -> float:
        return self.stats.get(guild_id, {}).get(user_id, 0.0)

    def add_time(self, guild_id: str, user_id: str, seconds: float):
        self.stats.setdefault(guild_id, {})
        self.stats[guild_id][user_id] = self.stats[guild_id].get(user_id, 0.0) + seconds

    def get_leaderboard(self, guild_id: str) -> list[tuple[str, float]]:
        return sorted(self.stats.get(guild_id, {}).items(), key=lambda x: x[1], reverse=True)

    def get_logs(self, guild_id: str) -> list[dict]:
        return self.logs.get(guild_id, [])

    # ── Admin logs channel ─────────────────────────────────────────────────────

    async def _get_or_create_logs_channel(self, guild: discord.Guild):
        ch = discord.utils.get(guild.text_channels, name="vc-logs")
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
            ch = await guild.create_text_channel("vc-logs", overwrites=overwrites,
                                                  topic="🔒 Admin-only VC session logs")
            logger.info("[%s] Created #vc-logs", guild.id)
        except discord.Forbidden:
            return None
        return ch

    async def _post_log_embed(self, guild, member, channel_name,
                               join_time, leave_time, duration, rank):
        ch = await self._get_or_create_logs_channel(guild)
        if not ch:
            return
        e = discord.Embed(title="📋 VC Session Log", color=0x57F287, timestamp=leave_time)
        e.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        e.add_field(name="👤 Member",        value=member.mention,                          inline=True)
        e.add_field(name="🏅 Rank",          value=rank_suffix(rank),                       inline=True)
        e.add_field(name="🎙️ Channel",      value=f"#{channel_name}",                      inline=True)
        e.add_field(name="🕐 Joined",        value=f"<t:{int(join_time.timestamp())}:T>",   inline=True)
        e.add_field(name="🕐 Left",          value=f"<t:{int(leave_time.timestamp())}:T>",  inline=True)
        e.add_field(name="⏱️ Session",       value=fmt(duration),                           inline=True)
        e.add_field(name="📊 Total VC Time", value=fmt(self.get_total(str(guild.id), str(member.id))), inline=True)
        e.set_footer(text=f"User ID: {member.id}")
        try:
            await ch.send(embed=e)
        except discord.Forbidden:
            pass

    # ── Voice state ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                     before: discord.VoiceState,
                                     after:  discord.VoiceState):
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

        lb = self.bot.get_cog("Leaderboard")
        if lb:
            await lb.schedule_refresh(member.guild)

    async def _finalise(self, gid, uid, member, channel, leave_time):
        join_time = self._sessions.get(gid, {}).pop(uid, None)
        if join_time is None:
            return

        duration = (leave_time - join_time).total_seconds()
        if duration < 1:
            return

        # ── Add time FIRST, then record log (so total is correct) ─────────────
        self.add_time(gid, uid, duration)

        # Rank after adding time
        board = self.get_leaderboard(gid)
        rank  = next((i+1 for i,(u,_) in enumerate(board) if u == uid), len(board))

        # Log entry
        self.logs.setdefault(gid, []).append({
            "user_id":    uid,
            "username":   member.display_name,
            "channel":    channel.name if channel else "Unknown",
            "joined_at":  join_time.isoformat(),
            "left_at":    leave_time.isoformat(),
            "duration_s": round(duration, 2),
            "duration":   fmt(duration),
            "rank":       rank_suffix(rank),
            "total_time": fmt(self.get_total(gid, uid)),
        })
        self.save()

        logger.info("[%s] %s left #%s | session %s | total %s | rank %s",
                    gid, member.display_name,
                    channel.name if channel else "?",
                    fmt(duration), fmt(self.get_total(gid, uid)), rank_suffix(rank))

        await self._post_log_embed(
            member.guild, member,
            channel.name if channel else "Unknown",
            join_time, leave_time, duration, rank,
        )


async def setup(bot):
    await bot.add_cog(TrackerCog(bot))
    logger.info("TrackerCog loaded.")
