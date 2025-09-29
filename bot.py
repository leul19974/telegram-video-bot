import os
import logging
import time
import json
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.environ.get('7979384382:AAFBilcp8cVHhXOm4OO_QVH4NKzQhlm8dt8')  # Make sure this is set!
MAX_FILE_SIZE = 1000 * 1024 * 1024  # 1GB
CACHE_DURATION = 10 * 60  # 10 minutes
FILE_DELETE_DELAY = 60  # 1 minute

video_cache = {}

SUPPORTED_PLATFORMS = [
    'youtube.com', 'youtu.be',
    'instagram.com', 'instagr.am',
    'tiktok.com',
    'twitter.com', 'x.com',
    'reddit.com',
]

YDL_OPTIONS = {
    'format': 'best',
    'outtmpl': '%(title)s.%(ext)s',
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'socket_timeout': 30,
}

def is_supported_url(url: str) -> bool:
    return any(p in url.lower() for p in SUPPORTED_PLATFORMS)

def get_video_info(url: str):
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            video_formats = []
            audio_formats = []

            for fmt in info.get('formats', []):
                try:
                    file_size = fmt.get('filesize') or fmt.get('filesize_approx', 0)
                    if file_size > MAX_FILE_SIZE:
                        continue
                    if fmt.get('vcodec') != 'none' and fmt.get('acodec') != 'none':
                        video_formats.append({
                            'format_id': fmt.get('format_id'),
                            'height': fmt.get('height', 0),
                            'ext': fmt.get('ext', 'mp4'),
                            'filesize': file_size
                        })
                    elif fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':
                        audio_formats.append({
                            'format_id': fmt.get('format_id'),
                            'abr': fmt.get('abr', 0),
                            'ext': fmt.get('ext', 'mp3'),
                            'filesize': file_size
                        })
                except Exception as e:
                    logger.warning(f"Error processing format: {e}")

            video_formats.sort(key=lambda x: x.get('height', 0), reverse=True)
            audio_formats.sort(key=lambda x: x.get('abr', 0), reverse=True)

            return {
                'title': info.get('title', 'Unknown Title'),
                'uploader': info.get('uploader', 'Unknown Uploader'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', None),
                'url': url,
                'video_formats': video_formats,
                'audio_formats': audio_formats,
                'timestamp': time.time()
            }
    except Exception as e:
        logger.error(f"Error fetching video info: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! Send me a video link (YouTube, TikTok, Instagram, Twitter/X, Reddit) and I'll prepare download options."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Send a supported video link and I'll show download options (video/audio)."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not is_supported_url(url):
        await update.message.reply_text("❌ Unsupported URL. Send a link from YouTube, TikTok, Instagram, Twitter/X, or Reddit.")
        return

    cache_key = hash(url)
    if cache_key in video_cache and time.time() - video_cache[cache_key]['timestamp'] < CACHE_DURATION:
        video_data = video_cache[cache_key]
    else:
        msg = await update.message.reply_text("⏳ Processing your link...")
        video_data = get_video_info(url)
        if not video_data:
            await msg.edit_text("❌ Failed to fetch video information.")
            return
        video_cache[cache_key] = video_data
        await msg.delete()

    await send_preview(update, context, video_data)

async def send_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, video_data: dict):
    duration = video_data.get('duration', 0)
    if duration:
        hours, remainder = divmod(duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    else:
        duration_str = "Unknown"

    caption = (
        f"📹 *{video_data.get('title')}*\n"
        f"👤 *Uploader:* {video_data.get('uploader')}\n"
        f"⏱️ *Duration:* {duration_str}\n\n"
        "Select download option:"
    )

    keyboard = []
    for fmt in video_data.get('video_formats', [])[:5]:
        size_str = f"{fmt['filesize']/(1024*1024):.1f}MB" if fmt['filesize'] else "Unknown"
        keyboard.append([InlineKeyboardButton(
            f"🎬 Video {fmt['height']}p ({size_str})",
            callback_data=json.dumps({'type': 'video', 'url': video_data['url'], 'format_id': fmt['format_id'], 'ext': fmt['ext']})
        )])

    if video_data.get('audio_formats'):
        best_audio = video_data['audio_formats'][0]
        size_str = f"{best_audio['filesize']/(1024*1024):.1f}MB" if best_audio['filesize'] else "Unknown"
        keyboard.append([InlineKeyboardButton(
            f"🎵 Audio (MP3) ({size_str})",
            callback_data=json.dumps({'type': 'audio', 'url': video_data['url'], 'format_id': best_audio['format_id'], 'ext': 'mp3'})
        )])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=json.dumps({'type': 'cancel'}))])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if video_data.get('thumbnail'):
        await context.bot.send_photo(chat_id=update.effective_chat.id,
                                     photo=video_data['thumbnail'],
                                     caption=caption,
                                     parse_mode='Markdown',
                                     reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text=caption,
                                       parse_mode='Markdown',
                                       reply_markup=reply_markup)

# Add handle_callback_query and download_media functions similarly with async support

def main():
    if not TOKEN:
        logger.error("Telegram token not set! Set TELEGRAM_BOT_TOKEN environment variable.")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_callback_query))  # implement async

    logger.info("✅ Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()

