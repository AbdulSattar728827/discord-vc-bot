"""
cogs/coins.py — Cheese Coins system
- Members earn 1 coin per 30 mins in VC
- /coins command to check balance
- #🧀cheese-coins leaderboard channel
- #🧀cheese-logs public activity log
- Join-to-create VCs in each game category
- Auto-delete VCs instantly when empty
- Singapore region for all created VCs
"""

import discord
from discord.ext import commands, tasks
import logging
import asyncio
from datetime import datetime, timezone
from database import db

logger = logging.getLogger(__name__)

COIN_EMOJI        = "🧀"
COINS_CHANNEL     = "🧀cheese-coins"
LOGS_CHANNEL      = "🧀cheese-logs"
PRIVATE_VC_COST   = 5
JOIN_PUBLIC_NAME  = "➕ Join To Create Public VC"
JOIN_PRIVATE_NAME = "➕ Join To Create Private VC"

GAME_CATEGORIES = ["AGE OF EMPIRES IV", "DOTA 2", "COUNTER STRIKE", "VALORANT"]
CATEGORY_PREFIX = {
    "AGE OF EMPIRES IV": "AOE",
    "DOTA 2":            "DOTA",
    "COUNTER STRIKE":    "CS",
    "VALORANT":          "VAL",
}

def _find_category(guild: discord.Guild, keyword: str):
    return discord.utils.find(
        lambda c: keyword.upper() in c.name.upper(),
        guild.categories
    )

VC_HOST_ROLE = "🎙️ VC Host"

def rank_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13: return f"{n}th"
    return {1:f"{n}st",2:f"{n}nd",3:f"{n}rd"}.get(n%10, f"{n}th")


