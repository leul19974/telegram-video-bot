import os
import tempfile
import shutil
import logging
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(name)

# Get token from environment (Railway/Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Supported domains
SUPPORTED_DOMAINS = [
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "reddit.com", "v.redd.it"
]


# ---------------- Commands ---------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message"""
    await update.message.reply_text(
        "🎬 *Video Downloader Bot*\n\n"
        "Send me a link from:\n"
        "• Instagram\n"
        "• TikTok\n"
        "• YouTube\n"
        "• Twitter/X\n"
        "• Reddit\n\n"
        "I'll download the video for you!\n\n"
        "_Max size: 50 MB (Telegram free limit)_",
        parse_mode="Markdown",
    )


# ---------------- Video Logic ---------------- #

def get_formats(url: str):
    """Extract available MP4 formats from a URL"""
    ydl_opts = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = []
        for f in info.get("formats", []):
            if f.get("ext") == "mp4" and f.get("height"):
                filesize = f.get("filesize") or 0
                quality = f"{f['height']}p"
                formats.append({
                    "format_id": f["format_id"],
                    "quality": quality,
                    "filesize": filesize
                })
        # Remove duplicates (keep highest quality per resolution)
        unique = {}
        for f in formats:
            if f["quality"] not in unique or f["filesize"] > unique[f["quality"]]["filesize"]:
                unique[f["quality"]] = f
        return list(unique.values())


def download_video(url: str, format_id: str) -> str:
    """Download video with selected quality, return file path"""
    temp_dir = tempfile.mkdtemp()
    outtmpl = os.path.join(temp_dir, "%(title)s.%(ext)s")
    ydl_opts = {
        "format": format_id,
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            base = os.path.splitext(filename)[0]
            for ext in ["mp4", "mkv", "webm"]:
                test_file = f"{base}.{ext}"
                if os.path.exists(test_file):
                    filename = test_file
                    break
        return filename


# ---------------- Handlers ---------------- #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages with URLs"""
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()

    if not any(domain in url for domain in SUPPORTED_DOMAINS):
        await update.message.reply_text(
            "❌ Unsupported platform. Please send a link from:\n"
            "Instagram, TikTok, YouTube, Twitter/X, or Reddit"
        )
        return

    try:
        formats = get_formats(url)
        if not formats:
            await update.message.reply_text("❌ No MP4 formats available")
            return

        # Save URL for callback
        context.user_data["url"] = url

        # Build inline buttons
        keyboard = [
            [InlineKeyboardButton(f"{f['quality']}", callback_data=f["format_id"])]
            for f in formats
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text("🎥 Choose video quality:", reply_markup=reply_markup)
except Exception as e:
        logger.error(f"Error fetching formats: {e}")
        await update.message.reply_text("❌ Failed to fetch video info.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quality button clicks"""
    query = update.callback_query
    await query.answer()

    url = context.user_data.get("url")
    format_id = query.data

    if not url or not format_id:
        await query.edit_message_text("❌ Error: missing video info.")
        return

    await query.edit_message_text(text=f"⏳ Downloading {format_id}...")

    video_path = None
    temp_dir = None
    try:
        video_path = download_video(url, format_id)

        file_size = os.path.getsize(video_path) / (1024 * 1024)  # MB
        if file_size > 50:
            await query.message.reply_text("❌ Video too large (max 50 MB)")
            return

        with open(video_path, "rb") as f:
            await query.message.reply_video(
                f, caption="✅ Here’s your video!", write_timeout=120
            )

    except Exception as e:
        logger.error(f"Download error: {e}")
        await query.message.reply_text("❌ Failed to download video.")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------- Main ---------------- #

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        raise ValueError("❌ BOT_TOKEN not set. Add it as an environment variable.")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot started...")
    application.run_polling()


if name == "main":
    main()