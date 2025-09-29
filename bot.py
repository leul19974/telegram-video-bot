import os
import logging
import time
import json
import yt_dlp
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, CallbackContext
from telegram.error import TelegramError

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
MAX_FILE_SIZE = 1000 * 1024 * 1024  # 1GB in bytes
CACHE_DURATION = 10 * 60  # 10 minutes in seconds
FILE_DELETE_DELAY = 60  # 1 minute in seconds

# Cache dictionary to store video info
video_cache = {}

# Supported platforms
SUPPORTED_PLATFORMS = [
    'youtube.com', 'youtu.be',  # YouTube
    'instagram.com', 'instagr.am',  # Instagram
    'tiktok.com',  # TikTok
    'twitter.com', 'x.com',  # Twitter/X
    'reddit.com',  # Reddit
]

# yt-dlp options
YDL_OPTIONS = {
    'format': 'best',
    'outtmpl': '%(title)s.%(ext)s',
    'quiet': True,
    'no_warnings': True,
    'noplaylist': True,
}

def is_supported_url(url: str) -> bool:
    """Check if the URL is from a supported platform."""
    return any(platform in url.lower() for platform in SUPPORTED_PLATFORMS)

def get_video_info(url: str) -> dict:
    """Fetch video information using yt-dlp."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Extract necessary information
            video_data = {
                'title': info.get('title', 'Unknown Title'),
                'uploader': info.get('uploader', 'Unknown Uploader'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', None),
                'formats': info.get('formats', []),
                'url': url,
                'timestamp': time.time()
            }
            
            # Process formats to get available qualities
            video_formats = []
            audio_formats = []
            
            for fmt in info.get('formats', []):
                if fmt.get('vcodec') != 'none' and fmt.get('acodec') != 'none':  # Video with audio
                    if fmt.get('filesize') and fmt.get('filesize') <= MAX_FILE_SIZE:
                        video_formats.append({
                            'format_id': fmt.get('format_id'),
                            'format_note': fmt.get('format_note', ''),
                            'height': fmt.get('height', 0),
                            'width': fmt.get('width', 0),
                            'filesize': fmt.get('filesize', 0),
                            'ext': fmt.get('ext', 'mp4')
                        })
                elif fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':  # Audio only
                    if fmt.get('filesize') and fmt.get('filesize') <= MAX_FILE_SIZE:
                        audio_formats.append({
                            'format_id': fmt.get('format_id'),
                            'abr': fmt.get('abr', 0),
                            'filesize': fmt.get('filesize', 0),
                            'ext': fmt.get('ext', 'mp3')
                        })
            
            # Remove duplicates and sort by quality
            video_formats = list({v['format_id']: v for v in video_formats}.values())
            video_formats.sort(key=lambda x: x.get('height', 0), reverse=True)
            
            audio_formats = list({a['format_id']: a for a in audio_formats}.values())
            audio_formats.sort(key=lambda x: x.get('abr', 0), reverse=True)
            
            video_data['video_formats'] = video_formats
            video_data['audio_formats'] = audio_formats
            
            return video_data
    except Exception as e:
        logger.error(f"Error fetching video info: {e}")
        return None

def start(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    update.message.reply_text(
        "👋 Hello! I'm a video downloader bot.\n\n"
        "Send me a link from YouTube, Instagram, TikTok, Twitter/X, or Reddit, "
        "and I'll help you download it.\n\n"
        "Use /help for more information."
    )

def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /help is issued."""
    update.message.reply_text(
        "📖 *Help*\n\n"
        "Simply send me a link from one of the supported platforms:\n"
        "• YouTube\n"
        "• Instagram\n"
        "• TikTok\n"
        "• Twitter/X\n"
        "• Reddit\n\n"
        "I'll show you a preview of the video and available download options.\n\n"
        "You can choose different video qualities or download as audio (MP3).\n\n"
        "⚠️ *Note:* Maximum file size is 1GB. Downloaded files are deleted after 1 minute.",
        parse_mode='Markdown'
    )

def handle_url(update: Update, context: CallbackContext) -> None:
    """Handle URL messages from users."""
    url = update.message.text
    
    if not is_supported_url(url):
        update.message.reply_text(
            "❌ This URL is not supported or invalid.\n\n"
            "Please send a link from YouTube, Instagram, TikTok, Twitter/X, or Reddit."
        )
        return
    
    # Check cache first
    cache_key = hash(url)
    if cache_key in video_cache and time.time() - video_cache[cache_key]['timestamp'] < CACHE_DURATION:
        video_data = video_cache[cache_key]
        logger.info(f"Using cached data for URL: {url}")
    else:
        # Send "processing" message
        processing_msg = update.message.reply_text("⏳ Processing your link...")
        
        # Fetch video info
        video_data = get_video_info(url)
        
        if not video_data:
            processing_msg.edit_text("❌ Failed to fetch video information. Please try again later.")
            return
        
        # Cache the video data
        video_cache[cache_key] = video_data
        
        # Edit the processing message
        try:
            processing_msg.delete()
        except TelegramError:
            pass
    
    # Send preview message
    send_preview(update, context, video_data)

