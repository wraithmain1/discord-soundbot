import asyncio
import logging
import os

import discord
import uvicorn

from .discord_bot import SoundBot
from . import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))


async def main():
    intents = discord.Intents.default()
    intents.members = True  # privileged - must also be enabled in the Discord Dev Portal
    intents.voice_states = True

    bot = SoundBot(intents=intents)
    web.set_bot_instance(bot)

    config = uvicorn.Config(web.app, host="0.0.0.0", port=WEB_PORT, log_level="info")
    server = uvicorn.Server(config)

    logger.info("Starting bot and web server on port %d", WEB_PORT)
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
