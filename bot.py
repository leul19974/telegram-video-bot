import os
import logging
import asyncio
from pathlib import Path
from uuid import uuid4

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp
import ffmpeg

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# --- Start / Help ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a video link (YouTube, TikTok, Instagram, Twitter/X, Reddit).\n"
        "I’ll let you choose quality, compress if needed, and send the video back.\n\n"
        "⚠️ Max size: 50 MB (Telegram Bot API limit)."
    )

# --- Get available qualities ---
def get_formats(url: str):
    ydl_opts = {"quiet": True, "no_warnings": True, "listformats": True}
    formats = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            for f in info.get("formats", []):
                if f.get("vcodec") != "none" and f.get("acodec") != "none":
                    if f.get("filesize") and f["filesize"] <= 200 * 1024 * 1024:  # <200MB
                        label = f"{f['format_note']} - {round(f['filesize']/1024/1024, 1)} MB"
                        formats.append((label, f["format_id"]))
    except Exception as e:
        logger.error(f"Format error: {e}")
    return formats

# --- Download video ---
async def download_video(url: str, format_id: str, output_path: Path):
    loop = asyncio.get_running_loop()
    def _download():
        ydl_opts = {
            "format": format_id,
            "outtmpl": str(output_path),
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return output_path
    return await loop.run_in_executor(None, _download)

# --- Safer compression ---
async def compress_video(input_path: Path, output_path: Path, target_res: str = "720p") -> Path:
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
            .output(
                str(output_path),
                vf=f"scale={scale}",
                vcodec="libx264",
                crf=32,              # higher = smaller file
                preset="ultrafast",  # faster compression, less CPU
                acodec="copy"        # don’t re-encode audio
            )
            .overwrite_output()
            .run(quiet=True)
        )
        return output_path

    return await loop.run_in_executor(None, _compress)

# --- Handle messages ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    formats = get_formats(url)
    if not formats:
        await update.message.reply_text("❌ No downloadable formats found or site not supported.")
        return

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"{url}|{fmt_id}")]
        for label, fmt_id in formats[:10]  # limit to first 10 options
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🎞 Select quality:", reply_markup=reply_markup)

# --- Handle quality selection ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    url, format_id = query.data.split("|")
    file_id = str(uuid4())
    raw_path = DOWNLOAD_DIR / f"{file_id}.mp4"
    comp_path = DOWNLOAD_DIR / f"{file_id}_c.mp4"

    try:
        await query.edit_message_text("⬇️ Downloading...")
        await download_video(url, format_id, raw_path)

        size_mb = raw_path.stat().st_size / 1024 / 1024
        if size_mb <= 50:
            await query.edit_message_text("✅ Sending video...")
            await query.message.reply_video(video=open(raw_path, "rb"))
        else:
            await query.edit_message_text(
                f"⚠️ File is {round(size_mb,1)} MB (limit 50 MB).\n"
                "Do you want me to compress it to 720p or 480p?"
            )
            keyboard = [
                [InlineKeyboardButton("Compress 720p", callback_data=f"compress|{raw_path}|720p")],
                [InlineKeyboardButton("Compress 480p", callback_data=f"compress|{raw_path}|480p")],
            ]
            await query.message.reply_text("Choose compression:", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"Download error: {e}")
        await query.edit_message_text("❌ Error while downloading video.")

# --- Handle compression choice ---
async def compress_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, path_str, res = query.data.split("|")
    raw_path = Path(path_str)
    comp_path = raw_path.with_name(raw_path.stem + f"_{res}.mp4")

    try:
        await query.edit_message_text(f"🗜 Compressing to {res}...")
        await compress_video(raw_path, comp_path, res)

        size_mb = comp_path.stat().st_size / 1024 / 1024
        if size_mb <= 50:
            await query.edit_message_text("✅ Sending compressed video...")
            await query.message.reply_video(video=open(comp_path, "rb"))
        else:
            await query.edit_message_text(f"❌ Still too big ({round(size_mb,1)} MB). Try lower quality.")

    except Exception as e:
        logger.error(f"Compression error: {e}")
        await query.edit_message_text("❌ Error while compressing video.")

    finally:
        # Clean up after 2 minutes
        async def cleanup():
            await asyncio.sleep(120)
            for f in [raw_path, comp_path]:
                if f.exists():
                    f.unlink()
        asyncio.create_task(cleanup())

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^http"))
    app.add_handler(CallbackQueryHandler(compress_handler, pattern="^compress"))
    app.run_polling()

if __name__ == "__main__":
    main()
