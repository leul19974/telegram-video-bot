#!/usr/bin/env python3
"""
Telegram Video & Audio Downloader Bot
- Uses python-telegram-bot v20.x and yt-dlp
- Supports YouTube/TikTok/Instagram/X/Reddit
- Video & audio download (≤50 MB)
- Deletes server files after 1 min
- Fully async-safe with logging
"""

import os
import logging
import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

from yt_dlp import YoutubeDL, utils as ytdlp_utils
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, __version__ as ptb_version
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
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "x.com", "twitter.com", "reddit.com", "v.redd.it"
)
PENDING: Dict[str, Dict[str, Any]] = {}  # in-memory mapping
# -----------------------------------

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
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

async def ytdl_download(url: str, format_id: str, outdir: str) -> Path:
    loop = asyncio.get_running_loop()

    def _download():
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        opts = {
            "format": format_id,
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))

    return await loop.run_in_executor(None, _download)

async def ytdl_download_audio(url: str, outdir: str, audio_format: str = "m4a") -> Path:
    loop = asyncio.get_running_loop()

    def _download_audio():
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": "192",
                }
            ],
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = Path(ydl.prepare_filename(info))
            # Adjust extension if ffmpeg changed it
            filename = filename.with_suffix(f".{audio_format}")
            if not filename.exists():
                raise FileNotFoundError("Audio file not found after download.")
            return filename

    return await loop.run_in_executor(None, _download_audio)

async def delayed_cleanup(temp_dir: str, token: str, delay: int = 60):
    await asyncio.sleep(delay)
    try:
        for p in Path(temp_dir).glob("*"):
            p.unlink(missing_ok=True)
        Path(temp_dir).rmdir()
        PENDING.pop(token, None)
        logger.info("Cleaned up temp_dir %s", temp_dir)
    except Exception as e:
        logger.warning("Cleanup failed for %s: %s", temp_dir, e)

def build_quality_keyboard(formats: List[Dict[str, Any]], token: str) -> InlineKeyboardMarkup:
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
    buttons.append([InlineKeyboardButton("Audio (m4a)", callback_data=f"AUDIO|{token}|m4a")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])
    return InlineKeyboardMarkup(buttons)

# ---------- Handlers ----------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send a YouTube/TikTok/Instagram/X/Reddit link and I will let you download video (≤50MB) or audio."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage:\n1. Send a video link.\n2. Choose video quality or audio.\n3. Files >50MB won't be sent.\n\n"
        "Note: Bot deletes server temp files after 1 minute. Telegram copy remains."
    )

async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not url_is_supported(url):
        await update.message.reply_text("Unsupported URL. Use YouTube, TikTok, Instagram, X/Twitter, Reddit.")
        return

    await update.message.reply_text("Fetching video formats, please wait...")
    try:
        info = await ytdl_extract_info(url)
        formats = [f for f in info.get("formats", []) if f.get("vcodec") != "none"]
        if not formats:
            await update.message.reply_text("No downloadable video formats found.")
            return

        token = str(uuid.uuid4())
        PENDING[token] = {"url": url, "formats": formats, "chat_id": update.message.chat_id}
        kb = build_quality_keyboard(formats, token)
        await update.message.reply_text("Select download option:", reply_markup=kb)
    except Exception as e:
        logger.exception("Failed to fetch info: %s", e)
        await update.message.reply_text("Failed to fetch video info. Check link or try again later.")

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    if len(data) < 2:
        return
    action, token = data[0], data[1]
    pending = PENDING.get(token)
    if not pending:
        await query.edit_message_text("Request expired or invalid.")
        return

    chat_id = pending["chat_id"]
    url = pending["url"]
    temp_dir = tempfile.mkdtemp(prefix="tgdl_")

    try:
        if action == "CANCEL":
            PENDING.pop(token, None)
            await query.edit_message_text("Cancelled.")
            return

        if action == "DL" and len(data) == 3:
            format_id = data[2]
            try:
                path = await ytdl_download(url, format_id, temp_dir)
                size = path.stat().st_size
                if size > MAX_FILESIZE_BYTES:
                    await context.bot.send_message(chat_id=chat_id,
                        text=f"Video size {round(size/1024/1024,2)} MB exceeds 50MB limit.")
                    return
                with open(path, "rb") as f:
                    await context.bot.send_video(chat_id=chat_id, video=f,
                                                 caption=f"Video size: {round(size/1024/1024,2)} MB")
            except Exception as e:
                logger.exception("Video download failed: %s", e)
                await context.bot.send_message(chat_id=chat_id,
                                               text="Failed to download video.")
            finally:
                asyncio.create_task(delayed_cleanup(temp_dir, token))

        if action == "AUDIO" and len(data) == 3:
            audio_format = data[2]  # usually m4a
            try:
                path = await ytdl_download_audio(url, temp_dir, audio_format)
                size = path.stat().st_size
                if size > MAX_FILESIZE_BYTES:
                    await context.bot.send_message(chat_id=chat_id,
                        text=f"Audio size {round(size/1024/1024,2)} MB exceeds 50MB limit.")
                    return
                with open(path, "rb") as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f,
                                                 caption=f"Audio ({audio_format}) size: {round(size/1024/1024,2)} MB")
            except Exception as e:
                logger.exception("Audio download failed: %s", e)
                await context.bot.send_message(chat_id=chat_id,
                                               text="Failed to download audio. Make sure ffmpeg is installed.")
            finally:
                asyncio.create_task(delayed_cleanup(temp_dir, token))

    except Exception as e:
        logger.exception("Callback handling error: %s", e)
        await context.bot.send_message(chat_id=chat_id, text="Unexpected error occurred. Incident logged.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception in update: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text="An unexpected error occurred. Incident logged.")
    except Exception:
        logger.exception("Failed to notify user of error.")

# ---------- Main ----------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
