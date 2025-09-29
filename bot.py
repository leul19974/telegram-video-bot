#!/usr/bin/env python3
"""
Telegram Video Downloader Bot with Compression
- Uses python-telegram-bot (v20.x) and yt-dlp
- Presents quality options, enforces 50 MB upload limit
- Offers compression if file is too big
- Deletes local files after 2 minutes
"""

import os
import logging
import asyncio
import tempfile
import uuid
from typing import Dict, Any, Optional, List
from pathlib import Path

import ffmpeg
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
SUPPORTED_PLATFORMS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "x.com", "twitter.com", "reddit.com", "v.redd.it")
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


async def compress_video(input_path: Path, output_path: Path, target_res: str = "720p") -> Path:
    """Compress video with ffmpeg to a target resolution."""
    res_map = {
        "720p": "1280x720",
        "480p": "854x480",
        "360p": "640x360",
    }
    scale = res_map.get(target_res, "854x480")

    loop = asyncio.get_running_loop()

    def _compress():
        (
            ffmpeg
            .input(str(input_path))
            .output(str(output_path), vf=f"scale={scale}", vcodec="libx264", crf=28, preset="veryfast")
            .overwrite_output()
            .run(quiet=True)
        )
        return output_path

    return await loop.run_in_executor(None, _compress)


async def delayed_cleanup(temp_dir: str, token: str, delay: int = 120):
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

    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])
    return InlineKeyboardMarkup(buttons)


# ---------- Handlers ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! Send me a YouTube/TikTok/Instagram/X/Reddit video link and I’ll download it (≤50 MB).")


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

    if action == "DL" and len(data) == 3:
        format_id = data[2]
        temp_dir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            path = await ytdl_download(url, format_id, temp_dir)
            size = path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Compress to 720p", callback_data=f"COMPRESS|{token}|720p")],
                    [InlineKeyboardButton("Compress to 480p", callback_data=f"COMPRESS|{token}|480p")],
                    [InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")]
                ])
                await context.bot.send_message(chat_id=chat_id, text=f"File is {round(size/1024/1024,2)} MB (>50 MB). Compress?", reply_markup=kb)
                return

            with open(path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Size: {round(size/1024/1024,2)} MB")
        finally:
            asyncio.create_task(delayed_cleanup(temp_dir, token))

    if action == "COMPRESS" and len(data) == 3:
        resolution = data[2]
        temp_dir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            input_path = await ytdl_download(url, "best", temp_dir)
            output_path = Path(temp_dir) / f"compressed_{resolution}.mp4"
            await compress_video(input_path, output_path, resolution)
            size = output_path.stat().st_size
            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text="Still too large even after compression.")
                return
            with open(output_path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Compressed to {resolution}, size {round(size/1024/1024,2)} MB")
        finally:
            asyncio.create_task(delayed_cleanup(temp_dir, token))


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
