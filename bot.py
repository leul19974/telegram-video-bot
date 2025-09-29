#!/usr/bin/env python3
"""
Telegram Video & Audio Downloader Bot
- Uses python-telegram-bot (v20.x) and yt-dlp
- Supports video and audio downloads
- Enforces 50 MB upload limit
- Deletes server files after 1 minute
- Designed for long-term maintainable code (>300 lines)
"""

import os
import logging
import asyncio
import tempfile
import uuid
from typing import Dict, Any, Optional, List
from pathlib import Path

from yt_dlp import YoutubeDL, utils as ytdlp_utils
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com", 
    "x.com", "twitter.com", "reddit.com", "v.redd.it"
)
PENDING: Dict[str, Dict[str, Any]] = {}  # In-memory mapping for pending requests
# ------------------------------------------------

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Starting Telegram Video & Audio Downloader Bot")

# ---------------- Utilities ----------------
def url_is_supported(url: str) -> bool:
    """Check if URL belongs to a supported platform."""
    return any(domain in url.lower() for domain in SUPPORTED_PLATFORMS)

async def ytdl_extract_info(url: str) -> Dict[str, Any]:
    """Extract video info from yt-dlp asynchronously."""
    loop = asyncio.get_running_loop()
    def _extract():
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)
    return await loop.run_in_executor(None, _extract)

async def ytdl_download(
    url: str, 
    format_id: str, 
    outdir: str, 
    audio_only: bool = False, 
    audio_format: str = "m4a"
) -> Path:
    """Download video or audio using yt-dlp asynchronously."""
    loop = asyncio.get_running_loop()

    def _download():
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        opts: Dict[str, Any] = {"quiet": True, "no_warnings": True, "outtmpl": outtmpl}

        if audio_only:
            opts["format"] = "bestaudio"
            if audio_format == "mp3":
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]
            else:
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "192",
                }]
        else:
            opts["format"] = format_id
            opts["merge_output_format"] = "mp4"

        final_path: Optional[Path] = None
        def hook(d):
            nonlocal final_path
            if d.get("status") == "finished":
                final_path = Path(d.get("filename"))

        opts["progress_hooks"] = [hook]

        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
            if final_path is None:
                raise FileNotFoundError("Downloaded file not found")
            return final_path

    return await loop.run_in_executor(None, _download)

async def delayed_cleanup(temp_dir: str, token: str, delay: int = 60):
    """Delete server files after delay (1 min default)."""
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
    """Build buttons for video quality selection + audio download."""
    resolutions = [1080, 720, 480, 360, 240]
    sorted_formats = sorted(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)

    buttons = []
    chosen = {}
    for res in resolutions:
        for f in sorted_formats:
            if (f.get("height") or 0) >= res and f.get("ext") in ("mp4", "mkv", "webm"):
                chosen[res] = f
                break

    for res, f in chosen.items():
        filesize = f.get("filesize") or f.get("filesize_approx")
        label = f"{res}p"
        if filesize:
            label += f" ({round(filesize/1024/1024, 2)} MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"DL|{token}|{f['format_id']}")])

    # Audio download buttons
    buttons.append([InlineKeyboardButton("Download Audio (MP3)", callback_data=f"AUDIO|{token}|mp3")])
    buttons.append([InlineKeyboardButton("Download Audio (M4A)", callback_data=f"AUDIO|{token}|m4a")])

    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])
    return InlineKeyboardMarkup(buttons)

# ---------------- Handlers ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message."""
    await update.message.reply_text(
        "Hi! Send me a YouTube/TikTok/Instagram/X/Reddit video link. "
        "I can download video ≤50MB or extract audio."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message."""
    await update.message.reply_text(
        "Usage:\n"
        "1. Send a video URL.\n"
        "2. Choose video quality or audio download.\n"
        "3. The bot will send the file if ≤50 MB.\n"
        "4. Files are deleted from server after 1 minute."
    )

async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming URL messages."""
    url = (update.message.text or "").strip()
    if not url_is_supported(url):
        await update.message.reply_text(
            "Unsupported URL. Supported: YouTube, TikTok, Instagram, X/Twitter, Reddit."
        )
        return

    await update.message.reply_text("Fetching formats... Please wait.")
    try:
        info = await ytdl_extract_info(url)
        formats = [f for f in info.get("formats", []) if f.get("vcodec") != "none"]
        if not formats:
            await update.message.reply_text("No video formats found.")
            return

        token = str(uuid.uuid4())
        PENDING[token] = {"url": url, "formats": formats, "chat_id": update.message.chat_id}
        kb = build_quality_keyboard(formats, token)
        await update.message.reply_text("Choose a quality or audio:", reply_markup=kb)

    except Exception as e:
        logger.exception("Failed to extract video info: %s", e)
        await update.message.reply_text("Failed to fetch video info. Try another link.")

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
                await context.bot.send_message(chat_id=chat_id,
                    text=f"File size {round(size/1024/1024,2)} MB exceeds 50 MB limit.")
                return
            with open(path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f,
                                             caption=f"Video size: {round(size/1024/1024,2)} MB")

        if action == "AUDIO" and len(data) == 3:
            audio_format = data[2]
            path = await ytdl_download(url, "bestaudio", temp_dir, audio_only=True, audio_format=audio_format)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id,
                    text=f"Audio size {round(size/1024/1024,2)} MB exceeds 50 MB limit.")
                return
            with open(path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f,
                                             caption=f"Audio ({audio_format.upper()}) size: {round(size/1024/1024,2)} MB")

    finally:
        asyncio.create_task(delayed_cleanup(temp_dir, token))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling update: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="An unexpected error occurred. Incident logged."
            )
    except Exception:
        logger.exception("Failed to notify user about error.")

# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
