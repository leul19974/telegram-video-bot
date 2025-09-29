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
SUPPORTED_PLATFORMS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "x.com", "twitter.com", "reddit.com", "v.redd.it")
# In-memory mapping for pending downloads: token -> data
PENDING: Dict[str, Dict[str, Any]] = {}
# -----------------------------------

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Starting Telegram Video Downloader Bot (PTB %s)", ptb_version)


# Utility: check if URL belongs to supported platforms
def url_is_supported(url: str) -> bool:
    lower = url.lower()
    return any(domain in lower for domain in SUPPORTED_PLATFORMS)


# Utility: call yt-dlp to extract info (non-blocking wrapper)
async def ytdl_extract_info(url: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    def _extract():
        ydl_opts = {"quiet": True, "no_warnings": True}
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await loop.run_in_executor(None, _extract)


# Utility: call yt-dlp to download a selected format into outdir (non-blocking)
async def ytdl_download(url: str, format_id: str, outdir: str) -> Path:
    loop = asyncio.get_running_loop()

    def _download():
        # outtmpl
        outtmpl = os.path.join(outdir, "%(title).200s.%(ext)s")
        ydl_opts = {
            "format": format_id,
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            # Avoid console progress to keep logs tidy:
            "progress_hooks": [],
            # Prefer mp4 if possible
            "merge_output_format": "mp4",
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Find the actual filename used (ydl returns 'requested_formats' maybe)
            # For downloaded single-file cases, info.get('_filename') or ydl.prepare_filename(info)
            try:
                filename = ydl.prepare_filename(info)
            except Exception:
                # Best effort: try common fields
                filename = None
            if filename and os.path.exists(filename):
                return Path(filename)
            # fallback: try to locate latest file in outdir
            files = list(Path(outdir).glob("*"))
            if files:
                # choose largest recent file
                files_sorted = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
                return files_sorted[0]
            raise FileNotFoundError("Download completed but file not found.")
    return await loop.run_in_executor(None, _download)


# Format helpers
def build_quality_keyboard(formats: List[Dict[str, Any]], token: str) -> InlineKeyboardMarkup:
    """
    Build InlineKeyboardMarkup with quality options.
    We map common heights (1080,720,480,360,240) to best format available for that target.
    """
    # Map height -> best format id for that height (prefer mp4/mkv where possible)
    resolutions = [1080, 720, 480, 360, 240]
    chosen = {}
    # Sort formats by resolution descending so we pick best for each height
    sorted_formats = sorted(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)

    for res in resolutions:
        for f in sorted_formats:
            h = f.get("height")
            ext = f.get("ext", "")
            # Accept formats with approximate height >= target or equal
            if h and h >= res and ext in ("mp4", "m4a", "webm", "mkv"):
                chosen[res] = f
                break
    # Also include audio+video "best" if nothing matched
    if not chosen and sorted_formats:
        chosen[sorted_formats[0].get("height", 0)] = sorted_formats[0]

    # Build keyboard rows
    buttons = []
    for res in resolutions:
        f = chosen.get(res)
        if not f:
            continue
        # Show filesize if yt-dlp provided it (in bytes)
        filesize = f.get("filesize") or f.get("filesize_approx")
        label = f"{res}p"
        if filesize:
            mb = round(filesize / (1024 * 1024), 2)
            label += f" ({mb} MB)"
        # callback data: token|format_id
        callback_data = f"DL|{token}|{f.get('format_id')}"
        buttons.append([InlineKeyboardButton(label, callback_data=callback_data)])

    # Add a "Cancel" button
    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")])

    return InlineKeyboardMarkup(buttons)


# Command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a YouTube / TikTok / Instagram / X (Twitter) / Reddit video link and I'll let you choose quality and download it (<= 50 MB).\n\nCommands:\n/start - this message\n/help - usage and tips"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Usage:\n1. Send a video URL (YouTube, TikTok, Instagram, X/Twitter, Reddit).\n2. I'll show available quality options.\n3. Choose a quality. I'll download and send the video if it's <= 50 MB.\n\nNotes:\n• If a selected quality file exceeds 50MB I'll cancel and tell you.\n• BOT_TOKEN must be set in environment variable BOT_TOKEN (do not hard-code it).\n• This bot runs best for short videos < 50 MB. For larger videos you can upgrade hosting or add re-encoding."
    )


# Message handler: expect URLs
async def url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = (message.text or "").strip()
    if not text:
        await message.reply_text("Please send a link to a video.")
        return

    # quick check for URL
    if not (text.startswith("http://") or text.startswith("https://")):
        await message.reply_text("Please send a full URL (starting with http:// or https://).")
        return

    if not url_is_supported(text):
        await message.reply_text("Unsupported platform or URL. Supported: YouTube, TikTok, Instagram, X/Twitter, Reddit.")
        return

    # Indicate we are processing
    await message.reply_text("Fetching available formats... please wait a moment.")

    try:
        info = await ytdl_extract_info(text)
    except Exception as e:
        logger.exception("Failed to extract info for URL %s: %s", text, e)
        await message.reply_text("Failed to fetch video info. The link may be invalid or the platform may block access. Try another link.")
        return

    # Extract formats list (prefer combined audio+video)
    formats = info.get("formats") or []
    if not formats:
        await message.reply_text("No downloadable formats were found for this video.")
        return

    # Filter to video-containing formats (has 'vcodec' not 'none' or has height)
    video_formats = []
    for f in formats:
        # prefer combined formats or best muxed format
        if f.get("vcodec") and f.get("vcodec") != "none":
            video_formats.append(f)

    if not video_formats:
        await message.reply_text("No video formats available for this link.")
        return

    # Create unique token and save pending info
    token = str(uuid.uuid4())
    PENDING[token] = {
        "url": text,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "info": info,
        "formats": video_formats,
        "from_user": message.from_user.id,
    }

    kb = build_quality_keyboard(video_formats, token)
    await message.reply_text("Choose the quality to download:", reply_markup=kb)


# CallbackQuery handler for quality selection and cancel
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # acknowledge to remove "loading"

    data = (query.data or "")
    if not data:
        return

    parts = data.split("|", 2)
    if len(parts) < 2:
        await query.edit_message_text("Invalid selection.")
        return

    action = parts[0]
    token = parts[1]

    pending = PENDING.get(token)
    if not pending:
        await query.edit_message_text("This request expired or is no longer available. Please send the link again.")
        return

    # Cancel flow
    if action == "CANCEL":
        PENDING.pop(token, None)
        await query.edit_message_text("Cancelled.")
        return

    # Download flow: data is "DL|token|format_id"
    if action != "DL" or len(parts) < 3:
        await query.edit_message_text("Invalid selection format.")
        return

    format_id = parts[2]
    url = pending["url"]
    chat_id = pending["chat_id"]

    # Inform the user we are starting download
    await query.edit_message_text("Downloading... I will upload if the file is <= 50 MB. This may take a bit.")

    # Create a temporary directory to download file(s)
    temp_dir = tempfile.mkdtemp(prefix="tgdl_")
    downloaded_path: Optional[Path] = None
    try:
        # Download using yt-dlp to temp_dir
        downloaded_path = await ytdl_download(url, format_id, temp_dir)

        if not downloaded_path or not downloaded_path.exists():
            raise FileNotFoundError("Downloaded file not found.")

        size = downloaded_path.stat().st_size
        logger.info("Downloaded file %s size=%d bytes", downloaded_path, size)

        if size > MAX_FILESIZE_BYTES:
            # Remove file and notify
            downloaded_path.unlink(missing_ok=True)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "The selected quality produced a file larger than 50 MB. "
                    "I won't send it to avoid exceeding Telegram size limits. "
                    "Try choosing a lower quality or use a different host with larger upload limits."
                ),
            )
            return

        # Send video (as video message)
        caption = f"Downloaded from: {url}\nSize: {round(size / (1024*1024), 2)} MB"
        # Use reply if callback query message exists
        try:
            # Opening file in binary mode
            with open(downloaded_path, "rb") as vf:
                await context.bot.send_video(chat_id=chat_id, video=vf, caption=caption, supports_streaming=True)
        except Exception as send_err:
            logger.exception("Failed to send video: %s", send_err)
            await context.bot.send_message(chat_id=chat_id, text="Failed to send video due to an error. See logs.")
            return

    except Exception as e:
        logger.exception("Error during download/send flow: %s", e)
        # Friendly error messages for known yt-dlp errors
        if isinstance(e, ytdlp_utils.DownloadError):
            msg = "Download failed (yt-dlp error). The video may be private, restricted, or blocked."
        else:
            msg = "An error occurred while processing the request."
        await context.bot.send_message(chat_id=chat_id, text=msg)
    finally:
        # Clean up temp files and pending state
        try:
            if downloaded_path and downloaded_path.exists():
                downloaded_path.unlink(missing_ok=True)
            # Remove any other files in temp_dir
            for p in Path(temp_dir).glob("*"):
                try:
                    p.unlink()
                except Exception:
                    pass
            Path(temp_dir).rmdir()
        except Exception:
            # ignore cleanup errors but log them
            logger.exception("Cleanup error for temp dir %s", temp_dir)
        PENDING.pop(token, None)


# Error handler (for unhandled exceptions in handlers)
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling an update: %s", context.error)
    # Attempt to notify the user if possible
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="An unexpected error occurred. The incident was logged.")
    except Exception:
        logger.exception("Failed to notify user about error.")


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set. Exiting.")
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler(["start"], start_command))
    application.add_handler(CommandHandler(["help"], help_command))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), url_message))
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    application.add_error_handler(error_handler)

    # Start polling (Railway worker will run this process)
    logger.info("Bot starting polling. Press Ctrl+C to stop.")
    application.run_polling()


if __name__ == "__main__":
    main()
