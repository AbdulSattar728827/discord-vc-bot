"""
database.py — PostgreSQL connection and all data operations
Replaces the JSON file system with a proper database.
"""

import asyncpg
import os
import logging
from datetime import datetime, timezone

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

    async def close(self):
        if self.pool:
            await self.pool.close()


# Global instance
db = Database()