class CoinsCog(commands.Cog, name="Coins"):

    def __init__(self, bot):
        self.bot = bot
        # {vc_id: {"type": "public"/"private", "creator_id": uid}}
        self._managed_vcs: dict[int, dict] = {}
        # Per-member lock to prevent duplicate processing
        self._processing: set[str] = set()
        self.coins_refresh.start()
        self.vc_cleanup.start()

    def cog_unload(self):
        if self.coins_refresh.is_running():
            self.coins_refresh.cancel()
        if self.vc_cleanup.is_running():
            self.vc_cleanup.cancel()

    def _is_admin(self, member: discord.Member) -> bool:
        return (
            member.guild_permissions.administrator or
            member.guild_permissions.manage_channels
        )

    async def _get_or_create_vc_host_role(self, guild: discord.Guild) -> discord.Role | None:
        """Get or create the temporary VC Host role with Move Members permission."""
        role = discord.utils.get(guild.roles, name=VC_HOST_ROLE)
        if role:
            return role
        try:
            role = await guild.create_role(
                name=VC_HOST_ROLE,
                permissions=discord.Permissions(move_members=True),
                color=discord.Color.gold(),
                reason="Temporary role for Private VC creators",
            )
            logger.info("[%s] Created role '%s'", guild.id, VC_HOST_ROLE)
        except discord.Forbidden:
            logger.error("[%s] Cannot create VC Host role — missing permission", guild.id)
            return None
        return role

    async def _assign_vc_host_role(self, member: discord.Member):
        role = await self._get_or_create_vc_host_role(member.guild)
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Created private VC")
                logger.info("[%s] Assigned VC Host role to %s", member.guild.id, member.display_name)
            except discord.Forbidden:
                logger.error("[%s] Cannot assign VC Host role", member.guild.id)

    async def _remove_vc_host_role(self, member: discord.Member):
        role = discord.utils.get(member.guild.roles, name=VC_HOST_ROLE)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Private VC deleted")
                logger.info("[%s] Removed VC Host role from %s", member.guild.id, member.display_name)
            except discord.Forbidden:
                pass

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

    # ── Cheese logs channel ────────────────────────────────────────────────────

    async def _get_or_create_logs_channel(self, guild: discord.Guild):
        ch = discord.utils.get(guild.text_channels, name=LOGS_CHANNEL)
        if ch:
            return ch
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True
                ),
            }
            ch = await guild.create_text_channel(
                LOGS_CHANNEL, overwrites=overwrites,
                topic="🧀 Cheese Coins activity log — VC creations and coin transactions",
            )
            logger.info("[%s] Created #%s", guild.id, LOGS_CHANNEL)
        except discord.Forbidden:
            return None
        return ch

    async def _post_coins_log(self, guild, member, title, description, color=0xFFD700):
        ch = await self._get_or_create_logs_channel(guild)
        if not ch:
            return
        e = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"User ID: {member.id}")
        try:
            await ch.send(embed=e)
        except Exception as ex:
            logger.error("[%s] Failed to post coins log: %s", guild.id, ex)

    # ── Setup join-to-create VCs ───────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._setup_join_to_create(guild)
            await self._update_coins_channel(guild)
            # Scan for existing managed VCs (bot restarted)
            await self._scan_existing_vcs(guild)

    async def _scan_existing_vcs(self, guild: discord.Guild):
        """On startup, find any Private/Public VCs the bot created and track or delete them."""
        for vc in guild.voice_channels:
            # Match VCs that look like bot-created ones
            is_private = vc.name == "🔒 Private VC"
            is_public  = any(
                vc.name.startswith(f"{prefix} Public Voice")
                for prefix in CATEGORY_PREFIX.values()
            )
            if not (is_private or is_public):
                continue

            if len(vc.members) == 0:
                # Empty — delete immediately
                try:
                    await vc.delete(reason="Startup cleanup: empty managed VC")
                    logger.info("[%s] Startup deleted empty VC: %s", guild.id, vc.name)
                except Exception as ex:
                    logger.warning("[%s] Could not delete %s: %s", guild.id, vc.name, ex)
            else:
                # Still has members — track it so it gets deleted when empty
                self._managed_vcs[vc.id] = {
                    "type":       "private" if is_private else "public",
                    "creator_id": None,
                    "host_id":    None,
                }
                logger.info("[%s] Tracking existing VC on startup: %s", guild.id, vc.name)

    async def _setup_join_to_create(self, guild: discord.Guild):
        for keyword in GAME_CATEGORIES:
            category = _find_category(guild, keyword)
            if not category:
                logger.warning("[%s] Category containing '%s' not found", guild.id, keyword)
                continue

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
                    logger.info("[%s] Created '%s' in %s", guild.id, JOIN_PUBLIC_NAME, category.name)
                except discord.Forbidden:
                    pass

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
                    logger.info("[%s] Created '%s' in %s", guild.id, JOIN_PRIVATE_NAME, category.name)
                except discord.Forbidden:
                    pass

    # ── Voice state — handle join-to-create + instant auto-delete ─────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                     before: discord.VoiceState,
                                     after:  discord.VoiceState):
        if member.bot:
            return

        guild = member.guild

        # Member joined a join-to-create channel
        if after.channel and after.channel.name in [JOIN_PUBLIC_NAME, JOIN_PRIVATE_NAME]:
            # Prevent duplicate processing for same member
            key = f"{guild.id}:{member.id}"
            if key in self._processing:
                return
            self._processing.add(key)
            try:
                # Small delay to ensure member is fully connected
                await asyncio.sleep(1)
                # Verify member is still in the trigger channel — use live voice state,
                # NOT after.channel, which may be a stale cached object from a previous
                # startup scan pointing at the wrong category.
                member_obj = guild.get_member(member.id)
                if not member_obj or not member_obj.voice or not member_obj.voice.channel:
                    return
                live_channel = member_obj.voice.channel
                if live_channel.name not in [JOIN_PUBLIC_NAME, JOIN_PRIVATE_NAME]:
                    return
                # Fetch the trigger channel fresh from the API so its .category is correct.
                # The gateway cache can associate the wrong category after a restart.
                try:
                    live_channel = await guild.fetch_channel(live_channel.id)
                except Exception as fetch_err:
                    logger.warning("[%s] fetch_channel for trigger failed: %s — using cached",
                                   guild.id, fetch_err)
                is_private = live_channel.name == JOIN_PRIVATE_NAME
                await self._handle_join_to_create(member, live_channel, is_private)
            finally:
                self._processing.discard(key)
            return

        # Instant delete when VC is empty (no timer/delay)
        if before.channel and before.channel.id in self._managed_vcs:
            vc = guild.get_channel(before.channel.id)
            if vc is not None and len(vc.members) == 0:
                try:
                    vc_info = self._managed_vcs.get(before.channel.id, {})
                    if vc_info.get("type") == "private":
                        host_id = vc_info.get("host_id")
                        if host_id:
                            host = guild.get_member(int(host_id))
                            if host:
                                await self._remove_vc_host_role(host)
                    await vc.delete(reason="Auto-delete: VC is empty")
                    self._managed_vcs.pop(before.channel.id, None)
                    logger.info("[%s] Instantly deleted empty VC: %s", guild.id, vc.name)
                except Exception as ex:
                    logger.warning("[%s] Failed to delete VC: %s", guild.id, ex)
            elif vc is None:
                self._managed_vcs.pop(before.channel.id, None)

        # If member joined a managed private VC (dragged in) — give them full speak permissions
        if (after.channel and
            after.channel.id in self._managed_vcs and
            self._managed_vcs[after.channel.id].get("type") == "private" and
            after.channel.name not in [JOIN_PUBLIC_NAME, JOIN_PRIVATE_NAME]):
            try:
                await after.channel.set_permissions(
                    member,
                    view_channel=True,
                    connect=True,
                    speak=True,
                    stream=True,
                    use_voice_activation=True,
                    deafen_members=False,
                    mute_members=False,
                )
                logger.info("[%s] Gave full audio perms to %s in private VC",
                            guild.id, member.display_name)
            except Exception as ex:
                logger.warning("[%s] Could not set perms for member: %s", guild.id, ex)

    # ── Handle join-to-create ──────────────────────────────────────────────────

    async def _handle_join_to_create(self, member: discord.Member,
                                      trigger_channel: discord.VoiceChannel,
                                      is_private: bool):
        guild    = member.guild
        gid      = str(guild.id)
        uid      = str(member.id)
        category = trigger_channel.category

        logger.info("[%s] Handling join-to-create for %s | private=%s | category=%s",
                    guild.id, member.display_name, is_private,
                    category.name if category else "None")

        # Store category id before any fetch
        category_id = category.id if category else None

        # Check coins for private VC (skip for admins/mods)
        if is_private and not self._is_admin(member):
            coin_data = await db.get_coins(gid, uid)
            if coin_data["coins"] < PRIVATE_VC_COST:
                # Disconnect ALL members from trigger VC (including anyone dragged in)
                for attempt in range(3):
                    try:
                        trigger_vc = guild.get_channel(trigger_channel.id)
                        if trigger_vc:
                            for m in list(trigger_vc.members):
                                try:
                                    await m.edit(voice_channel=None)
                                except Exception:
                                    try:
                                        await m.move_to(None)
                                    except Exception:
                                        pass
                        break
                    except Exception:
                        await asyncio.sleep(0.3)

                needed = PRIVATE_VC_COST - coin_data["coins"]
                msg    = (
                    f"❌ **Not enough Cheese Coins!**\n\n"
                    f"To create a **Private VC** you need **{PRIVATE_VC_COST} {COIN_EMOJI} Cheese Coins**.\n"
                    f"You currently have **{coin_data['coins']} {COIN_EMOJI}**.\n"
                    f"You need **{needed} more {COIN_EMOJI}** to create one!\n\n"
                    f"💡 Earn coins by spending **30 mins** in any Voice Channel — **30 mins = 1 🧀**"
                )

                # Send DM once only
                try:
                    dm = await member.create_dm()
                    await dm.send(msg)
                except Exception:
                    pass

                # Always log in cheese-logs
                await self._post_coins_log(
                    guild, member,
                    "❌ Private VC Denied — Not Enough Coins",
                    f"{member.mention} tried to create a **Private VC** but doesn't have enough coins!\n\n"
                    f"**Required:** {PRIVATE_VC_COST} {COIN_EMOJI}\n"
                    f"**Balance:** {coin_data['coins']} {COIN_EMOJI}\n"
                    f"**Still needs:** {needed} {COIN_EMOJI}",
                    color=0xED4245,
                )
                return

            # Deduct coins
            await db.spend_coins(gid, uid, PRIVATE_VC_COST)

        # Get fresh category object
        category = guild.get_channel(category_id) if category_id else None
        cat_upper = category.name.upper() if category else ""
        prefix    = next((v for k, v in CATEGORY_PREFIX.items() if k in cat_upper), "VC")

        if is_private:
            vc_name    = "🔒 Private VC"
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=False,
                    connect=False,
                    speak=False,
                    stream=False,
                    use_voice_activation=False,
                ),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    stream=True,
                    use_voice_activation=True,
                    move_members=True,
                    deafen_members=False,
                    mute_members=False,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    stream=True,
                    manage_channels=True,
                    move_members=True,
                    mute_members=True,
                    deafen_members=True,
                ),
            }
            # Give ALL admin/mod roles full audio access
            for role in guild.roles:
                if role.permissions.administrator or role.permissions.manage_channels:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        stream=True,
                        use_voice_activation=True,
                        move_members=True,
                        mute_members=True,
                        deafen_members=True,
                    )
        else:
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
                    view_channel=True, connect=True,
                    speak=True, stream=True, use_voice_activation=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True,
                    manage_channels=True, move_members=True
                ),
            }

        try:
            # Always fetch the category fresh from the API to bypass Discord Community
            # onboarding permission cache — get_channel() can return a stale object
            # that inherits onboarding restrictions even for bots with Administrator.
            fresh_category = None
            if category_id:
                try:
                    fresh_category = await guild.fetch_channel(category_id)
                except Exception as fetch_err:
                    logger.warning("[%s] fetch_channel(%s) failed: %s — falling back to get_channel",
                                   guild.id, category_id, fetch_err)
                    fresh_category = guild.get_channel(category_id)

            # Build explicit overwrites so Discord cannot inherit onboarding restrictions
            # onto the newly created channel.  We always set view_channel + connect on
            # @everyone explicitly; this prevents the Community server from treating the
            # new channel as "hidden" and blocking the creation call.
            if guild.default_role not in overwrites:
                overwrites[guild.default_role] = discord.PermissionOverwrite(
                    view_channel=True, connect=True
                )
            if guild.me not in overwrites:
                overwrites[guild.me] = discord.PermissionOverwrite(
                    view_channel=True, connect=True,
                    manage_channels=True, move_members=True
                )

            new_vc = await guild.create_voice_channel(
                vc_name,
                category=fresh_category,
                overwrites=overwrites,
                rtc_region="singapore",
            )
            try:
                await new_vc.edit(rtc_region="singapore")
            except Exception:
                pass
            self._managed_vcs[new_vc.id] = {
                "type":       "private" if is_private else "public",
                "creator_id": uid,
                "host_id":    uid if is_private else None,
            }

            # Bug fix 1: move member to the new VC
            await member.move_to(new_vc)

            # Give creator VC Host role so they can drag members
            if is_private:
                await self._assign_vc_host_role(member)
                self._managed_vcs[new_vc.id]["host_id"] = uid
            logger.info("[%s] Created %s VC '%s' for %s — moved member",
                        guild.id, "private" if is_private else "public",
                        vc_name, member.display_name)

            if is_private and not self._is_admin(member):
                coin_data = await db.get_coins(gid, uid)

                # Send DM once
                try:
                    dm = await member.create_dm()
                    await dm.send(
                        f"✅ **Private VC Created!**\n\n"
                        f"Your private VC **{vc_name}** is ready!\n"
                        f"💰 **{PRIVATE_VC_COST} {COIN_EMOJI}** deducted.\n"
                        f"💼 Remaining balance: **{coin_data['coins']} {COIN_EMOJI}**\n\n"
                        f"The VC will auto-delete when everyone leaves."
                    )
                except Exception:
                    pass

                # Log to cheese-logs
                await self._post_coins_log(
                    guild, member,
                    "🔒 Private VC Created",
                    f"{member.mention} created a **Private VC**!\n\n"
                    f"**VC Name:** {vc_name}\n"
                    f"**Category:** {category.name if category else 'Unknown'}\n"
                    f"**Coins Spent:** {PRIVATE_VC_COST} {COIN_EMOJI}\n"
                    f"**Remaining Balance:** {coin_data['coins']} {COIN_EMOJI}",
                    color=0x57F287,
                )
            else:
                # Public VC log
                await self._post_coins_log(
                    guild, member,
                    "🔊 Public VC Created",
                    f"{member.mention} created a **Public VC**!\n\n"
                    f"**VC Name:** {vc_name}\n"
                    f"**Category:** {category.name if category else 'Unknown'}",
                    color=0x5865F2,
                )

        except discord.Forbidden:
            logger.error("[%s] Missing permission to create VC in %s", guild.id,
                         category.name if category else "server")
        except Exception as ex:
            logger.error("[%s] Failed to create VC: %s", guild.id, ex)

    # ── Periodic cleanup (safety net for any missed deletions) ────────────────

    @tasks.loop(minutes=5)
    async def vc_cleanup(self):
        to_delete = []
        for vc_id in list(self._managed_vcs.keys()):
            for guild in self.bot.guilds:
                vc = guild.get_channel(vc_id)
                if vc and len(vc.members) == 0:
                    try:
                        await vc.delete(reason="Cleanup: empty VC")
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
        description="Check your Cheese Coin balance!",
    )
    async def coins(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        gid       = str(interaction.guild.id)
        uid       = str(interaction.user.id)
        coin_data = await db.get_coins(gid, uid)
        board     = await db.get_coins_leaderboard(gid)
        rank      = next((i+1 for i,(u,_) in enumerate(board) if u == uid), None)

        # Check if member is currently in VC — add live time to pending
        member         = interaction.user
        live_secs      = 0.0
        currently_in_vc = member.voice is not None and member.voice.channel is not None

        if currently_in_vc:
            # Get join time from tracker cog
            tracker = self.bot.get_cog("Tracker")
            if tracker:
                sessions = tracker._sessions.get(gid, {})
                join_time = sessions.get(uid)
                if join_time:
                    live_secs = (datetime.now(timezone.utc) - join_time).total_seconds()

        # Calculate total pending including live time
        total_pending = coin_data["pending_secs"] + live_secs
        needed        = 30 * 60
        pct           = min(int((total_pending / needed) * 100), 100)
        filled        = int(pct / 10)
        bar           = "█" * filled + "░" * (10 - filled)
        mins_left     = max(0, int((needed - total_pending) / 60))

        e = discord.Embed(
            title=f"{COIN_EMOJI} Your Cheese Coins",
            color=0xFFD700,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        e.add_field(name=f"{COIN_EMOJI} Balance",    value=f"**{coin_data['coins']}** coins",        inline=True)
        e.add_field(name="📊 Rank",                   value=rank_suffix(rank) if rank else "Unranked", inline=True)
        e.add_field(name="💰 Total Earned",           value=f"{coin_data['total_earned']} coins",     inline=True)
        e.add_field(name="🎙️ How to earn",           value="30 mins in VC = 1 coin",                 inline=True)
        e.add_field(name="🔒 Private VC Cost",        value=f"{PRIVATE_VC_COST} coins",               inline=True)

        progress_label = "⏳ Progress to next coin (live)" if currently_in_vc else "⏳ Progress to next coin"
        progress_note  = "\n🟢 You are currently in VC!" if currently_in_vc else "\n⚫ Join a VC to earn coins"
        e.add_field(
            name=progress_label,
            value=f"`{bar}` {pct}%\n{mins_left} min remaining{progress_note}",
            inline=False,
        )
        e.set_footer(text=f"{interaction.guild.name} • Coins are awarded when you leave VC")
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── /refreshcoins command (admin only) ────────────────────────────────────

    @discord.app_commands.command(
        name="testvc",
        description="Test if bot can create a VC (admin only).",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def testvc(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        results = []

        # Test 1: Create VC without category
        try:
            vc = await guild.create_voice_channel("🧪 Test VC")
            await vc.delete()
            results.append("✅ Can create VC without category")
        except discord.Forbidden as e:
            results.append(f"❌ Cannot create VC without category: {e}")
        except Exception as e:
            results.append(f"❌ Error: {e}")

        # Test 2: Try each game category
        for keyword in GAME_CATEGORIES:
            category = _find_category(guild, keyword)
            if not category:
                results.append(f"⚠️ Category '{keyword}' not found")
                continue
            try:
                vc = await guild.create_voice_channel("🧪 Test VC", category=category)
                await vc.delete()
                results.append(f"✅ Can create in {keyword}")
            except discord.Forbidden as e:
                results.append(f"❌ Cannot create in {keyword}: {e}")

        await interaction.followup.send("\n".join(results), ephemeral=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def refreshcoins(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._update_coins_channel(interaction.guild)
        await interaction.followup.send("✅ Cheese Coins leaderboard refreshed!", ephemeral=True)

    # ── /createvc command (admin/mod only) ─────────────────────────────────────

    @discord.app_commands.command(
        name="createvc",
        description="Create a public or private VC (admins/mods only).",
    )
    @discord.app_commands.describe(
        vc_type="Type of VC to create",
        category_name="Which game category keyword (e.g. AOE, DOTA, CS, VALORANT)",
    )
    @discord.app_commands.choices(vc_type=[
        discord.app_commands.Choice(name="Public",  value="public"),
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

        category = _find_category(interaction.guild, category_name)
        if not category:
            await interaction.followup.send(
                f"❌ No category containing `{category_name}` found!", ephemeral=True
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
                    view_channel=True, connect=True,
                    speak=True, stream=True, use_voice_activation=True
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True,
                    manage_channels=True, move_members=True
                ),
            }
        else:
            vc_name = "🔒 Private VC"
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(
                    view_channel=False, connect=False
                ),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True, connect=True,
                    speak=True, stream=True, use_voice_activation=True,
                    move_members=True
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True,
                    manage_channels=True, move_members=True, speak=True
                ),
            }
            for role in interaction.guild.roles:
                if role.permissions.administrator or role.permissions.manage_channels:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, connect=True,
                        speak=True, stream=True, move_members=True
                    )

        try:
            new_vc = await interaction.guild.create_voice_channel(
                vc_name,
                category=category,
                overwrites=overwrites,
                rtc_region="singapore",
            )
            # Force set region
            try:
                await new_vc.edit(rtc_region="singapore")
            except Exception:
                pass
            self._managed_vcs[new_vc.id] = {
                "type":       vc_type,
                "creator_id": str(interaction.user.id),
            }
            await interaction.followup.send(
                f"✅ Created **{vc_name}** in `{category.name}` (Singapore region)!", ephemeral=True
            )
            logger.info("[%s] Admin %s created %s VC '%s'",
                        interaction.guild.id, interaction.user.display_name, vc_type, vc_name)
        except discord.Forbidden:
            await interaction.followup.send("❌ Missing permission to create VC!", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CoinsCog(bot))
    logger.info("CoinsCog loaded.")
