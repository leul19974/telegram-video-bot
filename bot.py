#!/usr/bin/env python3
"""
Telegram Video Downloader Bot
- Uses python-telegram-bot (v20.x) and yt-dlp
- Presents quality options, enforces 50 MB upload limit
- Stores BOT_TOKEN in environment variable BOT_TOKEN
- Designed to run on Railway with: worker: python bot.py
"""

import os
import logging
import asyncio
import tempfile
import uuid
from typing import Dict, Any, Optional, List
from pathlib import Path

from yt_dlp import YoutubeDL, utils as ytdlp_utils
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

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILESIZE_BYTES = 50 * 1024 * 1024  # 50 MB
SUPPORTED_PLATFORMS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "reddit.com",
    "v.redd.it",
)
PENDING: Dict[str, Dict[str, Any]] = {}
# -----------------------------------

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Starting Telegram Video Downloader Bot (PTB %s)", ptb_version)


# ---------------- Utility ----------------
def url_is_supported(url: str) -> bool:
    lower = url.lower()
    return any(domain in lower for domain in SUPPORTED_PLATFORMS)


async def ytdl_extract_info(url: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()

    def _extract():
        ydl_opts = {"quiet": True, "no_warnings": True}
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    return await loop.run_in_executor(None, _extract)


async def ytdl_download(url: str, format_id: str, outdir: str) -> Path:
    loop = asyncio.get_running_loop()

    def _download():
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        ydl_opts = {
            "format": format_id,
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            try:
                filename = ydl.prepare_filename(info)
            except Exception:
                filename = None
            if filename and os.path.exists(filename):
                return Path(filename)
            files = list(Path(outdir).glob("*"))
            if files:
                return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]
            raise FileNotFoundError("Download completed but file not found.")

    return await loop.run_in_executor(None, _download)


async def convert_to_audio(input_path: Path, output_path: Path) -> Path:
    loop = asyncio.get_running_loop()

    def _convert():
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_audio": True,
            "format": "bestaudio/best",
            "outtmpl": str(output_path),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        with YoutubeDL(ydl_opts) as ydl:
            ydl.process_info({"_filename": str(input_path)})
        return output_path

    return await loop.run_in_executor(None, _convert)


def build_quality_keyboard(formats: List[Dict[str, Any]], token: str) -> InlineKeyboardMarkup:
    resolutions = [1080, 720, 480, 360, 240]
    chosen = {}
    sorted_formats = sorted(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)

    for res in resolutions:
        for f in sorted_formats:
            h = f.get("height")
            ext = f.get("ext", "")
            if h and h >= res and ext in ("mp4", "m4a", "webm", "mkv"):
                chosen[res] = f
                break
    if not chosen and sorted_formats:
        chosen[sorted_formats[0].get("height", 0)] = sorted_formats[0]

    buttons = []
    for res in resolutions:
        f = chosen.get(res)
        if not f:
            continue
        filesize = f.get("filesize") or f.get("filesize_approx")
        label = f"{res}p"
        if filesize:
            mb = round(filesize / (1024 * 1024), 2)
            label += f" ({mb} MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"DL|{token}|{f.get('format_id')}")])

    # Add audio option
    buttons.append([InlineKeyboardButton("Audio (MP3)", callback_data=f"AUDIO|{token}")])

    # Cancel
    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])

    return InlineKeyboardMarkup(buttons)


# ---------------- Commands ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a YouTube / TikTok / Instagram / X (Twitter) / Reddit video link.\n\n"
        "I'll let you choose quality or extract audio (<= 50 MB).\n\n"
        "Commands:\n/start - this message\n/help - usage and tips"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage:\n1. Send a video URL (YouTube, TikTok, Instagram, X/Twitter, Reddit).\n"
        "2. Choose quality or audio.\n3. I'll download and send if it's <= 50 MB.\n\n"
        "Notes:\n• If file > 50 MB I'll cancel.\n• Runs best for short videos.\n"
    )


# ---------------- Handlers ----------------
async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith("http"):
        await update.message.reply_text("Please send a valid video URL.")
        return
    if not url_is_supported(text):
        await update.message.reply_text("Unsupported URL. Supported: YouTube, TikTok, Instagram, X/Twitter, Reddit.")
        return

    await update.message.reply_text("Fetching available formats...")

    try:
        info = await ytdl_extract_info(text)
    except Exception as e:
        logger.exception("Failed to extract info: %s", e)
        await update.message.reply_text("❌ Failed to fetch video info.")
        return

    formats = info.get("formats") or []
    video_formats = [f for f in formats if f.get("vcodec") and f.get("vcodec") != "none"]
    if not video_formats:
        await update.message.reply_text("No video formats available.")
        return

    token = str(uuid.uuid4())
    PENDING[token] = {"url": text, "formats": video_formats}
    kb = build_quality_keyboard(video_formats, token)
    await update.message.reply_text("Choose download option:", reply_markup=kb)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = (query.data or "").split("|")

    if len(data) < 2:
        return
    action, token = data[0], data[1]
    pending = PENDING.get(token)

    if not pending:
        await query.edit_message_text("Request expired. Send the link again.")
        return

    url = pending["url"]
    temp_dir = tempfile.mkdtemp(prefix="tgdl_")
    downloaded_path: Optional[Path] = None

    if action == "CANCEL":
        PENDING.pop(token, None)
        await query.edit_message_text("❌ Cancelled.")
        return

    try:
        if action == "DL":
            format_id = data[2]
            await query.edit_message_text("⬇️ Downloading video...")
            downloaded_path = await ytdl_download(url, format_id, temp_dir)

            if downloaded_path.stat().st_size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(query.message.chat_id, "❌ File too large (>50MB). Try lower quality.")
                return

            with open(downloaded_path, "rb") as f:
                await context.bot.send_video(query.message.chat_id, f, caption=f"Downloaded from {url}")

        elif action == "AUDIO":
            await query.edit_message_text("🎵 Extracting audio...")
            downloaded_path = await ytdl_download(url, "bestaudio/best", temp_dir)
            audio_path = Path(temp_dir) / f"{downloaded_path.stem}.mp3"
            audio_path = await convert_to_audio(downloaded_path, audio_path)

            if audio_path.stat().st_size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(query.message.chat_id, "❌ Audio file too large (>50MB).")
                return

            with open(audio_path, "rb") as f:
                await context.bot.send_audio(query.message.chat_id, f, caption=f"Audio extracted from {url}")

    except Exception as e:
        logger.exception("Error: %s", e)
        await context.bot.send_message(query.message.chat_id, "❌ Failed to process this request.")
    finally:
        # Schedule deletion after 2 minutes
        async def delayed_cleanup(path: Path, folder: str):
            await asyncio.sleep(120)
            try:
                if path and path.exists():
                    path.unlink(missing_ok=True)
                for p in Path(folder).glob("*"):
                    p.unlink(missing_ok=True)
                Path(folder).rmdir()
            except Exception:
                pass

        if downloaded_path:
            asyncio.create_task(delayed_cleanup(downloaded_path, temp_dir))

        PENDING.pop(token, None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(update.effective_chat.id, "⚠️ An unexpected error occurred.")
    except Exception:
        pass


# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set.")
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
