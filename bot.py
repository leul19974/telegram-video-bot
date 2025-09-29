#!/usr/bin/env python3
"""
Telegram Video Downloader Bot
- python-telegram-bot (v20.x)
- yt-dlp for downloads
- Supports YouTube, TikTok, Instagram, X/Twitter, Reddit
- Lets user pick video quality or download audio
- Enforces 50 MB Telegram upload limit
- Cleans up server files after 1 min
"""

import os
import logging
import asyncio
import tempfile
import uuid
import shutil
from typing import Dict, Any, List
from pathlib import Path

import shutil as _shutil
import subprocess

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

# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILESIZE_BYTES = 50 * 1024 * 1024  # 50 MB
SUPPORTED_PLATFORMS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "reddit.com",
    "v.redd.it",
)

# Store pending user requests
PENDING: Dict[str, Dict[str, Any]] = {}

# ffmpeg check
def check_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        return False

FFMPEG_OK = check_ffmpeg()

# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Starting Telegram Video Downloader Bot (PTB %s)", ptb_version)


# ============================================================
# Utilities
# ============================================================

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
        opts: Dict[str, Any] = {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
        }

        if download_audio:
            if not FFMPEG_OK:
                raise RuntimeError("ffmpeg is not installed")
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": "192",
                }
            ]
        else:
            # Always try merging video+audio
            if format_id == "best":
                opts["format"] = "bestvideo+bestaudio/best"
            else:
                opts["format"] = f"{format_id}+bestaudio/best"

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if download_audio:
                filename = Path(filename).with_suffix(f".{audio_format}")
            else:
                filename = Path(filename).with_suffix(".mp4")
            return Path(filename)

    return await loop.run_in_executor(None, _download)


async def delayed_cleanup(temp_dir: str, token: str, delay: int = 60):
    """Delete temp files after 1 minute"""
    await asyncio.sleep(delay)
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
        PENDING.pop(token, None)
        logger.info("Cleaned up %s", temp_dir)
    except Exception as e:
        logger.warning("Cleanup failed for %s: %s", temp_dir, e)


def build_quality_keyboard(formats: List[Dict[str, Any]], token: str) -> InlineKeyboardMarkup:
    resolutions = [1080, 720, 480, 360, 240]
    sorted_formats = sorted(
        formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True
    )

    chosen = {}
    for res in resolutions:
        for f in sorted_formats:
            if (f.get("height") or 0) >= res and f.get("ext") in ("mp4", "webm", "mkv"):
                chosen[res] = f
                break

    buttons = []
    for res, f in chosen.items():
        filesize = f.get("filesize") or f.get("filesize_approx")
        label = f"{res}p"
        if filesize:
            label += f" ({round(filesize/1024/1024,2)} MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"DL|{token}|{f['format_id']}")])

    # Add audio button
    if FFMPEG_OK:
        buttons.append([InlineKeyboardButton("Download Audio (mp3)", callback_data=f"AUDIO|{token}|mp3")])
    else:
        buttons.append([InlineKeyboardButton("Audio not available (ffmpeg missing)", callback_data=f"NA|{token}")])

    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])
    return InlineKeyboardMarkup(buttons)


# ============================================================
# Handlers
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Send me a YouTube/TikTok/Instagram/X/Reddit video link and I’ll download it.\n"
        "Limit: 50 MB per file. Audio download available if ffmpeg is installed."
    )


async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not url_is_supported(url):
        await update.message.reply_text("⚠️ Unsupported URL. Try YouTube, TikTok, Instagram, X/Twitter, or Reddit.")
        return

    await update.message.reply_text("⏳ Fetching available formats...")
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
        logger.exception("Failed to fetch video info: %s", e)
        await update.message.reply_text("❌ Failed to fetch video info.")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")

    if len(data) < 2:
        return

    action, token = data[0], data[1]
    pending = PENDING.get(token)
    if not pending:
        await query.edit_message_text("⚠️ Request expired.")
        return

    chat_id = pending["chat_id"]
    url = pending["url"]

    if action == "CANCEL":
        PENDING.pop(token, None)
        await query.edit_message_text("❌ Cancelled.")
        return

    if action == "NA":
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Audio not available (ffmpeg missing).")
        return

    # Video download
    if action == "DL" and len(data) == 3:
        format_id = data[2]
        temp_dir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            path = await ytdl_download(url, format_id, temp_dir)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ File is {round(size/1024/1024,2)} MB (>50 MB). Cannot send.")
                return

            with open(path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Size: {round(size/1024/1024,2)} MB")
        except Exception as e:
            logger.exception("Download failed: %s", e)
            await context.bot.send_message(chat_id=chat_id, text="❌ Failed to download video.")
        finally:
            asyncio.create_task(delayed_cleanup(temp_dir, token))

    # Audio download
    if action == "AUDIO" and len(data) == 3:
        audio_format = data[2]
        temp_dir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            path = await ytdl_download(url, "bestaudio", temp_dir, download_audio=True, audio_format=audio_format)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Audio file is {round(size/1024/1024,2)} MB (>50 MB). Cannot send.")
                return

            with open(path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, caption=f"Audio ({round(size/1024/1024,2)} MB)")
        except Exception as e:
            logger.exception("Audio download failed: %s", e)
            await context.bot.send_message(chat_id=chat_id, text="❌ Failed to download audio.")
        finally:
            asyncio.create_task(delayed_cleanup(temp_dir, token))


# ============================================================
# Main
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required as environment variable.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_message))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
