#!/usr/bin/env python3
"""
Telegram Video Downloader Bot with Audio Format Selection
- Uses python-telegram-bot (v20.x) and yt-dlp
- Presents quality options for video
- Supports downloading audio with MP3/M4A choice
- Enforces 50 MB upload limit
- Deletes local files after 1 minute (Telegram copy remains)
- Designed for future expansion
"""

import os
import logging
import asyncio
import tempfile
import uuid
from typing import Dict, Any, List
from pathlib import Path

from yt_dlp import YoutubeDL
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, __version__ as ptb_version
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILESIZE_BYTES = 50 * 1024 * 1024  # 50 MB
SUPPORTED_PLATFORMS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "x.com", "twitter.com", "reddit.com", "v.redd.it")
PENDING: Dict[str, Dict[str, Any]] = {}
# -----------------------------------

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info("Starting Telegram Video Downloader Bot (PTB %s)", ptb_version)


# ---------- Utilities ----------
def url_is_supported(url: str) -> bool:
    return any(domain in url.lower() for domain in SUPPORTED_PLATFORMS)


async def ytdl_extract_info(url: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    def _extract():
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)
    return await loop.run_in_executor(None, _extract)


async def ytdl_download(url: str, format_id: str, outdir: str, audio_only: bool = False, audio_format: str = "m4a") -> Path:
    loop = asyncio.get_running_loop()
    def _download():
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        opts = {"format": format_id, "outtmpl": outtmpl, "quiet": True, "no_warnings": True}
        if audio_only:
            opts["format"] = "bestaudio"
            if audio_format == "mp3":
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            else:
                opts["merge_output_format"] = "m4a"
        else:
            opts["merge_output_format"] = "mp4"
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))
    return await loop.run_in_executor(None, _download)


async def delayed_cleanup(file_path: Path, delay: int = 60):
    """Delete file and parent temp dir after delay"""
    await asyncio.sleep(delay)
    try:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
            logger.info("Deleted temporary file: %s", file_path)
        temp_dir = file_path.parent
        if temp_dir.exists():
            temp_dir.rmdir()
    except Exception as e:
        logger.warning("Cleanup failed for %s: %s", file_path, e)


def build_quality_keyboard(formats: List[Dict[str, Any]], token: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with video qualities, audio option"""
    resolutions = [1080, 720, 480, 360, 240]
    sorted_formats = sorted(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    buttons = []

    for res in resolutions:
        for f in sorted_formats:
            if (f.get("height") or 0) >= res and f.get("ext") in ("mp4", "mkv", "webm"):
                filesize = f.get("filesize") or f.get("filesize_approx")
                label = f"{res}p"
                if filesize:
                    label += f" ({round(filesize/1024/1024,2)} MB)"
                buttons.append([InlineKeyboardButton(label, callback_data=f"DL|{token}|{f['format_id']}")])
                break

    # Audio options
    buttons.append([InlineKeyboardButton("Audio M4A", callback_data=f"AUDIO|{token}|m4a")])
    buttons.append([InlineKeyboardButton("Audio MP3", callback_data=f"AUDIO|{token}|mp3")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])
    return InlineKeyboardMarkup(buttons)


# ---------- Handlers ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a YouTube/TikTok/Instagram/X/Reddit video link and I’ll download it (≤50 MB)."
    )


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not url_is_supported(url):
        await update.message.reply_text("Unsupported URL. Use YouTube, TikTok, Instagram, X/Twitter, Reddit.")
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
        await update.message.reply_text("Choose a quality or audio format:", reply_markup=kb)
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
        # Video download
        if action == "DL" and len(data) == 3:
            format_id = data[2]
            path = await ytdl_download(url, format_id, temp_dir)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text=f"File is {round(size/1024/1024,2)} MB (>50 MB). Choose a lower quality.")
                return
            with open(path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Size: {round(size/1024/1024,2)} MB")

        # Audio download with format selection
        elif action == "AUDIO" and len(data) == 3:
            audio_format = data[2]
            path = await ytdl_download(url, "bestaudio", temp_dir, audio_only=True, audio_format=audio_format)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text=f"Audio is {round(size/1024/1024,2)} MB (>50 MB).")
                return
            with open(path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, caption=f"Audio size: {round(size/1024/1024,2)} MB, format: {audio_format.upper()}")

    finally:
        asyncio.create_task(delayed_cleanup(path))
        PENDING.pop(token, None)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
