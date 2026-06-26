"""
cogs/admin.py — Admin commands for managing VC time and streaks
All commands are admin only.
"""

import discord
from discord.ext import commands
import logging
from datetime import datetime, timezone
from database import db

logger = logging.getLogger(__name__)

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


class AdminCog(commands.Cog, name="Admin"):

    def __init__(self, bot):
        self.bot = bot

    # ── Time Management ────────────────────────────────────────────────────────

    @discord.app_commands.command(
        name="addtime",
        description="Add VC time to a member (admin only).",
    )
    @discord.app_commands.describe(
        member="The member to add time to",
        hours="Hours to add",
        minutes="Minutes to add",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def addtime(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        hours: int = 0,
        minutes: int = 0,
    ):
        await interaction.response.defer(ephemeral=True)

        if hours < 0 or minutes < 0:
            await interaction.followup.send("❌ Hours and minutes must be positive!", ephemeral=True)
            return
        if hours == 0 and minutes == 0:
            await interaction.followup.send("❌ Please specify hours or minutes to add!", ephemeral=True)
            return

        gid     = str(interaction.guild.id)
        uid     = str(member.id)
        seconds = (hours * 3600) + (minutes * 60)

        await db.add_time(gid, uid, seconds)
        new_total = await db.get_total(gid, uid)

        e = discord.Embed(title="✅ Time Added", color=0x57F287, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",     value=member.mention,    inline=True)
        e.add_field(name="➕ Added",       value=fmt(seconds),      inline=True)
        e.add_field(name="📊 New Total",  value=fmt(new_total),    inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")

        logger.info("[%s] Admin %s added %s to %s", gid, interaction.user.display_name, fmt(seconds), member.display_name)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="removetime",
        description="Remove VC time from a member (admin only).",
    )
    @discord.app_commands.describe(
        member="The member to remove time from",
        hours="Hours to remove",
        minutes="Minutes to remove",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def removetime(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        hours: int = 0,
        minutes: int = 0,
    ):
        await interaction.response.defer(ephemeral=True)

        if hours < 0 or minutes < 0:
            await interaction.followup.send("❌ Hours and minutes must be positive!", ephemeral=True)
            return
        if hours == 0 and minutes == 0:
            await interaction.followup.send("❌ Please specify hours or minutes to remove!", ephemeral=True)
            return

        gid     = str(interaction.guild.id)
        uid     = str(member.id)
        seconds = (hours * 3600) + (minutes * 60)

        current = await db.get_total(gid, uid)
        if current <= 0:
            await interaction.followup.send(f"❌ **{member.display_name}** has no VC time to remove!", ephemeral=True)
            return

        # Don't go below zero
        remove  = min(seconds, current)
        new_total = current - remove
        await db.set_time(gid, uid, new_total)

        e = discord.Embed(title="✅ Time Removed", color=0xED4245, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",     value=member.mention,    inline=True)
        e.add_field(name="➖ Removed",     value=fmt(remove),       inline=True)
        e.add_field(name="📊 New Total",  value=fmt(new_total),    inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")

        logger.info("[%s] Admin %s removed %s from %s", gid, interaction.user.display_name, fmt(remove), member.display_name)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="resettime",
        description="Reset a member's VC time to zero (admin only).",
    )
    @discord.app_commands.describe(member="The member to reset")
    @discord.app_commands.default_permissions(administrator=True)
    async def resettime(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        gid     = str(interaction.guild.id)
        uid     = str(member.id)
        old     = await db.get_total(gid, uid)

        await db.set_time(gid, uid, 0)

        e = discord.Embed(title="✅ Time Reset", color=0xFEE75C, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",      value=member.mention,  inline=True)
        e.add_field(name="🗑️ Cleared",    value=fmt(old),        inline=True)
        e.add_field(name="📊 New Total",   value="0s",            inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")

        logger.info("[%s] Admin %s reset time for %s", gid, interaction.user.display_name, member.display_name)
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── Streak Management ──────────────────────────────────────────────────────

    @discord.app_commands.command(
        name="addstreak",
        description="Add days to a member's current streak (admin only).",
    )
    @discord.app_commands.describe(
        member="The member to add streak to",
        days="Number of days to add",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def addstreak(self, interaction: discord.Interaction, member: discord.Member, days: int):
        await interaction.response.defer(ephemeral=True)

        if days <= 0:
            await interaction.followup.send("❌ Days must be a positive number!", ephemeral=True)
            return

        gid = str(interaction.guild.id)
        uid = str(member.id)

        await db.modify_streak(gid, uid, days)
        streak_data = await db.get_streak(gid, uid)

        e = discord.Embed(title="✅ Streak Added", color=0xFF7043, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",          value=member.mention,                        inline=True)
        e.add_field(name="➕ Added",             value=f"{days} day(s)",                      inline=True)
        e.add_field(name="🔥 Current Streak",  value=f"{streak_data['current_streak']} day(s)", inline=True)
        e.add_field(name="🏆 Longest Streak",  value=f"{streak_data['longest_streak']} day(s)", inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")

        logger.info("[%s] Admin %s added %d streak days to %s", gid, interaction.user.display_name, days, member.display_name)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="removestreak",
        description="Remove days from a member's current streak (admin only).",
    )
    @discord.app_commands.describe(
        member="The member to remove streak from",
        days="Number of days to remove",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def removestreak(self, interaction: discord.Interaction, member: discord.Member, days: int):
        await interaction.response.defer(ephemeral=True)

        if days <= 0:
            await interaction.followup.send("❌ Days must be a positive number!", ephemeral=True)
            return

        gid = str(interaction.guild.id)
        uid = str(member.id)

        await db.modify_streak(gid, uid, -days)
        streak_data = await db.get_streak(gid, uid)

        e = discord.Embed(title="✅ Streak Removed", color=0xED4245, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",          value=member.mention,                           inline=True)
        e.add_field(name="➖ Removed",           value=f"{days} day(s)",                         inline=True)
        e.add_field(name="🔥 Current Streak",  value=f"{streak_data['current_streak']} day(s)", inline=True)
        e.add_field(name="🏆 Longest Streak",  value=f"{streak_data['longest_streak']} day(s)", inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")

        logger.info("[%s] Admin %s removed %d streak days from %s", gid, interaction.user.display_name, days, member.display_name)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="resetstreak",
        description="Reset a member's current streak to zero (admin only).",
    )
    @discord.app_commands.describe(member="The member to reset streak for")
    @discord.app_commands.default_permissions(administrator=True)
    async def resetstreak(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        gid = str(interaction.guild.id)
        uid = str(member.id)

        streak_data = await db.get_streak(gid, uid)
        old_streak  = streak_data["current_streak"]

        await db.reset_streak(gid, uid)

        e = discord.Embed(title="✅ Streak Reset", color=0xFEE75C, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",          value=member.mention,   inline=True)
        e.add_field(name="🗑️ Cleared",        value=f"{old_streak} day(s)", inline=True)
        e.add_field(name="🔥 Current Streak",  value="0 days",         inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")

        logger.info("[%s] Admin %s reset streak for %s", gid, interaction.user.display_name, member.display_name)
        await interaction.followup.send(embed=e, ephemeral=True)


    # ── Coin Management ────────────────────────────────────────────────────────

    @discord.app_commands.command(
        name="addcoins",
        description="Add Cheese Coins to a member (admin only).",
    )
    @discord.app_commands.describe(member="The member to add coins to", amount="Number of coins to add")
    @discord.app_commands.default_permissions(administrator=True)
    async def addcoins(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Amount must be positive!", ephemeral=True)
            return
        gid = str(interaction.guild.id)
        uid = str(member.id)
        await db.add_coins(gid, uid, amount)
        coin_data = await db.get_coins(gid, uid)
        e = discord.Embed(title="✅ Coins Added", color=0xFFD700, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",      value=member.mention,              inline=True)
        e.add_field(name="➕ Added",        value=f"{amount} 🧀",              inline=True)
        e.add_field(name="💰 New Balance", value=f"{coin_data['coins']} 🧀",  inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="removecoins",
        description="Remove Cheese Coins from a member (admin only).",
    )
    @discord.app_commands.describe(member="The member to remove coins from", amount="Number of coins to remove")
    @discord.app_commands.default_permissions(administrator=True)
    async def removecoins(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Amount must be positive!", ephemeral=True)
            return
        gid = str(interaction.guild.id)
        uid = str(member.id)
        await db.remove_coins(gid, uid, amount)
        coin_data = await db.get_coins(gid, uid)
        e = discord.Embed(title="✅ Coins Removed", color=0xED4245, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",      value=member.mention,              inline=True)
        e.add_field(name="➖ Removed",      value=f"{amount} 🧀",              inline=True)
        e.add_field(name="💰 New Balance", value=f"{coin_data['coins']} 🧀",  inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="setcoins",
        description="Set a member's Cheese Coins to exact amount (admin only).",
    )
    @discord.app_commands.describe(member="The member to set coins for", amount="Exact coin amount")
    @discord.app_commands.default_permissions(administrator=True)
    async def setcoins(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=True)
        if amount < 0:
            await interaction.followup.send("❌ Amount cannot be negative!", ephemeral=True)
            return
        gid = str(interaction.guild.id)
        uid = str(member.id)
        await db.set_coins(gid, uid, amount)
        e = discord.Embed(title="✅ Coins Set", color=0xFEE75C, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Member",      value=member.mention,   inline=True)
        e.add_field(name="💰 New Balance", value=f"{amount} 🧀",   inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

async def setup(bot):
    await bot.add_cog(AdminCog(bot))
    logger.info("AdminCog loaded.")
