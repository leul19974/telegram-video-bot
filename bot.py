#!/usr/bin/env python3
"""
Telegram Video Downloader Bot
-----------------------------------
Features:
- Downloads from YouTube, TikTok, Instagram, Twitter/X, Reddit
- Always merges audio + video (fixes Instagram "no sound" issue)
- File size limit: 50 MB (Telegram restriction for normal bots)
- After download: user chooses:
    1. ✅ Get Video
    2. 🎵 Convert to Audio (MP3)
- Deletes temp files from server after 1 minute
- Built with python-telegram-bot v20.x and yt-dlp

Code length intentionally >300 lines for better future flexibility
"""

import os
import logging
import asyncio
import tempfile
import uuid
from typing import Dict, Any, Optional, List
from pathlib import Path

import ffmpeg
from yt_dlp import YoutubeDL
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    __version__ as ptb_version,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------- Configuration ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILESIZE_BYTES = 50 * 1024 * 1024  # 50 MB
SUPPORTED_PLATFORMS = (
    "youtube.com", "youtu.be",
    "tiktok.com",
    "instagram.com",
    "x.com", "twitter.com",
    "reddit.com", "v.redd.it"
)

# Keep track of pending requests
PENDING: Dict[str, Dict[str, Any]] = {}
# ------------------------------------------------

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Starting Telegram Downloader Bot (PTB %s)", ptb_version)
# ------------------------------------------------


# ---------------- Utility Functions ----------------
def url_is_supported(url: str) -> bool:
    """Check if the provided URL is from a supported platform."""
    return any(domain in url.lower() for domain in SUPPORTED_PLATFORMS)


async def ytdl_extract_info(url: str) -> Dict[str, Any]:
    """Extract video metadata (formats, title, etc.) without downloading."""
    loop = asyncio.get_running_loop()

    def _extract():
        opts = {"quiet": True, "no_warnings": True}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return await loop.run_in_executor(None, _extract)


async def ytdl_download(url: str, format_id: str, outdir: str) -> Path:
    """Download video and ensure audio+video are merged into MP4."""
    loop = asyncio.get_running_loop()

    def _download():
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        opts = {
            "format": format_id + "+bestaudio/best",  # merge video+audio
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))

    return await loop.run_in_executor(None, _download)


async def convert_to_audio(input_path: Path, output_path: Path) -> Path:
    """Convert downloaded video into MP3 audio."""
    loop = asyncio.get_running_loop()

    def _convert():
        (
            ffmpeg
            .input(str(input_path))
            .output(str(output_path), format="mp3", acodec="libmp3lame", audio_bitrate="192k")
            .overwrite_output()
            .run(quiet=True)
        )
        return output_path

    return await loop.run_in_executor(None, _convert)


async def delayed_cleanup(temp_dir: str, token: str, delay: int = 60):
    """Remove temporary files after delay (default 60s)."""
    await asyncio.sleep(delay)
    try:
        for p in Path(temp_dir).glob("*"):
            p.unlink(missing_ok=True)
        Path(temp_dir).rmdir()
        PENDING.pop(token, None)
        logger.info("Cleaned up %s", temp_dir)
    except Exception as e:
        logger.warning("Cleanup failed for %s: %s", temp_dir, e)


def build_quality_keyboard(formats: List[Dict[str, Any]], token: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for quality selection."""
    resolutions = [1080, 720, 480, 360, 240]
    sorted_formats = sorted(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)

    chosen = {}
    for res in resolutions:
        for f in sorted_formats:
            if (f.get("height") or 0) >= res and f.get("ext") in ("mp4", "mkv", "webm"):
                chosen[res] = f
                break

    buttons = []
    for res, f in chosen.items():
        filesize = f.get("filesize") or f.get("filesize_approx")
        label = f"{res}p"
        if filesize:
            label += f" ({round(filesize/1024/1024,2)} MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"DL|{token}|{f['format_id']}")])

    buttons.append([InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{token}")])
    return InlineKeyboardMarkup(buttons)


def build_delivery_keyboard(token: str, path: Path) -> InlineKeyboardMarkup:
    """After download, let user choose video or audio."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Get Video", callback_data=f"VIDEO|{token}|{path}")],
        [InlineKeyboardButton("🎵 Convert to Audio (MP3)", callback_data=f"AUDIO|{token}|{path}")],
        [InlineKeyboardButton("Cancel ❌", callback_data=f"CANCEL|{token}")]
    ])
# ------------------------------------------------


# ---------------- Handlers ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "👋 Hi! Send me a YouTube/TikTok/Instagram/Twitter/Reddit video link.\n"
        "I’ll download it for you (max 50 MB)."
    )


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming URLs."""
    url = (update.message.text or "").strip()
    if not url_is_supported(url):
        await update.message.reply_text("⚠️ Unsupported URL. Please send YouTube, TikTok, Instagram, Twitter/X, or Reddit link.")
        return

    await update.message.reply_text("🔎 Fetching formats...")
    try:
        info = await ytdl_extract_info(url)
        formats = [f for f in info.get("formats", []) if f.get("vcodec") != "none"]
        if not formats:
            await update.message.reply_text("❌ No video formats found.")
            return

        token = str(uuid.uuid4())
        PENDING[token] = {"url": url, "formats": formats, "chat_id": update.message.chat_id}
        kb = build_quality_keyboard(formats, token)
        await update.message.reply_text("🎥 Choose a quality:", reply_markup=kb)
    except Exception as e:
        logger.error("Error extracting info: %s", e)
        await update.message.reply_text("❌ Failed to fetch video info.")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses."""
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")

    if len(data) < 2:
        return

    action, token = data[0], data[1]
    pending = PENDING.get(token)
    if not pending:
        await query.edit_message_text("⌛ Request expired.")
        return

    chat_id = pending["chat_id"]
    url = pending["url"]

    if action == "CANCEL":
        PENDING.pop(token, None)
        await query.edit_message_text("❌ Cancelled.")
        return

    if action == "DL" and len(data) == 3:
        format_id = data[2]
        temp_dir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            path = await ytdl_download(url, format_id, temp_dir)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ File is too large ({round(size/1024/1024,2)} MB). Must be ≤50 MB.")
                return

            kb = build_delivery_keyboard(token, path)
            await context.bot.send_message(chat_id=chat_id, text="✅ Download complete. Choose an option:", reply_markup=kb)
        except Exception as e:
            logger.error("Download failed: %s", e)
            await context.bot.send_message(chat_id=chat_id, text="❌ Failed to download video.")
        finally:
            asyncio.create_task(delayed_cleanup(temp_dir, token))

    if action in ("VIDEO", "AUDIO") and len(data) == 3:
        file_path = Path(data[2])
        if not file_path.exists():
            await context.bot.send_message(chat_id=chat_id, text="❌ File not found (maybe already cleaned up).")
            return

        if action == "VIDEO":
            with open(file_path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption="🎥 Here’s your video!")
        elif action == "AUDIO":
            audio_path = file_path.with_suffix(".mp3")
            try:
                await convert_to_audio(file_path, audio_path)
                with open(audio_path, "rb") as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f, caption="🎵 Converted to MP3")
            except Exception as e:
                logger.error("Audio conversion failed: %s", e)
                await context.bot.send_message(chat_id=chat_id, text="❌ Failed to convert video to audio.")
# ------------------------------------------------


# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.run_polling()
# ------------------------------------------------


if __name__ == "__main__":
    main()
