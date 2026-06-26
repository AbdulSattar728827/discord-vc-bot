"""
cogs/coins.py — Cheese Coins system
- Members earn 1 coin per 30 mins in VC
- /coins command to check balance
- #🧀cheese-coins leaderboard channel
- /createvc to create public/private VCs
- Join-to-create VCs in each game category
- Auto-delete VCs when empty
"""

import discord
from discord.ext import commands, tasks
import logging
import asyncio
from datetime import datetime, timezone
from database import db

logger = logging.getLogger(__name__)

COIN_EMOJI       = "🧀"
COINS_CHANNEL    = "🧀cheese-coins"
PRIVATE_VC_COST  = 5
JOIN_PUBLIC_NAME = "➕ Join To Create Public VC"
JOIN_PRIVATE_NAME= "➕ Join To Create Private VC"

# Game categories where join-to-create VCs will be placed
GAME_CATEGORIES  = ["AGE OF EMPIRES IV", "DOTA 2", "COUNTER STRIKE", "VALORANT"]

# Category prefix for public VC names
CATEGORY_PREFIX  = {
    "AGE OF EMPIRES IV": "AOE",
    "DOTA 2":            "DOTA",
    "COUNTER STRIKE":    "CS",
    "VALORANT":          "VAL",
}

def rank_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13: return f"{n}th"
    return {1:f"{n}st",2:f"{n}nd",3:f"{n}rd"}.get(n%10, f"{n}th")


