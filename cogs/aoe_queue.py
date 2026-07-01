"""
cogs/aoe_queue.py — AOE 4 Queue System
- Single #aoe-queue channel with all 4 queues
- Single #aoe-leaderboard channel with all 4 leaderboards
- Civ selection, draft, temp VCs, match history
- No ELO system
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

QUEUE_CHANNEL_NAME      = "aoe-queue"
LEADERBOARD_CHANNEL_NAME = "aoe-leaderboard"
MATCH_HISTORY_CHANNEL   = "aoe-match-history"
OLD_QUEUE_CHANNELS      = ["1v1-aoe-queue", "2v2-aoe-queue", "3v3-aoe-queue", "4v4-aoe-queue"]
OLD_LB_CHANNELS         = ["1v1-aoe-leaderboard", "2v2-aoe-leaderboard",
                            "3v3-aoe-leaderboard", "4v4-aoe-leaderboard"]

QUEUE_TIMEOUT_SECS  = 1800
AOE_GENERAL_VC_NAME = "AOE IV General"   # Permanent VC everyone moves to after match

# Civ name → Discord emoji name (upload these to your server)
AOE_CIVS = [
    ("🎲 Random",            "aoe_random"),
    ("Abbasid Dynasty",      "aoe_abbasid"),
    ("Ayyubids",             "aoe_ayyubids"),
    ("Byzantines",           "aoe_byzantines"),
    ("Chinese",              "aoe_chinese"),
    ("Delhi Sultanate",      "aoe_delhi"),
    ("English",              "aoe_english"),
    ("French",               "aoe_french"),
    ("Golden Horde",         "aoe_golden_horde"),
    ("Holy Roman Empire",    "aoe_hre"),
    ("House of Lancaster",   "aoe_lancaster"),
    ("Japanese",             "aoe_japanese"),
    ("Jeanne d'Arc",         "aoe_jeanne"),
    ("Jin Dynasty",          "aoe_jin_dynasty"),
    ("Macedonian Dynasty",   "aoe_macedonian"),
    ("Malians",              "aoe_malians"),
    ("Mongols",              "aoe_mongols"),
    ("Order of the Dragon",  "aoe_dragon"),
    ("Ottomans",             "aoe_ottomans"),
    ("Rus",                  "aoe_rus"),
    ("Sengoku Daimyo",       "aoe_sengoku"),
    ("Templar Knights",      "aoe_templar"),
    ("Tughlaq Dynasty",      "aoe_tughlaq"),
    ("Zhu Xi's Legacy",      "aoe_zhu_xi"),
]

MAP_POOL = [
    "African Waters", "Archipelago", "Dry Arabia", "Forts", "Gorge",
    "Hedgemaze", "Rocky River", "Sunkenlands", "West Lake",
]

WIN_COINS           = 5
PRIVILEGED_ROLES    = {"👑 Grandmaster", "👑 King", "🔨 Moderator"}  # Can control match buttons
RESULT_DISPLAY_SECS = 30
AOE_CATEGORY_KEYWORD = "AGE OF EMPIRES"

# ── Helpers ────────────────────────────────────────────────────────────────────

def find_aoe_category(guild):
    return discord.utils.find(
        lambda c: AOE_CATEGORY_KEYWORD in c.name.upper(), guild.categories)

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

        self.team1     = [self.captain1]
        self.team2     = [self.captain2]
        self.remaining = [p for p in players if p not in (self.captain1, self.captain2)]

        self.pick_step       = 0
        self.draft_complete  = False
        self.draft_started   = False
        self.first_pick_team = None
        self.phase           = "coin_flip"

        self.civ_picks: dict   = {}
        self.cap1_locked       = False
        self.cap2_locked       = False
        self.temp_vc1          = None
        self.temp_vc2          = None

        # 1v1 specific: map veto
        self.map_pool          = MAP_POOL.copy()
        self.map_ban_order     = None   # 1 or 2 — who bans first
        self.map_bans: list    = []     # [(banned_by_player, map_name), ...]
        self.final_map         = None

        # 1v1 specific: civ ban (each player bans 1 civ for the OTHER)
        self.civ_ban_p1_choice = None   # civ captain1 bans for captain2
        self.civ_ban_p2_choice = None   # civ captain2 bans for captain1
        self.civ_ban_p1_locked = False
        self.civ_ban_p2_locked = False

    @property
    def civs_revealed(self):
        return self.cap1_locked and self.cap2_locked

    def all_picked_civs(self, team):
        return all(p.id in self.civ_picks for p in team)

    def thread_name(self):
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
        self.draft_started = True
        picker = self.current_picker()
        if picker == self.captain1:
            self.team1.append(player)
        else:
            self.team2.append(player)
        self.remaining.remove(player)
        self.pick_step += 1
        if not self.remaining:
            self.draft_complete = True
            self.phase = "teams_confirm"

    def replace_captain(self, team, new_captain, from_pool=False):
        """Replace captain with new_captain.
        from_pool=True  → new cap was in the undrafted pool
        from_pool=False → new cap was already on the team
        In both cases, the OLD captain moves to the pool.
        """
        if team == 1:
            old = self.captain1
            self.captain1 = new_captain

            # Remove new_cap from wherever they currently are
            if new_captain in self.remaining:
                self.remaining.remove(new_captain)
            if new_captain in self.team1:
                self.team1.remove(new_captain)

            # New cap goes to front of team1
            self.team1.insert(0, new_captain)

            # Old cap leaves team1 and goes to pool
            if old in self.team1:
                self.team1.remove(old)
            if old not in self.remaining:
                self.remaining.append(old)
        else:
            old = self.captain2
            self.captain2 = new_captain

            # Remove new_cap from wherever they currently are
            if new_captain in self.remaining:
                self.remaining.remove(new_captain)
            if new_captain in self.team2:
                self.team2.remove(new_captain)

            # New cap goes to front of team2
            self.team2.insert(0, new_captain)

            # Old cap leaves team2 and goes to pool
            if old in self.team2:
                self.team2.remove(old)
            if old not in self.remaining:
                self.remaining.append(old)

    def team_of(self, member):
        if member in self.team1: return 1
        if member in self.team2: return 2
        return None

    # ── 1v1 Map Veto helpers ──────────────────────────────────────────────────

    def current_map_banner(self):
        """Returns which player (1 or 2 -> captain) should ban next."""
        if len(self.map_pool) <= 1:
            return None
        ban_count = len(self.map_bans)
        starter = self.map_ban_order  # 1 or 2
        turn = (starter - 1 + ban_count) % 2  # alternate
        return self.captain1 if turn == 0 else self.captain2

    def ban_map(self, banner, map_name):
        self.map_pool.remove(map_name)
        self.map_bans.append((banner, map_name))
        if len(self.map_pool) == 1:
            self.final_map = self.map_pool[0]

    # ── 1v1 Civ Ban helpers ───────────────────────────────────────────────────

    def banned_civs_for(self, player):
        """Civs banned FOR this player (they cannot pick these)."""
        banned = []
        if player == self.captain1 and self.civ_ban_p2_choice:
            banned.append(self.civ_ban_p2_choice)
        if player == self.captain2 and self.civ_ban_p1_choice:
            banned.append(self.civ_ban_p1_choice)
        return banned

    @property
    def civ_bans_locked(self):
        return self.civ_ban_p1_locked and self.civ_ban_p2_locked


# ── Queue View ─────────────────────────────────────────────────────────────────

class QueueView(discord.ui.View):
    def __init__(self, cog, queue_type: str):
        super().__init__(timeout=None)
        self.cog        = cog
        self.queue_type = queue_type
        self._add_buttons()

    def _add_buttons(self):
        join_btn = discord.ui.Button(
            label="✅ Join Queue",
            style=discord.ButtonStyle.success,
            custom_id=f"aoe_join_{self.queue_type}"
        )
        async def join(interaction: discord.Interaction):
            await self.cog.handle_join(interaction, self.queue_type)
        join_btn.callback = join
        self.add_item(join_btn)

        leave_btn = discord.ui.Button(
            label="❌ Leave Queue",
            style=discord.ButtonStyle.danger,
            custom_id=f"aoe_leave_{self.queue_type}"
        )
        async def leave(interaction: discord.Interaction):
            await self.cog.handle_leave(interaction, self.queue_type)
        leave_btn.callback = leave
        self.add_item(leave_btn)


# ── Coin Flip View ─────────────────────────────────────────────────────────────

class CoinFlipView(discord.ui.View):
    def __init__(self, cog, match: MatchState, flipper):
        super().__init__(timeout=120)
        self.cog     = cog
        self.match   = match
        self.flipper = flipper
        self._add_captain_buttons()

    def _add_captain_buttons(self):
        # Replace Team 1 Captain button
        cap1_btn = discord.ui.Button(
            label="🔄 Replace Team 1 Captain",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        async def change_cap1(interaction: discord.Interaction):
            is_team1_cap = interaction.user.id == self.match.captain1.id
            is_privileged = self.cog._is_admin_or_privileged(interaction.user)
            if not is_team1_cap and not is_privileged:
                await interaction.response.send_message(
                    "❌ Only Team 1's captain or an admin can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain_from_flip(interaction, self.match, team=1)
        cap1_btn.callback = change_cap1
        self.add_item(cap1_btn)

        # Replace Team 2 Captain button
        cap2_btn = discord.ui.Button(
            label="🔄 Replace Team 2 Captain",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        async def change_cap2(interaction: discord.Interaction):
            is_team2_cap = interaction.user.id == self.match.captain2.id
            is_privileged = self.cog._is_admin_or_privileged(interaction.user)
            if not is_team2_cap and not is_privileged:
                await interaction.response.send_message(
                    "❌ Only Team 2's captain or an admin can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain_from_flip(interaction, self.match, team=2)
        cap2_btn.callback = change_cap2
        self.add_item(cap2_btn)

        # Cancel Match button
        cancel_btn = discord.ui.Button(
            label="🚫 Cancel Match",
            style=discord.ButtonStyle.danger,
            row=2,
        )
        async def cancel(interaction: discord.Interaction):
            if not self.cog._is_captain_or_admin(interaction.user, self.match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            await self.cog.cancel_match(interaction, self.match)
        cancel_btn.callback = cancel
        self.add_item(cancel_btn)

    @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.primary, row=0)
    async def heads(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id and not self.cog._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only the coin flipper can choose!", ephemeral=True)
            return
        await self.cog.resolve_flip(interaction, self.match, "heads")
        self.stop()

    @discord.ui.button(label="🪙 Tails", style=discord.ButtonStyle.primary, row=0)
    async def tails(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id and not self.cog._is_admin(interaction.user):
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
        if interaction.user.id != self.winner.id and not self.cog._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only the flip winner can choose!", ephemeral=True)
            return
        self.match.first_pick_team = self.match.team_of(self.winner)
        self.match.phase = "draft"
        await self.cog.show_draft(interaction, self.match)
        self.stop()

    @discord.ui.button(label="🛡️ Second Pick", style=discord.ButtonStyle.secondary)
    async def second(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.winner.id and not self.cog._is_admin(interaction.user):
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
                    await self.cog.show_teams_confirm(interaction, self.match)
                else:
                    await self.cog.show_draft(interaction, self.match)
            btn.callback = callback
            self.add_item(btn)

        # If only 1 player remains after building buttons, auto-assign them
        # This check runs at VIEW CREATION time — if we're here with 1 player,
        # it means the previous pick left exactly 1 remaining, so auto-assign now
        if len(self.match.remaining) == 1:
            last_player = self.match.remaining[0]
            self.match.pick_player(last_player)

        n          = len(self.match.remaining)
        cap_row    = min(max((n + 3) // 4, 1), 2)
        cancel_row = min(cap_row + 1, 4)

        # Team 1 captain swap candidates: remaining pool players + team1 members except current captain
        team1_swap_options = [p for p in self.match.remaining] +                              [p for p in self.match.team1 if p != self.match.captain1]
        cap1_disabled = len(team1_swap_options) == 0 or self.match.draft_started

        cap1_btn = discord.ui.Button(label="🔄 Replace Team 1 Captain",
                                      style=discord.ButtonStyle.secondary,
                                      row=cap_row, disabled=cap1_disabled)
        async def change_cap1(interaction: discord.Interaction):
            is_team1_cap = interaction.user.id == self.match.captain1.id
            is_privileged = self.cog._is_admin_or_privileged(interaction.user)
            if not is_team1_cap and not is_privileged:
                await interaction.response.send_message(
                    "❌ Only Team 1's captain or an admin can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain(interaction, self.match, team=1)
        cap1_btn.callback = change_cap1
        self.add_item(cap1_btn)

        # Team 2 captain swap candidates: remaining pool players + team2 members except current captain
        team2_swap_options = [p for p in self.match.remaining] +                              [p for p in self.match.team2 if p != self.match.captain2]
        cap2_disabled = len(team2_swap_options) == 0 or self.match.draft_started

        cap2_btn = discord.ui.Button(label="🔄 Replace Team 2 Captain",
                                      style=discord.ButtonStyle.secondary,
                                      row=cap_row, disabled=cap2_disabled)
        async def change_cap2(interaction: discord.Interaction):
            is_team2_cap = interaction.user.id == self.match.captain2.id
            is_privileged = self.cog._is_admin_or_privileged(interaction.user)
            if not is_team2_cap and not is_privileged:
                await interaction.response.send_message(
                    "❌ Only Team 2's captain or an admin can do this!", ephemeral=True)
                return
            await self.cog.show_change_captain(interaction, self.match, team=2)
        cap2_btn.callback = change_cap2
        self.add_item(cap2_btn)

        cancel_btn = discord.ui.Button(label="🚫 Cancel Match",
                                        style=discord.ButtonStyle.danger, row=cancel_row)
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
        self.current_cap_id = current_cap.id  # snapshot — who is allowed to act here

        # Options: undrafted pool players + already drafted team members (except current cap)
        # Players already drafted into the OTHER team are excluded
        pool_options = list(match.remaining)
        team_options = [m for m in team_members if m != current_cap]
        all_options  = pool_options + team_options

        if all_options:
            select_opts = []
            for m in all_options:
                if m in pool_options:
                    label = f"{m.display_name} (Pool)"
                else:
                    label = f"{m.display_name} (Team {team})"
                select_opts.append(discord.SelectOption(label=label, value=str(m.id)))

            select = discord.ui.Select(
                placeholder=f"Select new Team {team} Captain...",
                options=select_opts,
            )
            async def on_select(interaction: discord.Interaction):
                # Re-verify: only THIS team's captain (or admin/privileged) may complete
                # the swap — the entry button check alone isn't enough since this is a
                # separate interaction on a separate component.
                is_this_team_cap = interaction.user.id == self.current_cap_id
                is_privileged     = self.cog._is_admin_or_privileged(interaction.user)
                if not is_this_team_cap and not is_privileged:
                    await interaction.response.send_message(
                        f"❌ Only Team {self.team}'s captain or an admin can do this!",
                        ephemeral=True)
                    return

                new_id  = int(select.values[0])
                # Search in both pool and team
                new_cap = discord.utils.get(all_options, id=new_id)
                if not new_cap:
                    await interaction.response.send_message("❌ Player not found!", ephemeral=True)
                    return

                from_pool = new_cap in match.remaining
                match.replace_captain(team, new_cap, from_pool=from_pool)
                if match.thread:
                    try:
                        await match.thread.edit(name=match.thread_name())
                    except Exception:
                        pass
                await self.cog.show_draft(interaction, match)
                self.stop()
            select.callback = on_select
            self.add_item(select)
        else:
            # No valid swap candidates — show disabled placeholder
            select = discord.ui.Select(
                placeholder="No available players to swap with",
                options=[discord.SelectOption(label="None", value="none")],
                disabled=True,
            )
            self.add_item(select)

        back_btn = discord.ui.Button(label="↩️ Back", style=discord.ButtonStyle.secondary)
        async def back(interaction: discord.Interaction):
            await self.cog.show_draft(interaction, match)
            self.stop()
        back_btn.callback = back
        self.add_item(back_btn)


# ── Change Captain From Flip View ────────────────────────────────────────────────

class ChangeCaptainFromFlipView(discord.ui.View):
    """Same as ChangeCaptainView but returns to coin flip screen after swap."""
    def __init__(self, cog, match: MatchState, team: int):
        super().__init__(timeout=60)
        self.cog          = cog
        self.match        = match
        self.team         = team

        team_members = match.team1 if team == 1 else match.team2
        current_cap  = match.captain1 if team == 1 else match.captain2
        self.current_cap_id = current_cap.id

        # At coin flip stage teams only have the captain — include pool players too
        team_options = [m for m in team_members if m != current_cap]
        pool_options = list(match.remaining)
        all_options  = team_options + pool_options

        if all_options:
            select_opts = []
            for m in all_options:
                label = f"{m.display_name} (Pool)" if m in pool_options else f"{m.display_name} (Team {team})"
                select_opts.append(discord.SelectOption(label=label, value=str(m.id)))

            select = discord.ui.Select(
                placeholder=f"Select new Team {team} Captain...",
                options=select_opts,
            )
            async def on_select(interaction: discord.Interaction):
                is_this_cap   = interaction.user.id == self.current_cap_id
                is_privileged = self.cog._is_admin_or_privileged(interaction.user)
                if not is_this_cap and not is_privileged:
                    await interaction.response.send_message(
                        f"❌ Only Team {self.team}'s captain or an admin can do this!", ephemeral=True)
                    return
                new_id  = int(select.values[0])
                new_cap = discord.utils.get(all_options, id=new_id)
                if not new_cap:
                    await interaction.response.send_message("❌ Player not found!", ephemeral=True)
                    return
                from_pool = new_cap in pool_options
                match.replace_captain(team, new_cap, from_pool=from_pool)
                if match.thread:
                    try:
                        await match.thread.edit(name=match.thread_name())
                    except Exception:
                        pass
                # Return to coin flip screen with updated captains
                await self.cog._refresh_coin_flip(interaction, match)
                self.stop()
            select.callback = on_select
            self.add_item(select)
        else:
            select = discord.ui.Select(
                placeholder="No players available to swap with",
                options=[discord.SelectOption(label="None", value="none")],
                disabled=True,
            )
            self.add_item(select)

        back_btn = discord.ui.Button(label="↩️ Back", style=discord.ButtonStyle.secondary)
        async def back(interaction: discord.Interaction):
            await self.cog._refresh_coin_flip(interaction, match)
            self.stop()
        back_btn.callback = back
        self.add_item(back_btn)


# ── 1v1 Map Veto Coin Flip View ────────────────────────────────────────────────

class MapVetoCoinFlipView(discord.ui.View):
    def __init__(self, cog, match: MatchState, flipper):
        super().__init__(timeout=120)
        self.cog     = cog
        self.match   = match
        self.flipper = flipper

    @discord.ui.button(label="🪙 Heads", style=discord.ButtonStyle.primary)
    async def heads(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id and \
           not self.cog._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only the coin flipper can choose!", ephemeral=True)
            return
        await self.cog.resolve_map_veto_flip(interaction, self.match, "heads")
        self.stop()

    @discord.ui.button(label="🪙 Tails", style=discord.ButtonStyle.primary)
    async def tails(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.flipper.id and \
           not self.cog._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only the coin flipper can choose!", ephemeral=True)
            return
        await self.cog.resolve_map_veto_flip(interaction, self.match, "tails")
        self.stop()


class MapVetoOrderChoiceView(discord.ui.View):
    """Coin flip winner chooses to ban first or second in map veto."""
    def __init__(self, cog, match: MatchState, winner):
        super().__init__(timeout=120)
        self.cog    = cog
        self.match  = match
        self.winner = winner

    @discord.ui.button(label="🥇 Ban First", style=discord.ButtonStyle.success)
    async def ban_first(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.winner.id and \
           not self.cog._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only the flip winner can choose!", ephemeral=True)
            return
        self.match.map_ban_order = self.match.team_of(self.winner)
        await self.cog.show_map_veto(interaction, self.match)
        self.stop()

    @discord.ui.button(label="🥈 Ban Second", style=discord.ButtonStyle.secondary)
    async def ban_second(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.winner.id and \
           not self.cog._is_admin(interaction.user):
            await interaction.response.send_message("❌ Only the flip winner can choose!", ephemeral=True)
            return
        winner_team = self.match.team_of(self.winner)
        self.match.map_ban_order = 2 if winner_team == 1 else 1
        await self.cog.show_map_veto(interaction, self.match)
        self.stop()


# ── 1v1 Map Veto View ──────────────────────────────────────────────────────────

class MapVetoView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=180)
        self.cog   = cog
        self.match = match
        self._build()

    def _build(self):
        self.clear_items()
        for i, map_name in enumerate(self.match.map_pool):
            btn = discord.ui.Button(
                label=map_name,
                style=discord.ButtonStyle.danger,
                row=i // 3,
            )
            async def callback(interaction: discord.Interaction, m=map_name):
                # Only the CURRENT banner (or an admin override) may ban — being
                # the OTHER captain does not grant turn-bypass here, unlike
                # Start/Cancel/Victory which any captain can trigger.
                if interaction.user.id != self.match.current_map_banner().id and \
                   not self.cog._is_admin(interaction.user):
                    await interaction.response.send_message("❌ It's not your turn to ban!", ephemeral=True)
                    return
                self.match.ban_map(self.match.current_map_banner(), m)
                if self.match.final_map:
                    await self.cog.show_civ_ban(interaction, self.match)
                else:
                    await self.cog.show_map_veto(interaction, self.match)
            btn.callback = callback
            self.add_item(btn)


# ── 1v1 Civ Ban View ───────────────────────────────────────────────────────────

class CivBanView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=180)
        self.cog   = cog
        self.match = match
        self._build()

    def _build(self):
        self.clear_items()

        def make_options(guild):
            options = []
            for civ_name, emoji_name in AOE_CIVS:
                if civ_name == "🎲 Random":
                    continue
                emoji = discord.utils.get(guild.emojis, name=emoji_name) if guild else None
                options.append(discord.SelectOption(label=civ_name, value=civ_name, emoji=emoji))
            return options

        guild = self.match.thread.guild if self.match.thread else None
        opts  = make_options(guild)

        ban1_select = discord.ui.Select(
            placeholder=f"{self.match.captain1.display_name}: Ban a civ for opponent..."
                        if not self.match.civ_ban_p1_locked else "✅ Locked in",
            options=opts,
            disabled=self.match.civ_ban_p1_locked,
            row=0,
        )
        async def on_ban1(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain1.id:
                await interaction.response.send_message("❌ Only Player 1 can set this ban!", ephemeral=True)
                return
            self.match.civ_ban_p1_choice = ban1_select.values[0]
            await interaction.response.send_message(
                f"✅ You banned **{ban1_select.values[0]}** for your opponent!", ephemeral=True)
            await self.cog._refresh_civ_ban(self.match)
        ban1_select.callback = on_ban1
        self.add_item(ban1_select)

        ban2_select = discord.ui.Select(
            placeholder=f"{self.match.captain2.display_name}: Ban a civ for opponent..."
                        if not self.match.civ_ban_p2_locked else "✅ Locked in",
            options=opts,
            disabled=self.match.civ_ban_p2_locked,
            row=1,
        )
        async def on_ban2(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain2.id:
                await interaction.response.send_message("❌ Only Player 2 can set this ban!", ephemeral=True)
                return
            self.match.civ_ban_p2_choice = ban2_select.values[0]
            await interaction.response.send_message(
                f"✅ You banned **{ban2_select.values[0]}** for your opponent!", ephemeral=True)
            await self.cog._refresh_civ_ban(self.match)
        ban2_select.callback = on_ban2
        self.add_item(ban2_select)

        lock1_btn = discord.ui.Button(
            label="🔒 Lock Ban (P1)" if not self.match.civ_ban_p1_locked else "✅ P1 Locked",
            style=discord.ButtonStyle.success if not self.match.civ_ban_p1_locked else discord.ButtonStyle.secondary,
            disabled=self.match.civ_ban_p1_locked or self.match.civ_ban_p1_choice is None,
            row=2,
        )
        async def lock1(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain1.id:
                await interaction.response.send_message("❌ Only Player 1 can lock!", ephemeral=True)
                return
            if not self.match.civ_ban_p1_choice:
                await interaction.response.send_message("❌ Pick a civ to ban first!", ephemeral=True)
                return
            self.match.civ_ban_p1_locked = True
            await interaction.response.send_message("✅ Your ban is locked in!", ephemeral=True)
            await self.cog._refresh_civ_ban(self.match)
            if self.match.civ_bans_locked:
                await self.cog._proceed_to_civ_select_after_ban(self.match)
        lock1_btn.callback = lock1
        self.add_item(lock1_btn)

        lock2_btn = discord.ui.Button(
            label="🔒 Lock Ban (P2)" if not self.match.civ_ban_p2_locked else "✅ P2 Locked",
            style=discord.ButtonStyle.primary if not self.match.civ_ban_p2_locked else discord.ButtonStyle.secondary,
            disabled=self.match.civ_ban_p2_locked or self.match.civ_ban_p2_choice is None,
            row=2,
        )
        async def lock2(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain2.id:
                await interaction.response.send_message("❌ Only Player 2 can lock!", ephemeral=True)
                return
            if not self.match.civ_ban_p2_choice:
                await interaction.response.send_message("❌ Pick a civ to ban first!", ephemeral=True)
                return
            self.match.civ_ban_p2_locked = True
            await interaction.response.send_message("✅ Your ban is locked in!", ephemeral=True)
            await self.cog._refresh_civ_ban(self.match)
            if self.match.civ_bans_locked:
                await self.cog._proceed_to_civ_select_after_ban(self.match)
        lock2_btn.callback = lock2
        self.add_item(lock2_btn)

        cancel_btn = discord.ui.Button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger, row=3)
        async def cancel(interaction: discord.Interaction):
            if not self.cog._is_captain_or_admin(interaction.user, self.match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            await self.cog.cancel_match(interaction, self.match)
        cancel_btn.callback = cancel
        self.add_item(cancel_btn)


# ── Civ Select View ────────────────────────────────────────────────────────────

class CivSelectView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=300)
        self.cog   = cog
        self.match = match
        self._build()

    def _build(self):
        self.clear_items()

        def make_civ_options(guild, exclude_civs=None):
            exclude_civs = exclude_civs or []
            options = []
            for civ_name, emoji_name in AOE_CIVS:
                if civ_name in exclude_civs:
                    continue
                emoji = discord.utils.get(guild.emojis, name=emoji_name) if guild else None
                options.append(discord.SelectOption(
                    label=civ_name,
                    value=civ_name,
                    emoji=emoji if emoji else None,
                ))
            return options

        guild = self.match.thread.guild if self.match.thread else None

        if self.match.queue_type == "1v1":
            # Separate dropdown per player, filtering out civs banned for them
            p1_banned = self.match.banned_civs_for(self.match.captain1)
            p2_banned = self.match.banned_civs_for(self.match.captain2)

            p1_select = discord.ui.Select(
                placeholder=f"{self.match.captain1.display_name}: Pick your civilization...",
                options=make_civ_options(guild, exclude_civs=p1_banned),
                row=0,
            )
            async def on_p1_select(interaction: discord.Interaction):
                if interaction.user.id != self.match.captain1.id:
                    await interaction.response.send_message("❌ This isn't your dropdown!", ephemeral=True)
                    return
                chosen = p1_select.values[0]
                self.match.civ_picks[interaction.user.id] = chosen
                await interaction.response.send_message(
                    f"✅ You picked **{chosen}**! You can change it anytime before lock-in.", ephemeral=True)
                await self.cog._refresh_civ_status(self.match)
            p1_select.callback = on_p1_select
            self.add_item(p1_select)

            p2_select = discord.ui.Select(
                placeholder=f"{self.match.captain2.display_name}: Pick your civilization...",
                options=make_civ_options(guild, exclude_civs=p2_banned),
                row=1,
            )
            async def on_p2_select(interaction: discord.Interaction):
                if interaction.user.id != self.match.captain2.id:
                    await interaction.response.send_message("❌ This isn't your dropdown!", ephemeral=True)
                    return
                chosen = p2_select.values[0]
                self.match.civ_picks[interaction.user.id] = chosen
                await interaction.response.send_message(
                    f"✅ You picked **{chosen}**! You can change it anytime before lock-in.", ephemeral=True)
                await self.cog._refresh_civ_status(self.match)
            p2_select.callback = on_p2_select
            self.add_item(p2_select)
        else:
            civ_select = discord.ui.Select(
                placeholder="🎭 Pick your civilization...",
                options=make_civ_options(guild),
                row=0,
            )
            async def on_civ_select(interaction: discord.Interaction):
                if interaction.user not in self.match.all_players:
                    await interaction.response.send_message("❌ You're not in this match!", ephemeral=True)
                    return
                chosen = civ_select.values[0]
                self.match.civ_picks[interaction.user.id] = chosen
                await interaction.response.send_message(
                    f"✅ You picked **{chosen}**! You can change it anytime before lock-in.", ephemeral=True)
                await self.cog._refresh_civ_status(self.match)
            civ_select.callback = on_civ_select
            self.add_item(civ_select)

        lock_row    = 2 if self.match.queue_type == "1v1" else 1
        cancel_row  = 3 if self.match.queue_type == "1v1" else 2

        lock1_btn = discord.ui.Button(
            label="🔒 Lock In Civs (Team 1)" if not self.match.cap1_locked else "✅ Team 1 Locked",
            style=discord.ButtonStyle.success if not self.match.cap1_locked else discord.ButtonStyle.secondary,
            disabled=self.match.cap1_locked, row=lock_row)
        async def lock1(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain1.id:
                await interaction.response.send_message("❌ Only Team 1's captain can lock in!", ephemeral=True)
                return
            if not self.match.all_picked_civs(self.match.team1):
                missing = [p.display_name for p in self.match.team1 if p.id not in self.match.civ_picks]
                await interaction.response.send_message(
                    f"❌ These Team 1 players haven't picked yet: **{', '.join(missing)}**", ephemeral=True)
                return
            self.match.cap1_locked = True
            await interaction.response.send_message("✅ Team 1 civs locked in!", ephemeral=True)
            await self.cog._refresh_civ_status(self.match)
            if self.match.civs_revealed:
                await self.cog._reveal_civs(self.match)
        lock1_btn.callback = lock1
        self.add_item(lock1_btn)

        lock2_btn = discord.ui.Button(
            label="🔒 Lock In Civs (Team 2)" if not self.match.cap2_locked else "✅ Team 2 Locked",
            style=discord.ButtonStyle.primary if not self.match.cap2_locked else discord.ButtonStyle.secondary,
            disabled=self.match.cap2_locked, row=lock_row)
        async def lock2(interaction: discord.Interaction):
            if interaction.user.id != self.match.captain2.id:
                await interaction.response.send_message("❌ Only Team 2's captain can lock in!", ephemeral=True)
                return
            if not self.match.all_picked_civs(self.match.team2):
                missing = [p.display_name for p in self.match.team2 if p.id not in self.match.civ_picks]
                await interaction.response.send_message(
                    f"❌ These Team 2 players haven't picked yet: **{', '.join(missing)}**", ephemeral=True)
                return
            self.match.cap2_locked = True
            await interaction.response.send_message("✅ Team 2 civs locked in!", ephemeral=True)
            await self.cog._refresh_civ_status(self.match)
            if self.match.civs_revealed:
                await self.cog._reveal_civs(self.match)
        lock2_btn.callback = lock2
        self.add_item(lock2_btn)

        cancel_btn = discord.ui.Button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger, row=cancel_row)
        async def cancel(interaction: discord.Interaction):
            if not self.cog._is_captain_or_admin(interaction.user, self.match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            await self.cog.cancel_match(interaction, self.match)
        cancel_btn.callback = cancel
        self.add_item(cancel_btn)


# ── Teams Confirm View ────────────────────────────────────────────────────────

class TeamsConfirmView(discord.ui.View):
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=300)
        self.cog   = cog
        self.match = match

    @discord.ui.button(label="🔄 Swap Players", style=discord.ButtonStyle.secondary)
    async def swap(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.show_swap_team1_select(interaction, self.match)

    @discord.ui.button(label="✅ Confirm Teams", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.show_civ_select(interaction, self.match)

    @discord.ui.button(label="🚫 Cancel Match", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog._is_captain_or_admin(interaction.user, self.match):
            await interaction.response.send_message("❌ Not your match!", ephemeral=True)
            return
        await self.cog.cancel_match(interaction, self.match)
        self.stop()


# ── Swap Players View ──────────────────────────────────────────────────────────

class SwapTeam1SelectView(discord.ui.View):
    """Step 1 — pick player from Team 1 (no captains)."""
    def __init__(self, cog, match: MatchState):
        super().__init__(timeout=60)
        self.cog   = cog
        self.match = match

        options = [
            discord.SelectOption(label=p.display_name, value=str(p.id))
            for p in match.team1 if p != match.captain1
        ]
        if not options:
            options = [discord.SelectOption(label="No swappable players", value="none")]

        select = discord.ui.Select(
            placeholder="Select player from Team 1 to swap...",
            options=options,
            disabled=not any(p != match.captain1 for p in match.team1),
        )
        async def on_select(interaction: discord.Interaction):
            if not self.cog._is_captain_or_admin(interaction.user, match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            if select.values[0] == "none":
                await interaction.response.send_message("❌ No swappable players in Team 1!", ephemeral=True)
                return
            p1_id = int(select.values[0])
            p1    = discord.utils.get(match.team1, id=p1_id)
            await self.cog.show_swap_team2_select(interaction, match, p1)
            self.stop()
        select.callback = on_select
        self.add_item(select)

        back_btn = discord.ui.Button(label="↩️ Back", style=discord.ButtonStyle.secondary)
        async def back(interaction: discord.Interaction):
            await self.cog.show_teams_confirm(interaction, match)
            self.stop()
        back_btn.callback = back
        self.add_item(back_btn)


class SwapTeam2SelectView(discord.ui.View):
    """Step 2 — pick player from Team 2 (no captains)."""
    def __init__(self, cog, match: MatchState, p1):
        super().__init__(timeout=60)
        self.cog   = cog
        self.match = match
        self.p1    = p1

        options = [
            discord.SelectOption(label=p.display_name, value=str(p.id))
            for p in match.team2 if p != match.captain2
        ]
        if not options:
            options = [discord.SelectOption(label="No swappable players", value="none")]

        select = discord.ui.Select(
            placeholder="Select player from Team 2 to swap...",
            options=options,
            disabled=not any(p != match.captain2 for p in match.team2),
        )
        async def on_select(interaction: discord.Interaction):
            if not self.cog._is_captain_or_admin(interaction.user, match):
                await interaction.response.send_message("❌ Not your match!", ephemeral=True)
                return
            if select.values[0] == "none":
                await interaction.response.send_message("❌ No swappable players in Team 2!", ephemeral=True)
                return
            p2_id = int(select.values[0])
            p2    = discord.utils.get(match.team2, id=p2_id)
            # Perform the swap
            idx1 = match.team1.index(p1)
            idx2 = match.team2.index(p2)
            match.team1[idx1] = p2
            match.team2[idx2] = p1
            await interaction.response.send_message(
                f"✅ Swapped **{p1.display_name}** (Team 1) ↔ **{p2.display_name}** (Team 2)!",
                ephemeral=True)
            await self.cog.show_teams_confirm_refresh(interaction, match)
            self.stop()
        select.callback = on_select
        self.add_item(select)

        back_btn = discord.ui.Button(label="↩️ Back", style=discord.ButtonStyle.secondary)
        async def back(interaction: discord.Interaction):
            await self.cog.show_swap_team1_select(interaction, match)
            self.stop()
        back_btn.callback = back
        self.add_item(back_btn)


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
        self._queues: dict         = {}
        # {guild_id: {queue_type: message}}
        self._queue_messages: dict = {}
        self._matches: dict        = {}
        self._queue_timers: dict   = {}

    def _is_admin(self, member) -> bool:
        return member.guild_permissions.administrator or member.guild_permissions.manage_channels

    def _is_admin_or_privileged(self, member) -> bool:
        """Admin or Grandmaster/King/Moderator role — NOT 'any captain'.
        Use this for team-specific overrides (e.g. Change Team 1 Captain)
        where the OTHER team's captain must not be able to act."""
        if self._is_admin(member):
            return True
        member_role_names = {r.name for r in member.roles}
        return bool(member_role_names & PRIVILEGED_ROLES)

    def _is_captain_or_admin(self, member, match: MatchState) -> bool:
        if self._is_admin(member):
            return True
        if member == match.captain1 or member == match.captain2:
            return True
        # Grandmaster, King, Moderator roles can also control match buttons
        member_role_names = {r.name for r in member.roles}
        if member_role_names & PRIVILEGED_ROLES:
            return True
        return False

    def _get_queue(self, guild_id, queue_type):
        return self._queues.setdefault(guild_id, {}).setdefault(queue_type, [])

    def _get_matches(self, guild_id):
        return self._matches.setdefault(guild_id, [])

    def _find_match_by_id(self, guild, match_id):
        for m in self._get_matches(guild.id):
            if m.match_id == match_id:
                return m
        return None

    def _get_timers(self, guild_id, queue_type):
        return self._queue_timers.setdefault(guild_id, {}).setdefault(queue_type, {})

    def _cancel_timer(self, guild_id, queue_type, member_id):
        timer = self._get_timers(guild_id, queue_type).pop(member_id, None)
        if timer and not timer.done():
            timer.cancel()

    def _start_timer(self, guild, queue_type, member):
        self._cancel_timer(guild.id, queue_type, member.id)
        task = asyncio.create_task(self._queue_timeout(guild, queue_type, member))
        self._get_timers(guild.id, queue_type)[member.id] = task

    async def _queue_timeout(self, guild, queue_type, member):
        await asyncio.sleep(QUEUE_TIMEOUT_SECS)
        queue = self._get_queue(guild.id, queue_type)
        if member not in queue:
            return
        queue.remove(member)
        self._get_timers(guild.id, queue_type).pop(member.id, None)
        logger.info("[%s] %s auto-removed from %s queue (timeout)", guild.id, member.display_name, queue_type)
        await self._post_queue_embed(guild, queue_type)
        try:
            await member.send(
                f"⏰ **Queue Timeout** — You were automatically removed from the "
                f"**{queue_type.upper()} AOE queue** in **{guild.name}** because the queue "
                f"didn't fill within 30 minutes. Rejoin anytime you're ready!")
        except Exception:
            pass

    # ── Channel setup ──────────────────────────────────────────────────────────

    async def _get_or_create_channel(self, guild, name, category=None, read_only=True):
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

    async def _delete_old_channels(self, guild):
        for name in OLD_QUEUE_CHANNELS + OLD_LB_CHANNELS:
            ch = discord.utils.get(guild.text_channels, name=name)
            if ch:
                try:
                    await ch.delete(reason="Merged into single AOE queue/leaderboard channel")
                    logger.info("[%s] Deleted old channel #%s", guild.id, name)
                except Exception as ex:
                    logger.warning("[%s] Could not delete #%s: %s", guild.id, name, ex)

    async def _setup_channels(self, guild):
        category = find_aoe_category(guild)
        self._queue_messages.setdefault(guild.id, {})

        # Delete old separate channels
        await self._delete_old_channels(guild)

        # Create single queue + leaderboard + history channels
        queue_ch = await self._get_or_create_channel(guild, QUEUE_CHANNEL_NAME, category)
        await self._get_or_create_channel(guild, LEADERBOARD_CHANNEL_NAME, category)
        await self._get_or_create_channel(guild, MATCH_HISTORY_CHANNEL, category)

        # Create/update permanent general VC
        await self._get_or_create_general_vc(guild, category)

        # Post all 4 queue embeds in single channel — purge old ones first
        if queue_ch:
            try:
                await queue_ch.purge(limit=50, check=lambda m: m.author == guild.me)
            except Exception as ex:
                logger.warning("[%s] Could not purge queue channel: %s", guild.id, ex)
            for qtype in QUEUE_CONFIGS:
                await self._post_queue_embed(guild, qtype, queue_ch)

    async def _get_or_create_general_vc(self, guild, category=None):
        vc = discord.utils.get(guild.voice_channels, name=AOE_GENERAL_VC_NAME)
        # Full open permissions — same as AOE IV (Team 1)
        full_perms = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_voice_activation=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
            add_reactions=True,
            use_application_commands=True,
        )
        bot_perms = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            manage_channels=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
            stream=True,
        )
        if vc:
            # Always sync permissions on startup to keep them correct
            try:
                await vc.set_permissions(guild.default_role, overwrite=full_perms)
                await vc.set_permissions(guild.me, overwrite=bot_perms)
                logger.info("[%s] Updated permissions for %s", guild.id, AOE_GENERAL_VC_NAME)
            except Exception as ex:
                logger.warning("[%s] Could not update general VC perms: %s", guild.id, ex)
            return vc
        if not category:
            category = find_aoe_category(guild)
        overwrites = {
            guild.default_role: full_perms,
            guild.me: bot_perms,
        }
        try:
            vc = await guild.create_voice_channel(
                AOE_GENERAL_VC_NAME, category=category, overwrites=overwrites)
            logger.info("[%s] Created permanent VC: %s", guild.id, AOE_GENERAL_VC_NAME)
        except Exception as ex:
            logger.error("[%s] Could not create general VC: %s", guild.id, ex)
            return None
        return vc

    async def _create_match_thread(self, guild, match, queue_channel):
        ch = discord.utils.get(guild.text_channels, name=QUEUE_CHANNEL_NAME)
        if not ch:
            ch = queue_channel
        thread = await ch.create_thread(
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

    # ── Temp VC management ─────────────────────────────────────────────────────

    async def _create_temp_vcs(self, guild, match):
        category = find_aoe_category(guild)
        if not category:
            return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                use_voice_activation=True,
                stream=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
            ),
        }
        try:
            vc1 = await guild.create_voice_channel(
                f"Match #{match.match_id} Team 1", category=category,
                overwrites=overwrites, rtc_region="singapore")
            vc2 = await guild.create_voice_channel(
                f"Match #{match.match_id} Team 2", category=category,
                overwrites=overwrites, rtc_region="singapore")
            match.temp_vc1 = vc1
            match.temp_vc2 = vc2
            logger.info("[%s] Created temp VCs for match #%s", guild.id, match.match_id)

            # Move each team into their temp VC
            for player in match.team1:
                member = guild.get_member(player.id)
                if member and member.voice and member.voice.channel:
                    try:
                        await member.move_to(vc1)
                    except Exception as ex:
                        logger.warning("[%s] Could not move %s to VC1: %s", guild.id, member.display_name, ex)

            for player in match.team2:
                member = guild.get_member(player.id)
                if member and member.voice and member.voice.channel:
                    try:
                        await member.move_to(vc2)
                    except Exception as ex:
                        logger.warning("[%s] Could not move %s to VC2: %s", guild.id, member.display_name, ex)

        except Exception as ex:
            logger.error("[%s] Failed to create temp VCs: %s", guild.id, ex)

    async def _cleanup_temp_vcs(self, guild, match):
        # Find target VC — try exact name first, then partial match
        target_vc = discord.utils.get(guild.voice_channels, name=AOE_GENERAL_VC_NAME)
        if not target_vc:
            category = find_aoe_category(guild)
            if category:
                target_vc = discord.utils.find(
                    lambda v: "team 1" in v.name.lower() and v.category == category,
                    guild.voice_channels)
        if not target_vc:
            logger.error("[%s] Could not find target VC '%s' — players will be disconnected",
                         guild.id, AOE_GENERAL_VC_NAME)

        for temp_vc in [match.temp_vc1, match.temp_vc2]:
            if not temp_vc:
                continue
            members_in_vc = list(temp_vc.members)
            logger.info("[%s] Cleaning up %s — %d members inside, target: %s",
                        guild.id, temp_vc.name, len(members_in_vc),
                        target_vc.name if target_vc else "None")
            if target_vc and members_in_vc:
                for member in members_in_vc:
                    try:
                        await member.move_to(target_vc)
                        logger.info("[%s] Moved %s → %s", guild.id, member.display_name, target_vc.name)
                        # Small delay between each move so Discord has time to
                        # properly apply the target VC's permissions to the member.
                        # Without this delay members arrive "muted" and have to
                        # rejoin manually to get speaking permissions.
                        await asyncio.sleep(0.5)
                    except Exception as ex:
                        logger.error("[%s] Could not move %s to %s: %s",
                                     guild.id, member.display_name, target_vc.name, ex)
            # Small delay to ensure all moves complete before deleting
            await asyncio.sleep(2)
            try:
                await temp_vc.delete(reason=f"Match #{match.match_id} ended")
                logger.info("[%s] Deleted temp VC: %s", guild.id, temp_vc.name)
            except Exception as ex:
                logger.warning("[%s] Could not delete temp VC: %s", guild.id, ex)
        match.temp_vc1 = None
        match.temp_vc2 = None

    # ── Queue embed ────────────────────────────────────────────────────────────

    async def _post_queue_embed(self, guild, queue_type, channel=None):
        if channel is None:
            channel = discord.utils.get(guild.text_channels, name=QUEUE_CHANNEL_NAME)
        if not channel:
            return

        gid    = guild.id
        queue  = self._get_queue(gid, queue_type)
        needed = QUEUE_CONFIGS[queue_type]["size"]

        e = discord.Embed(
            title=f"⚔️ AOE 4 — {queue_type.upper()} Queue",
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc))
        e.add_field(
            name=f"Players ({len(queue)}/{needed})",
            value="\n".join(f"• {m.display_name}" for m in queue) or "*Empty — be the first!*",
            inline=False)
        e.set_footer(text=f"Need {needed - len(queue)} more player(s) to start")

        view     = QueueView(self, queue_type)
        existing = self._queue_messages.get(gid, {}).get(queue_type)
        if existing:
            try:
                await existing.edit(embed=e, view=view)
                return
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

    async def _start_match(self, guild, queue_type, players, queue_channel):
        match          = MatchState(queue_type, players)
        match.match_id = await db.create_aoe_match(str(guild.id), queue_type, [str(p.id) for p in players])
        self._get_matches(guild.id).append(match)
        for p in players:
            self._cancel_timer(guild.id, queue_type, p.id)

        thread   = await self._create_match_thread(guild, match, queue_channel)
        mentions = " ".join(p.mention for p in players)
        await thread.send(
            f"🎮 **Queue popped!** {mentions}\n"
            f"Your **{queue_type.upper()}** match is ready — Match ID: **#{match.match_id}**")

        if queue_type == "1v1":
            match.draft_complete = True
            match.phase = "map_veto_coinflip"
            await self._show_map_veto_coinflip(guild, match, thread)
        else:
            await self._show_coin_flip(guild, match, thread)

    # ── 1v1 Map Veto Coin Flip ─────────────────────────────────────────────────

    async def _show_map_veto_coinflip(self, guild, match, thread):
        flipper = match.captain1
        e = discord.Embed(
            title="🪙 Map Veto — Coin Flip!",
            description=(
                f"{flipper.mention} — you're flipping the coin!\n\n"
                f"**Player 1:** {match.captain1.mention}\n"
                f"**Player 2:** {match.captain2.mention}\n\n"
                f"Winner chooses to **ban first** or **ban second** in the map veto."
            ),
            color=0xF1C40F,
            timestamp=datetime.now(timezone.utc),
        )
        view = MapVetoCoinFlipView(self, match, flipper)
        msg  = await thread.send(embed=e, view=view)
        match.thread_message = msg

    async def resolve_map_veto_flip(self, interaction, match, choice):
        result = random.choice(["heads", "tails"])
        won    = choice == result
        winner = match.captain1 if won else match.captain2

        e = discord.Embed(
            title=f"🪙 Coin landed on **{result.upper()}**!",
            description=f"{winner.mention} **won the flip!**\n\nChoose your map ban order:",
            color=0xF1C40F, timestamp=datetime.now(timezone.utc))
        view = MapVetoOrderChoiceView(self, match, winner)
        await interaction.response.edit_message(embed=e, view=view)

    # ── 1v1 Map Veto ───────────────────────────────────────────────────────────

    async def show_map_veto(self, interaction, match):
        match.phase = "map_veto"
        embed = self._build_map_veto_embed(match)
        view  = MapVetoView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    def _build_map_veto_embed(self, match):
        banner = match.current_map_banner()
        e = discord.Embed(
            title="🗺️ Map Veto",
            description="Take turns banning maps until only **1 remains**.",
            color=0xE74C3C,
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(
            name=f"🗺️ Remaining Maps ({len(match.map_pool)})",
            value="\n".join(f"• {m}" for m in match.map_pool) or "—",
            inline=True,
        )
        if match.map_bans:
            ban_lines = [f"❌ {m} (by {p.display_name})" for p, m in match.map_bans]
            e.add_field(name="🚫 Banned Maps", value="\n".join(ban_lines), inline=True)
        e.set_footer(text=f"👑 {banner.display_name}'s turn to ban | Match #{match.match_id}")
        return e

    # ── 1v1 Civ Ban ────────────────────────────────────────────────────────────

    async def show_civ_ban(self, interaction, match):
        match.phase = "civ_ban"
        embed = self._build_civ_ban_embed(match)
        view  = CivBanView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    def _build_civ_ban_embed(self, match):
        e = discord.Embed(
            title="🚫 Civilization Ban Phase",
            description=(
                f"🗺️ **Map:** {match.final_map}\n\n"
                "Each player bans **1 civilization** the *other* player cannot pick.\n"
                "Bans are hidden until both players lock in."
            ),
            color=0x992D22,
            timestamp=datetime.now(timezone.utc),
        )
        p1_status = "✅ Locked" if match.civ_ban_p1_locked else (
            "⏳ Chosen, not locked" if match.civ_ban_p1_choice else "⏳ Choosing...")
        p2_status = "✅ Locked" if match.civ_ban_p2_locked else (
            "⏳ Chosen, not locked" if match.civ_ban_p2_choice else "⏳ Choosing...")
        e.add_field(name=f"Player 1 — {match.captain1.display_name}", value=p1_status, inline=True)
        e.add_field(name=f"Player 2 — {match.captain2.display_name}", value=p2_status, inline=True)
        e.set_footer(text=f"Match #{match.match_id} • Bans revealed when both lock in")
        return e

    async def _refresh_civ_ban(self, match):
        if not match.thread_message or not match.thread:
            return
        try:
            embed = self._build_civ_ban_embed(match)
            view  = CivBanView(self, match)
            await match.thread_message.edit(embed=embed, view=view)
        except Exception as ex:
            logger.warning("Could not refresh civ ban status: %s", ex)

    async def _proceed_to_civ_select_after_ban(self, match):
        """Both civ bans locked — reveal bans, then move to civ selection."""
        if not match.thread:
            return
        e = discord.Embed(
            title="🚫 Civilizations Banned!",
            description=f"🗺️ **Map:** {match.final_map}",
            color=0x992D22,
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(
            name=f"{match.captain1.display_name} banned for {match.captain2.display_name}",
            value=f"**{match.civ_ban_p1_choice}**", inline=True)
        e.add_field(
            name=f"{match.captain2.display_name} banned for {match.captain1.display_name}",
            value=f"**{match.civ_ban_p2_choice}**", inline=True)
        e.set_footer(text=f"Match #{match.match_id} • Moving to civ selection...")
        try:
            await match.thread_message.edit(embed=e, view=None)
        except Exception:
            pass

        await asyncio.sleep(3)
        match.phase = "civ_select"
        guild = match.thread.guild

        # Create temp VCs and move players — was missing for the 1v1 ban flow
        await self._create_temp_vcs(guild, match)

        embed = self._build_civ_status_embed(guild, match)
        view  = CivSelectView(self, match)
        msg   = await match.thread.send(embed=embed, view=view)
        match.thread_message = msg

        if match.temp_vc1 and match.temp_vc2:
            await match.thread.send(
                f"🔊 Two VCs created for this match!\n"
                f"**Team 1:** {match.temp_vc1.mention}\n"
                f"**Team 2:** {match.temp_vc2.mention}\n"
                f"Everyone moves to **{AOE_GENERAL_VC_NAME}** when match ends."
            )

    # ── Coin flip ──────────────────────────────────────────────────────────────

    async def _show_coin_flip(self, guild, match, thread):
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

    async def resolve_flip(self, interaction, match, choice):
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

    async def show_draft(self, interaction, match):
        # DraftView._build() auto-assigns last player if only 1 remains.
        # After that, draft_complete will be True — go to Teams Confirm instead.
        DraftView(self, match)  # trigger auto-assign side effect if applicable
        if match.draft_complete:
            await self.show_teams_confirm(interaction, match)
            return
        embed = await self._build_draft_embed(interaction.guild, match)
        view  = DraftView(self, match)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _build_draft_embed(self, guild, match):
        qt     = match.queue_type
        picker = match.current_picker()
        e = discord.Embed(title=f"⚔️ {qt.upper()} Draft", color=0x3498DB,
                          timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            t1_lines.append(f"{p.display_name}{cap_tag}")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines) or "—", inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            t2_lines.append(f"{p.display_name}{cap_tag}")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines) or "—", inline=True)

        pool_lines = [p.display_name for p in match.remaining]
        e.add_field(name="🎯 Player Pool",
                    value="\n".join(pool_lines) or "All players drafted!", inline=False)

        footer = f"👑 {picker.display_name}'s turn to pick" if picker else "Draft complete!"
        e.set_footer(text=f"{footer} | Match #{match.match_id}")
        return e

    async def _refresh_coin_flip(self, interaction, match):
        """Rebuild and show the coin flip screen after a captain swap."""
        flipper = match.captain1
        e = discord.Embed(
            title="🪙 Coin Flip!",
            description=(
                f"{flipper.mention} — you're flipping the coin!\n\n"
                f"**Team 1 Captain:** {match.captain1.mention}\n"
                f"**Team 2 Captain:** {match.captain2.mention}\n\n"
                f"Winner chooses **First Pick** or **Second Pick**."
            ),
            color=0xF1C40F, timestamp=datetime.now(timezone.utc))
        view = CoinFlipView(self, match, flipper)
        await interaction.response.defer()
        await interaction.message.edit(embed=e, view=view)

    async def show_change_captain_from_flip(self, interaction, match, team):
        """Open Change Captain from coin flip phase — returns to coin flip after swap."""
        e = discord.Embed(
            title=f"🔄 Replace Team {team} Captain",
            description="Select a player from your team to become the new captain.\nYou will return to the coin flip after swapping.",
            color=0x95A5A6)
        view = ChangeCaptainFromFlipView(self, match, team)
        await interaction.response.defer()
        await interaction.message.edit(embed=e, view=view)

    async def show_change_captain(self, interaction, match, team):
        e = discord.Embed(title=f"🔄 Change Team {team} Captain",
                          description="Select a player from your team to become the new captain.",
                          color=0x95A5A6)
        view = ChangeCaptainView(self, match, team)
        await interaction.response.edit_message(embed=e, view=view)

    # ── Civ Selection ──────────────────────────────────────────────────────────

    # ── Teams Confirm ─────────────────────────────────────────────────────────────

    async def show_teams_confirm(self, interaction, match):
        """Show teams confirm screen from a button interaction."""
        match.phase = "teams_confirm"
        embed = self._build_teams_confirm_embed(match)
        view  = TeamsConfirmView(self, match)
        await interaction.response.defer()
        await interaction.message.edit(embed=embed, view=view)

    async def show_teams_confirm_refresh(self, interaction, match):
        """Refresh teams confirm screen after a swap."""
        embed = self._build_teams_confirm_embed(match)
        view  = TeamsConfirmView(self, match)
        await interaction.response.defer()
        await interaction.message.edit(embed=embed, view=view)

    async def show_teams_confirm_fresh(self, guild, match, thread):
        """Post teams confirm as fresh message in thread (for 2v2/3v3/4v4)."""
        match.phase = "teams_confirm"
        embed = self._build_teams_confirm_embed(match)
        view  = TeamsConfirmView(self, match)
        msg   = await thread.send(embed=embed, view=view)
        match.thread_message = msg

    def _build_teams_confirm_embed(self, match):
        qt = match.queue_type
        e  = discord.Embed(
            title=f"✅ Draft Complete — {qt.upper()}",
            description=(
                "Teams are set! Swap players if needed, then confirm to proceed to civ selection. "
                "**Captains cannot be swapped.**"
            ),
            color=0x2ECC71,
            timestamp=datetime.now(timezone.utc),
        )
        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            t1_lines.append(f"{p.display_name}{cap_tag}")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            t2_lines.append(f"{p.display_name}{cap_tag}")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines), inline=True)
        e.set_footer(text=f"Match #{match.match_id} • Swap players or confirm teams")
        return e

    async def show_swap_team1_select(self, interaction, match):
        e = discord.Embed(
            title="🔄 Swap Players — Step 1",
            description="Select the player from **Team 1** you want to swap. Captains are not swappable.",
            color=0x95A5A6)
        view = SwapTeam1SelectView(self, match)
        await interaction.response.defer()
        await interaction.message.edit(embed=e, view=view)

    async def show_swap_team2_select(self, interaction, match, p1):
        e = discord.Embed(
            title="🔄 Swap Players — Step 2",
            description=f"Swapping **{p1.display_name}** (Team 1) with... Select the player from **Team 2**.",
            color=0x95A5A6)
        view = SwapTeam2SelectView(self, match, p1)
        await interaction.response.defer()
        await interaction.message.edit(embed=e, view=view)

    async def _show_civ_select_fresh(self, guild, match, thread):
        match.phase = "civ_select"
        await self._create_temp_vcs(guild, match)
        embed = self._build_civ_status_embed(guild, match)
        view  = CivSelectView(self, match)
        msg   = await thread.send(embed=embed, view=view)
        match.thread_message = msg
        if match.temp_vc1 and match.temp_vc2:
            await thread.send(
                f"\U0001f509 Two VCs created for this match!\n"
                f"**Team 1:** {match.temp_vc1.mention}\n"
                f"**Team 2:** {match.temp_vc2.mention}\n"
                f"Everyone moves to **{AOE_GENERAL_VC_NAME}** when match ends.")

    async def show_civ_select(self, interaction, match):
        match.phase = "civ_select"
        await self._create_temp_vcs(interaction.guild, match)
        embed = self._build_civ_status_embed(interaction.guild, match)
        view  = CivSelectView(self, match)
        # Use defer + message.edit since this may be called from a standalone
        # thread message (Teams Confirm) rather than an original interaction response
        await interaction.response.defer()
        await interaction.message.edit(embed=embed, view=view)
        match.thread_message = interaction.message
        if match.temp_vc1 and match.temp_vc2 and match.thread:
            await match.thread.send(
                f"\U0001f509 Two VCs created for this match!\n"
                f"**Team 1:** {match.temp_vc1.mention}\n"
                f"**Team 2:** {match.temp_vc2.mention}\n"
                f"Everyone moves to **{AOE_GENERAL_VC_NAME}** when match ends.")

    def _build_civ_status_embed(self, guild, match):
        e = discord.Embed(
            title=f"🎭 Civilization Selection — {match.queue_type.upper()}",
            description=(
                "Each player picks their civ using the dropdown below.\n"
                "You can change anytime before your captain locks in.\n"
                "**Civs are hidden until both captains lock in!**"
            ),
            color=0x9B59B6, timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            picked  = "✅ Ready" if p.id in match.civ_picks else "⏳ Picking..."
            t1_lines.append(f"{p.display_name}{cap_tag} — {picked}")
        e.add_field(
            name=f"🔴 Team 1 {'✅ Locked' if match.cap1_locked else '⏳ Picking'}",
            value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            picked  = "✅ Ready" if p.id in match.civ_picks else "⏳ Picking..."
            t2_lines.append(f"{p.display_name}{cap_tag} — {picked}")
        e.add_field(
            name=f"🔵 Team 2 {'✅ Locked' if match.cap2_locked else '⏳ Picking'}",
            value="\n".join(t2_lines), inline=True)

        e.set_footer(text=f"Match #{match.match_id} • Civs revealed when both captains lock in")
        return e

    async def _refresh_civ_status(self, match):
        if not match.thread_message or not match.thread:
            return
        try:
            embed = self._build_civ_status_embed(match.thread.guild, match)
            view  = CivSelectView(self, match)
            await match.thread_message.edit(embed=embed, view=view)
        except Exception as ex:
            logger.warning("Could not refresh civ status: %s", ex)

    async def _reveal_civs(self, match):
        if not match.thread:
            return
        guild = match.thread.guild
        gid   = str(guild.id)
        qt    = match.queue_type

        e = discord.Embed(
            title=f"🎭 Civilizations Revealed! — {qt.upper()}",
            color=0x9B59B6, timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = match.civ_picks.get(p.id, "Unknown")
            t1_lines.append(f"{p.display_name}{cap_tag} — **{civ}**")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = match.civ_picks.get(p.id, "Unknown")
            t2_lines.append(f"{p.display_name}{cap_tag} — **{civ}**")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines), inline=True)
        e.set_footer(text=f"Match #{match.match_id} • Both teams locked in — ready to play!")

        match.phase = "pre_match"
        view = PreMatchView(self, match)
        try:
            await match.thread_message.edit(embed=e, view=view)
        except Exception:
            msg = await match.thread.send(embed=e, view=view)
            match.thread_message = msg

    # ── Pre-match / In-match ───────────────────────────────────────────────────

    async def show_pre_match(self, interaction, match):
        embed = self._build_teams_embed(match, phase="pre_match")
        view  = PreMatchView(self, match)
        await interaction.response.defer()
        await interaction.message.edit(embed=embed, view=view)

    async def show_in_match(self, interaction, match):
        embed = self._build_teams_embed(match, phase="in_match")
        view  = InMatchView(self, match)
        await interaction.response.defer()
        await interaction.message.edit(embed=embed, view=view)

    def _build_teams_embed(self, match, phase="pre_match"):
        qt     = match.queue_type
        colors = {"pre_match": 0x2ECC71, "in_match": 0xE74C3C}
        titles = {"pre_match": f"✅ Teams Set — {qt.upper()}", "in_match": f"⚔️ Match In Progress — {qt.upper()}"}
        e = discord.Embed(title=titles.get(phase, f"⚔️ {qt.upper()}"),
                          color=colors.get(phase, 0xE67E22), timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = f" — **{match.civ_picks[p.id]}**" if p.id in match.civ_picks else ""
            t1_lines.append(f"{p.display_name}{cap_tag}{civ}")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = f" — **{match.civ_picks[p.id]}**" if p.id in match.civ_picks else ""
            t2_lines.append(f"{p.display_name}{cap_tag}{civ}")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines), inline=True)
        e.set_footer(text=f"Match #{match.match_id}")
        return e

    # ── Resolve / Cancel ───────────────────────────────────────────────────────

    async def resolve_match(self, interaction, match, winner):
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

        civ_data = {str(k): v for k, v in match.civ_picks.items()}
        await db.finish_aoe_match(match.match_id, f"team{winner}",
                                   [str(p.id) for p in match.team1],
                                   [str(p.id) for p in match.team2], civ_data)

        e = discord.Embed(title=f"🏆 Team {winner} Victory! — {qt.upper()}",
                          color=0xFFD700, timestamp=datetime.now(timezone.utc))

        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = match.civ_picks.get(p.id, "?")
            tag     = "🏆" if winner == 1 else "💔"
            t1_lines.append(f"{tag} {p.display_name}{cap_tag} — **{civ}**")
        e.add_field(name=f"{'🏆' if winner==1 else '💔'} Team 1", value="\n".join(t1_lines), inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = match.civ_picks.get(p.id, "?")
            tag     = "🏆" if winner == 2 else "💔"
            t2_lines.append(f"{tag} {p.display_name}{cap_tag} — **{civ}**")
        e.add_field(name=f"{'🏆' if winner==2 else '💔'} Team 2", value="\n".join(t2_lines), inline=True)
        e.add_field(name="🧀 Coin Reward",
                    value=f"Winning team each received **{WIN_COINS} 🧀 Cheese Coins!**", inline=False)
        e.set_footer(text=f"Match #{match.match_id} • Players moved to AOE IV General in {RESULT_DISPLAY_SECS}s")

        await interaction.message.edit(embed=e, view=None)
        await self._post_match_history(guild, match, result=f"Team {winner} Victory",
                                        winning_team=winning_team, losing_team=losing_team)
        await self._update_leaderboard(guild)
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        # Wait 90s then move players and delete thread
        await asyncio.sleep(RESULT_DISPLAY_SECS)
        await self._cleanup_temp_vcs(guild, match)
        if match.thread:
            try:
                await match.thread.delete()
            except Exception:
                pass

    async def cancel_match(self, interaction, match):
        await interaction.response.defer(thinking=False)
        guild = interaction.guild
        gid   = str(guild.id)
        qt    = match.queue_type

        for p in match.all_players:
            await db.update_aoe_stats(gid, str(p.id), qt, result="no_result")
        civ_data = {str(k): v for k, v in match.civ_picks.items()}
        await db.finish_aoe_match(match.match_id, "cancelled",
                                   [str(p.id) for p in match.team1],
                                   [str(p.id) for p in match.team2], civ_data)

        e = discord.Embed(
            title=f"🚫 Match Cancelled — {qt.upper()}",
            description=f"Match cancelled. No changes.\nThread closes in {RESULT_DISPLAY_SECS}s.",
            color=0x95A5A6, timestamp=datetime.now(timezone.utc))
        e.set_footer(text=f"Match #{match.match_id}")
        await interaction.message.edit(embed=e, view=None)

        await self._post_match_history(guild, match, result="Cancelled", winning_team=[], losing_team=[])
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)

        # Wait 90s then move players and delete thread
        await asyncio.sleep(RESULT_DISPLAY_SECS)
        await self._cleanup_temp_vcs(guild, match)
        if match.thread:
            try:
                await match.thread.delete()
            except Exception:
                pass

    # ── Match history ──────────────────────────────────────────────────────────

    async def _post_match_history(self, guild, match, result, winning_team, losing_team):
        ch = discord.utils.get(guild.text_channels, name=MATCH_HISTORY_CHANNEL)
        if not ch:
            return
        qt = match.queue_type
        e  = discord.Embed(
            title=f"📜 Match #{match.match_id} — {qt.upper()} | {result}",
            color=0xFFD700 if "Victory" in result else 0x95A5A6,
            timestamp=datetime.now(timezone.utc))

        # Show map played (1v1 only, if map veto completed)
        if qt == "1v1" and getattr(match, "final_map", None):
            e.add_field(name="🗺️ Map Played", value=f"**{match.final_map}**", inline=False)

        # Show civ bans (1v1 only, if civ ban phase completed)
        if qt == "1v1" and (match.civ_ban_p1_choice or match.civ_ban_p2_choice):
            ban_lines = []
            if match.civ_ban_p1_choice:
                ban_lines.append(
                    f"{match.captain1.display_name} banned **{match.civ_ban_p1_choice}** "
                    f"(for {match.captain2.display_name})")
            if match.civ_ban_p2_choice:
                ban_lines.append(
                    f"{match.captain2.display_name} banned **{match.civ_ban_p2_choice}** "
                    f"(for {match.captain1.display_name})")
            e.add_field(name="🚫 Civ Bans", value="\n".join(ban_lines), inline=False)

        t1_lines = []
        for p in match.team1:
            cap_tag = " 👑" if p == match.captain1 else ""
            civ     = match.civ_picks.get(p.id, "—")
            tag     = "🏆" if p in winning_team else ("💔" if losing_team else "🚫")
            t1_lines.append(f"{tag} {p.display_name}{cap_tag} — **{civ}**")
        e.add_field(name="🔴 Team 1", value="\n".join(t1_lines) or "—", inline=True)

        t2_lines = []
        for p in match.team2:
            cap_tag = " 👑" if p == match.captain2 else ""
            civ     = match.civ_picks.get(p.id, "—")
            tag     = "🏆" if p in winning_team else ("💔" if losing_team else "🚫")
            t2_lines.append(f"{tag} {p.display_name}{cap_tag} — **{civ}**")
        e.add_field(name="🔵 Team 2", value="\n".join(t2_lines) or "—", inline=True)
        e.set_footer(text=f"Match #{match.match_id} • {guild.name}")
        try:
            await ch.send(embed=e)
        except Exception as ex:
            logger.error("[%s] Failed to post match history: %s", guild.id, ex)

    # ── Leaderboard ────────────────────────────────────────────────────────────

    async def _update_leaderboard(self, guild):
        ch = discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL_NAME)
        if not ch:
            return
        try:
            await ch.purge(limit=10, check=lambda m: m.author == guild.me)
        except Exception:
            pass

        for qt in QUEUE_CONFIGS:
            board = await db.get_aoe_leaderboard(str(guild.id), qt)
            now   = datetime.now(timezone.utc)
            # Only show players with at least 1 win or 1 loss
            filtered = [r for r in board if r["wins"] > 0 or r["losses"] > 0]
            if not filtered:
                e = discord.Embed(title=f"⚔️ AOE 4 — {qt.upper()} Leaderboard",
                                  description="No matches played yet!",
                                  color=0xE67E22, timestamp=now)
            else:
                rows = []
                for i, row in enumerate(filtered):
                    member  = guild.get_member(int(row["user_id"]))
                    name    = member.display_name if member else f"Unknown ({row['user_id']})"
                    total   = row["wins"] + row["losses"]
                    win_pct = f"{(row['wins']/total*100):.1f}%" if total > 0 else "0%"
                    medal   = ["🥇", "🥈", "🥉"][i] if i < 3 else f"`{i+1}.`"
                    rows.append(f"{medal} **{name}** — W:{row['wins']} L:{row['losses']} NR:{row['no_results']} WR:{win_pct}")
                e = discord.Embed(title=f"⚔️ AOE 4 — {qt.upper()} Leaderboard",
                                  description="\n".join(rows), color=0xE67E22, timestamp=now)
            e.set_footer(text=f"Updates after each match • {guild.name}")
            try:
                await ch.send(embed=e)
            except Exception as ex:
                logger.error("[%s] Failed to post leaderboard: %s", guild.id, ex)

    # ── on_ready ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._setup_channels(guild)

    # ── Slash commands ─────────────────────────────────────────────────────────

    @discord.app_commands.command(name="aoe_setup", description="Set up AOE 4 channels (admin only).")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._setup_channels(interaction.guild)
        await interaction.followup.send("✅ AOE 4 channels set up!", ephemeral=True)

    @discord.app_commands.command(name="aoe_leaderboard", description="Refresh AOE leaderboard (admin only).")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._update_leaderboard(interaction.guild)
        await interaction.followup.send("✅ AOE leaderboard refreshed!", ephemeral=True)

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
                        value=f"W:{stats['wins']} L:{stats['losses']} NR:{stats['no_results']} WR:{wp}",
                        inline=True)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(name="aoe_listmatches", description="List all active AOE matches (admin only).")
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
                value=f"🔴 {t1}\n🔵 {t2}", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(name="aoe_forcecancel", description="Force cancel an active AOE match (admin only).")
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
                e_t = discord.Embed(
                    title=f"🚫 Match Force Cancelled — {qt.upper()}",
                    description=f"Cancelled by admin. No changes. Thread closes in {RESULT_DISPLAY_SECS}s.",
                    color=0x95A5A6, timestamp=datetime.now(timezone.utc))
                e_t.set_footer(text=f"Match #{match.match_id} • By {interaction.user.display_name}")
                await match.thread.send(embed=e_t)
            except Exception:
                pass
        await self._post_match_history(guild, match, result="Force Cancelled (Admin)",
                                        winning_team=[], losing_team=[])
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)
        await self._cleanup_temp_vcs(guild, match)
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

    @discord.app_commands.command(name="aoe_forcestart", description="Force start an active AOE match (admin only).")
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
                embed = self._build_teams_embed(match, phase="in_match")
                embed.set_footer(text=f"Match #{match.match_id} • Force started by {interaction.user.display_name}")
                view = InMatchView(self, match)
                await match.thread.send(embed=embed, view=view)
            except Exception as ex:
                logger.warning("[%s] Could not post force start: %s", guild.id, ex)
        e = discord.Embed(title="✅ Match Force Started", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID", value=f"#{match_id}",          inline=True)
        e.add_field(name="📋 Queue",    value=match.queue_type.upper(), inline=True)
        e.add_field(name="🔴 Team 1",   value=", ".join(p.display_name for p in match.team1), inline=True)
        e.add_field(name="🔵 Team 2",   value=", ".join(p.display_name for p in match.team2), inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(name="aoe_forcevictory", description="Force assign victory to a team (admin only).")
    @discord.app_commands.describe(match_id="Match ID", winning_team="Which team wins")
    @discord.app_commands.choices(winning_team=[
        discord.app_commands.Choice(name="Team 1 🔴", value=1),
        discord.app_commands.Choice(name="Team 2 🔵", value=2),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forcevictory(self, interaction: discord.Interaction, match_id: int, winning_team: int):
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
                    title=f"🏆 Team {winning_team} Victory! — {qt.upper()} (Admin)",
                    color=0xFFD700, timestamp=datetime.now(timezone.utc))
                t1_lines = [f"{'🏆' if winning_team==1 else '💔'} {p.display_name}"
                            f"{' 👑' if p==match.captain1 else ''} — **{match.civ_picks.get(p.id,'—')}**"
                            for p in match.team1]
                t2_lines = [f"{'🏆' if winning_team==2 else '💔'} {p.display_name}"
                            f"{' 👑' if p==match.captain2 else ''} — **{match.civ_picks.get(p.id,'—')}**"
                            for p in match.team2]
                e_t.add_field(name=f"{'🏆' if winning_team==1 else '💔'} Team 1", value="\n".join(t1_lines), inline=True)
                e_t.add_field(name=f"{'🏆' if winning_team==2 else '💔'} Team 2", value="\n".join(t2_lines), inline=True)
                e_t.add_field(name="🧀 Coins", value=f"{WIN_COINS} 🧀 each to winners!", inline=False)
                e_t.set_footer(text=f"Match #{match.match_id} • By {interaction.user.display_name} • Thread closes in {RESULT_DISPLAY_SECS}s")
                await match.thread.send(embed=e_t)
            except Exception as ex:
                logger.warning("[%s] Could not post force victory: %s", guild.id, ex)
        await self._post_match_history(guild, match, result=f"Team {winning_team} Victory (Admin)",
                                        winning_team=winning, losing_team=losing)
        await self._update_leaderboard(guild)
        if match in self._get_matches(guild.id):
            self._get_matches(guild.id).remove(match)
        await self._cleanup_temp_vcs(guild, match)
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

    @discord.app_commands.command(name="aoe_addwin", description="Add a win to a player's AOE stats (admin only).")
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
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild)

    @discord.app_commands.command(name="aoe_removewin", description="Remove a win from a player's AOE stats (admin only).")
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
            await interaction.followup.send(f"❌ **{member.display_name}** has no wins in {queue_type.upper()}!", ephemeral=True)
            return
        remove = min(amount, stats["wins"])
        await db.adjust_aoe_stats(gid, str(member.id), queue_type, wins_delta=-remove)
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
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild)

    @discord.app_commands.command(name="aoe_addloss", description="Add a loss to a player's AOE stats (admin only).")
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
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild)

    @discord.app_commands.command(name="aoe_removeloss", description="Remove a loss from a player's AOE stats (admin only).")
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
            await interaction.followup.send(f"❌ **{member.display_name}** has no losses in {queue_type.upper()}!", ephemeral=True)
            return
        remove = min(amount, stats["losses"])
        await db.adjust_aoe_stats(gid, str(member.id), queue_type, losses_delta=-remove)
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
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild)

    @discord.app_commands.command(name="aoe_resetstats", description="Reset a player's AOE stats (admin only).")
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
        e.add_field(name="🔄 Reset",  value="W:0 L:0 NR:0", inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild)


    @discord.app_commands.command(
        name="aoe_changeplayer",
        description="Replace a player in an active match (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        old_player="Player to remove from the match",
        new_player="Player to add to the match",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_changeplayer(self, interaction: discord.Interaction,
                                match_id: int,
                                old_player: discord.Member,
                                new_player: discord.Member):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        gid   = str(guild.id)
        match = self._find_match_by_id(guild, match_id)

        if not match:
            await interaction.followup.send(
                f"❌ No active match with ID **#{match_id}**. Use `/aoe_listmatches`.",
                ephemeral=True)
            return

        if old_player not in match.all_players:
            await interaction.followup.send(
                f"❌ **{old_player.display_name}** is not in match **#{match_id}**.",
                ephemeral=True)
            return

        if new_player in match.all_players:
            await interaction.followup.send(
                f"❌ **{new_player.display_name}** is already in this match!",
                ephemeral=True)
            return

        # Find which team old_player is in
        team_num = match.team_of(old_player)
        team     = match.team1 if team_num == 1 else match.team2
        was_captain = (old_player == match.captain1 or old_player == match.captain2)

        # Replace in team list
        idx = team.index(old_player)
        team[idx] = new_player

        # Replace in all_players
        ap_idx = match.all_players.index(old_player)
        match.all_players[ap_idx] = new_player

        # If old player was captain, new player becomes captain
        if old_player == match.captain1:
            match.captain1 = new_player
        elif old_player == match.captain2:
            match.captain2 = new_player

        # Handle VC moves if in civ select or in_match phase
        if match.phase in ("civ_select", "in_match", "pre_match"):
            temp_vc = match.temp_vc1 if team_num == 1 else match.temp_vc2
            # Move old player out of temp VC
            old_member = guild.get_member(old_player.id)
            if old_member and old_member.voice and temp_vc and old_member.voice.channel == temp_vc:
                try:
                    await old_member.move_to(None)  # disconnect from temp VC
                except Exception:
                    pass
            # Move new player into temp VC
            new_member = guild.get_member(new_player.id)
            if new_member and new_member.voice and temp_vc:
                try:
                    await new_member.move_to(temp_vc)
                except Exception:
                    pass

        # Transfer civ pick if old player had one
        if old_player.id in match.civ_picks:
            match.civ_picks[new_player.id] = match.civ_picks.pop(old_player.id)

        # Update thread name if captain changed
        if was_captain and match.thread:
            try:
                await match.thread.edit(name=match.thread_name())
            except Exception:
                pass

        # Notify in thread
        if match.thread:
            try:
                cap_note = " (now captain)" if was_captain else ""
                await match.thread.send(
                    f"🔄 **{old_player.display_name}** has been replaced by **{new_player.mention}** in Team {team_num}{cap_note}. Done by {interaction.user.mention}"
                )
            except Exception:
                pass

        logger.info("[%s] Admin %s replaced %s with %s in match #%s team %s",
                    guild.id, interaction.user.display_name,
                    old_player.display_name, new_player.display_name,
                    match_id, team_num)

        e = discord.Embed(title="✅ Player Changed", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID",   value=f"#{match_id}",              inline=True)
        e.add_field(name="👥 Team",       value=f"Team {team_num}",          inline=True)
        e.add_field(name="➖ Removed",     value=old_player.display_name,     inline=True)
        e.add_field(name="➕ Added",       value=new_player.display_name,     inline=True)
        if was_captain:
            e.add_field(name="👑 Captain", value=f"{new_player.display_name} is now captain", inline=False)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)


    @discord.app_commands.command(
        name="aoe_removefromleaderboard",
        description="Remove a player from the AOE leaderboard (admin only).",
    )
    @discord.app_commands.describe(
        member="Player to remove",
        queue_type="Which queue type to remove from",
    )
    @discord.app_commands.choices(queue_type=[
        discord.app_commands.Choice(name="1v1", value="1v1"),
        discord.app_commands.Choice(name="2v2", value="2v2"),
        discord.app_commands.Choice(name="3v3", value="3v3"),
        discord.app_commands.Choice(name="4v4", value="4v4"),
        discord.app_commands.Choice(name="All queues", value="all"),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_removefromleaderboard(self, interaction: discord.Interaction,
                                         member: discord.Member, queue_type: str):
        await interaction.response.defer(ephemeral=True)
        gid    = str(interaction.guild.id)
        uid    = str(member.id)
        queues = list(QUEUE_CONFIGS.keys()) if queue_type == "all" else [queue_type]
        removed = []
        for qt in queues:
            stats = await db.get_aoe_stats(gid, uid, qt)
            if stats["wins"] == 0 and stats["losses"] == 0 and stats["no_results"] == 0:
                continue
            await db.reset_aoe_stats(gid, uid, qt)
            removed.append(qt.upper())
        if not removed:
            await interaction.followup.send(
                f"⚠️ **{member.display_name}** has no stats to remove.", ephemeral=True)
            return
        label = ", ".join(removed)
        e = discord.Embed(title="✅ Player Removed from Leaderboard",
                          color=0xED4245, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="👤 Player", value=member.mention, inline=True)
        e.add_field(name="🎮 Queue",  value=label,          inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        await self._update_leaderboard(interaction.guild)
        logger.info("[%s] Admin %s removed %s from leaderboard (%s)",
                    interaction.guild.id, interaction.user.display_name,
                    member.display_name, label)


    @discord.app_commands.command(
        name="aoe_swapplayers",
        description="Swap two non-captain players between teams in an active match (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        player1="Player from Team 1 to swap",
        player2="Player from Team 2 to swap",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_swapplayers(self, interaction: discord.Interaction,
                               match_id: int,
                               player1: discord.Member,
                               player2: discord.Member):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)

        if not match:
            await interaction.followup.send(
                f"❌ No active match with ID **#{match_id}**. Use `/aoe_listmatches`.",
                ephemeral=True)
            return

        # Verify both players are in the match
        if player1 not in match.all_players:
            await interaction.followup.send(
                f"❌ **{player1.display_name}** is not in match **#{match_id}**.", ephemeral=True)
            return
        if player2 not in match.all_players:
            await interaction.followup.send(
                f"❌ **{player2.display_name}** is not in match **#{match_id}**.", ephemeral=True)
            return

        # Verify they're on different teams
        team1 = match.team_of(player1)
        team2 = match.team_of(player2)
        if team1 == team2:
            await interaction.followup.send(
                f"❌ Both players are on the same team! You can only swap players from different teams.",
                ephemeral=True)
            return

        # Prevent swapping captains
        if player1 == match.captain1 or player1 == match.captain2:
            await interaction.followup.send(
                f"❌ **{player1.display_name}** is a captain and cannot be swapped. "
                f"Use `/aoe_changeplayer` to replace a captain.", ephemeral=True)
            return
        if player2 == match.captain1 or player2 == match.captain2:
            await interaction.followup.send(
                f"❌ **{player2.display_name}** is a captain and cannot be swapped. "
                f"Use `/aoe_changeplayer` to replace a captain.", ephemeral=True)
            return

        # Make sure player1 is from team1 and player2 is from team2
        # (swap so player1 = team1 player, player2 = team2 player)
        if team1 == 2:
            player1, player2 = player2, player1
            team1, team2 = team2, team1

        # Perform the swap
        idx1 = match.team1.index(player1)
        idx2 = match.team2.index(player2)
        match.team1[idx1] = player2
        match.team2[idx2] = player1

        # Handle VC moves if in civ select or in_match phase
        if match.phase in ("civ_select", "in_match", "pre_match"):
            # Move player1 (now in team2) to temp_vc2
            if match.temp_vc2:
                m1 = guild.get_member(player1.id)
                if m1 and m1.voice:
                    try:
                        await m1.move_to(match.temp_vc2)
                    except Exception:
                        pass
            # Move player2 (now in team1) to temp_vc1
            if match.temp_vc1:
                m2 = guild.get_member(player2.id)
                if m2 and m2.voice:
                    try:
                        await m2.move_to(match.temp_vc1)
                    except Exception:
                        pass

        # Transfer civ picks if they had them
        civ1 = match.civ_picks.pop(player1.id, None)
        civ2 = match.civ_picks.pop(player2.id, None)
        if civ1:
            match.civ_picks[player2.id] = civ1
        if civ2:
            match.civ_picks[player1.id] = civ2

        # Notify in thread
        if match.thread:
            try:
                await match.thread.send(
                    f"🔄 **{player1.display_name}** (Team 1) ↔ **{player2.display_name}** (Team 2) have been swapped by {interaction.user.mention}"
                )
            except Exception:
                pass

        logger.info("[%s] Admin %s swapped %s (T1) ↔ %s (T2) in match #%s",
                    guild.id, interaction.user.display_name,
                    player1.display_name, player2.display_name, match_id)

        e = discord.Embed(title="✅ Players Swapped", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID",  value=f"#{match_id}",           inline=True)
        e.add_field(name="📋 Queue",     value=match.queue_type.upper(), inline=True)
        e.add_field(name="🔴 → 🔵",     value=f"{player1.display_name} moved to Team 2", inline=False)
        e.add_field(name="🔵 → 🔴",     value=f"{player2.display_name} moved to Team 1", inline=False)
        e.add_field(name="📍 Phase",     value=match.phase,              inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)


    @discord.app_commands.command(
        name="aoe_forceflip",
        description="Force resolve the coin flip in an active match (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        result="Coin flip result",
        first_pick_team="Which team gets first pick",
    )
    @discord.app_commands.choices(
        result=[
            discord.app_commands.Choice(name="Heads", value="heads"),
            discord.app_commands.Choice(name="Tails", value="tails"),
        ],
        first_pick_team=[
            discord.app_commands.Choice(name="Team 1 🔴", value=1),
            discord.app_commands.Choice(name="Team 2 🔵", value=2),
        ]
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forceflip(self, interaction: discord.Interaction,
                             match_id: int, result: str, first_pick_team: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if match.phase not in ("coin_flip",):
            await interaction.followup.send(
                f"⚠️ Match **#{match_id}** is not in coin flip phase (current: {match.phase}).", ephemeral=True)
            return
        match.first_pick_team = first_pick_team
        match.phase = "draft"
        if match.thread:
            try:
                e = discord.Embed(
                    title=f"🪙 Coin landed on **{result.upper()}**! (Admin Override)",
                    description=f"Team {first_pick_team} has **First Pick**. Draft starting now.",
                    color=0xF1C40F, timestamp=datetime.now(timezone.utc))
                embed = await self._build_draft_embed(guild, match)
                view  = DraftView(self, match)
                await match.thread_message.edit(embed=embed, view=view)
            except Exception as ex:
                logger.warning("[%s] Could not update thread after forceflip: %s", guild.id, ex)
        e = discord.Embed(title="✅ Coin Flip Force Resolved", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID",       value=f"#{match_id}",           inline=True)
        e.add_field(name="🪙 Result",         value=result.upper(),           inline=True)
        e.add_field(name="⚡ First Pick",     value=f"Team {first_pick_team}", inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        logger.info("[%s] Admin %s force-flipped match #%s → Team %s first pick",
                    guild.id, interaction.user.display_name, match_id, first_pick_team)

    @discord.app_commands.command(
        name="aoe_forcepick",
        description="Force pick a player from the pool into a team during draft (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        player="Player to pick from the pool",
        team="Which team to add the player to",
    )
    @discord.app_commands.choices(team=[
        discord.app_commands.Choice(name="Team 1 🔴", value=1),
        discord.app_commands.Choice(name="Team 2 🔵", value=2),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forcepick(self, interaction: discord.Interaction,
                             match_id: int, player: discord.Member, team: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if match.phase != "draft":
            await interaction.followup.send(
                f"⚠️ Match **#{match_id}** is not in draft phase (current: {match.phase}).", ephemeral=True)
            return
        if player not in match.remaining:
            await interaction.followup.send(
                f"❌ **{player.display_name}** is not in the player pool.", ephemeral=True)
            return
        # Add to correct team
        match.remaining.remove(player)
        if team == 1:
            match.team1.append(player)
        else:
            match.team2.append(player)
        # Check if draft is now complete
        if not match.remaining:
            match.draft_complete = True
            match.phase = "teams_confirm"
        if match.thread and match.thread_message:
            try:
                embed = await self._build_draft_embed(guild, match)
                view  = DraftView(self, match) if not match.draft_complete else None
                if match.draft_complete:
                    await match.thread.send(
                        f"✅ Draft complete! Proceeding to Teams Confirm.")
                await match.thread_message.edit(embed=embed, view=view)
            except Exception as ex:
                logger.warning("[%s] Could not update thread after forcepick: %s", guild.id, ex)
        e = discord.Embed(title="✅ Player Force Picked", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID", value=f"#{match_id}",              inline=True)
        e.add_field(name="👤 Player",   value=player.display_name,         inline=True)
        e.add_field(name="👥 Team",     value=f"Team {team}",              inline=True)
        e.add_field(name="📍 Phase",    value=match.phase,                 inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="aoe_forceconfirm",
        description="Skip Teams Confirm and go straight to civ selection (admin only).",
    )
    @discord.app_commands.describe(match_id="Match ID (use /aoe_listmatches)")
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forceconfirm(self, interaction: discord.Interaction, match_id: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if match.phase != "teams_confirm":
            await interaction.followup.send(
                f"⚠️ Match **#{match_id}** is not in teams confirm phase (current: {match.phase}).", ephemeral=True)
            return
        await self._create_temp_vcs(guild, match)
        match.phase = "civ_select"
        if match.thread and match.thread_message:
            try:
                embed = self._build_civ_status_embed(guild, match)
                view  = CivSelectView(self, match)
                await match.thread_message.edit(embed=embed, view=view)
                if match.temp_vc1 and match.temp_vc2:
                    await match.thread.send("Two VCs created! Check team channels.")
            except Exception as ex:
                logger.warning("[%s] forceconfirm thread update failed: %s", guild.id, ex)
        e = discord.Embed(title="Teams Confirmed (Admin)", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID", value=f"#{match_id}", inline=True)
        e.add_field(name="📍 Now",      value="Civ Selection", inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="aoe_forcelockin",
        description="Force lock in civs for a team (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        team="Which team to lock in",
    )
    @discord.app_commands.choices(team=[
        discord.app_commands.Choice(name="Team 1 🔴", value=1),
        discord.app_commands.Choice(name="Team 2 🔵", value=2),
        discord.app_commands.Choice(name="Both Teams", value=0),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forcelockin(self, interaction: discord.Interaction,
                               match_id: int, team: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if match.phase != "civ_select":
            await interaction.followup.send(
                f"⚠️ Match **#{match_id}** is not in civ selection phase (current: {match.phase}).", ephemeral=True)
            return
        # Assign "Random" to anyone who hasn't picked yet
        for p in match.all_players:
            if p.id not in match.civ_picks:
                match.civ_picks[p.id] = "🎲 Random"
        if team in (1, 0):
            match.cap1_locked = True
        if team in (2, 0):
            match.cap2_locked = True
        if match.civs_revealed:
            await self._reveal_civs(match)
        else:
            if match.thread and match.thread_message:
                try:
                    embed = self._build_civ_status_embed(guild, match)
                    view  = CivSelectView(self, match)
                    await match.thread_message.edit(embed=embed, view=view)
                except Exception:
                    pass
        label = "Both Teams" if team == 0 else f"Team {team}"
        e = discord.Embed(title="✅ Civs Force Locked In", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID", value=f"#{match_id}", inline=True)
        e.add_field(name="🔒 Locked",   value=label,          inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="aoe_forcemap",
        description="Manually set the final map for a 1v1 match (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        map_name="Map to set as the final map",
    )
    @discord.app_commands.choices(map_name=[
        discord.app_commands.Choice(name=m, value=m) for m in [
            "African Waters", "Archipelago", "Dry Arabia", "Forts",
            "Gorge", "Hedgemaze", "Rocky River", "Sunkenlands", "West Lake"
        ]
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forcemap(self, interaction: discord.Interaction,
                            match_id: int, map_name: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if match.queue_type != "1v1":
            await interaction.followup.send("❌ Map veto is only for 1v1 matches.", ephemeral=True)
            return
        match.final_map    = map_name
        match.map_veto_done = True
        if match.thread:
            try:
                await match.thread.send(f"Map set by admin: **{map_name}**. Done by {interaction.user.mention}")
            except Exception:
                pass
            except Exception:
                pass
        e = discord.Embed(title="✅ Map Force Set", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID", value=f"#{match_id}", inline=True)
        e.add_field(name="🗺️ Map",      value=map_name,       inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="aoe_forceban",
        description="Manually set a civ ban for a player in a 1v1 match (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        player="Player whose ban to set",
        civ="Civ to ban for the opponent",
    )
    @discord.app_commands.choices(civ=[
        discord.app_commands.Choice(name=c, value=c) for c in [
            "Chinese", "Jin Dynasty", "Zhu Xi's Legacy", "Abbasid Dynasty", "Ayyubids",
            "Byzantines", "Macedonian Dynasty", "Delhi Sultanate", "Tughlaq Dynasty",
            "English", "House of Lancaster", "French", "Jeanne d'Arc", "Templar Knights",
            "Holy Roman Empire", "Order of the Dragon", "Japanese", "Sengoku Daimyo",
            "Malians", "Mongols", "Golden Horde", "Ottomans", "Rus",
        ]
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_forceban(self, interaction: discord.Interaction,
                            match_id: int, player: discord.Member, civ: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if match.queue_type != "1v1":
            await interaction.followup.send("❌ Civ bans are only for 1v1 matches.", ephemeral=True)
            return
        if player not in match.all_players:
            await interaction.followup.send(
                f"❌ **{player.display_name}** is not in match **#{match_id}**.", ephemeral=True)
            return
        if player == match.captain1:
            match.civ_ban_p1_choice = civ
            match.civ_ban_p1_locked = True
            banned_for = match.captain2.display_name
        else:
            match.civ_ban_p2_choice = civ
            match.civ_ban_p2_locked = True
            banned_for = match.captain1.display_name
        if match.thread:
            try:
                await match.thread.send(
                    f"🚫 **{player.display_name}** ban set to **{civ}** (for {banned_for}) by {interaction.user.mention}")
            except Exception:
                pass
        e = discord.Embed(title="✅ Civ Ban Force Set", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID",  value=f"#{match_id}",      inline=True)
        e.add_field(name="👤 Player",    value=player.display_name, inline=True)
        e.add_field(name="🚫 Civ Banned", value=civ,               inline=True)
        e.add_field(name="🎯 Banned For", value=banned_for,         inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="aoe_changecaptain",
        description="Change the captain of a team in an active match (admin only).",
    )
    @discord.app_commands.describe(
        match_id="Match ID (use /aoe_listmatches)",
        team="Which team's captain to change",
        new_captain="New captain (must already be in the match)",
    )
    @discord.app_commands.choices(team=[
        discord.app_commands.Choice(name="Team 1 🔴", value=1),
        discord.app_commands.Choice(name="Team 2 🔵", value=2),
    ])
    @discord.app_commands.default_permissions(administrator=True)
    async def aoe_changecaptain(self, interaction: discord.Interaction,
                                 match_id: int, team: int, new_captain: discord.Member):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        match = self._find_match_by_id(guild, match_id)
        if not match:
            await interaction.followup.send(f"❌ No active match with ID **#{match_id}**.", ephemeral=True)
            return
        if new_captain not in match.all_players:
            await interaction.followup.send(
                f"❌ **{new_captain.display_name}** is not in match **#{match_id}**.", ephemeral=True)
            return
        current_cap = match.captain1 if team == 1 else match.captain2
        if new_captain == current_cap:
            await interaction.followup.send(
                f"⚠️ **{new_captain.display_name}** is already Team {team}'s captain!", ephemeral=True)
            return
        # Check new captain is on the correct team or in pool
        team_members = match.team1 if team == 1 else match.team2
        in_pool = new_captain in match.remaining
        in_team = new_captain in team_members
        other_team = match.team2 if team == 1 else match.team1
        if new_captain in other_team:
            await interaction.followup.send(
                f"❌ **{new_captain.display_name}** is on the other team! "
                f"Use `/aoe_swapplayers` first to move them.", ephemeral=True)
            return
        from_pool = in_pool
        match.replace_captain(team, new_captain, from_pool=from_pool)
        # Update thread name
        if match.thread:
            try:
                await match.thread.edit(name=match.thread_name())
                await match.thread.send(
                    f"👑 **{new_captain.display_name}** is now the captain of Team {team}! "
                    f"(Set by {interaction.user.mention})")
            except Exception:
                pass
        old_cap_name = current_cap.display_name
        e = discord.Embed(title="✅ Captain Changed", color=0x57F287,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="🎮 Match ID",    value=f"#{match_id}",              inline=True)
        e.add_field(name="👥 Team",        value=f"Team {team}",              inline=True)
        e.add_field(name="👑 New Captain", value=new_captain.display_name,    inline=True)
        e.add_field(name="➡️ Old Captain", value=old_cap_name,               inline=True)
        e.add_field(name="📍 Phase",       value=match.phase,                inline=True)
        e.set_footer(text=f"Done by {interaction.user.display_name}")
        await interaction.followup.send(embed=e, ephemeral=True)
        logger.info("[%s] Admin %s changed Team %s captain from %s to %s in match #%s",
                    guild.id, interaction.user.display_name, team,
                    old_cap_name, new_captain.display_name, match_id)


    @discord.app_commands.command(
        name="aoe_civstats",
        description="Show your civ win/loss stats across all AOE matches.",
    )
    @discord.app_commands.describe(member="Member to check (leave blank for yourself)")
    async def aoe_civstats(self, interaction: discord.Interaction,
                            member: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user
        gid    = str(interaction.guild.id)
        uid    = str(target.id)

        civ_stats = await db.get_aoe_civ_stats(gid, uid)

        if not civ_stats:
            await interaction.followup.send(
                f"❌ **{target.display_name}** has no civ stats yet.", ephemeral=True)
            return

        # Sort by most played (wins + losses)
        sorted_civs = sorted(
            civ_stats.items(),
            key=lambda x: x[1]["wins"] + x[1]["losses"],
            reverse=True
        )

        e = discord.Embed(
            title=f"🎭 Civ Stats — {target.display_name}",
            color=0x9B59B6,
            timestamp=datetime.now(timezone.utc),
        )
        e.set_thumbnail(url=target.display_avatar.url)

        rows = []
        for civ, stats in sorted_civs:
            total   = stats["wins"] + stats["losses"]
            win_pct = f"{(stats['wins']/total*100):.0f}%" if total > 0 else "0%"
            rows.append(
                f"**{civ}** — W:{stats['wins']} L:{stats['losses']} WR:{win_pct}"
            )

        # Split into two columns if many civs
        if len(rows) > 6:
            mid = len(rows) // 2
            e.add_field(name="Civilization Stats", value="\n".join(rows[:mid]), inline=True)
            e.add_field(name="\u200b", value="\n".join(rows[mid:]), inline=True)
        else:
            e.add_field(name="Civilization Stats", value="\n".join(rows), inline=False)
        e.set_footer(text=f"Based on all finished matches • {interaction.guild.name}")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.app_commands.command(
        name="aoe_h2h",
        description="Show head to head match history between two players.",
    )
    @discord.app_commands.describe(
        player1="First player",
        player2="Second player",
    )
    async def aoe_h2h(self, interaction: discord.Interaction,
                       player1: discord.Member, player2: discord.Member):
        await interaction.response.defer(ephemeral=True)
        gid = str(interaction.guild.id)

        if player1.id == player2.id:
            await interaction.followup.send("❌ Please select two different players!", ephemeral=True)
            return

        matches = await db.get_aoe_h2h(gid, str(player1.id), str(player2.id))

        if not matches:
            await interaction.followup.send(
                f"❌ No matches found between **{player1.display_name}** and **{player2.display_name}**.",
                ephemeral=True)
            return

        # Count H2H record
        p1_wins = 0
        p2_wins = 0
        for m in matches:
            p1_in_t1 = str(player1.id) in m["team1_ids"]
            if (p1_in_t1 and m["result"] == "team1") or                (not p1_in_t1 and m["result"] == "team2"):
                p1_wins += 1
            else:
                p2_wins += 1

        e = discord.Embed(
            title=f"⚔️ Head to Head",
            description=(
                f"**{player1.display_name}** vs **{player2.display_name}**\n\n"
                f"\U0001f534 {player1.display_name}: **{p1_wins}W**\n"
                f"\U0001f535 {player2.display_name}: **{p2_wins}W**\n"
                f"Total matches: **{len(matches)}**"
            ),
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc),
        )
        history_lines = []
        for m in matches[:5]:
            p1_in_t1 = str(player1.id) in m["team1_ids"]
            p1_won   = (p1_in_t1 and m["result"] == "team1") or (not p1_in_t1 and m["result"] == "team2")
            result   = "🏆" if p1_won else "💔"
            p1_civ   = m["civ_data"].get(str(player1.id), "?")
            p2_civ   = m["civ_data"].get(str(player2.id), "?")
            qt       = m["queue_type"].upper()
            date     = m["date"].strftime("%d %b") if m["date"] else "?"
            history_lines.append(
                f"{result} **#{m['id']}** {qt} — {player1.display_name} ({p1_civ}) vs {player2.display_name} ({p2_civ}) • {date}"
            )

        if history_lines:
            e.add_field(
                name=f"\U0001f4dc Last {len(history_lines)} Matches",
                value="\n".join(history_lines),
                inline=False
            )

        e.set_footer(text=f"Showing last {len(matches)} matches • {interaction.guild.name}")
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AOEQueueCog(bot))
    logger.info("AOEQueueCog loaded.")