def send_preview(update: Update, context: CallbackContext, video_data: dict) -> None:
    """Send a preview message with video details and download options."""
    chat_id = update.effective_chat.id
    
    # Format duration
    duration = video_data.get('duration', 0)
    if duration:
        hours, remainder = divmod(duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    else:
        duration_str = "Unknown"
    
    # Create caption
    caption = (
        f"📹 *{video_data.get('title', 'Unknown Title')}*\n\n"
        f"👤 *Uploader:* {video_data.get('uploader', 'Unknown Uploader')}\n"
        f"⏱️ *Duration:* {duration_str}\n\n"
        "Please select a download option:"
    )
    
    # Create keyboard with download options
    keyboard = []
    
    # Add video quality options
    if video_data.get('video_formats'):
        video_formats = video_data['video_formats'][:5]  # Limit to 5 options
        for fmt in video_formats:
            quality = f"{fmt.get('height', 0)}p"
            file_size = fmt.get('filesize', 0)
            size_str = f"{file_size / (1024 * 1024):.1f}MB" if file_size else "Unknown size"
            keyboard.append([
                InlineKeyboardButton(
                    f"🎬 Video: {quality} ({size_str})",
                    callback_data=json.dumps({
                        'type': 'video',
                        'url': video_data['url'],
                        'format_id': fmt['format_id'],
                        'ext': fmt['ext']
                    })
                )
            ])
    
    # Add audio option
    if video_data.get('audio_formats'):
        audio_formats = video_data['audio_formats']
        if audio_formats:
            best_audio = audio_formats[0]
            file_size = best_audio.get('filesize', 0)
            size_str = f"{file_size / (1024 * 1024):.1f}MB" if file_size else "Unknown size"
            keyboard.append([
                InlineKeyboardButton(
                    f"🎵 Audio (MP3) ({size_str})",
                    callback_data=json.dumps({
                        'type': 'audio',
                        'url': video_data['url'],
                        'format_id': best_audio['format_id'],
                        'ext': 'mp3'
                    })
                )
            ])
    
    # Add cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=json.dumps({'type': 'cancel'}))])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send preview with thumbnail if available
    thumbnail_url = video_data.get('thumbnail')
    if thumbnail_url:
        try:
            context.bot.send_photo(
                chat_id=chat_id,
                photo=thumbnail_url,
                caption=caption,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except TelegramError:
            # If sending thumbnail fails, send text only
            context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    else:
        context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

def handle_callback_query(update: Update, context: CallbackContext) -> None:
    """Handle callback queries from inline keyboards."""
    query = update.callback_query
    query.answer()
    
    try:
        data = json.loads(query.data)
        action_type = data.get('type')
        
        if action_type == 'cancel':
            query.edit_message_text("❌ Download cancelled.")
            return
        
        # Send "processing" message
        processing_msg = query.edit_message_text("⏳ Preparing your download...")
        
        # Download the video/audio
        file_path = download_media(data)
        
        if not file_path:
            processing_msg.edit_text("❌ Failed to download the media. Please try again.")
            return
        
        # Send the file
        try:
            if action_type == 'video':
                with open(file_path, 'rb') as video_file:
                    context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=video_file,
                        caption="Here's your video! 🎬"
                    )
            elif action_type == 'audio':
                with open(file_path, 'rb') as audio_file:
                    context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=audio_file,
                        caption="Here's your audio! 🎵"
                    )
            
            # Edit the processing message
            processing_msg.edit_text("✅ Download completed!")
            
            # Schedule file deletion
            context.job_queue.run_once(
                delete_file,
                FILE_DELETE_DELAY,
                context={'file_path': file_path},
                name=f"delete_{file_path.replace('/', '_')}"
            )
            
        except TelegramError as e:
            logger.error(f"Error sending file: {e}")
            processing_msg.edit_text("❌ Failed to send the file. It might be too large for Telegram.")
            
            # Delete the file immediately if sending failed
            try:
                os.remove(file_path)
            except OSError:
                pass
                
    except Exception as e:
        logger.error(f"Error handling callback query: {e}")
        query.edit_message_text("❌ An error occurred. Please try again.")

def download_media(data: dict) -> str:
    """Download media using yt-dlp."""
    url = data.get('url')
    format_id = data.get('format_id')
    ext = data.get('ext')
    
    if not all([url, format_id, ext]):
        return None
    
    # Create a temporary directory if it doesn't exist
    temp_dir = 'temp_downloads'
    os.makedirs(temp_dir, exist_ok=True)
    
    # Prepare yt-dlp options
    ydl_options = {
        'format': format_id,
        'outtmpl': f'{temp_dir}/%(title)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'postprocessors': []  # We'll add postprocessors if needed
    }
    
    # Add postprocessor for audio conversion to MP3
    if data.get('type') == 'audio':
        ydl_options['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    
    try:
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # If we converted to MP3, adjust the filename
            if data.get('type') == 'audio':
                filename = os.path.splitext(filename)[0] + '.mp3'
            
            return filename
    except Exception as e:
        logger.error(f"Error downloading media: {e}")
        return None

def delete_file(context: CallbackContext) -> None:
    """Delete a file from the filesystem."""
    file_path = context.job.context.get('file_path')
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(f"Deleted file: {file_path}")
        except OSError as e:
            logger.error(f"Error deleting file {file_path}: {e}")

def error_handler(update: Update, context: CallbackContext) -> None:
    """Log errors caused by updates."""
    logger.error(f"Update {update} caused error {context.error}")

def main() -> None:
    """Start the bot."""
    # Create the Updater and pass it your bot's token.
    updater = Updater(TOKEN)
    
    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher
    
    # Register command handlers
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("help", help_command))
    
    # Register message handler for URLs
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_url))
    
    # Register callback query handler
    dispatcher.add_handler(CallbackQueryHandler(handle_callback_query))
    
    # Register error handler
    dispatcher.add_error_handler(error_handler)
    
    # Start the Bot
    updater.start_polling()
    logger.info("Bot started successfully!")
    
    # Run the bot until you press Ctrl-C
    updater.idle()

if __name__ == '__main__':
    main()
