import os
import asyncio
import logging
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get Telegram token from Railway env
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Download path
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a TikTok, Instagram, Reddit, X, or YouTube link (max 50MB).")


async def download_video(url: str, file_path: str) -> str:
    """Download video using yt-dlp and return the file path"""
    ydl_opts = {
        "outtmpl": file_path,
        "format": "mp4[filesize<50M]/best[filesize<50M]",
        "noplaylist": True,
        "quiet": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not any(x in url for x in ["tiktok.com", "instagram.com", "reddit.com", "twitter.com", "x.com", "youtu"]):
        await update.message.reply_text("❌ Not a supported link.")
        return

    file_path = os.path.join(DOWNLOAD_DIR, "%(title).50s.%(ext)s")

    await update.message.reply_text("⏳ Downloading video...")

    downloaded_file = await asyncio.to_thread(download_video, url, file_path)

    if not downloaded_file:
        await update.message.reply_text("❌ Failed to download. Maybe >50MB or unsupported.")
        return

    try:
        await update.message.reply_video(video=open(downloaded_file, "rb"))
        await update.message.reply_text("✅ Sent! File will be deleted in 10 minutes.")

        # Schedule deletion
        asyncio.create_task(delete_file_later(downloaded_file, 600))
    except Exception as e:
        logger.error(f"Send error: {e}")
        await update.message.reply_text("❌ Error sending video.")
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)


async def delete_file_later(file_path: str, delay: int):
    await asyncio.sleep(delay)
    if os.path.exists(file_path):
        os.remove(file_path)
        logger.info(f"Deleted file: {file_path}")


def main():
    if not TOKEN:
        logger.error("Telegram token not set! Set TELEGRAM_BOT_TOKEN environment variable.")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
