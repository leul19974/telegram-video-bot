import os
import asyncio
import logging
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ------------------------------
# Logging
# ------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------
# Telegram token
# ------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ------------------------------
# Download directory & cookies
# ------------------------------
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES_FILE = "cookies.txt"  # Optional for Instagram/X private posts

# ------------------------------
# /start command
# ------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a TikTok, Instagram, Reddit, X/Twitter, or YouTube link (max 50MB)."
    )

# ------------------------------
# Blocking download using yt-dlp
# ------------------------------
def download_video_blocking(url: str, file_path: str) -> str:
    """Download video using yt-dlp and return the downloaded file path"""
    ydl_opts = {
        "outtmpl": file_path,
        "format": "mp4[filesize<50M]/best[filesize<50M]",
        "noplaylist": True,
        "quiet": True,
        "allow_unplayable_formats": True,  # Helps with X/Twitter
    }

    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "requested_downloads" in info:
                return info["requested_downloads"][0]["filepath"]
            return ydl.prepare_filename(info)
    except Exception as e:
        logger.error(f"Download error for {url}: {e}")
        return None

# ------------------------------
# Async wrapper for download
# ------------------------------
async def download_video(url: str, file_path: str) -> str:
    return await asyncio.to_thread(download_video_blocking, url, file_path)

# ------------------------------
# Handle incoming messages
# ------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not any(x in url for x in ["tiktok.com", "instagram.com", "reddit.com", "twitter.com", "x.com", "youtu"]):
        await update.message.reply_text("❌ Not a supported link.")
        return

    file_template = os.path.join(DOWNLOAD_DIR, "%(title).50s.%(ext)s")
    await update.message.reply_text("⏳ Downloading video...")

    downloaded_file = await download_video(url, file_template)

    if not downloaded_file or not os.path.exists(downloaded_file):
        await update.message.reply_text("❌ Failed to download. Maybe >50MB or unsupported.")
        return

    try:
        with open(downloaded_file, "rb") as f:
            await update.message.reply_video(video=f)
        await update.message.reply_text("✅ Sent! File will be deleted in 10 minutes.")
        asyncio.create_task(delete_file_later(downloaded_file, 600))
    except Exception as e:
        logger.error(f"Send error: {e}")
        await update.message.reply_text("❌ Error sending video.")
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)

# ------------------------------
# Delete file after delay
# ------------------------------
async def delete_file_later(file_path: str, delay: int):
    await asyncio.sleep(delay)
    if os.path.exists(file_path):
        os.remove(file_path)
        logger.info(f"Deleted file: {file_path}")

# ------------------------------
# Main bot
# ------------------------------
def main():
    if not TOKEN:
        logger.error("Telegram token not set! Set TELEGRAM_BOT_TOKEN environment variable.")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started!")
    app.run_polling()

# ------------------------------
# Run bot
# ------------------------------
if __name__ == "__main__":
    main()
