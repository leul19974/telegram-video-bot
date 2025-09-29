import os
import logging
import time
import json
import yt_dlp
from datetime import timedelta
from pathlib import Path
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Config
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
MAX_FILE_SIZE = 1000 * 1024 * 1024  # 1 GB
CACHE_DURATION = 10 * 60  # 10 minutes
FILE_DELETE_DELAY = 60  # 1 minute

# Cache
video_cache = {}

# Supported platforms
SUPPORTED_PLATFORMS = [
    "youtube.com", "youtu.be",
    "instagram.com", "instagr.am",
    "tiktok.com",
    "twitter.com", "x.com",
    "reddit.com"
]

# yt-dlp options
YDL_OPTIONS = {
    "format": "best",
    "outtmpl": "temp_downloads/%(title).200s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "socket_timeout": 30,
}


def is_supported_url(url: str) -> bool:
    return any(site in url.lower() for site in SUPPORTED_PLATFORMS)


def get_video_info(url: str) -> dict | None:
    """Extract video info using yt-dlp."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            video_data = {
                "title": info.get("title", "Unknown Title"),
                "uploader": info.get("uploader", "Unknown Uploader"),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail"),
                "formats": info.get("formats", []),
                "url": url,
                "timestamp": time.time()
            }

            video_formats = []
            audio_formats = []

            for fmt in info.get("formats", []):
                try:
                    filesize = fmt.get("filesize") or fmt.get("filesize_approx", 0)
                    if filesize > MAX_FILE_SIZE:
                        continue

                    if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                        video_formats.append({
                            "format_id": fmt.get("format_id"),
                            "height": fmt.get("height"),
                            "filesize": filesize,
                            "ext": fmt.get("ext", "mp4"),
                        })
                    elif fmt.get("vcodec") == "none" and fmt.get("acodec") != "none":
                        audio_formats.append({
                            "format_id": fmt.get("format_id"),
                            "abr": fmt.get("abr", 0),
                            "filesize": filesize,
                            "ext": fmt.get("ext", "mp3"),
                        })
                except Exception:
                    continue

            # sort
            video_formats = sorted(video_formats, key=lambda x: x.get("height", 0), reverse=True)
            audio_formats = sorted(audio_formats, key=lambda x: x.get("abr", 0), reverse=True)

            video_data["video_formats"] = video_formats
            video_data["audio_formats"] = audio_formats
            return video_data
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! I'm a video downloader bot.\n\n"
        "Send me a link from YouTube, Instagram, TikTok, Twitter/X, or Reddit."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Help*\n\n"
        "Send me a link from supported platforms and I'll show a preview "
        "with options to download video or audio.\n\n"
        "⚠️ Limit: 1GB file size.\n"
        "Files are deleted from server after 1 minute.",
        parse_mode="Markdown"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not is_supported_url(url):
        await update.message.reply_text("❌ Unsupported URL. Try YouTube, Instagram, TikTok, Twitter/X, or Reddit.")
        return

    cache_key = hash(url)
    if cache_key in video_cache and time.time() - video_cache[cache_key]["timestamp"] < CACHE_DURATION:
        video_data = video_cache[cache_key]
    else:
        msg = await update.message.reply_text("⏳ Fetching video info...")
        video_data = get_video_info(url)
        if not video_data:
            await msg.edit_text("❌ Failed to fetch video info.")
            return
        video_cache[cache_key] = video_data
        await msg.delete()

    await send_preview(update, context, video_data)


async def send_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, video_data: dict):
    duration = video_data.get("duration", 0)
    if duration:
        mins, secs = divmod(duration, 60)
        hrs, mins = divmod(mins, 60)
        duration_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"
    else:
        duration_str = "Unknown"

    caption = (
        f"📹 *{video_data['title']}*\n"
        f"👤 Uploader: {video_data['uploader']}\n"
        f"⏱ Duration: {duration_str}\n\n"
        "Select download option:"
    )

    keyboard = []
    for fmt in video_data.get("video_formats", [])[:4]:
        size = fmt["filesize"] / (1024 * 1024) if fmt["filesize"] else 0
        btn_text = f"🎬 {fmt['height']}p ({size:.1f}MB)"
        keyboard.append([InlineKeyboardButton(
            btn_text,
            callback_data=json.dumps({
                "type": "video",
                "url": video_data["url"],
                "format_id": fmt["format_id"],
                "ext": fmt["ext"],
            })
        )])

    if video_data.get("audio_formats"):
        best_audio = video_data["audio_formats"][0]
        size = best_audio["filesize"] / (1024 * 1024) if best_audio["filesize"] else 0
        keyboard.append([InlineKeyboardButton(
            f"🎵 Audio MP3 ({size:.1f}MB)",
            callback_data=json.dumps({
                "type": "audio",
                "url": video_data["url"],
                "format_id": best_audio["format_id"],
                "ext": "mp3",
            })
        )])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=json.dumps({"type": "cancel"}))])

    if video_data.get("thumbnail"):
        try:
            await update.message.reply_photo(
                photo=video_data["thumbnail"],
                caption=caption,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return
        except Exception:
            pass

    await update.message.reply_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        data = json.loads(query.data)
    except Exception:
        await query.edit_message_text("❌ Invalid request.")
        return

    if data["type"] == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return

    msg = await query.edit_message_text("⏳ Downloading...")

    file_path = download_media(data)
    if not file_path:
        await msg.edit_text("❌ Failed to download.")
        return

    try:
        if data["type"] == "video":
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=open(file_path, "rb"),
                caption="✅ Here’s your video!"
            )
        else:
            await context.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=open(file_path, "rb"),
                caption="✅ Here’s your audio!"
            )
        await msg.edit_text("✅ Done!")
    except Exception as e:
        logger.error(f"Send file error: {e}")
        await msg.edit_text("❌ Failed to send file (too large?)")

    # Schedule delete
    context.job_queue.run_once(delete_file, FILE_DELETE_DELAY, data={"file_path": file_path})


def download_media(data: dict) -> str | None:
    url, format_id, ext = data.get("url"), data.get("format_id"), data.get("ext")
    if not all([url, format_id, ext]):
        return None

    os.makedirs("temp_downloads", exist_ok=True)
    ydl_opts = YDL_OPTIONS.copy()
    ydl_opts.update({
        "format": format_id,
        "outtmpl": f"temp_downloads/%(title).200s.%(ext)s",
    })
    if data["type"] == "audio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if data["type"] == "audio":
                filename = Path(filename).with_suffix(".mp3")
            return str(filename)
    except Exception as e:
        logger.error(f"yt-dlp download error: {e}")
        return None


async def delete_file(context: ContextTypes.DEFAULT_TYPE):
    file_path = context.job.data.get("file_path")
    if file_path and Path(file_path).exists():
        try:
            Path(file_path).unlink()
            logger.info(f"Deleted {file_path}")
        except Exception as e:
            logger.error(f"Delete error: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused {context.error}")


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    async def clean_cache(context: ContextTypes.DEFAULT_TYPE):
        now = time.time()
        to_del = [k for k, v in video_cache.items() if now - v["timestamp"] > CACHE_DURATION]
        for k in to_del:
            del video_cache[k]
        logger.info(f"Cleaned {len(to_del)} cache entries")

    app.job_queue.run_repeating(clean_cache, interval=3600, first=3600)

    logger.info("Bot started ✅")
    app.run_polling()


if __name__ == "__main__":
    main()
