import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")

# Disk persistant Render
DATA_DIR = os.getenv("RENDER_DISK_PATH", "./data")
os.makedirs(DATA_DIR, exist_ok=True)


async def send_startup_message():
    if BOT_TOKEN and CHANNEL_ID:
        try:
            bot = Bot(token=BOT_TOKEN)
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text="✅ *APEX-OMEGA-EPL démarré*\nBot opérationnel sur Render.",
                parse_mode="Markdown"
            )
            logger.info("Startup message sent to Telegram")
        except Exception as e:
            logger.error(f"Telegram error: {e}")


async def main():
    logger.info("=== APEX-OMEGA-EPL Bot starting ===")
    logger.info(f"Data directory: {DATA_DIR}")

    await send_startup_message()

    logger.info("Bot running — waiting for pipeline tasks...")

    # Boucle principale — le pipeline sera ajouté ici
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
