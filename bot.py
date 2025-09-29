import os
import logging
import time
import json
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get Telegram token from environment
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.error("Telegram token not set! Set TELEGRAM_BOT_TOKEN environment variable.")
    exit(1)

# Max file size 1GB
MAX_FILE_SIZE = 1_000_000_000

# Cache
video_cache = {}

# Supported platforms
SUPPORTED_PLATFORMS = ["youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com", "reddit.com"]

# yt-dlp options
YDL_OPTIONS = {"format": "best", "quiet": True, "no_warnings": True, "noplaylist": True}

def is_supported_url(url: str) -> bool:
    return any(p in url.lower() for p in SUPPORTED_PLATFORMS)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! Send me a video link and I will prepare download options.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Help:\nSend a link from YouTube, TikTok, Instagram, Twitter/X, or Reddit.\n"
        "You'll get a preview and download options."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not is_supported_url(url):
        await update.message.reply_text("❌ URL not supported. Send YouTube, TikTok, Instagram, Twitter/X, or Reddit links.")
        return

    # Check cache
    cache_key = hash(url)
    if cache_key in video_cache and time.time() - video_cache[cache_key]["timestamp"] < 600:
        video_data = video_cache[cache_key]
        logger.info("Using cached video data.")
    else:
        msg = await update.message.reply_text("⏳ Processing your link...")
        try:
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.error(f"yt-dlp error: {e}")
            await msg.edit_text("❌ Failed to fetch video info.")
            return

        video_data = {
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader", "Unknown"),
            "url": url,
            "formats": info.get("formats", []),
            "timestamp": time.time()
        }
        video_cache[cache_key] = video_data
        await msg.delete()

    # Send preview
    await send_preview(update, context, video_data)

async def send_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, video_data):
    chat_id = update.effective_chat.id
    keyboard = []

    # Add first 3 video formats
    for fmt in video_data["formats"][:3]:
        keyboard.append([InlineKeyboardButton(
            f"🎬 {fmt.get('format_id')} ({fmt.get('ext')})",
            callback_data=json.dumps({"type": "video", "format_id": fmt.get("format_id"), "url": video_data["url"]})
        )])

    # Add cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=json.dumps({"type": "cancel"}))])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📹 *{video_data['title']}*\n👤 {video_data['uploader']}\nSelect an option:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        data = json.loads(query.data)
        if data["type"] == "cancel":
            await query.edit_message_text("❌ Download cancelled.")
            return

        # Download media
        await query.edit_message_text("⏳ Downloading...")
        ydl_opts = {"format": data["format_id"], "outtmpl": "temp/%(title)s.%(ext)s"}
        os.makedirs("temp", exist_ok=True)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(data["url"], download=True)
                filename = ydl.prepare_filename(info)
            await context.bot.send_document(chat_id=query.message.chat.id, document=open(filename, "rb"))
        except Exception as e:
            logger.error(f"Download failed: {e}")
            await query.edit_message_text("❌ Download failed.")
        finally:
            if os.path.exists(filename):
                os.remove(filename)
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await query.edit_message_text("❌ Error occurred.")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_url))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started!")
    app.run_polling()

if __name__ == "__main__":
    main()
