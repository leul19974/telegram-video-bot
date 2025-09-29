import os
import logging
import time
import json
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram token from environment
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.error("Telegram token not set! Set TELEGRAM_BOT_TOKEN environment variable.")
    exit(1)

# Max file size ~1GB
MAX_FILE_SIZE = 1_000_000_000
CACHE_DURATION = 600  # 10 minutes
FILE_DELETE_DELAY = 60  # seconds

# Cache
video_cache = {}

# Supported platforms
SUPPORTED_PLATFORMS = [
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com", "reddit.com"
]

# yt-dlp options
YDL_OPTIONS = {"format": "best", "quiet": True, "no_warnings": True, "noplaylist": True}


def is_supported_url(url: str) -> bool:
    return any(p in url.lower() for p in SUPPORTED_PLATFORMS)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Send me a video link and I will prepare download options."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Help:\nSend a link from YouTube, TikTok, Instagram, Twitter/X, or Reddit.\n"
        "You'll get a preview and download options."
    )


def get_video_info(url: str):
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            # Process formats
            video_formats, audio_formats = [], []
            for f in info.get("formats", []):
                filesize = f.get("filesize") or f.get("filesize_approx") or 0
                if filesize > MAX_FILE_SIZE:
                    continue
                if f.get("vcodec") != "none":
                    video_formats.append(
                        {
                            "format_id": f.get("format_id"),
                            "ext": f.get("ext"),
                            "height": f.get("height"),
                            "filesize": filesize,
                        }
                    )
                elif f.get("acodec") != "none":
                    audio_formats.append(
                        {
                            "format_id": f.get("format_id"),
                            "ext": f.get("ext"),
                            "abr": f.get("abr"),
                            "filesize": filesize,
                        }
                    )
            return {
                "title": info.get("title", "Unknown"),
                "uploader": info.get("uploader", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "url": url,
                "video_formats": video_formats,
                "audio_formats": audio_formats,
            }
    except Exception as e:
        logger.error(f"yt-dlp fetch error: {e}")
        return None


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not is_supported_url(url):
        await update.message.reply_text(
            "❌ URL not supported. Send YouTube, TikTok, Instagram, Twitter/X, or Reddit links."
        )
        return

    cache_key = hash(url)
    if cache_key in video_cache and time.time() - video_cache[cache_key]["timestamp"] < CACHE_DURATION:
        video_data = video_cache[cache_key]
        logger.info("Using cached video data.")
    else:
        msg = await update.message.reply_text("⏳ Processing your link...")
        video_data = get_video_info(url)
        if not video_data:
            await msg.edit_text("❌ Failed to fetch video info.")
            return
        video_data["timestamp"] = time.time()
        video_cache[cache_key] = video_data
        await msg.delete()

    await send_preview(update, context, video_data)


async def send_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, video_data):
    chat_id = update.effective_chat.id
    keyboard = []

    # Video options (max 5)
    for v in video_data["video_formats"][:5]:
        size_str = f"{v['filesize'] / (1024*1024):.1f}MB" if v["filesize"] else "Unknown"
        keyboard.append([InlineKeyboardButton(
            f"🎬 {v['height']}p ({size_str})",
            callback_data=json.dumps({"type": "video", "format_id": v["format_id"], "url": video_data["url"]})
        )])

    # Audio option (best)
    if video_data["audio_formats"]:
        a = video_data["audio_formats"][0]
        size_str = f"{a['filesize'] / (1024*1024):.1f}MB" if a["filesize"] else "Unknown"
        keyboard.append([InlineKeyboardButton(
            f"🎵 Audio (MP3) ({size_str})",
            callback_data=json.dumps({"type": "audio", "format_id": a["format_id"], "url": video_data["url"]})
        )])

    # Cancel
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=json.dumps({"type": "cancel"}))])

    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"📹 *{video_data['title']}*\n👤 {video_data['uploader']}\nSelect download option:"
    if video_data["thumbnail"]:
        await context.bot.send_photo(chat_id=chat_id, photo=video_data["thumbnail"], caption=text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        data = json.loads(query.data)
        if data["type"] == "cancel":
            await query.edit_message_text("❌ Download cancelled.")
            return

        await query.edit_message_text("⏳ Downloading...")

        temp_dir = "temp"
        os.makedirs(temp_dir, exist_ok=True)
        ydl_opts = {
            "format": data["format_id"],
            "outtmpl": f"{temp_dir}/%(title)s.%(ext)s",
            "quiet": True,
            "noplaylist": True,
        }

        # Convert audio to mp3
        if data["type"] == "audio":
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(data["url"], download=True)
            filename = ydl.prepare_filename(info)
            if data["type"] == "audio":
                filename = os.path.splitext(filename)[0] + ".mp3"

        # Send file
        if data["type"] == "video":
            await context.bot.send_video(chat_id=query.message.chat.id, video=open(filename, "rb"))
        else:
            await context.bot.send_audio(chat_id=query.message.chat.id, audio=open(filename, "rb"))

        await query.edit_message_text("✅ Download completed!")

    except Exception as e:
        logger.error(f"Callback error: {e}")
        await query.edit_message_text("❌ Error occurred during download.")
    finally:
        # Clean up
        if "filename" in locals() and os.path.exists(filename):
            os.remove(filename)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_url))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