class CoinsCog(commands.Cog, name="Coins"):

    def __init__(self, bot):
        self.bot = bot
        # track created VCs: {vc_id: {"type": "public"/"private", "creator_id": uid}}
        self._managed_vcs: dict[int, dict] = {}
        self.coins_refresh.start()
        self.vc_cleanup.start()

    def cog_unload(self):
        if self.coins_refresh.is_running():
            self.coins_refresh.cancel()
        if self.vc_cleanup.is_running():
            self.vc_cleanup.cancel()

    # ── Is admin/mod ───────────────────────────────────────────────────────────

    def _is_admin(self, member: discord.Member) -> bool:
        return (
            member.guild_permissions.administrator or
            member.guild_permissions.manage_channels
        )

    # ── Coins leaderboard channel ──────────────────────────────────────────────

    async def _get_or_create_coins_channel(self, guild: discord.Guild):
        ch = discord.utils.get(guild.text_channels, name=COINS_CHANNEL)
        if ch:
            return ch
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, add_reactions=False
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True,
                    manage_messages=True, embed_links=True
                ),
            }
            ch = await guild.create_text_channel(
                COINS_CHANNEL, overwrites=overwrites,
                topic=f"{COIN_EMOJI} Cheese Coins Leaderboard — earn coins by spending time in VC!",
            )
            logger.info("[%s] Created #%s", guild.id, COINS_CHANNEL)
        except discord.Forbidden:
            return None
        return ch

    async def _update_coins_channel(self, guild: discord.Guild):
        channel = await self._get_or_create_coins_channel(guild)
        if not channel:
            return

        board = await db.get_coins_leaderboard(str(guild.id))
        now   = datetime.now(timezone.utc)

        if not board:
            e = discord.Embed(
                title=f"{COIN_EMOJI} Cheese Coins Leaderboard",
                description="No coins earned yet!\nSpend 30 mins in a Voice Channel to earn your first coin!",
                color=0xFFD700, timestamp=now,
            )
            e.set_footer(text=f"30 mins in VC = 1 {COIN_EMOJI} • {guild.name}")
        else:
            rows = []
            for i, (uid, coins) in enumerate(board):
                member = guild.get_member(int(uid))
                name   = member.display_name if member else f"Unknown ({uid})"
                rows.append(f"`{rank_suffix(i+1):>4}`  **{name}**  —  {coins} {COIN_EMOJI}")

            e = discord.Embed(
                title=f"{COIN_EMOJI} Cheese Coins Leaderboard",
                description="\n".join(rows),
                color=0xFFD700, timestamp=now,
            )
            e.set_footer(text=f"30 mins in VC = 1 {COIN_EMOJI} • {guild.name}")

        try:
            await channel.purge(limit=10, check=lambda m: m.author == guild.me)
            await channel.send(embed=e)
        except Exception as ex:
            logger.error("[%s] Failed to update coins channel: %s", guild.id, ex)

    @tasks.loop(minutes=30)
    async def coins_refresh(self):
        for guild in self.bot.guilds:
            await self._update_coins_channel(guild)

    @coins_refresh.before_loop
    async def _before_coins(self):
        await self.bot.wait_until_ready()

    # ── Setup join-to-create VCs in game categories ───────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._setup_join_to_create(guild)
            await self._update_coins_channel(guild)

    async def _setup_join_to_create(self, guild: discord.Guild):
        """Create join-to-create VCs in each game category if they don't exist."""
        for cat_name in GAME_CATEGORIES:
            category = discord.utils.find(
                lambda c: c.name.upper() == cat_name.upper(),
                guild.categories
            )
            if not category:
                continue

            # Check and create public join VC
            pub = discord.utils.get(category.voice_channels, name=JOIN_PUBLIC_NAME)
            if not pub:
                try:
                    await guild.create_voice_channel(
                        JOIN_PUBLIC_NAME, category=category,
                        overwrites={
                            guild.default_role: discord.PermissionOverwrite(
                                view_channel=True, connect=True
                            )
                        }
                    )
                    logger.info("[%s] Created '%s' in %s", guild.id, JOIN_PUBLIC_NAME, cat_name)
                except discord.Forbidden:
                    pass

            # Check and create private join VC
            prv = discord.utils.get(category.voice_channels, name=JOIN_PRIVATE_NAME)
            if not prv:
                try:
                    await guild.create_voice_channel(
                        JOIN_PRIVATE_NAME, category=category,
                        overwrites={
                            guild.default_role: discord.PermissionOverwrite(
                                view_channel=True, connect=True
                            )
                        }
                    )
                    logger.info("[%s] Created '%s' in %s", guild.id, JOIN_PRIVATE_NAME, cat_name)
                except discord.Forbidden:
                    pass

    # ── Voice state — handle join-to-create ───────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                     before: discord.VoiceState,
                                     after:  discord.VoiceState):
        if member.bot:
            return

        guild = member.guild

        # Member joined a join-to-create channel
        if after.channel and after.channel.name in [JOIN_PUBLIC_NAME, JOIN_PRIVATE_NAME]:
            is_private = after.channel.name == JOIN_PRIVATE_NAME
            await self._handle_join_to_create(member, after.channel, is_private)

        # Check if a managed VC is now empty — delete it
        if before.channel and before.channel.id in self._managed_vcs:
            await asyncio.sleep(3)  # Small delay to avoid false positives
            vc = guild.get_channel(before.channel.id)
            if vc and len(vc.members) == 0:
                try:
                    await vc.delete(reason="Auto-delete: VC is empty")
                    self._managed_vcs.pop(before.channel.id, None)
                    logger.info("[%s] Deleted empty VC: %s", guild.id, vc.name)
                except Exception:
                    pass

    async def _handle_join_to_create(self, member: discord.Member,
                                      trigger_channel: discord.VoiceChannel,
                                      is_private: bool):
        guild    = member.guild
        gid      = str(guild.id)
        uid      = str(member.id)
        category = trigger_channel.category

        # Check coins for private VC (skip for admins/mods)
        if is_private and not self._is_admin(member):
            coin_data = await db.get_coins(gid, uid)
            if coin_data["coins"] < PRIVATE_VC_COST:
                try:
                    await member.move_to(None)  # Kick from trigger VC
                except Exception:
                    pass

                # Try DM first
                dm_sent = False
                try:
                    dm = await member.create_dm()
                    await dm.send(
                        f"❌ **Not enough Cheese Coins!**\n\n"
                        f"To create a **Private VC** you need **{PRIVATE_VC_COST} {COIN_EMOJI} Cheese Coins**.\n"
                        f"You currently have **{coin_data['coins']} {COIN_EMOJI}**.\n"
                        f"You need **{PRIVATE_VC_COST - coin_data['coins']} more {COIN_EMOJI}** to create one!\n\n"
                        f"💡 Earn coins by spending time in Voice Channels — **30 mins = 1 🧀**"
                    )
                    dm_sent = True
                except Exception:
                    pass

                # If DM failed, post in cheese-coins channel
                if not dm_sent:
                    coins_ch = discord.utils.get(guild.text_channels, name=COINS_CHANNEL)
                    if coins_ch:
                        try:
                            await coins_ch.send(
                                f"{member.mention} ❌ **Not enough Cheese Coins!**\n"
                                f"You need **{PRIVATE_VC_COST} {COIN_EMOJI}** to create a Private VC "
                                f"but you only have **{coin_data['coins']} {COIN_EMOJI}**.\n"
                                f"You need **{PRIVATE_VC_COST - coin_data['coins']} more {COIN_EMOJI}**! "
                                f"Spend 30 mins in VC to earn a coin.",
                                delete_after=15,
                            )
                        except Exception:
                            pass
                return

            # Deduct coins
            await db.spend_coins(gid, uid, PRIVATE_VC_COST)

        # Get category prefix for naming
        cat_upper = category.name.upper() if category else ""
        prefix    = next((v for k, v in CATEGORY_PREFIX.items() if k in cat_upper), "")

        if is_private:
            vc_name = "🔒 Private VC"
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=False, connect=False
                ),
                member: discord.PermissionOverwrite(
                    view_channel=True, connect=True, move_members=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True, manage_channels=True
                ),
            }
            # Give admins access
            for role in guild.roles:
                if role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, connect=True, move_members=True
                    )
        else:
            # Find next available public VC number
            existing = [
                vc.name for vc in (category.voice_channels if category else guild.voice_channels)
                if vc.name.startswith(f"{prefix} Public Voice")
            ]
            num = 1
            while f"{prefix} Public Voice {num}" in existing:
                num += 1
            vc_name    = f"{prefix} Public Voice {num}"
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, connect=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True, manage_channels=True
                ),
            }

        try:
            new_vc = await guild.create_voice_channel(
                vc_name, category=category, overwrites=overwrites
            )
            self._managed_vcs[new_vc.id] = {
                "type":       "private" if is_private else "public",
                "creator_id": uid,
            }
            await member.move_to(new_vc)
            logger.info("[%s] Created %s VC '%s' for %s", guild.id,
                        "private" if is_private else "public", vc_name, member.display_name)

            if is_private and not self._is_admin(member):
                try:
                    dm = await member.create_dm()
                    coin_data = await db.get_coins(gid, uid)
                    await dm.send(
                        f"✅ Your private VC **{vc_name}** has been created!\n"
                        f"💰 **{PRIVATE_VC_COST} {COIN_EMOJI}** deducted. Remaining balance: **{coin_data['coins']} {COIN_EMOJI}**\n"
                        f"The VC will auto-delete when everyone leaves."
                    )
                except Exception:
                    pass

        except discord.Forbidden:
            logger.error("[%s] Missing permission to create VC in %s", guild.id,
                         category.name if category else "server")

    # ── Periodic cleanup of empty managed VCs ─────────────────────────────────

    @tasks.loop(minutes=5)
    async def vc_cleanup(self):
        to_delete = []
        for vc_id, info in self._managed_vcs.items():
            for guild in self.bot.guilds:
                vc = guild.get_channel(vc_id)
                if vc and len(vc.members) == 0:
                    try:
                        await vc.delete(reason="Auto-cleanup: empty VC")
                        to_delete.append(vc_id)
                    except Exception:
                        pass
                elif not vc:
                    to_delete.append(vc_id)
        for vid in to_delete:
            self._managed_vcs.pop(vid, None)

    @vc_cleanup.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # ── /coins command ─────────────────────────────────────────────────────────

    @discord.app_commands.command(
        name="coins",
        description=f"Check your Cheese Coin balance!",
    )
    async def coins(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        gid       = str(interaction.guild.id)
        uid       = str(interaction.user.id)
        coin_data = await db.get_coins(gid, uid)
        board     = await db.get_coins_leaderboard(gid)
        rank      = next((i+1 for i,(u,_) in enumerate(board) if u == uid), None)

        e = discord.Embed(
            title=f"{COIN_EMOJI} Your Cheese Coins",
            color=0xFFD700,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        e.add_field(name=f"{COIN_EMOJI} Balance",     value=f"**{coin_data['coins']}** coins",        inline=True)
        e.add_field(name="📊 Rank",                    value=rank_suffix(rank) if rank else "Unranked", inline=True)
        e.add_field(name="💰 Total Earned",            value=f"{coin_data['total_earned']} coins",     inline=True)
        e.add_field(name="🎙️ How to earn",            value="30 mins in VC = 1 coin",                 inline=True)
        e.add_field(name="🔒 Private VC Cost",         value=f"{PRIVATE_VC_COST} coins",               inline=True)

        # Progress to next coin
        pending  = coin_data["pending_secs"]
        needed   = 30 * 60
        pct      = min(int((pending / needed) * 100), 100)
        filled   = int(pct / 10)
        bar      = "█" * filled + "░" * (10 - filled)
        mins_left = int((needed - pending) / 60)
        e.add_field(
            name="⏳ Progress to next coin",
            value=f"`{bar}` {pct}%\n{mins_left} min remaining",
            inline=False,
        )
        e.set_footer(text=f"{interaction.guild.name} • Keep grinding!")
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── /createvc command (admin/mod only via slash) ───────────────────────────

    @discord.app_commands.command(
        name="createvc",
        description="Create a public or private VC (admins/mods only).",
    )
    @discord.app_commands.describe(
        vc_type="Type of VC to create",
        category_name="Which game category to create it in",
    )
    @discord.app_commands.choices(vc_type=[
        discord.app_commands.Choice(name="Public", value="public"),
        discord.app_commands.Choice(name="Private", value="private"),
    ])
    @discord.app_commands.default_permissions(manage_channels=True)
    async def createvc(
        self,
        interaction: discord.Interaction,
        vc_type: str,
        category_name: str = "AGE OF EMPIRES IV",
    ):
        await interaction.response.defer(ephemeral=True)

        category = discord.utils.find(
            lambda c: c.name.upper() == category_name.upper(),
            interaction.guild.categories
        )
        if not category:
            await interaction.followup.send(
                f"❌ Category `{category_name}` not found!", ephemeral=True
            )
            return

        cat_upper = category.name.upper()
        prefix    = next((v for k, v in CATEGORY_PREFIX.items() if k in cat_upper), "VC")

        if vc_type == "public":
            existing = [
                vc.name for vc in category.voice_channels
                if vc.name.startswith(f"{prefix} Public Voice")
            ]
            num    = 1
            while f"{prefix} Public Voice {num}" in existing:
                num += 1
            vc_name    = f"{prefix} Public Voice {num}"
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, connect=True
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True, manage_channels=True
                ),
            }
        else:
            vc_name = "🔒 Private VC"
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(
                    view_channel=False, connect=False
                ),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True, connect=True, move_members=True
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True, manage_channels=True
                ),
            }
            for role in interaction.guild.roles:
                if role.permissions.administrator:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, connect=True, move_members=True
                    )

        try:
            new_vc = await interaction.guild.create_voice_channel(
                vc_name, category=category, overwrites=overwrites
            )
            self._managed_vcs[new_vc.id] = {
                "type":       vc_type,
                "creator_id": str(interaction.user.id),
            }
            await interaction.followup.send(
                f"✅ Created **{vc_name}** in `{category.name}`!", ephemeral=True
            )
            logger.info("[%s] Admin %s created %s VC '%s'",
                        interaction.guild.id, interaction.user.display_name, vc_type, vc_name)
        except discord.Forbidden:
            await interaction.followup.send("❌ Missing permission to create VC!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CoinsCog(bot))
    logger.info("CoinsCog loaded.")
