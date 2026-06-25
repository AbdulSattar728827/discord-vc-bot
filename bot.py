import discord
from discord.ext import commands
import os, logging, asyncio
from dotenv import load_dotenv
from database import db

load_dotenv()
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

intents              = discord.Intents.default()
intents.voice_states = True
intents.members      = True
intents.guilds       = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Connected to {len(bot.guilds)} guild(s).")
    for g in bot.guilds:
        logger.info(f"  - {g.name} ({g.id})")

    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Slash sync failed: {e}")

    lb = bot.get_cog("Leaderboard")
    if lb:
        for guild in bot.guilds:
            await lb._update(guild)
        lb._ready = True
        if not lb.auto_refresh.is_running():
            lb.auto_refresh.start()
        logger.info("Leaderboard ready. Auto-refresh started.")

@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception(f"Unhandled exception in event: {event}")

async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN not found!")

    # Connect to database first
    await db.connect()

    async with bot:
        await bot.load_extension("cogs.tracker")
        await bot.load_extension("cogs.leaderboard")
        await bot.load_extension("cogs.stats")
        logger.info("All cogs loaded.")
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
