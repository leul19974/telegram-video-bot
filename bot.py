import os
import asyncio
import logging
import yt_dlp
import ffmpeg
from datetime import datetime
from pathlib import Path
from functools import partial
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

# ===============================
# CONFIG
# ===============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set. Please set environment variable.")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_SIZE_MB = 1000  # 1 GB limit for Telegram bots

# Cache settings
request_cache = {}  # {user_id: {"url":..., "formats":..., "expiry":...}}
CACHE_TTL = 600  # 10 minutes

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ===============================
# HELPERS
# ===============================

async def cleanup_cache():
    """Clean expired cache entries every minute."""
    while True:
        now = datetime.now().timestamp()
        expired = [uid for uid, data in request_cache.items() if data["expiry"] < now]
        for uid in expired:
            del request_cache[uid]
        await asyncio.sleep(60)


async def cache_request(user_id, url, formats):
    """Save request to cache."""
    request_cache[user_id] = {
        "url": url,
        "formats": formats,
        "expiry": datetime.now().timestamp() + CACHE_TTL
    }


def get_cached_request(user_id):
    """Return cached request if still valid."""
    data = request_cache.get(user_id)
    if data and datetime.now().timestamp() < data["expiry"]:
        return data
    return None


async def run_ffmpeg_convert(input_path: Path, output_path: Path):
    """Convert video to mp3 using ffmpeg."""
    try:
        (
            ffmpeg
            .input(str(input_path))
            .output(str(output_path), format="mp3", audio_bitrate="192k")
            .overwrite_output()
            .run(quiet=True)
        )
        return output_path
    except Exception as e:
        logger.error(f"FFmpeg conversion failed: {e}")
        return None


async def download_with_ytdlp(url, format_id, out_path):
    """Download video/audio with yt-dlp."""
    ydl_opts = {
        "format": format_id,
        "outtmpl": str(out_path),
        "quiet": True,
        "merge_output_format": "mp4",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return out_path
    except Exception as e:
        logger.error(f"yt-dlp download error: {e}")
        return None


async def fetch_formats(url: str):
    """Fetch available formats for a given URL."""
    ydl_opts = {"quiet": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = [
                {
                    "format_id": f["format_id"],
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution") or f"{f.get('height')}p",
                    "filesize": f.get("filesize")
                }
                for f in info.get("formats", [])
                if f.get("vcodec") != "none" and f.get("acodec") != "none"
            ]
            return formats, info
    except Exception as e:
        logger.error(f"Failed to fetch formats: {e}")
        return None, None


async def delete_file_later(path: Path, delay: int = 60):
    """Delete file from server after delay (not from Telegram)."""
    await asyncio.sleep(delay)
    try:
        if path.exists():
            path.unlink()
            logger.info(f"Deleted file {path}")
    except Exception as e:
        logger.error(f"Failed to delete {path}: {e}")


# ===============================
# BOT HANDLERS
# ===============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Welcome to *Silence Downloader Bot*!\n\n"
        "📥 Send me a link from YouTube, TikTok, Instagram, Twitter/X, or Reddit.\n"
        "🎥 I will fetch available video qualities for you.\n"
        "🎧 You can also convert the video to audio (MP3).\n\n"
        "⚠️ Note: Max file size is 1GB.\n\n"
        "Use /help for more info."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📌 *How to use Silence Downloader:*\n\n"
        "1. Send me a video link (YouTube, TikTok, Instagram, Twitter, Reddit).\n"
        "2. Choose the desired video quality.\n"
        "3. Or select *Convert to Audio* to get MP3.\n"
        "4. File will be sent (max 1GB).\n\n"
        "✅ Files are auto-deleted from the server after 1 minute."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    user_id = update.message.from_user.id

    await update.message.reply_text("🔎 Fetching available formats...")

    formats, info = await fetch_formats(url)
    if not formats:
        await update.message.reply_text("❌ Failed to fetch video info.")
        return

    # Cache the request
    await cache_request(user_id, url, formats)

    # Build buttons
    buttons = []
    for f in formats:
        res = f["resolution"] or "N/A"
        btn_text = f"{res} ({f['ext']})"
        buttons.append(
            [InlineKeyboardButton(btn_text, callback_data=f"video|{f['format_id']}")]
        )

    buttons.append([InlineKeyboardButton("🎧 Convert to Audio", callback_data="audio")])

    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("📥 Select quality or convert:", reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Get cached request or re-fetch
    data = get_cached_request(user_id)
    if not data:
        await query.edit_message_text("⚠️ Request expired. Send the link again.")
        return

    url = data["url"]
    formats = data["formats"]

    choice = query.data.split("|")
    action = choice[0]

    if action == "video":
        format_id = choice[1]
        out_path = DOWNLOAD_DIR / f"{user_id}_{format_id}.mp4"
        await query.edit_message_text("⬇️ Downloading video...")

        file_path = await download_with_ytdlp(url, format_id, out_path)
        if not file_path or not file_path.exists():
            await query.edit_message_text("❌ Failed to download video.")
            return

        if file_path.stat().st_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await query.edit_message_text("⚠️ File too large for Telegram.")
            file_path.unlink(missing_ok=True)
            return

        await context.bot.send_video(
            chat_id=user_id, video=open(file_path, "rb"),
            caption="✅ Here is your video!"
        )
        asyncio.create_task(delete_file_later(file_path))

    elif action == "audio":
        out_path = DOWNLOAD_DIR / f"{user_id}_audio.mp3"
        tmp_path = DOWNLOAD_DIR / f"{user_id}_tmp.mp4"
        await query.edit_message_text("⬇️ Downloading & converting to audio...")

        # Pick best audio format
        format_id = "bestaudio"
        file_path = await download_with_ytdlp(url, format_id, tmp_path)
        if not file_path or not file_path.exists():
            await query.edit_message_text("❌ Failed to download audio.")
            return

        converted = await run_ffmpeg_convert(tmp_path, out_path)
        tmp_path.unlink(missing_ok=True)

        if not converted or not converted.exists():
            await query.edit_message_text("❌ Failed to convert audio.")
            return

        if converted.stat().st_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await query.edit_message_text("⚠️ Audio file too large for Telegram.")
            converted.unlink(missing_ok=True)
            return

        await context.bot.send_audio(
            chat_id=user_id, audio=open(converted, "rb"),
            caption="🎧 Here is your audio!"
        )
        asyncio.create_task(delete_file_later(converted))


# ===============================
# MAIN ENTRY
# ===============================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Run cache cleaner
    app.job_queue.run_repeating(lambda ctx: asyncio.create_task(cleanup_cache()), interval=60, first=60)

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
