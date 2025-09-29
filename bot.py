#!/usr/bin/env python3
"""
Telegram Video Downloader Bot
- Video & audio download from YouTube, TikTok, Instagram, X/Twitter, Reddit
- Enforces 50 MB max file upload
- Deletes server files after 1 minute
- Audio button available if ffmpeg is installed
- Works on Railway (requires ffmpeg in environment)
"""

import os
import logging
import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, List

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
import shutil
import subprocess

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
PENDING: Dict[str, Dict[str, Any]] = {}  # token -> data
# -----------------------------------

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Starting Telegram Video Downloader Bot (PTB %s)", ptb_version)

# Check ffmpeg availability
def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

FFMPEG_OK = ffmpeg_available()
logger.info("ffmpeg available: %s", FFMPEG_OK)

# ---------- Utilities ----------
def url_is_supported(url: str) -> bool:
    return any(domain in url.lower() for domain in SUPPORTED_PLATFORMS)


async def ytdl_extract_info(url: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()

    def _extract():
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)

    return await loop.run_in_executor(None, _extract)


async def ytdl_download(
    url: str,
    format_id: str,
    outdir: str,
    download_audio: bool = False,
    audio_format: str = "mp3",
) -> Path:
    loop = asyncio.get_running_loop()

    def _download():
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        ydl_opts: Dict[str, Any] = {
            "format": format_id,
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }
        if download_audio:
            if not FFMPEG_OK:
                raise RuntimeError("ffmpeg is not installed")
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": "192",
                }
            ]

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if download_audio:
                filename = Path(filename).with_suffix(f".{audio_format}")
            return Path(filename)

    return await loop.run_in_executor(None, _download)


async def delayed_cleanup(temp_dir: str, token: str, delay: int = 60):
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
    resolutions = [1080, 720, 480, 360, 240]
    sorted_formats = sorted(
        formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True
    )

    chosen: Dict[int, Dict[str, Any]] = {}
    for res in resolutions:
        for f in sorted_formats:
            if (f.get("height") or 0) >= res and f.get("ext") in ("mp4", "mkv", "webm"):
                chosen[res] = f
                break

    buttons: List[List[InlineKeyboardButton]] = []
    for res, f in chosen.items():
        filesize = f.get("filesize") or f.get("filesize_approx")
        label = f"{res}p"
        if filesize:
            label += f" ({round(filesize/1024/1024,2)} MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"DL|{token}|{f['format_id']}")])

    if FFMPEG_OK:
        buttons.append([InlineKeyboardButton("Download Audio", callback_data=f"AUDIO|{token}|mp3")])

    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])
    return InlineKeyboardMarkup(buttons)

# ---------- Handlers ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a YouTube/TikTok/Instagram/X/Reddit video link and I’ll download it (≤50 MB)."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Usage:\n"
        "1. Send a video URL.\n"
        "2. Choose a quality or audio (if ffmpeg installed).\n"
        "3. Video/audio will be sent if ≤50 MB.\n"
        "Notes:\n"
        "• Files are deleted from server after 1 minute.\n"
        "• ffmpeg must be installed for audio extraction."
    )
    await update.message.reply_text(msg)


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not url.startswith("http"):
        await update.message.reply_text("Please send a full URL (http/https).")
        return
    if not url_is_supported(url):
        await update.message.reply_text("Unsupported URL.")
        return

    await update.message.reply_text("Fetching formats...")
    try:
        info = await ytdl_extract_info(url)
        formats = [f for f in info.get("formats", []) if f.get("vcodec") != "none"]
        if not formats:
            await update.message.reply_text("No video formats found.")
            return

        token = str(uuid.uuid4())
        PENDING[token] = {"url": url, "formats": formats, "chat_id": update.message.chat_id}
        kb = build_quality_keyboard(formats, token)
        await update.message.reply_text("Choose a quality:", reply_markup=kb)
    except Exception:
        await update.message.reply_text("Failed to fetch video info.")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")

    if len(data) < 2:
        return

    action, token = data[0], data[1]
    pending = PENDING.get(token)
    if not pending:
        await query.edit_message_text("Request expired.")
        return

    chat_id = pending["chat_id"]
    url = pending["url"]

    if action == "CANCEL":
        PENDING.pop(token, None)
        await query.edit_message_text("Cancelled.")
        return

    temp_dir = tempfile.mkdtemp(prefix="tgdl_")

    try:
        if action == "DL" and len(data) == 3:
            format_id = data[2]
            path = await ytdl_download(url, format_id, temp_dir)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text=f"File too large ({round(size/1024/1024,2)} MB).")
                return
            with open(path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Size: {round(size/1024/1024,2)} MB")

        elif action == "AUDIO" and len(data) == 3:
            if not FFMPEG_OK:
                await context.bot.send_message(chat_id=chat_id, text="Audio extraction not available (ffmpeg missing).")
                return
            audio_format = data[2]
            try:
                path = await ytdl_download(url, "bestaudio/best", temp_dir, download_audio=True, audio_format=audio_format)
                size = path.stat().st_size
                if size > MAX_FILESIZE_BYTES:
                    await context.bot.send_message(chat_id=chat_id, text=f"Audio too large ({round(size/1024/1024,2)} MB).")
                    return
                with open(path, "rb") as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f, caption=f"Size: {round(size/1024/1024,2)} MB")
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text="Failed to download audio. Make sure ffmpeg is installed.")

    finally:
        asyncio.create_task(delayed_cleanup(temp_dir, token))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling an update: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="An unexpected error occurred. Incident logged.")
    except Exception:
        logger.exception("Failed to notify user about error.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
