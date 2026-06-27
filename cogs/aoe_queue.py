"""
cogs/aoe_queue.py — AOE 4 Queue System
- 4 queue channels: 1v1, 2v2, 3v3, 4v4
- Join/Leave queue buttons — queue embed always stays clean at bottom
- All match activity (coin flip, draft, result) in private threads
- Thread named after captains e.g. "⚔️ 1v1 Match #5 — MicroMan vs Ragnar"
- Thread auto-deleted 60s after match ends
- ELO system (default 1000, ±25 per match)
- Per-queue leaderboards + match history channel
"""

import discord
from discord.ext import commands
import logging
import asyncio
import random
from datetime import datetime, timezone
from database import db

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

QUEUE_CONFIGS = {
    "1v1": {"size": 2,  "team_size": 1, "pick_order": []},
    "2v2": {"size": 4,  "team_size": 2, "pick_order": [0, 1, 0]},
    "3v3": {"size": 6,  "team_size": 3, "pick_order": [0, 1, 1, 0]},
    "4v4": {"size": 8,  "team_size": 4, "pick_order": [0, 1, 1, 0, 0, 1]},
}

QUEUE_CHANNEL_NAMES = {
    "1v1": "1v1-aoe-queue",
    "2v2": "2v2-aoe-queue",
    "3v3": "3v3-aoe-queue",
    "4v4": "4v4-aoe-queue",
}

LEADERBOARD_CHANNEL_NAMES = {
    "1v1": "1v1-aoe-leaderboard",
    "2v2": "2v2-aoe-leaderboard",
    "3v3": "3v3-aoe-leaderboard",
    "4v4": "4v4-aoe-leaderboard",
}

MATCH_HISTORY_CHANNEL  = "aoe-match-history"
DEFAULT_ELO            = 1000
ELO_CHANGE             = 25
AOE_CATEGORY_KEYWORD   = "AGE OF EMPIRES"
RESULT_DISPLAY_SECS    = 60   # how long result shows before thread is deleted
WIN_COINS              = 5    # 🧀 coins awarded to winning team


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_aoe_category(guild: discord.Guild):
    return discord.utils.find(
        lambda c: AOE_CATEGORY_KEYWORD in c.name.upper(),
        guild.categories
    )

def elo_bar(elo: int) -> str:
    if elo >= 1200: return "🔥"
    if elo >= 1100: return "⭐"
    if elo >= 1000: return "🟢"
    if elo >= 900:  return "🟡"
    return "🔴"


# ── Active match state (in-memory) ────────────────────────────────────────────

class MatchState:
    def __init__(self, queue_type: str, players: list[discord.Member]):
        cfg              = QUEUE_CONFIGS[queue_type]
        self.queue_type  = queue_type
        self.team_size   = cfg["team_size"]
        self.pick_order  = cfg["pick_order"]
        self.all_players = players.copy()
        self.match_id    = None   # set after DB insert
        self.thread      = None   # discord.Thread — set after creation

        # Captains randomly selected
        shuffled = players.copy()
        random.shuffle(shuffled)
        self.captain1: discord.Member = shuffled[0]
        self.captain2: discord.Member = shuffled[1]

        # Teams start with just captains
        self.team1: list[discord.Member] = [self.captain1]
        self.team2: list[discord.Member] = [self.captain2]

        # Remaining players to draft
        self.remaining: list[discord.Member] = [
            p for p in players if p not in (self.captain1, self.captain2)
        ]

        # Draft state
        self.pick_step       = 0
        self.draft_complete  = False
        self.coin_flip_done  = False
        self.first_pick_team = None   # 1 or 2
        self.phase           = "coin_flip"  # coin_flip | draft | pre_match | in_match

        # The single message inside the thread that gets edited throughout
        self.thread_message: discord.Message | None = None

    def thread_name(self) -> str:
        """Generate thread name. Uses captain names once both are known."""
        if self.queue_type == "1v1":
            return f"⚔️ 1v1 Match #{self.match_id} — {self.captain1.display_name} vs {self.captain2.display_name}"
        return f"⚔️ {self.queue_type.upper()} Match #{self.match_id} — {self.captain1.display_name}'s Team vs {self.captain2.display_name}'s Team"

    def current_picker(self) -> discord.Member | None:
        if self.pick_step >= len(self.pick_order):
            return None
        team_idx = self.pick_order[self.pick_step]
        if self.first_pick_team == 2:
            team_idx = 1 - team_idx
        return self.captain1 if team_idx == 0 else self.captain2

    def pick_player(self, player: discord.Member):
        picker = self.current_picker()
        if picker == self.captain1:
            self.team1.append(player)
        else:
            self.team2.append(player)
        self.remaining.remove(player)
        self.pick_step += 1
        if not self.remaining:
            self.draft_complete = True
            self.phase = "pre_match"

    def replace_captain(self, team: int, new_captain: discord.Member):
        if team == 1:
            old = self.captain1
            self.captain1 = new_captain
            if new_captain in self.team1:
                idx = self.team1.index(new_captain)
                self.team1[idx] = old
            self.team1.remove(new_captain) if new_captain in self.team1 else None
            self.team1.insert(0, new_captain)
            if old not in self.team1:
                self.team1.append(old)
        else:
            old = self.captain2
            self.captain2 = new_captain
            if new_captain in self.team2:
                self.team2.remove(new_captain)
            self.team2.insert(0, new_captain)
            if old not in self.team2:
                self.team2.append(old)

    def team_of(self, member: discord.Member) -> int | None:
        if member in self.team1: return 1
        if member in self.team2: return 2
        return None


