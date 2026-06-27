"""
cogs/aoe_queue.py — AOE 4 Queue System with Civ Selection
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

MATCH_HISTORY_CHANNEL = "aoe-match-history"
DEFAULT_ELO           = 1000
ELO_CHANGE            = 25
AOE_CATEGORY_KEYWORD  = "AGE OF EMPIRES"
RESULT_DISPLAY_SECS   = 60
WIN_COINS             = 5

QUEUE_TIMEOUT_SECS = 1800   # 30 minutes — auto-remove from queue if not filled

AOE_CIVS = [
    "Chinese", "Jin Dynasty", "Zhu Xi's Legacy",
    "Abbasid Dynasty", "Ayyubids",
    "Byzantines", "Macedonian Dynasty",
    "Delhi Sultanate", "Tughlaq Dynasty",
    "English", "House of Lancaster",
    "French", "Jeanne d'Arc", "Templar Knights",
    "Holy Roman Empire", "Order of the Dragon",
    "Japanese", "Sengoku Daimyo",
    "Malians", "Mongols", "Golden Horde",
    "Ottomans", "Rus",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def find_aoe_category(guild: discord.Guild):
    return discord.utils.find(
        lambda c: AOE_CATEGORY_KEYWORD in c.name.upper(), guild.categories)

def elo_bar(elo: int) -> str:
    if elo >= 1200: return "🔥"
    if elo >= 1100: return "⭐"
    if elo >= 1000: return "🟢"
    if elo >= 900:  return "🟡"
    return "🔴"

# ── Match State ────────────────────────────────────────────────────────────────

class MatchState:
    def __init__(self, queue_type: str, players: list):
        cfg              = QUEUE_CONFIGS[queue_type]
        self.queue_type  = queue_type
        self.team_size   = cfg["team_size"]
        self.pick_order  = cfg["pick_order"]
        self.all_players = players.copy()
        self.match_id    = None
        self.thread      = None
        self.thread_message = None

        shuffled = players.copy()
        random.shuffle(shuffled)
        self.captain1 = shuffled[0]
        self.captain2 = shuffled[1]

        self.team1 = [self.captain1]
        self.team2 = [self.captain2]
        self.remaining = [p for p in players if p not in (self.captain1, self.captain2)]

        self.pick_step       = 0
        self.draft_complete  = False
        self.coin_flip_done  = False
        self.first_pick_team = None
        self.phase           = "coin_flip"

        # Civ selection state
        # {member_id: civ_name} — private, not revealed until both lock in
        self.civ_picks: dict[int, str] = {}
        # Which captains have locked in
        self.cap1_locked = False
        self.cap2_locked = False

    @property
    def civs_revealed(self) -> bool:
        return self.cap1_locked and self.cap2_locked

    def all_picked_civs(self, team: list) -> bool:
        """Check if every player in a team has picked a civ."""
        return all(p.id in self.civ_picks for p in team)

    def thread_name(self) -> str:
        if self.queue_type == "1v1":
            return f"⚔️ 1v1 Match #{self.match_id} — {self.captain1.display_name} vs {self.captain2.display_name}"
        return f"⚔️ {self.queue_type.upper()} Match #{self.match_id} — {self.captain1.display_name}'s Team vs {self.captain2.display_name}'s Team"

    def current_picker(self):
        if self.pick_step >= len(self.pick_order):
            return None
        team_idx = self.pick_order[self.pick_step]
        if self.first_pick_team == 2:
            team_idx = 1 - team_idx
        return self.captain1 if team_idx == 0 else self.captain2

    def pick_player(self, player):
        picker = self.current_picker()
        if picker == self.captain1:
            self.team1.append(player)
        else:
            self.team2.append(player)
        self.remaining.remove(player)
        self.pick_step += 1
        if not self.remaining:
            self.draft_complete = True
            self.phase = "civ_select"

    def replace_captain(self, team: int, new_captain):
        if team == 1:
            old = self.captain1
            self.captain1 = new_captain
            if new_captain in self.team1:
                self.team1.remove(new_captain)
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

    def team_of(self, member) -> int:
        if member in self.team1: return 1
        if member in self.team2: return 2
        return None


# ── Queue View ─────────────────────────────────────────────────────────────────

class QueueView(discord.ui.View):
    def __init__(self, cog, queue_type: str):
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


# ── Coin Flip View ─────────────────────────────────────────────────────────────

class CoinFlipView(discord.ui.View):
    def __init__(self, cog, match: MatchState, flipper):
        super().__init__(timeout=120)
        self.cog     = cog
        self.match   = match
        self.flipper = flipper

    @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.primary)
    async def heads(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id:
            await interaction.response.send_message("❌ Only the coin flipper can choose!", ephemeral=True)
            return
        await self.cog.resolve_flip(interaction, self.match, "heads")
        self.stop()

    @discord.ui.button(label="🪙 Tails", style=discord.ButtonStyle.primary)
    async def tails(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id:
            await interaction.response.send_message("❌ Only the coin flipper can choose!", ephemeral=True)
            return
        await self.cog.resolve_flip(interaction, self.match, "tails")
        self.stop()


# ── First Pick View ────────────────────────────────────────────────────────────

class FirstPickView(discord.ui.View):
    def __init__(self, cog, match: MatchState, winner):
        super().__init__(timeout=120)
        self.cog    = cog
        self.match  = match
        self.winner = winner

    @discord.ui.button(label="⚡ First Pick", style=discord.ButtonStyle.success)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.winner.id:
            await interaction.response.send_message("❌ Only the flip winner can choose!", ephemeral=True)
            return
        self.match.first_pick_team = self.match.team_of(self.winner)
        self.match.phase = "draft"
        await self.cog.show_draft(interaction, self.match)
        self.stop()

    @discord.ui.button(label="🛡️ Second Pick", style=discord.ButtonStyle.secondary)
    async def second(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.winner.id:
            await interaction.response.send_message("❌ Only the flip winner can choose!", ephemeral=True)
            return
        team = self.match.team_of(self.winner)
        self.match.first_pick_team = 2 if team == 1 else 1
        self.match.phase = "draft"
        await self.cog.show_draft(interaction, self.match)
        self.stop()


# ── Draft View ─────────────────────────────────────────────────────────────────

class DraftView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=120)
        self.cog   = cog
        self.match = match
        self._build()

    def _build(self):
        self.clear_items()
        # Pick buttons on rows 0 and 1
        for i, player in enumerate(self.match.remaining):
            btn = discord.ui.Button(
                label=player.display_name,
                style=discord.ButtonStyle.primary,
                row=min(i // 4, 1),
            )
            async def callback(interaction: discord.Interaction, p=player):
                if interaction.user.id != self.match.current_picker().id:
                    await interaction.response.send_message("❌ It's not your turn to pick!", ephemeral=True)
                    return
                self.match.pick_player(p)
                if self.match.draft_complete:
                    await self.cog.show_civ_select(interaction, self.match)
                else:
                    await self.cog.show_draft(interaction, self.match)
            btn.callback = callback
            self.add_item(btn)

        # Calculate rows dynamically — must be sequential from 0
        # If there are no pick buttons, captain buttons go on row 0
        # If pick buttons used rows 0-1, captain buttons go on row 2
        n = len(self.match.remaining)
        cap_row    = min(max((n + 3) // 4, 1), 2)  # 0 if no picks, else 2
        cancel_row = min(cap_row + 1, 4)

        cap1_btn = discord.ui.Button(
            label="🔄 Change Team 1 Captain",
            style=discord.ButtonStyle.secondary,
            row=cap_row)
        async def change_cap1(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain1.id:
                await interaction.response.send_message("❌ Only Team 1's captain can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain(interaction, self.match, team=1)
        cap1_btn.callback = change_cap1
        self.add_item(cap1_btn)

        cap2_btn = discord.ui.Button(
            label="🔄 Change Team 2 Captain",
            style=discord.ButtonStyle.secondary,
            row=cap_row)
        async def change_cap2(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain2.id:
                await interaction.response.send_message("❌ Only Team 2's captain can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain(interaction, self.match, team=2)
        cap2_btn.callback = change_cap2
        self.add_item(cap2_btn)

        cancel_btn = discord.ui.Button(
            label="🚫 Cancel Match",
            style=discord.ButtonStyle.danger,
            row=cancel_row)
        async def cancel(interaction: discord.Interaction):
            if not self.cog._is_captain_or_admin(interaction.user, self.match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            await self.cog.cancel_match(interaction, self.match)
        cancel_btn.callback = cancel
        self.add_item(cancel_btn)


# ── Change Captain View ────────────────────────────────────────────────────────

class ChangeCaptainView(discord.ui.View):
    def __init__(self, cog, match: MatchState, team: int):
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
                options=[discord.SelectOption(label=m.display_name, value=str(m.id)) for m in options],
            )
            async def on_select(interaction: discord.Interaction):
                new_id  = int(select.values[0])
                new_cap = discord.utils.get(team_members, id=new_id)
                if not new_cap:
                    await interaction.response.send_message("❌ Player not found!", ephemeral=True)
                    return
                match.replace_captain(team, new_cap)
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


# ── Civ Select View ────────────────────────────────────────────────────────────

class CivSelectView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=300)
        self.cog   = cog
        self.match = match
        self._build()

    def _build(self):
        self.clear_items()

        # Civ picker dropdown — ephemeral, any player can use
        civ_select = discord.ui.Select(
            placeholder="🎭 Pick your civilization...",
            options=[discord.SelectOption(label=civ, value=civ) for civ in AOE_CIVS],
            row=0,
        )
        async def on_civ_select(interaction: discord.Interaction):
            if interaction.user not in self.match.all_players:
                await interaction.response.send_message("❌ You're not in this match!", ephemeral=True)
                return
            chosen = civ_select.values[0]
            self.match.civ_picks[interaction.user.id] = chosen
            await interaction.response.send_message(
                f"✅ You picked **{chosen}**! You can change it anytime before lock-in.",
                ephemeral=True)
            # Refresh the public civ status embed
            await self.cog._refresh_civ_status(self.match)
        civ_select.callback = on_civ_select
        self.add_item(civ_select)

        # Lock in button — Team 1 captain
        lock1_btn = discord.ui.Button(
            label="🔒 Lock In Civs (Team 1)" if not self.match.cap1_locked else "✅ Team 1 Locked",
            style=discord.ButtonStyle.success if not self.match.cap1_locked else discord.ButtonStyle.secondary,
            disabled=self.match.cap1_locked,
            row=1,
        )
        async def lock1(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain1.id:
                await interaction.response.send_message("❌ Only Team 1's captain can lock in!", ephemeral=True)
                return
            if not self.match.all_picked_civs(self.match.team1):
                missing = [p.display_name for p in self.match.team1 if p.id not in self.match.civ_picks]
                await interaction.response.send_message(
                    f"❌ These Team 1 players haven't picked a civ yet: **{', '.join(missing)}**",
                    ephemeral=True)
                return
            self.match.cap1_locked = True
            await interaction.response.send_message("✅ Team 1 civs locked in!", ephemeral=True)
            await self.cog._refresh_civ_status(self.match)
            if self.match.civs_revealed:
                await self.cog._reveal_civs(self.match)
        lock1_btn.callback = lock1
        self.add_item(lock1_btn)

        # Lock in button — Team 2 captain
        lock2_btn = discord.ui.Button(
            label="🔒 Lock In Civs (Team 2)" if not self.match.cap2_locked else "✅ Team 2 Locked",
            style=discord.ButtonStyle.primary if not self.match.cap2_locked else discord.ButtonStyle.secondary,
            disabled=self.match.cap2_locked,
            row=1,
        )
        async def lock2(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain2.id:
                await interaction.response.send_message("❌ Only Team 2's captain can lock in!", ephemeral=True)
                return
            if not self.match.all_picked_civs(self.match.team2):
                missing = [p.display_name for p in self.match.team2 if p.id not in self.match.civ_picks]
                await interaction.response.send_message(
                    f"❌ These Team 2 players haven't picked a civ yet: **{', '.join(missing)}**",
                    ephemeral=True)
                return
            self.match.cap2_locked = True
            await interaction.response.send_message("✅ Team 2 civs locked in!", ephemeral=True)
            await self.cog._refresh_civ_status(self.match)
            if self.match.civs_revealed:
                await self.cog._reveal_civs(self.match)
        lock2_btn.callback = lock2
        self.add_item(lock2_btn)

        # Cancel match
        cancel_btn = discord.ui.Button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger, row=2)
        async def cancel(interaction: discord.Interaction):
            if not self.cog._is_captain_or_admin(interaction.user, self.match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            await self.cog.cancel_match(interaction, self.match)
        cancel_btn.callback = cancel
        self.add_item(cancel_btn)


# ── Pre-match View ─────────────────────────────────────────────────────────────

class PreMatchView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=300)
        self.cog   = cog
        self.match = match

    @discord.ui.button(label="⚔️ Start Match", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        self.match.phase = "in_match"
        await self.cog.show_in_match(interaction, self.match)
        self.stop()

    @discord.ui.button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.cancel_match(interaction, self.match)
        self.stop()


# ── In-match View ──────────────────────────────────────────────────────────────

class InMatchView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=None)
        self.cog   = cog
        self.match = match

    @discord.ui.button(label="🏆 Team 1 Victory", style=discord.ButtonStyle.success)
    async def team1_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.resolve_match(interaction, self.match, winner=1)
        self.stop()

    @discord.ui.button(label="🏆 Team 2 Victory", style=discord.ButtonStyle.primary)
    async def team2_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.resolve_match(interaction, self.match, winner=2)
        self.stop()

    @discord.ui.button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.cancel_match(interaction, self.match)
        self.stop()


# ── Main Cog ───────────────────────────────────────────────────────────────────

class AOEQueueCog(commands.Cog, name="AOEQueue"):

    def __init__(self, bot):
        self.bot = bot
        self._queues: dict        = {}
        self._queue_messages: dict = {}
        self._matches: dict       = {}
        # {guild_id: {queue_type: {member_id: asyncio.Task}}}
        self._queue_timers: dict  = {}

    def _is_admin(self, member) -> bool:
        return member.guild_permissions.administrator or member.guild_permissions.manage_channels

    def _is_captain_or_admin(self, member, match: MatchState) -> bool:
        return self._is_admin(member) or member == match.captain1 or member == match.captain2

    def _get_queue(self, guild_id: int, queue_type: str) -> list:
        return self._queues.setdefault(guild_id, {}).setdefault(queue_type, [])

    def _get_matches(self, guild_id: int) -> list:
        return self._matches.setdefault(guild_id, [])

    def _get_timers(self, guild_id: int, queue_type: str) -> dict:
        return self._queue_timers.setdefault(guild_id, {}).setdefault(queue_type, {})

    def _cancel_timer(self, guild_id: int, queue_type: str, member_id: int):
        timer = self._get_timers(guild_id, queue_type).pop(member_id, None)
        if timer and not timer.done():
            timer.cancel()

    def _start_timer(self, guild: discord.Guild, queue_type: str, member: discord.Member):
        self._cancel_timer(guild.id, queue_type, member.id)
        task = asyncio.create_task(
            self._queue_timeout(guild, queue_type, member))
        self._get_timers(guild.id, queue_type)[member.id] = task

    async def _queue_timeout(self, guild: discord.Guild, queue_type: str, member: discord.Member):
        await asyncio.sleep(QUEUE_TIMEOUT_SECS)
        queue = self._get_queue(guild.id, queue_type)
        if member not in queue:
            return   # already popped or left

        queue.remove(member)
        self._get_timers(guild.id, queue_type).pop(member.id, None)
        logger.info("[%s] %s auto-removed from %s queue (timeout)",
                    guild.id, member.display_name, queue_type)

        # Refresh queue embed
        await self._post_queue_embed(guild, queue_type)

        # DM the player
        try:
            await member.send(
                f"⏰ **Queue Timeout** — You were automatically removed from the "
                f"**{queue_type.upper()} AOE queue** in **{guild.name}** "
                f"because the queue didn't fill within 30 minutes. "
                f"Rejoin anytime you're ready!"
            )
        except Exception:
            pass   # DMs may be closed

    def _find_match_by_id(self, guild, match_id: int):
        for m in self._get_matches(guild.id):
            if m.match_id == match_id:
                return m
        return None

    # ── Channel setup ──────────────────────────────────────────────────────────

    async def _get_or_create_channel(self, guild, name: str, category=None, read_only: bool = True):
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch:
            return ch
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, add_reactions=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
                embed_links=True, create_public_threads=True, manage_threads=True),
        }
        try:
            ch = await guild.create_text_channel(name, overwrites=overwrites, category=category)
            logger.info("[%s] Created #%s", guild.id, name)
        except discord.Forbidden:
            logger.error("[%s] Cannot create #%s", guild.id, name)
            return None
        return ch

    async def _setup_channels(self, guild):
        category = find_aoe_category(guild)
        gid      = guild.id
        self._queue_messages.setdefault(gid, {})
        for qtype, ch_name in QUEUE_CHANNEL_NAMES.items():
            ch = await self._get_or_create_channel(guild, ch_name, category, read_only=False)
            if ch:
                await self._post_queue_embed(guild, qtype, ch)
        for ch_name in list(LEADERBOARD_CHANNEL_NAMES.values()) + [MATCH_HISTORY_CHANNEL]:
            await self._get_or_create_channel(guild, ch_name, category, read_only=True)

    async def _create_match_thread(self, guild, match: MatchState, queue_channel):
        thread = await queue_channel.create_thread(
            name=match.thread_name(),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
            reason=f"AOE Match #{match.match_id}",
        )
        for player in match.all_players:
            try:
                await thread.add_user(player)
            except Exception as e:
                logger.warning("[%s] Could not add %s to thread: %s", guild.id, player.display_name, e)
        match.thread = thread
        return thread

    # ── Queue embed ────────────────────────────────────────────────────────────

    async def _post_queue_embed(self, guild, queue_type: str, channel=None):
        if channel is None:
            channel = discord.utils.get(guild.text_channels, name=QUEUE_CHANNEL_NAMES[queue_type])
        if not channel:
            return

        gid    = guild.id
        queue  = self._get_queue(gid, queue_type)
        needed = QUEUE_CONFIGS[queue_type]["size"]

        e = discord.Embed(title=f"⚔️ AOE 4 — {queue_type.upper()} Queue",
                          color=0xE67E22, timestamp=datetime.now(timezone.utc))
        e.add_field(
            name=f"Players ({len(queue)}/{needed})",
            value="\n".join(f"• {m.display_name}" for m in queue) or "*Empty — be the first!*",
            inline=False)
        e.set_footer(text=f"Need {needed - len(queue)} more player(s) to start")

        view     = QueueView(self, queue_type)
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
                    f"⚠️ You're already in the **{qt.upper()}** queue! Leave it first.", ephemeral=True)
                return
        for match in self._get_matches(gid):
            if member in match.all_players:
                await interaction.followup.send("⚠️ You're already in an active match!", ephemeral=True)
                return

        queue.append(member)
        self._start_timer(guild, queue_type, member)
        await interaction.followup.send(
            f"✅ You joined the **{queue_type.upper()}** queue! ({len(queue)}/{QUEUE_CONFIGS[queue_type]['size']})\n"
            f"⏰ You'll be auto-removed in **30 minutes** if the queue doesn't fill.",
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
        self._cancel_timer(guild.id, queue_type, member.id)
        await interaction.followup.send(f"✅ You left the **{queue_type.upper()}** queue.", ephemeral=True)
        await self._post_queue_embed(guild, queue_type)

    # ── Match start ────────────────────────────────────────────────────────────

    async def _start_match(self, guild, queue_type: str, players: list, queue_channel):
        match          = MatchState(queue_type, players)
        match.match_id = await db.create_aoe_match(str(guild.id), queue_type, [str(p.id) for p in players])
        self._get_matches(guild.id).append(match)

        # Cancel queue timers for all matched players
        for p in players:
            self._cancel_timer(guild.id, queue_type, p.id)

        thread   = await self._create_match_thread(guild, match, queue_channel)
        mentions = " ".join(p.mention for p in players)
        await thread.send(
            f"🎮 **Queue popped!** {mentions}\n"
            f"Your **{queue_type.upper()}** match is ready — Match ID: **#{match.match_id}**")

        if queue_type == "1v1":
            match.draft_complete = True
            match.phase = "civ_select"
            await self._show_civ_select_fresh(guild, match, thread)
        else:
            await self._show_coin_flip(guild, match, thread)

    # ── Coin flip ──────────────────────────────────────────────────────────────

    async def _show_coin_flip(self, guild, match: MatchState, thread):
        e = discord.Embed(
            title="🪙 Coin Flip!",
            description=(
                f"{match.captain1.mention} — you're flipping the coin!\n\n"
                f"**Team 1 Captain:** {match.captain1.mention}\n"
                f"**Team 2 Captain:** {match.captain2.mention}\n\n"
                f"Winner chooses **First Pick** or **Second Pick**."
            ),
            color=0xF1C40F, timestamp=datetime.now(timezone.utc))
        view = CoinFlipView(self, match, match.captain1)
        msg  = await thread.send(embed=e, view=view)
        match.thread_message = msg

    async def resolve_flip(self, interaction: discord.Interaction, match: MatchState, choice: str):
        result = random.choice(["heads", "tails"])
        won    = choice == result
        winner = match.captain1 if won else match.captain2
        e = discord.Embed(
            title=f"🪙 Coin landed on **{result.upper()}**!",
            description=f"{winner.mention} **won the flip!**\n\nChoose your pick order:",
            color=0xF1C40F, timestamp=datetime.now(timezone.utc))
        view = FirstPickView(self, match, winner)
        await interaction.response.edit_message(embed=e, view=view)

    # ── Draft ──────────────────────────────────────────────────────────────────

    async def show_draft(self, interaction: discord.Interaction, match: MatchState):
        embed = await self._build_draft_embed(interaction.guild, match)
        view  = DraftView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _build_draft_embed(self, guild, match: MatchState) -> discord.Embed:
        gid    = str(guild.id)
        qt     = match.queue_type
        picker = match.current_picker()
        e = discord.Embed(title=f"⚔️ {qt.upper()} Draft", color=0x3498DB,
                          timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain1 else ""
            t1_lines.append(f"{p.display_name}{cap_tag} — {elo_bar(stats['elo'])} **{stats['elo']}** ELO")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines) or "—", inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain2 else ""
            t2_lines.append(f"{p.display_name}{cap_tag} — {elo_bar(stats['elo'])} **{stats['elo']}** ELO")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines) or "—", inline=True)

        pool_lines = []
        for p in match.remaining:
            stats = await db.get_aoe_stats(gid, str(p.id), qt)
            pool_lines.append(f"{p.display_name} — {elo_bar(stats['elo'])} **{stats['elo']}** ELO")
        e.add_field(name="🎯 Player Pool", value="\n".join(pool_lines) or "All players drafted!", inline=False)

        footer = f"👑 {picker.display_name}'s turn to pick" if picker else "Draft complete!"
        e.set_footer(text=f"{footer} | Match #{match.match_id}")
        return e

    async def show_change_captain(self, interaction: discord.Interaction, match: MatchState, team: int):
        e = discord.Embed(title=f"🔄 Change Team {team} Captain",
                          description="Select a player from your team to become the new captain.",
                          color=0x95A5A6)
        view = ChangeCaptainView(self, match, team)
        await interaction.response.edit_message(embed=e, view=view)

    # ── Civ Selection ──────────────────────────────────────────────────────────

    async def _show_civ_select_fresh(self, guild, match: MatchState, thread):
        """Post a fresh civ selection message in the thread (no interaction)."""
        match.phase = "civ_select"
        embed = self._build_civ_status_embed(guild, match)
        view  = CivSelectView(self, match)
        msg   = await thread.send(embed=embed, view=view)
        match.thread_message = msg

    async def show_civ_select(self, interaction: discord.Interaction, match: MatchState):
        """Edit existing message to civ selection (called from draft)."""
        match.phase = "civ_select"
        embed = self._build_civ_status_embed(interaction.guild, match)
        view  = CivSelectView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    def _build_civ_status_embed(self, guild, match: MatchState) -> discord.Embed:
        e = discord.Embed(
            title=f"🎭 Civilization Selection — {match.queue_type.upper()}",
            description=(
                "Each player must pick their civilization using the dropdown below.\n"
                "You can change your pick anytime before your captain locks in.\n"
                "**Civs are hidden until both captains lock in!**"
            ),
            color=0x9B59B6,
            timestamp=datetime.now(timezone.utc),
        )

        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            picked  = "✅ Ready" if p.id in match.civ_picks else "⏳ Picking..."
            locked  = " 🔒" if match.cap1_locked else ""
            t1_lines.append(f"{p.display_name}{cap_tag}{locked} — {picked}")
        e.add_field(
            name=f"🔴 Team 1 {'✅ Locked' if match.cap1_locked else '⏳ Picking'}",
            value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            picked  = "✅ Ready" if p.id in match.civ_picks else "⏳ Picking..."
            locked  = " 🔒" if match.cap2_locked else ""
            t2_lines.append(f"{p.display_name}{cap_tag}{locked} — {picked}")
        e.add_field(
            name=f"🔵 Team 2 {'✅ Locked' if match.cap2_locked else '⏳ Picking'}",
            value="\n".join(t2_lines), inline=True)

        e.set_footer(text=f"Match #{match.match_id} • Civs revealed when both captains lock in")
        return e

    async def _refresh_civ_status(self, match: MatchState):
        """Refresh the civ selection embed in the thread without an interaction."""
        if not match.thread_message or not match.thread:
            return
        try:
            embed = self._build_civ_status_embed(match.thread.guild, match)
            view  = CivSelectView(self, match)
            await match.thread_message.edit(embed=embed, view=view)
        except Exception as ex:
            logger.warning("[%s] Could not refresh civ status: %s",
                           match.thread.guild.id if match.thread else "?", ex)

    async def _reveal_civs(self, match: MatchState):
        """Both captains locked in — reveal all civs and show pre-match buttons."""
        if not match.thread:
            return
        guild = match.thread.guild
        gid   = str(guild.id)
        qt    = match.queue_type

        e = discord.Embed(
            title=f"🎭 Civilizations Revealed! — {qt.upper()}",
            color=0x9B59B6,
            timestamp=datetime.now(timezone.utc),
        )

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = match.civ_picks.get(p.id, "Unknown")
            t1_lines.append(f"{p.display_name}{cap_tag} — **{civ}** | {elo_bar(stats['elo'])} {stats['elo']} ELO")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = match.civ_picks.get(p.id, "Unknown")
            t2_lines.append(f"{p.display_name}{cap_tag} — **{civ}** | {elo_bar(stats['elo'])} {stats['elo']} ELO")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines), inline=True)

        e.set_footer(text=f"Match #{match.match_id} • Both teams locked in — ready to play!")

        match.phase = "pre_match"
        view = PreMatchView(self, match)
        try:
            await match.thread_message.edit(embed=e, view=view)
        except Exception as ex:
            msg = await match.thread.send(embed=e, view=view)
            match.thread_message = msg

    # ── Pre-match / In-match ───────────────────────────────────────────────────

    async def show_pre_match(self, interaction: discord.Interaction, match: MatchState):
        embed = await self._build_teams_embed(interaction.guild, match, phase="pre_match")
        view  = PreMatchView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    async def show_in_match(self, interaction: discord.Interaction, match: MatchState):
        embed = await self._build_teams_embed(interaction.guild, match, phase="in_match")
        view  = InMatchView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _build_teams_embed(self, guild, match: MatchState, phase: str = "pre_match") -> discord.Embed:
        gid    = str(guild.id)
        qt     = match.queue_type
        colors = {"pre_match": 0x2ECC71, "in_match": 0xE74C3C}
        titles = {"pre_match": f"✅ Teams Set — {qt.upper()}", "in_match": f"⚔️ Match In Progress — {qt.upper()}"}
        e = discord.Embed(title=titles.get(phase, f"⚔️ {qt.upper()}"),
                          color=colors.get(phase, 0xE67E22), timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = f" — **{match.civ_picks[p.id]}**" if p.id in match.civ_picks else ""
            t1_lines.append(f"{p.display_name}{cap_tag}{civ} | {elo_bar(stats['elo'])} **{stats['elo']}**")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = f" — **{match.civ_picks[p.id]}**" if p.id in match.civ_picks else ""
            t2_lines.append(f"{p.display_name}{cap_tag}{civ} | {elo_bar(stats['elo'])} **{stats['elo']}**")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines), inline=True)
        e.set_footer(text=f"Match #{match.match_id}")
        return e

    # ── Resolve / Cancel ───────────────────────────────────────────────────────

    async def resolve_match(self, interaction: discord.Interaction, match: MatchState, winner: int):
        await interaction.response.defer(thinking=False)
        guild        = interaction.guild
        gid          = str(guild.id)
        qt           = match.queue_type
        winning_team = match.team1 if winner == 1 else match.team2
        losing_team  = match.team2 if winner == 1 else match.team1

        for p in winning_team:
            await db.update_aoe_stats(gid, str(p.id), qt, result="win")
        for p in losing_team:
            await db.update_aoe_stats(gid, str(p.id), qt, result="loss")
        for p in winning_team:
            await db.add_coins(gid, str(p.id), WIN_COINS)

        t1_ids = [str(p.id) for p in match.team1]
        t2_ids = [str(p.id) for p in match.team2]
        civ_data = {str(k): v for k, v in match.civ_picks.items()}
        await db.finish_aoe_match(match.match_id, f"team{winner}", t1_ids, t2_ids, civ_data)

        e = discord.Embed(title=f"🏆 Team {winner} Victory! — {qt.upper()}",
                          color=0xFFD700, timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            change  = f"+{ELO_CHANGE}" if winner == 1 else f"-{ELO_CHANGE}"
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = match.civ_picks.get(p.id, "?")
            t1_lines.append(f"{p.display_name}{cap_tag} — **{civ}** | **{stats['elo']}** ELO ({change})")
        e.add_field(name=f"{'🏆' if winner==1 else '💔'} Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            change  = f"+{ELO_CHANGE}" if winner == 2 else f"-{ELO_CHANGE}"
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = match.civ_picks.get(p.id, "?")
            t2_lines.append(f"{p.display_name}{cap_tag} — **{civ}** | **{stats['elo']}** ELO ({change})")
        e.add_field(name=f"{'🏆' if winner==2 else '💔'} Team 2", value="\n".join(t2_lines), inline=True)
        e.add_field(name="🧀 Coin Reward",
                    value=f"Winning team each received **{WIN_COINS} 🧀 Cheese Coins!**", inline=False)
        e.set_footer(text=f"Match #{match.match_id} • Thread closes in {RESULT_DISPLAY_SECS}s")

        await interaction.edit_original_response(embed=e, view=None)
        await self._post_match_history(guild, match, result=f"Team {winner} Victory",
                                        winning_team=winning_team, losing_team=losing_team)
        await self._update_leaderboard(guild, qt)
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        await asyncio.sleep(RESULT_DISPLAY_SECS)
        if match.thread:
            try:
                await match.thread.delete()
            except Exception:
                pass

    async def cancel_match(self, interaction: discord.Interaction, match: MatchState):
        await interaction.response.defer(thinking=False)
        guild = interaction.guild
        gid   = str(guild.id)
        qt    = match.queue_type

        for p in match.all_players:
            await db.update_aoe_stats(gid, str(p.id), qt, result="no_result")
        t1_ids   = [str(p.id) for p in match.team1]
        t2_ids   = [str(p.id) for p in match.team2]
        civ_data = {str(k): v for k, v in match.civ_picks.items()}
        await db.finish_aoe_match(match.match_id, "cancelled", t1_ids, t2_ids, civ_data)

        e = discord.Embed(
            title=f"🚫 Match Cancelled — {qt.upper()}",
            description=f"This match has been cancelled. No ELO changes.\nThread closes in {RESULT_DISPLAY_SECS}s.",
            color=0x95A5A6, timestamp=datetime.now(timezone.utc))
        e.set_footer(text=f"Match #{match.match_id}")
        await interaction.edit_original_response(embed=e, view=None)

        await self._post_match_history(guild, match, result="Cancelled", winning_team=[], losing_team=[])
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        await asyncio.sleep(RESULT_DISPLAY_SECS)
        if match.thread:
            try:
                await match.thread.delete()
            except Exception:
                pass

    # ── Match history ──────────────────────────────────────────────────────────

    async def _post_match_history(self, guild, match: MatchState,
                                   result: str, winning_team: list, losing_team: list):
        ch = discord.utils.get(guild.text_channels, name=MATCH_HISTORY_CHANNEL)
        if not ch:
            return
        gid = str(guild.id)
        qt  = match.queue_type
        e   = discord.Embed(
            title=f"📜 Match #{match.match_id} — {qt.upper()} | {result}",
            color=0xFFD700 if "Victory" in result else 0x95A5A6,
            timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = match.civ_picks.get(p.id, "—")
            tag     = "🏆" if p in winning_team else ("💔" if losing_team else "🚫")
            t1_lines.append(f"{tag} {p.display_name}{cap_tag} — **{civ}** | **{stats['elo']}** ELO")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines) or "—", inline=True)

        t2_lines = []
        for p in match.team2:
            stats   = await db.get_aoe_stats(gid, str(p.id), qt)
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = match.civ_picks.get(p.id, "—")
            tag     = "🏆" if p in winning_team else ("💔" if losing_team else "🚫")
            t2_lines.append(f"{tag} {p.display_name}{cap_tag} — **{civ}** | **{stats['elo']}** ELO")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines) or "—", inline=True)
        e.set_footer(text=f"Match #{match.match_id} • {guild.name}")
        try:
            await ch.send(embed=e)
        except Exception as ex:
            logger.error("[%s] Failed to post match history: %s", guild.id, ex)

    # ── Leaderboard ────────────────────────────────────────────────────────────

    async def _update_leaderboard(self, guild, queue_type: str):
        ch_name = LEADERBOARD_CHANNEL_NAMES.get(queue_type)
        if not ch_name:
            return
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            return
        board = await db.get_aoe_leaderboard(str(guild.id), queue_type)
        now   = datetime.now(timezone.utc)
        if not board:
            e = discord.Embed(title=f"⚔️ AOE 4 — {queue_type.upper()} Leaderboard",
                              description="No matches played yet!", color=0xE67E22, timestamp=now)
        else:
            rows = []
            for i, row in enumerate(board):
                member  = guild.get_member(int(row["user_id"]))
                name    = member.display_name if member else f"Unknown ({row['user_id']})"
                total   = row["wins"] + row["losses"]
                win_pct = f"{(row['wins']/total*100):.1f}%" if total > 0 else "0%"
                medal   = ["🥇", "🥈", "🥉"][i] if i < 3 else f"`{i+1}.`"
                rows.append(
                    f"{medal} **{name}** — W:{row['wins']} L:{row['losses']} "
                    f"NR:{row['no_results']} WR:{win_pct} {elo_bar(row['elo'])}**{row['elo']}** ELO")
            e = discord.Embed(title=f"⚔️ AOE 4 — {queue_type.upper()} Leaderboard",
                              description="\n".join(rows), color=0xE67E22, timestamp=now)
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

    # ── Slash commands ─────────────────────────────────────────────────────────

    @discord.app_commands.command(name="aoe_setup",
                                   description="Set up all AOE 4 queue channels (admin only).")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._setup_channels(interaction.guild)
        await interaction.followup.send("✅ AOE 4 queue channels set up!", ephemeral=True)

    @discord.app_commands.command(name="aoe_leaderboard",
                                   description="Refresh all AOE leaderboards (admin only).")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        for qt in QUEUE_CONFIGS:
            await self._update_leaderboard(interaction.guild, qt)
        await interaction.followup.send("✅ All AOE leaderboards refreshed!", ephemeral=True)

    @discord.app_commands.command(name="aoe_stats", description="Check AOE 4 queue stats.")
    @discord.app_commands.describe(member="Member to check (leave blank for yourself)")
    async def aoe_stats(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user
        gid    = str(interaction.guild.id)
        e = discord.Embed(title=f"⚔️ AOE 4 Stats — {target.display_name}",
                          color=0xE67E22, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=target.display_avatar.url)
        for qt in QUEUE_CONFIGS:
            stats = await db.get_aoe_stats(gid, str(target.id), qt)
            total = stats["wins"] + stats["losses"]
            wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
            e.add_field(name=f"{qt.upper()}",
                        value=f"{elo_bar(stats['elo'])} **{stats['elo']}** ELO\n"
                              f"W:{stats['wins']} L:{stats['losses']} NR:{stats['no_results']} WR:{wp}",
                        inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(name="aoe_listmatches",
                                   description="List all active AOE matches (admin only).")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_listmatches(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        matches = self._get_matches(interaction.guild.id)
        if not matches:
            await interaction.followup.send("📭 No active matches right now.", ephemeral=True)
            return
        e = discord.Embed(title="⚔️ Active AOE Matches", color=0xE67E22,
                          timestamp=datetime.now(timezone.utc))
        for match in matches:
            t1 = ", ".join(p.display_name for p in match.team1) or "TBD"
            t2 = ", ".join(p.display_name for p in match.team2) or "TBD"
            e.add_field(
                name=f"Match #{match.match_id} — {match.queue_type.upper()} [{match.phase}]",
                value=f"🔴 {t1}\n🔵 {t2}",
                inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(name="aoe_forcecancel",
                                   description="Force cancel an active AOE match (admin only).")
    @discord.app_commands.describe(match_id="Match ID to cancel (use /aoe_listmatches)")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forcecancel(self, interaction: discord.Interaction, match_id: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        gid   = str(guild.id)
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return

        qt = match.queue_type
        for p in match.all_players:
            await db.update_aoe_stats(gid, str(p.id), qt, result="no_result")
        civ_data = {str(k): v for k, v in match.civ_picks.items()}
        await db.finish_aoe_match(match.match_id, "cancelled",
                                   [str(p.id) for p in match.team1],
                                   [str(p.id) for p in match.team2], civ_data)

        if match.thread:
            try:
                e_thread = discord.Embed(
                    title=f"🚫 Match Force Cancelled — {qt.upper()}",
                    description=f"Cancelled by an admin. No ELO changes. Thread closes in {RESULT_DISPLAY_SECS}s.",
                    color=0x95A5A6, timestamp=datetime.now(timezone.utc))
                e_thread.set_footer(text=f"Match #{match.match_id} • By {interaction.user.display_name}")
                await match.thread.send(embed=e_thread)
            except Exception:
                pass

        await self._post_match_history(guild, match, result="Force Cancelled (Admin)",
                                        winning_team=[], losing_team=[])
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        e2 = discord.Embed(title="✅ Match Force Cancelled", color=0x95A5A6,
                           timestamp=datetime.now(timezone.utc))
        e2.add_field(name="🎮 Match ID", value=f"#{match_id}", inline=True)
        e2.add_field(name="📋 Queue",    value=qt.upper(),     inline=True)
        e2.add_field(name="👥 Players",
                     value=", ".join(p.display_name for p in match.all_players), inline=False)
        e2.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e2, ephemeral=True)

        if match.thread:
            await asyncio.sleep(RESULT_DISPLAY_SECS)
            try:
                await match.thread.delete()
            except Exception:
                pass

    @discord.app_commands.command(name="aoe_forcestart",
                                   description="Force start an active AOE match (admin only).")
    @discord.app_commands.describe(match_id="Match ID to start (use /aoe_listmatches)")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forcestart(self, interaction: discord.Interaction, match_id: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if match.phase == "in_match":
            await interaction.followup.send(f"⚠️ Match **#{match_id}** is already in progress!", ephemeral=True)
            return

        while match.remaining:
            match.pick_player(match.remaining[0])

        match.phase = "in_match"
        if match.thread:
            try:
                embed = await self._build_teams_embed(guild, match, phase="in_match")
                embed.set_footer(text=f"Match #{match.match_id} • Force started by {interaction.user.display_name}")
                view = InMatchView(self, match)
                await match.thread.send(embed=embed, view=view)
            except Exception as ex:
                logger.warning("[%s] Could not post force start in thread: %s", guild.id, ex)

        e = discord.Embed(title="✅ Match Force Started", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID", value=f"#{match_id}",          inline=True)
        e.add_field(name="📋 Queue",    value=match.queue_type.upper(), inline=True)
        e.add_field(name="🔴 Team 1",   value=", ".join(p.display_name for p in match.team1), inline=True)
        e.add_field(name="🔵 Team 2",   value=", ".join(p.display_name for p in match.team2), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(name="aoe_forcevictory",
                                   description="Force assign victory to a team (admin only).")
    @discord.app_commands.describe(match_id="Match ID (use /aoe_listmatches)",
                                    winning_team="Which team wins")
    @discord.app_commands.choices(winning_team=[
        discord.app_commands.Choice(name="Team 1 🔴", value=1),
        discord.app_commands.Choice(name="Team 2 🔵", value=2),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forcevictory(self, interaction: discord.Interaction,
                                match_id: int, winning_team: int):
        await interaction.response.defer(ephemeral=True)
        guild   = interaction.guild
        gid     = str(guild.id)
        match   = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return

        qt      = match.queue_type
        winning = match.team1 if winning_team == 1 else match.team2
        losing  = match.team2 if winning_team == 1 else match.team1

        for p in winning:
            await db.update_aoe_stats(gid, str(p.id), qt, result="win")
        for p in losing:
            await db.update_aoe_stats(gid, str(p.id), qt, result="loss")
        for p in winning:
            await db.add_coins(gid, str(p.id), WIN_COINS)

        civ_data = {str(k): v for k, v in match.civ_picks.items()}
        await db.finish_aoe_match(match.match_id, f"team{winning_team}",
                                   [str(p.id) for p in match.team1],
                                   [str(p.id) for p in match.team2], civ_data)

        if match.thread:
            try:
                e_t = discord.Embed(
                    title=f"🏆 Team {winning_team} Victory! — {qt.upper()} (Admin Override)",
                    color=0xFFD700, timestamp=datetime.now(timezone.utc))
                t1_lines = []
                for p in match.team1:
                    stats   = await db.get_aoe_stats(gid, str(p.id), qt)
                    change  = f"+{ELO_CHANGE}" if winning_team == 1 else f"-{ELO_CHANGE}"
                    cap_tag = " 👑" if p == match.captain1 else ""
                    civ     = match.civ_picks.get(p.id, "—")
                    t1_lines.append(f"{p.display_name}{cap_tag} — **{civ}** | **{stats['elo']}** ELO ({change})")
                e_t.add_field(name=f"{'🏆' if winning_team==1 else '💔'} Team 1",
                              value="\n".join(t1_lines), inline=True)
                t2_lines = []
                for p in match.team2:
                    stats   = await db.get_aoe_stats(gid, str(p.id), qt)
                    change  = f"+{ELO_CHANGE}" if winning_team == 2 else f"-{ELO_CHANGE}"
                    cap_tag = " 👑" if p == match.captain2 else ""
                    civ     = match.civ_picks.get(p.id, "—")
                    t2_lines.append(f"{p.display_name}{cap_tag} — **{civ}** | **{stats['elo']}** ELO ({change})")
                e_t.add_field(name=f"{'🏆' if winning_team==2 else '💔'} Team 2",
                              value="\n".join(t2_lines), inline=True)
                e_t.add_field(name="🧀 Coin Reward",
                              value=f"Winning team each received **{WIN_COINS} 🧀 Cheese Coins!**", inline=False)
                e_t.set_footer(text=f"Match #{match.match_id} • By {interaction.user.display_name} • Thread closes in {RESULT_DISPLAY_SECS}s")
                await match.thread.send(embed=e_t)
            except Exception as ex:
                logger.warning("[%s] Could not post force victory in thread: %s", guild.id, ex)

        await self._post_match_history(guild, match, result=f"Team {winning_team} Victory (Admin)",
                                        winning_team=winning, losing_team=losing)
        await self._update_leaderboard(guild, qt)
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        e2 = discord.Embed(title=f"✅ Force Victory — Team {winning_team}",
                           color=0xFFD700, timestamp=datetime.now(timezone.utc))
        e2.add_field(name="🎮 Match ID",    value=f"#{match_id}", inline=True)
        e2.add_field(name="📋 Queue",       value=qt.upper(),     inline=True)
        e2.add_field(name="🏆 Winners",     value=", ".join(p.display_name for p in winning), inline=False)
        e2.add_field(name="💔 Losers",      value=", ".join(p.display_name for p in losing),  inline=False)
        e2.add_field(name="🧀 Coins Given", value=f"{WIN_COINS} per winner", inline=True)
        e2.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e2, ephemeral=True)

        if match.thread:
            await asyncio.sleep(RESULT_DISPLAY_SECS)
            try:
                await match.thread.delete()
            except Exception:
                pass

    @discord.app_commands.command(name="aoe_addwin",
                                   description="Add a win to a player's AOE stats (admin only).")
    @discord.app_commands.describe(member="Player", queue_type="Queue type", amount="Wins to add (default 1)")
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
        for _ in range(amount):
            await db.update_aoe_stats(gid, str(member.id), queue_type, "win")
        stats = await db.get_aoe_stats(gid, str(member.id), queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Win Added", color=0x57F287, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player",   value=member.mention,      inline=True)
        e.add_field(name="🎮 Queue",    value=queue_type.upper(),  inline=True)
        e.add_field(name="➕ Added",     value=f"{amount} win(s)",  inline=True)
        e.add_field(name="🏆 Wins",     value=str(stats["wins"]),  inline=True)
        e.add_field(name="💔 Losses",   value=str(stats["losses"]), inline=True)
        e.add_field(name="📊 Win Rate", value=wp,                  inline=True)
        e.add_field(name=f"{elo_bar(stats['elo'])} ELO", value=str(stats["elo"]), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild, queue_type)

    @discord.app_commands.command(name="aoe_removewin",
                                   description="Remove a win from a player's AOE stats (admin only).")
    @discord.app_commands.describe(member="Player", queue_type="Queue type", amount="Wins to remove (default 1)")
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
        stats = await db.get_aoe_stats(gid, str(member.id), queue_type)
        if stats["wins"] == 0:
            await interaction.followup.send(
                f"❌ **{member.display_name}** has no wins in {queue_type.upper()}!", ephemeral=True)
            return
        remove = min(amount, stats["wins"])
        await db.adjust_aoe_stats(gid, str(member.id), queue_type,
                                   wins_delta=-remove, elo_delta=-(remove * ELO_CHANGE))
        stats = await db.get_aoe_stats(gid, str(member.id), queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Win Removed", color=0xED4245, timestamp=datetime.now(timezone.utc))
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

    @discord.app_commands.command(name="aoe_addloss",
                                   description="Add a loss to a player's AOE stats (admin only).")
    @discord.app_commands.describe(member="Player", queue_type="Queue type", amount="Losses to add (default 1)")
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
        for _ in range(amount):
            await db.update_aoe_stats(gid, str(member.id), queue_type, "loss")
        stats = await db.get_aoe_stats(gid, str(member.id), queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Loss Added", color=0xED4245, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player",   value=member.mention,        inline=True)
        e.add_field(name="🎮 Queue",    value=queue_type.upper(),    inline=True)
        e.add_field(name="➕ Added",     value=f"{amount} loss(es)",  inline=True)
        e.add_field(name="🏆 Wins",     value=str(stats["wins"]),    inline=True)
        e.add_field(name="💔 Losses",   value=str(stats["losses"]),  inline=True)
        e.add_field(name="📊 Win Rate", value=wp,                    inline=True)
        e.add_field(name=f"{elo_bar(stats['elo'])} ELO", value=str(stats["elo"]), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild, queue_type)

    @discord.app_commands.command(name="aoe_removeloss",
                                   description="Remove a loss from a player's AOE stats (admin only).")
    @discord.app_commands.describe(member="Player", queue_type="Queue type", amount="Losses to remove (default 1)")
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
        stats = await db.get_aoe_stats(gid, str(member.id), queue_type)
        if stats["losses"] == 0:
            await interaction.followup.send(
                f"❌ **{member.display_name}** has no losses in {queue_type.upper()}!", ephemeral=True)
            return
        remove = min(amount, stats["losses"])
        await db.adjust_aoe_stats(gid, str(member.id), queue_type,
                                   losses_delta=-remove, elo_delta=(remove * ELO_CHANGE))
        stats = await db.get_aoe_stats(gid, str(member.id), queue_type)
        total = stats["wins"] + stats["losses"]
        wp    = f"{(stats['wins']/total*100):.1f}%" if total > 0 else "0%"
        e = discord.Embed(title="✅ AOE Loss Removed", color=0x57F287, timestamp=datetime.now(timezone.utc))
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

    @discord.app_commands.command(name="aoe_resetstats",
                                   description="Reset a player's AOE stats (admin only).")
    @discord.app_commands.describe(member="Player to reset", queue_type="Queue type to reset")
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
        queues = list(QUEUE_CONFIGS.keys()) if queue_type == "all" else [queue_type]
        for qt in queues:
            await db.reset_aoe_stats(gid, str(member.id), qt)
        label = "all queues" if queue_type == "all" else queue_type.upper()
        e = discord.Embed(title="✅ AOE Stats Reset", color=0xFEE75C, timestamp=datetime.now(timezone.utc))
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
