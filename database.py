"""
database.py — PostgreSQL connection and all data operations
"""

import asyncpg
import os
import logging
from datetime import datetime, timezone, date, timedelta

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL not found in environment variables!")
        self.pool = await asyncpg.create_pool(database_url, ssl="require")
        await self._create_tables()
        logger.info("Database connected and tables ready.")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vc_stats (
                    guild_id    TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    total_secs  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vc_logs (
                    id          SERIAL PRIMARY KEY,
                    guild_id    TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    username    TEXT NOT NULL,
                    channel     TEXT NOT NULL,
                    joined_at   TIMESTAMPTZ NOT NULL,
                    left_at     TIMESTAMPTZ NOT NULL,
                    duration_s  DOUBLE PRECISION NOT NULL,
                    duration    TEXT NOT NULL,
                    rank        TEXT NOT NULL,
                    total_time  TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vc_milestones (
                    guild_id    TEXT NOT NULL,
                    user_id     TEXT NOT NULL,
                    milestone   INTEGER NOT NULL,
                    achieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id, milestone)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS vc_streaks (
                    guild_id        TEXT NOT NULL,
                    user_id         TEXT NOT NULL,
                    current_streak  INTEGER NOT NULL DEFAULT 0,
                    longest_streak  INTEGER NOT NULL DEFAULT 0,
                    last_active_day DATE,
                    daily_secs      DOUBLE PRECISION NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS cheese_coins (
                    guild_id        TEXT NOT NULL,
                    user_id         TEXT NOT NULL,
                    coins           INTEGER NOT NULL DEFAULT 0,
                    total_earned    INTEGER NOT NULL DEFAULT 0,
                    pending_secs    DOUBLE PRECISION NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        await self._create_aoe_tables()

    # ── Stats ──────────────────────────────────────────────────────────────────

    async def get_total(self, guild_id: str, user_id: str) -> float:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT total_secs FROM vc_stats WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            return row["total_secs"] if row else 0.0

    async def add_time(self, guild_id: str, user_id: str, seconds: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO vc_stats (guild_id, user_id, total_secs)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET total_secs = vc_stats.total_secs + $3
            """, guild_id, user_id, seconds)

    async def get_leaderboard(self, guild_id: str) -> list[tuple[str, float]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, total_secs FROM vc_stats WHERE guild_id=$1 ORDER BY total_secs DESC",
                guild_id
            )
            return [(r["user_id"], r["total_secs"]) for r in rows]

    # ── Logs ───────────────────────────────────────────────────────────────────

    async def add_log(self, guild_id, user_id, username, channel,
                      joined_at, left_at, duration_s, duration, rank, total_time):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO vc_logs
                (guild_id, user_id, username, channel, joined_at, left_at,
                 duration_s, duration, rank, total_time)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """, guild_id, user_id, username, channel,
                joined_at, left_at, duration_s, duration, rank, total_time)

    async def get_session_count(self, guild_id: str, user_id: str) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM vc_logs WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            return row["cnt"] if row else 0

    # ── Milestones ─────────────────────────────────────────────────────────────

    async def get_achieved_milestones(self, guild_id: str, user_id: str) -> list[int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT milestone FROM vc_milestones WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            return [r["milestone"] for r in rows]

    async def save_milestone(self, guild_id: str, user_id: str, milestone: int):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO vc_milestones (guild_id, user_id, milestone)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
            """, guild_id, user_id, milestone)

    # ── Streaks ────────────────────────────────────────────────────────────────

    STREAK_THRESHOLD_SECS = 30 * 60  # 30 minutes to count as a streak day

    async def update_streak(self, guild_id: str, user_id: str, session_secs: float) -> dict:
        """
        Update streak for a member after a session.
        Returns dict with current_streak, longest_streak, streak_updated (bool).
        """
        today = datetime.now(timezone.utc).date()

        async with self.pool.acquire() as conn:
            # Get existing streak record
            row = await conn.fetchrow(
                "SELECT * FROM vc_streaks WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )

            if not row:
                # First time — create record
                new_daily = session_secs
                streak_updated = new_daily >= self.STREAK_THRESHOLD_SECS
                current_streak = 1 if streak_updated else 0
                longest_streak = current_streak

                await conn.execute("""
                    INSERT INTO vc_streaks
                    (guild_id, user_id, current_streak, longest_streak, last_active_day, daily_secs)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, guild_id, user_id, current_streak, longest_streak,
                    today if streak_updated else None, new_daily)

                return {
                    "current_streak": current_streak,
                    "longest_streak": longest_streak,
                    "streak_updated": streak_updated,
                }

            last_day    = row["last_active_day"]
            daily_secs  = row["daily_secs"]
            curr_streak = row["current_streak"]
            long_streak = row["longest_streak"]

            # Reset daily_secs if it's a new day
            if last_day != today:
                daily_secs = 0.0

            new_daily = daily_secs + session_secs

            # Check if threshold met today
            was_met_before = daily_secs >= self.STREAK_THRESHOLD_SECS
            is_met_now     = new_daily >= self.STREAK_THRESHOLD_SECS
            streak_updated = is_met_now and not was_met_before

            if streak_updated:
                yesterday = today - timedelta(days=1)
                if last_day == yesterday or last_day is None:
                    # Consecutive day — extend streak
                    curr_streak += 1
                elif last_day != today:
                    # Streak broken — reset to 1
                    curr_streak = 1

                long_streak = max(long_streak, curr_streak)
                last_day    = today

            await conn.execute("""
                UPDATE vc_streaks
                SET current_streak=$3, longest_streak=$4,
                    last_active_day=$5, daily_secs=$6
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id, curr_streak, long_streak, last_day, new_daily)

            return {
                "current_streak": curr_streak,
                "longest_streak": long_streak,
                "streak_updated": streak_updated,
            }

    async def get_streak(self, guild_id: str, user_id: str) -> dict:
        """Get current streak info for a member."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_streak, longest_streak, last_active_day FROM vc_streaks WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            if not row:
                return {"current_streak": 0, "longest_streak": 0}

            # Check if streak is still active (last active was yesterday or today)
            today     = datetime.now(timezone.utc).date()
            yesterday = today - timedelta(days=1)
            last_day  = row["last_active_day"]

            current = row["current_streak"]
            if last_day and last_day < yesterday:
                # Streak is broken — they missed a day
                current = 0

            return {
                "current_streak": current,
                "longest_streak": row["longest_streak"],
            }

    async def get_leaderboard_with_streaks(self, guild_id: str) -> list[tuple[str, float, int]]:
        """Return leaderboard with streak info: [(user_id, total_secs, current_streak), ...]"""
        today     = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT v.user_id, v.total_secs,
                       COALESCE(s.current_streak, 0) as current_streak,
                       s.last_active_day
                FROM vc_stats v
                LEFT JOIN vc_streaks s
                    ON v.guild_id = s.guild_id AND v.user_id = s.user_id
                WHERE v.guild_id = $1
                ORDER BY v.total_secs DESC
            """, guild_id)

            result = []
            for r in rows:
                streak = r["current_streak"]
                last   = r["last_active_day"]
                # Reset streak if they missed a day
                if last and last < yesterday:
                    streak = 0
                result.append((r["user_id"], r["total_secs"], streak))
            return result

    # ── Cheese Coins ───────────────────────────────────────────────────────────

    COIN_THRESHOLD_SECS = 30 * 60  # 30 minutes = 1 coin

    async def get_coins(self, guild_id: str, user_id: str) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT coins, total_earned, pending_secs FROM cheese_coins WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            if not row:
                return {"coins": 0, "total_earned": 0, "pending_secs": 0.0}
            return {"coins": row["coins"], "total_earned": row["total_earned"], "pending_secs": row["pending_secs"]}

    async def add_session_coins(self, guild_id: str, user_id: str, session_secs: float) -> int:
        """Add pending seconds and award coins if threshold met. Returns coins earned."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT coins, total_earned, pending_secs FROM cheese_coins WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            pending = (row["pending_secs"] if row else 0.0) + session_secs
            coins_earned = int(pending // self.COIN_THRESHOLD_SECS)
            remaining    = pending % self.COIN_THRESHOLD_SECS

            if not row:
                await conn.execute("""
                    INSERT INTO cheese_coins (guild_id, user_id, coins, total_earned, pending_secs)
                    VALUES ($1, $2, $3, $4, $5)
                """, guild_id, user_id, coins_earned, coins_earned, remaining)
            else:
                await conn.execute("""
                    UPDATE cheese_coins
                    SET coins=$3, total_earned=$4, pending_secs=$5
                    WHERE guild_id=$1 AND user_id=$2
                """, guild_id, user_id,
                    row["coins"] + coins_earned,
                    row["total_earned"] + coins_earned,
                    remaining)
            return coins_earned

    async def spend_coins(self, guild_id: str, user_id: str, amount: int) -> bool:
        """Spend coins. Returns True if successful, False if not enough coins."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT coins FROM cheese_coins WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            if not row or row["coins"] < amount:
                return False
            await conn.execute("""
                UPDATE cheese_coins SET coins = coins - $3
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id, amount)
            return True

    async def add_coins(self, guild_id: str, user_id: str, amount: int):
        """Admin: add coins to a member."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO cheese_coins (guild_id, user_id, coins, total_earned)
                VALUES ($1, $2, $3, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET coins = cheese_coins.coins + $3,
                              total_earned = cheese_coins.total_earned + $3
            """, guild_id, user_id, amount)

    async def remove_coins(self, guild_id: str, user_id: str, amount: int):
        """Admin: remove coins from a member (won't go below 0)."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE cheese_coins
                SET coins = GREATEST(0, coins - $3)
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id, amount)

    async def set_coins(self, guild_id: str, user_id: str, amount: int):
        """Admin: set coins to exact amount."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO cheese_coins (guild_id, user_id, coins, total_earned)
                VALUES ($1, $2, $3, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET coins = $3
            """, guild_id, user_id, max(0, amount))

    async def get_coins_leaderboard(self, guild_id: str) -> list[tuple[str, int]]:
        """Returns [(user_id, coins), ...] sorted by coins descending."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, coins FROM cheese_coins WHERE guild_id=$1 AND coins > 0 ORDER BY coins DESC",
                guild_id
            )
            return [(r["user_id"], r["coins"]) for r in rows]

    # ── Admin methods ──────────────────────────────────────────────────────────

    async def set_time(self, guild_id: str, user_id: str, seconds: float):
        """Set a member's total VC time to a specific value."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO vc_stats (guild_id, user_id, total_secs)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET total_secs = $3
            """, guild_id, user_id, max(0.0, seconds))

    async def modify_streak(self, guild_id: str, user_id: str, days: int):
        """Add or remove days from a member's current streak."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_streak, longest_streak FROM vc_streaks WHERE guild_id=$1 AND user_id=$2",
                guild_id, user_id
            )
            if not row:
                new_streak  = max(0, days)
                long_streak = new_streak
                await conn.execute("""
                    INSERT INTO vc_streaks (guild_id, user_id, current_streak, longest_streak)
                    VALUES ($1, $2, $3, $4)
                """, guild_id, user_id, new_streak, long_streak)
            else:
                new_streak  = max(0, row["current_streak"] + days)
                long_streak = max(row["longest_streak"], new_streak)
                await conn.execute("""
                    UPDATE vc_streaks SET current_streak=$3, longest_streak=$4
                    WHERE guild_id=$1 AND user_id=$2
                """, guild_id, user_id, new_streak, long_streak)

    async def reset_streak(self, guild_id: str, user_id: str):
        """Reset a member's current streak to zero."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE vc_streaks SET current_streak=0
                WHERE guild_id=$1 AND user_id=$2
            """, guild_id, user_id)

    async def close(self):
        if self.pool:
            await self.pool.close()


    async def close(self):
        if self.pool:
            await self.pool.close()

    # ── AOE Queue ──────────────────────────────────────────────────────────────

    async def _create_aoe_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS aoe_queue_stats (
                    guild_id    TEXT    NOT NULL,
                    user_id     TEXT    NOT NULL,
                    queue_type  TEXT    NOT NULL,
                    wins        INTEGER NOT NULL DEFAULT 0,
                    losses      INTEGER NOT NULL DEFAULT 0,
                    no_results  INTEGER NOT NULL DEFAULT 0,
                    elo         INTEGER NOT NULL DEFAULT 1000,
                    PRIMARY KEY (guild_id, user_id, queue_type)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS aoe_matches (
                    id          SERIAL  PRIMARY KEY,
                    guild_id    TEXT    NOT NULL,
                    queue_type  TEXT    NOT NULL,
                    player_ids  TEXT[]  NOT NULL,
                    team1_ids   TEXT[]  DEFAULT \'{}\',
                    team2_ids   TEXT[]  DEFAULT \'{}\',
                    result      TEXT    NOT NULL DEFAULT \'pending\',
                    civ_data    JSONB   NOT NULL DEFAULT \'{}\',
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ
                )
            """)
            # Migration: add civ_data column if it doesn't exist yet
            await conn.execute("""
                ALTER TABLE aoe_matches
                ADD COLUMN IF NOT EXISTS civ_data JSONB NOT NULL DEFAULT '{}'
            """)

    async def get_aoe_stats(self, guild_id: str, user_id: str, queue_type: str) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT wins, losses, no_results, elo
                   FROM aoe_queue_stats
                   WHERE guild_id=$1 AND user_id=$2 AND queue_type=$3""",
                guild_id, user_id, queue_type
            )
            if not row:
                return {"wins": 0, "losses": 0, "no_results": 0, "elo": 1000}
            return dict(row)

    async def update_aoe_stats(self, guild_id: str, user_id: str,
                                queue_type: str, result: str):
        async with self.pool.acquire() as conn:
            if result == "win":
                await conn.execute("""
                    INSERT INTO aoe_queue_stats (guild_id, user_id, queue_type, wins, elo)
                    VALUES ($1, $2, $3, 1, 1025)
                    ON CONFLICT (guild_id, user_id, queue_type)
                    DO UPDATE SET wins = aoe_queue_stats.wins + 1,
                                  elo  = GREATEST(0, aoe_queue_stats.elo + 25)
                """, guild_id, user_id, queue_type)
            elif result == "loss":
                await conn.execute("""
                    INSERT INTO aoe_queue_stats (guild_id, user_id, queue_type, losses, elo)
                    VALUES ($1, $2, $3, 1, 975)
                    ON CONFLICT (guild_id, user_id, queue_type)
                    DO UPDATE SET losses = aoe_queue_stats.losses + 1,
                                  elo    = GREATEST(0, aoe_queue_stats.elo - 25)
                """, guild_id, user_id, queue_type)
            else:
                await conn.execute("""
                    INSERT INTO aoe_queue_stats (guild_id, user_id, queue_type, no_results)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (guild_id, user_id, queue_type)
                    DO UPDATE SET no_results = aoe_queue_stats.no_results + 1
                """, guild_id, user_id, queue_type)

    async def create_aoe_match(self, guild_id: str, queue_type: str,
                                player_ids: list) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO aoe_matches (guild_id, queue_type, player_ids)
                VALUES ($1, $2, $3)
                RETURNING id
            """, guild_id, queue_type, player_ids)
            return row["id"]

    async def finish_aoe_match(self, match_id: int, result: str,
                                team1_ids: list, team2_ids: list,
                                civ_data: dict = None):
        import json
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE aoe_matches
                SET result=$2, team1_ids=$3, team2_ids=$4,
                    civ_data=$5, finished_at=NOW()
                WHERE id=$1
            """, match_id, result, team1_ids, team2_ids,
                json.dumps(civ_data or {}))

    async def get_aoe_leaderboard(self, guild_id: str, queue_type: str) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, wins, losses, no_results, elo
                FROM aoe_queue_stats
                WHERE guild_id=$1 AND queue_type=$2
                ORDER BY elo DESC, wins DESC
            """, guild_id, queue_type)
            return [dict(r) for r in rows]

    async def adjust_aoe_stats(self, guild_id: str, user_id: str, queue_type: str,
                                wins_delta: int = 0, losses_delta: int = 0,
                                elo_delta: int = 0):
        """Directly adjust wins/losses/elo by a delta (can be negative)."""
        async with self.pool.acquire() as conn:
            # Ensure row exists first
            await conn.execute("""
                INSERT INTO aoe_queue_stats (guild_id, user_id, queue_type)
                VALUES ($1, $2, $3)
                ON CONFLICT DO NOTHING
            """, guild_id, user_id, queue_type)
            await conn.execute("""
                UPDATE aoe_queue_stats
                SET wins   = GREATEST(0, wins   + $4),
                    losses = GREATEST(0, losses + $5),
                    elo    = GREATEST(0, elo    + $6)
                WHERE guild_id=$1 AND user_id=$2 AND queue_type=$3
            """, guild_id, user_id, queue_type,
                wins_delta, losses_delta, elo_delta)

    async def reset_aoe_stats(self, guild_id: str, user_id: str, queue_type: str):
        """Reset a player's stats for a queue type back to defaults."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO aoe_queue_stats
                    (guild_id, user_id, queue_type, wins, losses, no_results, elo)
                VALUES ($1, $2, $3, 0, 0, 0, 1000)
                ON CONFLICT (guild_id, user_id, queue_type)
                DO UPDATE SET wins=0, losses=0, no_results=0, elo=1000
            """, guild_id, user_id, queue_type)


# Global instance
db = Database()