# ── Queue embed View (persistent Join/Leave) ──────────────────────────────────

class QueueView(discord.ui.View):
    def __init__(self, cog: "AOEQueueCog", queue_type: str):
        super().__init__(timeout=None)
        self.cog        = cog
        self.queue_type = queue_type

    @discord.ui.button(label="✅ Join Queue", style=discord.ButtonStyle.success,
                       custom_id="aoe_join_queue")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_join(interaction, self.queue_type)

    @discord.ui.button(label="❌ Leave Queue", style=discord.ButtonStyle.danger,
                       custom_id="aoe_leave_queue")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_leave(interaction, self.queue_type)


# ── Coin flip View ─────────────────────────────────────────────────────────────

class CoinFlipView(discord.ui.View):
    def __init__(self, cog: "AOEQueueCog", match: MatchState, flipper: discord.Member):
        super().__init__(timeout=120)
        self.cog     = cog
        self.match   = match
        self.flipper = flipper

    @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.primary)
    async def heads(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id:
            await interaction.response.send_message(
                "❌ Only the coin flipper can choose!", ephemeral=True)
            return
        await self.cog.resolve_flip(interaction, self.match, "heads")
        self.stop()

    @discord.ui.button(label="🪙 Tails", style=discord.ButtonStyle.primary)
    async def tails(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id:
            await interaction.response.send_message(
                "❌ Only the coin flipper can choose!", ephemeral=True)
            return
        await self.cog.resolve_flip(interaction, self.match, "tails")
        self.stop()


# ── First pick View ────────────────────────────────────────────────────────────

class FirstPickView(discord.ui.View):
    def __init__(self, cog: "AOEQueueCog", match: MatchState, winner: discord.Member):
        super().__init__(timeout=120)
        self.cog    = cog
        self.match  = match
        self.winner = winner

    @discord.ui.button(label="⚡ First Pick", style=discord.ButtonStyle.success)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.winner.id:
            await interaction.response.send_message(
                "❌ Only the flip winner can choose!", ephemeral=True)
            return
        self.match.first_pick_team = self.match.team_of(self.winner)
        self.match.phase = "draft"
        await self.cog.show_draft(interaction, self.match)
        self.stop()

    @discord.ui.button(label="🛡️ Second Pick", style=discord.ButtonStyle.secondary)
    async def second(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.winner.id:
            await interaction.response.send_message(
                "❌ Only the flip winner can choose!", ephemeral=True)
            return
        team = self.match.team_of(self.winner)
        self.match.first_pick_team = 2 if team == 1 else 1
        self.match.phase = "draft"
        await self.cog.show_draft(interaction, self.match)
        self.stop()


# ── Draft View ─────────────────────────────────────────────────────────────────

class DraftView(discord.ui.View):
    def __init__(self, cog: "AOEQueueCog", match: MatchState):
        super().__init__(timeout=120)
        self.cog   = cog
        self.match = match
        self._build()

    def _build(self):
        self.clear_items()

        # Player pick buttons (row 0 and 1)
        for i, player in enumerate(self.match.remaining):
            btn = discord.ui.Button(
                label=player.display_name,
                style=discord.ButtonStyle.primary,
                row=i // 4,
            )
            async def callback(interaction: discord.Interaction, p=player):
                if interaction.user.id != self.match.current_picker().id:
                    await interaction.response.send_message(
                        "❌ It's not your turn to pick!", ephemeral=True)
                    return
                self.match.pick_player(p)
                if self.match.draft_complete:
                    await self.cog.show_pre_match(interaction, self.match)
                else:
                    await self.cog.show_draft(interaction, self.match)
            btn.callback = callback
            self.add_item(btn)

        # Change captain buttons (row 2)
        cap1_btn = discord.ui.Button(
            label="🔄 Change Team 1 Captain",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        async def change_cap1(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain1.id:
                await interaction.response.send_message(
                    "❌ Only Team 1's captain can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain(interaction, self.match, team=1)
        cap1_btn.callback = change_cap1
        self.add_item(cap1_btn)

        cap2_btn = discord.ui.Button(
            label="🔄 Change Team 2 Captain",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        async def change_cap2(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain2.id:
                await interaction.response.send_message(
                    "❌ Only Team 2's captain can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain(interaction, self.match, team=2)
        cap2_btn.callback = change_cap2
        self.add_item(cap2_btn)

        # Cancel (row 3)
        cancel_btn = discord.ui.Button(
            label="🚫 Cancel Match",
            style=discord.ButtonStyle.danger,
            row=3,
        )
        async def cancel(interaction: discord.Interaction):
            if not self.cog._is_participant(interaction.user, self.match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            await self.cog.cancel_match(interaction, self.match)
        cancel_btn.callback = cancel
        self.add_item(cancel_btn)


# ── Change Captain View ────────────────────────────────────────────────────────

class ChangeCaptainView(discord.ui.View):
    def __init__(self, cog: "AOEQueueCog", match: MatchState, team: int):
        super().__init__(timeout=60)
        self.cog   = cog
        self.match = match
        self.team  = team

        team_members = match.team1 if team == 1 else match.team2
        current_cap  = match.captain1 if team == 1 else match.captain2
        options      = [m for m in team_members if m != current_cap]

        if options:
            select = discord.ui.Select(
                placeholder=f"Select new Team {team} Captain...",
                options=[
                    discord.SelectOption(label=m.display_name, value=str(m.id))
                    for m in options
                ],
            )
            async def on_select(interaction: discord.Interaction):
                new_id  = int(select.values[0])
                new_cap = discord.utils.get(team_members, id=new_id)
                if not new_cap:
                    await interaction.response.send_message("❌ Player not found!", ephemeral=True)
                    return
                match.replace_captain(team, new_cap)
                # Rename thread to reflect new captains
                if match.thread:
                    try:
                        await match.thread.edit(name=match.thread_name())
                    except Exception:
                        pass
                await self.cog.show_draft(interaction, match)
                self.stop()
            select.callback = on_select
            self.add_item(select)

        back_btn = discord.ui.Button(label="↩️ Back", style=discord.ButtonStyle.secondary)
        async def back(interaction: discord.Interaction):
            await self.cog.show_draft(interaction, match)
            self.stop()
        back_btn.callback = back
        self.add_item(back_btn)


# ── Pre-match View ─────────────────────────────────────────────────────────────

class PreMatchView(discord.ui.View):
    def __init__(self, cog: "AOEQueueCog", match: MatchState):
        super().__init__(timeout=300)
        self.cog   = cog
        self.match = match

    @discord.ui.button(label="⚔️ Start Match", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_participant(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        self.match.phase = "in_match"
        await self.cog.show_in_match(interaction, self.match)
        self.stop()

    @discord.ui.button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_participant(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.cancel_match(interaction, self.match)
        self.stop()


# ── In-match View ──────────────────────────────────────────────────────────────

class InMatchView(discord.ui.View):
    def __init__(self, cog: "AOEQueueCog", match: MatchState):
        super().__init__(timeout=None)
        self.cog   = cog
        self.match = match

    @discord.ui.button(label="🏆 Team 1 Victory", style=discord.ButtonStyle.success)
    async def team1_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_participant(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.resolve_match(interaction, self.match, winner=1)
        self.stop()

    @discord.ui.button(label="🏆 Team 2 Victory", style=discord.ButtonStyle.primary)
    async def team2_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_participant(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.resolve_match(interaction, self.match, winner=2)
        self.stop()

    @discord.ui.button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_participant(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.cancel_match(interaction, self.match)
        self.stop()


# ── Main Cog ───────────────────────────────────────────────────────────────────

class AOEQueueCog(commands.Cog, name="AOEQueue"):

    def __init__(self, bot):
        self.bot = bot
        # {guild_id: {queue_type: [member, ...]}}
        self._queues: dict[int, dict[str, list[discord.Member]]] = {}
        # {guild_id: {queue_type: message}} — persistent queue embeds
        self._queue_messages: dict[int, dict[str, discord.Message]] = {}
        # {guild_id: [MatchState, ...]}
        self._matches: dict[int, list[MatchState]] = {}
        # Per-member lock to prevent duplicate queue joins
        self._processing: set[str] = set()

    def _is_admin(self, member: discord.Member) -> bool:
        return (member.guild_permissions.administrator or
                member.guild_permissions.manage_channels)

    def _is_participant(self, member: discord.Member, match: MatchState) -> bool:
        return self._is_admin(member) or member in match.all_players

    def _get_queue(self, guild_id: int, queue_type: str) -> list[discord.Member]:
        return self._queues.setdefault(guild_id, {}).setdefault(queue_type, [])

    def _get_matches(self, guild_id: int) -> list[MatchState]:
        return self._matches.setdefault(guild_id, [])

    # ── Channel / thread setup ─────────────────────────────────────────────────

    async def _get_or_create_channel(self, guild: discord.Guild, name: str,
                                      category=None, read_only: bool = True):
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch:
            return ch
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                add_reactions=False,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                manage_messages=True, embed_links=True,
                create_private_threads=True, manage_threads=True,
            ),
        }
        if not read_only:
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,   # members can't post, only use buttons
                add_reactions=False,
                use_application_commands=True,
            )
        try:
            ch = await guild.create_text_channel(name, overwrites=overwrites,
                                                  category=category)
            logger.info("[%s] Created #%s", guild.id, name)
        except discord.Forbidden:
            logger.error("[%s] Cannot create #%s — missing permission", guild.id, name)
            return None
        return ch

    async def _setup_channels(self, guild: discord.Guild):
        category = find_aoe_category(guild)
        gid      = guild.id
        self._queue_messages.setdefault(gid, {})

        for qtype, ch_name in QUEUE_CHANNEL_NAMES.items():
            ch = await self._get_or_create_channel(guild, ch_name, category, read_only=False)
            if ch:
                await self._post_queue_embed(guild, qtype, ch)

        for ch_name in list(LEADERBOARD_CHANNEL_NAMES.values()) + [MATCH_HISTORY_CHANNEL]:
            await self._get_or_create_channel(guild, ch_name, category, read_only=True)

    async def _create_match_thread(self, guild: discord.Guild,
                                    match: MatchState,
                                    queue_channel: discord.TextChannel) -> discord.Thread:
        """Create a private thread for the match inside the queue channel."""
        thread = await queue_channel.create_thread(
            name=match.thread_name(),
            type=discord.ChannelType.private_thread,
            auto_archive_duration=60,
            reason=f"AOE Match #{match.match_id}",
        )
        # Add all players to the thread
        for player in match.all_players:
            try:
                await thread.add_user(player)
            except Exception as e:
                logger.warning("[%s] Could not add %s to thread: %s",
                               guild.id, player.display_name, e)
        match.thread = thread
        logger.info("[%s] Created match thread: %s", guild.id, thread.name)
        return thread

    # ── Queue embed ────────────────────────────────────────────────────────────

    async def _post_queue_embed(self, guild: discord.Guild, queue_type: str,
                                 channel: discord.TextChannel = None):
        if channel is None:
            channel = discord.utils.get(
                guild.text_channels, name=QUEUE_CHANNEL_NAMES[queue_type])
        if not channel:
            return

        gid    = guild.id
        queue  = self._get_queue(gid, queue_type)
        cfg    = QUEUE_CONFIGS[queue_type]
        needed = cfg["size"]

        e = discord.Embed(
            title=f"⚔️ AOE 4 — {queue_type.upper()} Queue",
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(
            name=f"Players ({len(queue)}/{needed})",
            value="\n".join(f"• {m.display_name}" for m in queue) or "*Empty — be the first!*",
            inline=False,
        )
        e.set_footer(text=f"Need {needed - len(queue)} more player(s) to start")

        view = QueueView(self, queue_type)

        # Always delete old embed and repost at bottom so it stays prominent
        existing = self._queue_messages.get(gid, {}).get(queue_type)
        if existing:
            try:
                await existing.delete()
            except Exception:
                pass

        try:
            await channel.purge(limit=5, check=lambda m: m.author == guild.me)
        except Exception:
            pass

        msg = await channel.send(embed=e, view=view)
        self._queue_messages.setdefault(gid, {})[queue_type] = msg

    # ── Queue join / leave ─────────────────────────────────────────────────────

    async def handle_join(self, interaction: discord.Interaction, queue_type: str):
        await interaction.response.defer(ephemeral=True)
        guild  = interaction.guild
        member = interaction.user
        gid    = guild.id
        queue  = self._get_queue(gid, queue_type)

        if member in queue:
            await interaction.followup.send("⚠️ You're already in this queue!", ephemeral=True)
            return

        for qt, q in self._queues.get(gid, {}).items():
            if member in q and qt != queue_type:
                await interaction.followup.send(
                    f"⚠️ You're already in the **{qt.upper()}** queue! Leave it first.",
                    ephemeral=True)
                return

        for match in self._get_matches(gid):
            if member in match.all_players:
                await interaction.followup.send(
                    "⚠️ You're already in an active match!", ephemeral=True)
                return

        queue.append(member)
        logger.info("[%s] %s joined %s queue (%d/%d)",
                    gid, member.display_name, queue_type,
                    len(queue), QUEUE_CONFIGS[queue_type]["size"])

        await interaction.followup.send(
            f"✅ You joined the **{queue_type.upper()}** queue! "
            f"({len(queue)}/{QUEUE_CONFIGS[queue_type]['size']})",
            ephemeral=True)
        await self._post_queue_embed(guild, queue_type)

        if len(queue) >= QUEUE_CONFIGS[queue_type]["size"]:
            players = queue.copy()
            self._queues[gid][queue_type] = []
            await self._post_queue_embed(guild, queue_type)
            await self._start_match(guild, queue_type, players, interaction.channel)

    async def handle_leave(self, interaction: discord.Interaction, queue_type: str):
        await interaction.response.defer(ephemeral=True)
        guild  = interaction.guild
        member = interaction.user
        queue  = self._get_queue(guild.id, queue_type)

        if member not in queue:
            await interaction.followup.send("⚠️ You're not in this queue!", ephemeral=True)
            return

        queue.remove(member)
        await interaction.followup.send(
            f"✅ You left the **{queue_type.upper()}** queue.", ephemeral=True)
        await self._post_queue_embed(guild, queue_type)

    # ── Match start ────────────────────────────────────────────────────────────

    async def _start_match(self, guild: discord.Guild, queue_type: str,
                            players: list[discord.Member],
                            queue_channel: discord.TextChannel):
        match          = MatchState(queue_type, players)
        match.match_id = await db.create_aoe_match(
            str(guild.id), queue_type, [str(p.id) for p in players])
        self._get_matches(guild.id).append(match)

        # Create private thread
        thread = await self._create_match_thread(guild, match, queue_channel)

        # Ping all players in thread
        mentions = " ".join(p.mention for p in players)
        await thread.send(
            f"🎮 **Queue popped!** {mentions}\n"
            f"Your **{queue_type.upper()}** match is ready! All activity happens here."
        )

        if queue_type == "1v1":
            match.draft_complete = True
            match.phase          = "pre_match"
            await self._show_1v1_pre_match(guild, match, thread)
        else:
            await self._show_coin_flip(guild, match, thread)

    # ── 1v1 pre-match ──────────────────────────────────────────────────────────

    async def _show_1v1_pre_match(self, guild: discord.Guild,
                                   match: MatchState, thread: discord.Thread):
        gid  = str(guild.id)
        qt   = match.queue_type
        elo1 = (await db.get_aoe_stats(gid, str(match.team1[0].id), qt))["elo"]
        elo2 = (await db.get_aoe_stats(gid, str(match.team2[0].id), qt))["elo"]

        e = discord.Embed(
            title=f"⚔️ 1v1 Match Ready!",
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(
            name="🔴 Player 1",
            value=f"{match.team1[0].mention}\n{elo_bar(elo1)} ELO: **{elo1}**",
            inline=True,
        )
        e.add_field(name="VS", value="⚔️", inline=True)
        e.add_field(
            name="🔵 Player 2",
            value=f"{match.team2[0].mention}\n{elo_bar(elo2)} ELO: **{elo2}**",
            inline=True,
        )
        e.set_footer(text=f"Match ID: {match.match_id}")

        view = PreMatchView(self, match)
        msg  = await thread.send(embed=e, view=view)
        match.thread_message = msg

    # ── Coin flip ──────────────────────────────────────────────────────────────

    async def _show_coin_flip(self, guild: discord.Guild,
                               match: MatchState, thread: discord.Thread):
        flipper = match.captain1
        e = discord.Embed(
            title="🪙 Coin Flip!",
            description=(
                f"{flipper.mention} — you're flipping the coin!\n\n"
                f"**Team 1 Captain:** {match.captain1.mention}\n"
                f"**Team 2 Captain:** {match.captain2.mention}\n\n"
                f"Winner chooses **First Pick** or **Second Pick**."
            ),
            color=0xF1C40F,
            timestamp=datetime.now(timezone.utc),
        )
        view = CoinFlipView(self, match, flipper)
        msg  = await thread.send(embed=e, view=view)
        match.thread_message = msg

    async def resolve_flip(self, interaction: discord.Interaction,
                            match: MatchState, choice: str):
        result = random.choice(["heads", "tails"])
        won    = choice == result
        winner = match.captain1 if won else match.captain2

        e = discord.Embed(
            title=f"🪙 Coin landed on **{result.upper()}**!",
            description=(
                f"{winner.mention} **won the flip!**\n\n"
                f"Choose your pick order:"
            ),
            color=0xF1C40F,
            timestamp=datetime.now(timezone.utc),
        )
        view = FirstPickView(self, match, winner)
        await interaction.response.edit_message(embed=e, view=view)

    # ── Draft ──────────────────────────────────────────────────────────────────

    async def show_draft(self, interaction: discord.Interaction, match: MatchState):
        embed = await self._build_draft_embed(interaction.guild, match)
        view  = DraftView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _build_draft_embed(self, guild: discord.Guild,
                                  match: MatchState) -> discord.Embed:
        gid    = str(guild.id)
        qt     = match.queue_type
        picker = match.current_picker()

        e = discord.Embed(
            title=f"⚔️ {qt.upper()} Draft",
            color=0x3498DB,
            timestamp=datetime.now(timezone.utc),
        )

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain1 else ""
            t1_lines.append(
                f"{p.display_name}{cap_tag} — {elo_bar(stats['elo'])} **{stats['elo']}** ELO")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines) or "—", inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain2 else ""
            t2_lines.append(
                f"{p.display_name}{cap_tag} — {elo_bar(stats['elo'])} **{stats['elo']}** ELO")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines) or "—", inline=True)

        pool_lines = []
        for p in match.remaining:
            stats = await db.get_aoe_stats(gid, str(p.id), qt)
            pool_lines.append(
                f"{p.display_name} — {elo_bar(stats['elo'])} **{stats['elo']}** ELO")
        e.add_field(
            name="🎯 Player Pool",
            value="\n".join(pool_lines) or "All players drafted!",
            inline=False,
        )

        footer = f"👑 {picker.display_name}'s turn to pick" if picker else "Draft complete!"
        e.set_footer(text=f"{footer} | Match ID: {match.match_id}")
        return e

    async def show_change_captain(self, interaction: discord.Interaction,
                                   match: MatchState, team: int):
        e = discord.Embed(
            title=f"🔄 Change Team {team} Captain",
            description="Select a player from your team to become the new captain.",
            color=0x95A5A6,
        )
        view = ChangeCaptainView(self, match, team)
        await interaction.response.edit_message(embed=e, view=view)

    # ── Pre-match teams display ────────────────────────────────────────────────

    async def show_pre_match(self, interaction: discord.Interaction, match: MatchState):
        embed = await self._build_teams_embed(interaction.guild, match, phase="pre_match")
        view  = PreMatchView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    async def show_in_match(self, interaction: discord.Interaction, match: MatchState):
        embed = await self._build_teams_embed(interaction.guild, match, phase="in_match")
        view  = InMatchView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _build_teams_embed(self, guild: discord.Guild, match: MatchState,
                                  phase: str = "pre_match") -> discord.Embed:
        gid = str(guild.id)
        qt  = match.queue_type

        colors = {"pre_match": 0x2ECC71, "in_match": 0xE74C3C}
        titles = {
            "pre_match": f"✅ Teams Set — {qt.upper()}",
            "in_match":  f"⚔️ Match In Progress — {qt.upper()}",
        }
        e = discord.Embed(
            title=titles.get(phase, f"⚔️ {qt.upper()}"),
            color=colors.get(phase, 0xE67E22),
            timestamp=datetime.now(timezone.utc),
        )

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain1 else ""
            t1_lines.append(
                f"{p.display_name}{cap_tag} — {elo_bar(stats['elo'])} **{stats['elo']}**")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain2 else ""
            t2_lines.append(
                f"{p.display_name}{cap_tag} — {elo_bar(stats['elo'])} **{stats['elo']}**")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines), inline=True)
        e.set_footer(text=f"Match ID: {match.match_id}")
        return e

    # ── Resolve match ──────────────────────────────────────────────────────────

    async def resolve_match(self, interaction: discord.Interaction,
                             match: MatchState, winner: int):
        await interaction.response.defer()
        guild = interaction.guild
        gid   = str(guild.id)
        qt    = match.queue_type

        winning_team = match.team1 if winner == 1 else match.team2
        losing_team  = match.team2 if winner == 1 else match.team1

        for p in winning_team:
            await db.update_aoe_stats(gid, str(p.id), qt, result="win")
        for p in losing_team:
            await db.update_aoe_stats(gid, str(p.id), qt, result="loss")

        # Award cheese coins to winners
        for p in winning_team:
            await db.add_coins(gid, str(p.id), WIN_COINS)
        logger.info("[%s] Awarded %d 🧀 coins to %s winners of match #%s",
                    guild.id, WIN_COINS, qt, match.match_id)

        t1_ids = [str(p.id) for p in match.team1]
        t2_ids = [str(p.id) for p in match.team2]
        await db.finish_aoe_match(match.match_id, f"team{winner}", t1_ids, t2_ids)

        e = discord.Embed(
            title=f"🏆 Team {winner} Victory! — {qt.upper()}",
            color=0xFFD700,
            timestamp=datetime.now(timezone.utc),
        )

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            change  = f"+{ELO_CHANGE}" if winner == 1 else f"-{ELO_CHANGE}"
            cap_tag = " 👑" if p == match.captain1 else ""
            t1_lines.append(
                f"{p.display_name}{cap_tag} — **{stats['elo']}** ELO ({change})")
        e.add_field(
            name=f"{'🏆' if winner==1 else '💔'} Team 1",
            value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            change  = f"+{ELO_CHANGE}" if winner == 2 else f"-{ELO_CHANGE}"
            cap_tag = " 👑" if p == match.captain2 else ""
            t2_lines.append(
                f"{p.display_name}{cap_tag} — **{stats['elo']}** ELO ({change})")
        e.add_field(
            name=f"{'🏆' if winner==2 else '💔'} Team 2",
            value="\n".join(t2_lines), inline=True)

        e.add_field(
            name="🧀 Coin Reward",
            value=f"Winning team each received **{WIN_COINS} 🧀 Cheese Coins!**",
            inline=False,
        )
        e.set_footer(text=f"Match ID: {match.match_id} • Thread closes in {RESULT_DISPLAY_SECS}s")
        await interaction.edit_original_response(embed=e, view=None)

        await self._post_match_history(guild, match, result=f"Team {winner} Victory",
                                        winning_team=winning_team, losing_team=losing_team)
        await self._update_leaderboard(guild, qt)

        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        # Delete thread after 60s
        await asyncio.sleep(RESULT_DISPLAY_SECS)
        if match.thread:
            try:
                await match.thread.delete()
                logger.info("[%s] Deleted match thread for match #%s", guild.id, match.match_id)
            except Exception as ex:
                logger.warning("[%s] Could not delete thread: %s", guild.id, ex)

    async def cancel_match(self, interaction: discord.Interaction, match: MatchState):
        await interaction.response.defer()
        guild = interaction.guild
        gid   = str(guild.id)
        qt    = match.queue_type

        for p in match.all_players:
            await db.update_aoe_stats(gid, str(p.id), qt, result="no_result")

        t1_ids = [str(p.id) for p in match.team1]
        t2_ids = [str(p.id) for p in match.team2]
        await db.finish_aoe_match(match.match_id, "cancelled", t1_ids, t2_ids)

        e = discord.Embed(
            title=f"🚫 Match Cancelled — {qt.upper()}",
            description=f"This match has been cancelled. No ELO changes.\nThread closes in {RESULT_DISPLAY_SECS}s.",
            color=0x95A5A6,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_footer(text=f"Match ID: {match.match_id}")
        await interaction.edit_original_response(embed=e, view=None)

        await self._post_match_history(guild, match, result="Cancelled",
                                        winning_team=[], losing_team=[])

        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        await asyncio.sleep(RESULT_DISPLAY_SECS)
        if match.thread:
            try:
                await match.thread.delete()
            except Exception as ex:
                logger.warning("[%s] Could not delete thread: %s", guild.id, ex)

    # ── Match history ──────────────────────────────────────────────────────────

    async def _post_match_history(self, guild: discord.Guild, match: MatchState,
                                   result: str, winning_team: list, losing_team: list):
        ch = discord.utils.get(guild.text_channels, name=MATCH_HISTORY_CHANNEL)
        if not ch:
            return

        gid = str(guild.id)
        qt  = match.queue_type
        e   = discord.Embed(
            title=f"📜 Match #{match.match_id} — {qt.upper()} | {result}",
            color=0xFFD700 if "Victory" in result else 0x95A5A6,
            timestamp=datetime.now(timezone.utc),
        )

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain1 else ""
            tag     = "🏆" if p in winning_team else ("💔" if losing_team else "🚫")
            t1_lines.append(f"{tag} {p.display_name}{cap_tag} — **{stats['elo']}** ELO")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines) or "—", inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain2 else ""
            tag     = "🏆" if p in winning_team else ("💔" if losing_team else "🚫")
            t2_lines.append(f"{tag} {p.display_name}{cap_tag} — **{stats['elo']}** ELO")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines) or "—", inline=True)

        e.set_footer(text=f"Match ID: {match.match_id} • {guild.name}")
        try:
            await ch.send(embed=e)
        except Exception as ex:
            logger.error("[%s] Failed to post match history: %s", guild.id, ex)

    # ── Leaderboard ────────────────────────────────────────────────────────────

    async def _update_leaderboard(self, guild: discord.Guild, queue_type: str):
        ch_name = LEADERBOARD_CHANNEL_NAMES.get(queue_type)
        if not ch_name:
            return
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            return

        board = await db.get_aoe_leaderboard(str(guild.id), queue_type)
        now   = datetime.now(timezone.utc)

        if not board:
            e = discord.Embed(
                title=f"⚔️ AOE 4 — {queue_type.upper()} Leaderboard",
                description="No matches played yet!",
                color=0xE67E22, timestamp=now,
            )
        else:
            rows = []
            for i, row in enumerate(board):
                member  = guild.get_member(int(row["user_id"]))
                name    = member.display_name if member else f"Unknown ({row['user_id']})"
                total   = row["wins"] + row["losses"]
                win_pct = f"{(row['wins']/total*100):.1f}%" if total > 0 else "0%"
                medal   = ["🥇", "🥈", "🥉"][i] if i < 3 else f"`{i+1}.`"
                rows.append(
                    f"{medal} **{name}** — "
                    f"W:{row['wins']} L:{row['losses']} NR:{row['no_results']} "
                    f"WR:{win_pct} {elo_bar(row['elo'])}**{row['elo']}** ELO"
                )
            e = discord.Embed(
                title=f"⚔️ AOE 4 — {queue_type.upper()} Leaderboard",
                description="\n".join(rows),
                color=0xE67E22, timestamp=now,
            )
        e.set_footer(text=f"Updates after each match • {guild.name}")

        try:
            await ch.purge(limit=5, check=lambda m: m.author == guild.me)
            await ch.send(embed=e)
        except Exception as ex:
            logger.error("[%s] Failed to update leaderboard: %s", guild.id, ex)

    # ── on_ready ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._setup_channels(guild)

    # ── Admin commands ─────────────────────────────────────────────────────────

    @discord.app_commands.command(
        name="aoe_setup",
        description="Set up all AOE 4 queue channels (admin only).",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._setup_channels(interaction.guild)
        await interaction.followup.send("✅ AOE 4 queue channels set up!", ephemeral=True)

    @discord.app_commands.command(
        name="aoe_leaderboard",
        description="Refresh all AOE leaderboards (admin only).",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        for qt in QUEUE_CONFIGS:
            await self._update_leaderboard(interaction.guild, qt)
        await interaction.followup.send("✅ All AOE leaderboards refreshed!", ephemeral=True)

    @discord.app_commands.command(
        name="aoe_stats",
        description="Check AOE 4 queue stats.",
    )
    @discord.app_commands.describe(member="Member to check (leave blank for yourself)")
    async def aoe_stats(self, interaction: discord.Interaction,
                         member: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user
        gid    = str(interaction.guild.id)

        e = discord.Embed(
            title=f"⚔️ AOE 4 Stats — {target.display_name}",
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_thumbnail(url=target.display_avatar.url)

        for qt in QUEUE_CONFIGS:
            stats = await db.get_aoe_stats(gid, str(target.id), qt)
            total = stats["wins"] + stats["losses"]
            wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
            e.add_field(
                name=f"{qt.upper()}",
                value=(
                    f"{elo_bar(stats['elo'])} **{stats['elo']}** ELO\n"
                    f"W:{stats['wins']} L:{stats['losses']} NR:{stats['no_results']} WR:{wp}"
                ),
                inline=True,
            )
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="aoe_addwin",
        description="Add a win to a player's AOE stats (admin only).",
    )
    @discord.app_commands.describe(
        member="The player to add a win to",
        queue_type="Which queue type",
        amount="Number of wins to add (default 1)",
    )
    @discord.app_commands.choices(queue_type=[
        discord.app_commands.Choice(name="1v1", value="1v1"),
        discord.app_commands.Choice(name="2v2", value="2v2"),
        discord.app_commands.Choice(name="3v3", value="3v3"),
        discord.app_commands.Choice(name="4v4", value="4v4"),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_addwin(self, interaction: discord.Interaction,
                          member: discord.Member, queue_type: str, amount: int = 1):
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Amount must be positive!", ephemeral=True)
            return
        gid = str(interaction.guild.id)
        uid = str(member.id)
        for _ in range(amount):
            await db.update_aoe_stats(gid, uid, queue_type, "win")
        stats = await db.get_aoe_stats(gid, uid, queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Win Added", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player",   value=member.mention,     inline=True)
        e.add_field(name="🎮 Queue",    value=queue_type.upper(), inline=True)
        e.add_field(name="➕ Added",     value=f"{amount} win(s)", inline=True)
        e.add_field(name="🏆 Wins",     value=str(stats["wins"]), inline=True)
        e.add_field(name="💔 Losses",   value=str(stats["losses"]), inline=True)
        e.add_field(name="📊 Win Rate", value=wp,                 inline=True)
        e.add_field(name=f"{elo_bar(stats['elo'])} ELO", value=str(stats["elo"]), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild, queue_type)

    @discord.app_commands.command(
        name="aoe_removewin",
        description="Remove a win from a player's AOE stats (admin only).",
    )
    @discord.app_commands.describe(
        member="The player to remove a win from",
        queue_type="Which queue type",
        amount="Number of wins to remove (default 1)",
    )
    @discord.app_commands.choices(queue_type=[
        discord.app_commands.Choice(name="1v1", value="1v1"),
        discord.app_commands.Choice(name="2v2", value="2v2"),
        discord.app_commands.Choice(name="3v3", value="3v3"),
        discord.app_commands.Choice(name="4v4", value="4v4"),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_removewin(self, interaction: discord.Interaction,
                             member: discord.Member, queue_type: str, amount: int = 1):
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Amount must be positive!", ephemeral=True)
            return
        gid   = str(interaction.guild.id)
        uid   = str(member.id)
        stats = await db.get_aoe_stats(gid, uid, queue_type)
        if stats["wins"] == 0:
            await interaction.followup.send(
                f"❌ **{member.display_name}** has no wins to remove in {queue_type.upper()}!",
                ephemeral=True)
            return
        remove = min(amount, stats["wins"])
        await db.adjust_aoe_stats(gid, uid, queue_type,
                                   wins_delta=-remove, elo_delta=-(remove * ELO_CHANGE))
        stats = await db.get_aoe_stats(gid, uid, queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Win Removed", color=0xED4245,
                          timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player",   value=member.mention,      inline=True)
        e.add_field(name="🎮 Queue",    value=queue_type.upper(),  inline=True)
        e.add_field(name="➖ Removed",   value=f"{remove} win(s)",  inline=True)
        e.add_field(name="🏆 Wins",     value=str(stats["wins"]),  inline=True)
        e.add_field(name="💔 Losses",   value=str(stats["losses"]), inline=True)
        e.add_field(name="📊 Win Rate", value=wp,                  inline=True)
        e.add_field(name=f"{elo_bar(stats['elo'])} ELO", value=str(stats["elo"]), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild, queue_type)

    @discord.app_commands.command(
        name="aoe_addloss",
        description="Add a loss to a player's AOE stats (admin only).",
    )
    @discord.app_commands.describe(
        member="The player to add a loss to",
        queue_type="Which queue type",
        amount="Number of losses to add (default 1)",
    )
    @discord.app_commands.choices(queue_type=[
        discord.app_commands.Choice(name="1v1", value="1v1"),
        discord.app_commands.Choice(name="2v2", value="2v2"),
        discord.app_commands.Choice(name="3v3", value="3v3"),
        discord.app_commands.Choice(name="4v4", value="4v4"),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_addloss(self, interaction: discord.Interaction,
                           member: discord.Member, queue_type: str, amount: int = 1):
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Amount must be positive!", ephemeral=True)
            return
        gid = str(interaction.guild.id)
        uid = str(member.id)
        for _ in range(amount):
            await db.update_aoe_stats(gid, uid, queue_type, "loss")
        stats = await db.get_aoe_stats(gid, uid, queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Loss Added", color=0xED4245,
                          timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player",   value=member.mention,       inline=True)
        e.add_field(name="🎮 Queue",    value=queue_type.upper(),   inline=True)
        e.add_field(name="➕ Added",     value=f"{amount} loss(es)", inline=True)
        e.add_field(name="🏆 Wins",     value=str(stats["wins"]),   inline=True)
        e.add_field(name="💔 Losses",   value=str(stats["losses"]), inline=True)
        e.add_field(name="📊 Win Rate", value=wp,                   inline=True)
        e.add_field(name=f"{elo_bar(stats['elo'])} ELO", value=str(stats["elo"]), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild, queue_type)

    @discord.app_commands.command(
        name="aoe_removeloss",
        description="Remove a loss from a player's AOE stats (admin only).",
    )
    @discord.app_commands.describe(
        member="The player to remove a loss from",
        queue_type="Which queue type",
        amount="Number of losses to remove (default 1)",
    )
    @discord.app_commands.choices(queue_type=[
        discord.app_commands.Choice(name="1v1", value="1v1"),
        discord.app_commands.Choice(name="2v2", value="2v2"),
        discord.app_commands.Choice(name="3v3", value="3v3"),
        discord.app_commands.Choice(name="4v4", value="4v4"),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_removeloss(self, interaction: discord.Interaction,
                              member: discord.Member, queue_type: str, amount: int = 1):
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Amount must be positive!", ephemeral=True)
            return
        gid   = str(interaction.guild.id)
        uid   = str(member.id)
        stats = await db.get_aoe_stats(gid, uid, queue_type)
        if stats["losses"] == 0:
            await interaction.followup.send(
                f"❌ **{member.display_name}** has no losses to remove in {queue_type.upper()}!",
                ephemeral=True)
            return
        remove = min(amount, stats["losses"])
        await db.adjust_aoe_stats(gid, uid, queue_type,
                                   losses_delta=-remove, elo_delta=(remove * ELO_CHANGE))
        stats = await db.get_aoe_stats(gid, uid, queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Loss Removed", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player",   value=member.mention,        inline=True)
        e.add_field(name="🎮 Queue",    value=queue_type.upper(),    inline=True)
        e.add_field(name="➖ Removed",   value=f"{remove} loss(es)",  inline=True)
        e.add_field(name="🏆 Wins",     value=str(stats["wins"]),    inline=True)
        e.add_field(name="💔 Losses",   value=str(stats["losses"]),  inline=True)
        e.add_field(name="📊 Win Rate", value=wp,                    inline=True)
        e.add_field(name=f"{elo_bar(stats['elo'])} ELO", value=str(stats["elo"]), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild, queue_type)

    @discord.app_commands.command(
        name="aoe_resetstats",
        description="Reset a player's AOE stats (admin only).",
    )
    @discord.app_commands.describe(
        member="The player to reset",
        queue_type="Which queue type to reset",
    )
    @discord.app_commands.choices(queue_type=[
        discord.app_commands.Choice(name="1v1", value="1v1"),
        discord.app_commands.Choice(name="2v2", value="2v2"),
        discord.app_commands.Choice(name="3v3", value="3v3"),
        discord.app_commands.Choice(name="4v4", value="4v4"),
        discord.app_commands.Choice(name="All queues", value="all"),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_resetstats(self, interaction: discord.Interaction,
                              member: discord.Member, queue_type: str):
        await interaction.response.defer(ephemeral=True)
        gid    = str(interaction.guild.id)
        uid    = str(member.id)
        queues = list(QUEUE_CONFIGS.keys()) if queue_type == "all" else [queue_type]
        for qt in queues:
            await db.reset_aoe_stats(gid, uid, qt)
        label = "all queues" if queue_type == "all" else queue_type.upper()
        e = discord.Embed(title="✅ AOE Stats Reset", color=0xFEE75C,
                          timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player", value=member.mention, inline=True)
        e.add_field(name="🎮 Queue",  value=label,          inline=True)
        e.add_field(name="🔄 Reset",  value="W:0 L:0 NR:0 ELO:1000", inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        for qt in queues:
            await self._update_leaderboard(interaction.guild, qt)


async def setup(bot):
    await bot.add_cog(AOEQueueCog(bot))
    logger.info("AOEQueueCog loaded.")
