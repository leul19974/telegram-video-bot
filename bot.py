import ffmpeg  # add this to top imports

# ----------------- New Cleanup Utility -----------------
async def delayed_cleanup(temp_dir: str, delay: int = 120):
    """Delete all files in temp_dir after delay seconds."""
    await asyncio.sleep(delay)
    try:
        for p in Path(temp_dir).glob("*"):
            p.unlink(missing_ok=True)
        Path(temp_dir).rmdir()
        logger.info("✅ Cleaned up temporary directory %s", temp_dir)
    except Exception as e:
        logger.warning("⚠️ Cleanup failed for %s: %s", temp_dir, e)

# ----------------- Add Audio Conversion -----------------
async def convert_to_audio(input_path: Path, output_path: Path) -> Path:
    """Extract audio from video using ffmpeg and save as MP3."""
    loop = asyncio.get_running_loop()

    def _convert():
        (
            ffmpeg
            .input(str(input_path))
            .output(str(output_path), format="mp3", acodec="libmp3lame", audio_bitrate="128k")
            .overwrite_output()
            .run(quiet=True)
        )
        return output_path

    return await loop.run_in_executor(None, _convert)

# ----------------- Modify Callback Flow -----------------
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = (query.data or "")
    parts = data.split("|", 2)
    if len(parts) < 2:
        return

    action = parts[0]
    token = parts[1]
    pending = PENDING.get(token)

    if not pending:
        await query.edit_message_text("This request expired. Send the link again.")
        return

    chat_id = pending["chat_id"]
    url = pending["url"]

    # Cancel
    if action == "CANCEL":
        PENDING.pop(token, None)
        await query.edit_message_text("Cancelled.")
        return

    # Download video
    if action == "DL" and len(parts) == 3:
        format_id = parts[2]
        temp_dir = tempfile.mkdtemp(prefix="tgdl_")
        try:
            file_path = await ytdl_download(url, format_id, temp_dir)
            size = file_path.stat().st_size

            if size > MAX_FILESIZE_BYTES:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ File too large (>50MB). Try lower quality.")
                return

            # Save path for reuse
            pending["file_path"] = str(file_path)
            pending["temp_dir"] = temp_dir

            # Ask user: Video or Audio?
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Send Video", callback_data=f"SENDVIDEO|{token}")],
                [InlineKeyboardButton("🎵 Convert to Audio (MP3)", callback_data=f"SENDAUDIO|{token}")],
                [InlineKeyboardButton("Cancel", callback_data=f"CANCEL|{token}")]
            ])
            await context.bot.send_message(chat_id=chat_id, text="Choose format to send:", reply_markup=kb)

        except Exception as e:
            logger.exception("Download error: %s", e)
            await context.bot.send_message(chat_id=chat_id, text="❌ Failed to download video.")
        # don’t cleanup yet → wait until sending
        return

    # Send Video
    if action == "SENDVIDEO":
        file_path = Path(pending.get("file_path"))
        temp_dir = pending.get("temp_dir")
        if not file_path or not file_path.exists():
            await context.bot.send_message(chat_id=chat_id, text="❌ File not found.")
            return
        try:
            with open(file_path, "rb") as f:
                await context.bot.send_video(chat_id=chat_id, video=f, caption=f"Downloaded from: {url}")
        except Exception as e:
            logger.exception("Send video failed: %s", e)
            await context.bot.send_message(chat_id=chat_id, text="❌ Failed to send video.")
        finally:
            asyncio.create_task(delayed_cleanup(temp_dir))

    # Send Audio
    if action == "SENDAUDIO":
        file_path = Path(pending.get("file_path"))
        temp_dir = pending.get("temp_dir")
        if not file_path or not file_path.exists():
            await context.bot.send_message(chat_id=chat_id, text="❌ File not found.")
            return
        try:
            audio_path = Path(temp_dir) / "audio.mp3"
            await convert_to_audio(file_path, audio_path)
            with open(audio_path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, caption=f"Audio extracted from: {url}")
        except Exception as e:
            logger.exception("Send audio failed: %s", e)
            await context.bot.send_message(chat_id=chat_id, text="❌ Failed to convert to audio.")
        finally:
            asyncio.create_task(delayed_cleanup(temp_dir))
