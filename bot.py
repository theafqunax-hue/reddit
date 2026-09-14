import asyncio
import logging
import os
import re
from html import escape

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

import config
import database

from reddit import fetch_posts
from downloader import download


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# BOT
# ============================================================

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# QUEUE
# ============================================================

video_queue = asyncio.Queue()

# Prevent the same Reddit post from being queued twice
queued_posts = set()

# Currently processing item
current_job = None


def queue_key(post):
    """
    Creates a unique key for a queued post.
    """
    post_id = post.get("id")

    if post_id and post_id != "manual":
        return f"post:{post_id}"

    url = post.get("permalink") or post.get("url") or ""

    return f"url:{url}"


# ============================================================
# ADMIN
# ============================================================

def is_admin(message: Message):
    return (
        message.from_user
        and message.from_user.id == config.ADMIN_ID
    )


# ============================================================
# SUBREDDIT CLEANER
# ============================================================

def clean_subreddit(value):
    value = value.strip()

    if value.lower().startswith("r/"):
        value = value[2:]

    value = value.strip("/")

    if not re.fullmatch(
        r"[A-Za-z0-9_]+",
        value
    ):
        return None

    return value


# ============================================================
# DESTINATION
# ============================================================

def get_destination():
    value = database.get_setting(
        "destination_chat_id"
    )

    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return value


# ============================================================
# FILE HELPERS
# ============================================================

def file_size_mb(filepath):
    if not filepath or not os.path.exists(filepath):
        return 0

    return os.path.getsize(filepath) / 1024 / 1024


def is_file_too_large_error(error):
    """
    Detect Telegram's 'Request Entity Too Large'
    and similar file-size errors.
    """

    text = str(error).lower()

    return (
        "request entity too large" in text
        or "entity too large" in text
        or "file is too big" in text
        or "file too large" in text
        or "413" in text
    )


# ============================================================
# FFmpeg COMPRESSION
# ============================================================

async def compress_video(input_file):
    """
    Compress video to approximately <= 45 MB.

    Uses FFmpeg.

    Returns:
        compressed filepath
    """

    if not input_file or not os.path.exists(input_file):
        raise RuntimeError(
            "Cannot compress: source file does not exist."
        )

    base, _ = os.path.splitext(input_file)

    output_file = f"{base}_compressed.mp4"

    # Remove old compressed file if it exists
    if os.path.exists(output_file):
        try:
            os.remove(output_file)
        except Exception:
            pass

    logging.info(
        "Compressing oversized video: %.2f MB",
        file_size_mb(input_file)
    )

    # First compression attempt
    #
    # CRF 28 gives a decent balance between size and quality.
    #
    # 720p max keeps huge videos manageable.
    command = [
        "ffmpeg",
        "-y",
        "-i",
        input_file,

        "-vf",
        "scale='min(1280,iw)':-2",

        "-c:v",
        "libx264",

        "-preset",
        "medium",

        "-crf",
        "28",

        "-c:a",
        "aac",

        "-b:a",
        "96k",

        "-movflags",
        "+faststart",

        output_file
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_text = stderr.decode(
            errors="ignore"
        )

        logging.error(
            "FFmpeg compression failed:\n%s",
            error_text[-3000:]
        )

        raise RuntimeError(
            "FFmpeg compression failed."
        )

    if not os.path.exists(output_file):
        raise RuntimeError(
            "FFmpeg finished but compressed file was not created."
        )

    size = file_size_mb(output_file)

    logging.info(
        "Compressed video size: %.2f MB",
        size
    )

    # If still too large, perform a stronger compression.
    if size > 45:
        logging.info(
            "Compressed file is still too large. "
            "Running stronger compression..."
        )

        second_output = f"{base}_compressed2.mp4"

        if os.path.exists(second_output):
            try:
                os.remove(second_output)
            except Exception:
                pass

        command = [
            "ffmpeg",
            "-y",
            "-i",
            input_file,

            "-vf",
            "scale='min(960,iw)':-2",

            "-c:v",
            "libx264",

            "-preset",
            "medium",

            "-crf",
            "32",

            "-c:a",
            "aac",

            "-b:a",
            "64k",

            "-movflags",
            "+faststart",

            second_output
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode == 0 and os.path.exists(second_output):

            second_size = file_size_mb(second_output)

            logging.info(
                "Second compression size: %.2f MB",
                second_size
            )

            # Use the better compressed file
            try:
                os.remove(output_file)
            except Exception:
                pass

            output_file = second_output

    return output_file


# ============================================================
# CLEANUP
# ============================================================

async def delete_file(filepath):
    """
    Windows-friendly file deletion.

    Telegram/aiohttp can briefly keep a file handle open,
    so retry several times.
    """

    if not filepath:
        return

    for attempt in range(30):

        if not os.path.exists(filepath):
            return

        try:
            os.remove(filepath)

            logging.info(
                "Deleted file: %s",
                filepath
            )

            return

        except PermissionError:

            if attempt == 0:
                logging.info(
                    "File still locked, waiting before cleanup: %s",
                    filepath
                )

            await asyncio.sleep(1)

        except Exception:
            logging.exception(
                "Could not delete file: %s",
                filepath
            )

            return

    logging.warning(
        "Could not delete file after retries: %s",
        filepath
    )


# ============================================================
# SEND VIDEO DIRECTLY
# ============================================================

async def upload_video(filepath, destination, caption):
    """
    Try sending a file as Telegram video.
    """

    video = FSInputFile(filepath)

    await bot.send_video(
        chat_id=destination,
        video=video,
        caption=caption,
        request_timeout=600
    )


# ============================================================
# SEND AS DOCUMENT
# ============================================================

async def upload_document(filepath, destination, caption):
    """
    Send file as Telegram document.
    """

    document = FSInputFile(filepath)

    await bot.send_document(
        chat_id=destination,
        document=document,
        caption=caption[:1024],
        request_timeout=600
    )


# ============================================================
# MAIN DOWNLOAD + UPLOAD PIPELINE
# ============================================================

async def send_video(post):
    """
    Complete pipeline:

    Reddit
        ↓
    Download
        ↓
    Try normal video upload
        ↓
    Too large?
        ↓
    Compress
        ↓
    Try video upload again
        ↓
    Still fails due size?
        ↓
    Send as document
    """

    original_file = None
    compressed_file = None

    destination = get_destination()

    if not destination:
        raise RuntimeError(
            "Destination chat is not set. Use /setchat first."
        )

    caption = post.get(
        "title",
        ""
    )

    reddit_url = (
        post.get("permalink")
        or post.get("url")
    )

    try:

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        logging.info(
            "Downloading video: %s",
            reddit_url
        )

        original_file, info = await download(
            reddit_url
        )

        if not original_file:
            raise RuntimeError(
                "Downloader returned no file."
            )

        if not os.path.exists(original_file):
            raise RuntimeError(
                "Downloaded file was not found."
            )

        original_size = file_size_mb(
            original_file
        )

        logging.info(
            "Downloaded video: %.2f MB",
            original_size
        )

        # ----------------------------------------------------
        # FIRST UPLOAD ATTEMPT
        # ----------------------------------------------------

        try:

            logging.info(
                "Uploading original video: %.2f MB",
                original_size
            )

            await upload_video(
                original_file,
                destination,
                caption
            )

            logging.info(
                "Video uploaded successfully."
            )

            return True

        except Exception as upload_error:

            # ------------------------------------------------
            # ONLY COMPRESS WHEN TELEGRAM REJECTS SIZE
            # ------------------------------------------------

            if not is_file_too_large_error(
                upload_error
            ):
                raise

            logging.warning(
                "Telegram rejected original file because "
                "it is too large: %.2f MB",
                original_size
            )

            # ------------------------------------------------
            # COMPRESS
            # ------------------------------------------------

            compressed_file = await compress_video(
                original_file
            )

            compressed_size = file_size_mb(
                compressed_file
            )

            logging.info(
                "Trying compressed video: %.2f MB",
                compressed_size
            )

            # ------------------------------------------------
            # SECOND VIDEO UPLOAD
            # ------------------------------------------------

            try:

                await upload_video(
                    compressed_file,
                    destination,
                    caption
                )

                logging.info(
                    "Compressed video uploaded successfully."
                )

                return True

            except Exception as compressed_error:

                # --------------------------------------------
                # IF SIZE ERROR AGAIN → DOCUMENT
                # --------------------------------------------

                if not is_file_too_large_error(
                    compressed_error
                ):
                    raise

                logging.warning(
                    "Compressed video is still too large. "
                    "Trying document upload."
                )

                await upload_document(
                    compressed_file,
                    destination,
                    caption
                )

                logging.info(
                    "Compressed video sent as document."
                )

                return True

    except Exception:
        logging.exception(
            "Video processing failed: %s",
            reddit_url
        )

        raise

    finally:

        # Delete compressed file first
        if compressed_file:
            await delete_file(
                compressed_file
            )

        # Delete original file
        if original_file:
            await delete_file(
                original_file
            )


# ============================================================
# QUEUE ADD
# ============================================================

async def add_to_queue(
    post,
    status_message=None,
    manual=False
):
    key = queue_key(post)

    if key in queued_posts:
        return False

    queued_posts.add(key)

    await video_queue.put(
        {
            "post": post,
            "status_message": status_message,
            "manual": manual,
            "key": key
        }
    )

    logging.info(
        "Added to video queue. Queue size: %s | %s",
        video_queue.qsize(),
        post.get("permalink") or post.get("url")
    )

    return True


# ============================================================
# QUEUE WORKER
# ============================================================

async def queue_worker():
    global current_job

    logging.info(
        "Video queue worker started."
    )

    while True:

        job = await video_queue.get()

        current_job = job

        post = job["post"]
        status_message = job.get(
            "status_message"
        )
        manual = job.get(
            "manual",
            False
        )
        key = job["key"]

        url = (
            post.get("permalink")
            or post.get("url")
        )

        try:

            logging.info(
                "Queue worker processing: %s",
                url
            )

            if status_message:

                try:
                    await status_message.edit_text(
                        "⬇️ Processing video...\n\n"
                        f"{escape(url)}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # ------------------------------------------------
            # ACTUAL DOWNLOAD + SEND
            # ------------------------------------------------

            await send_video(post)

            # ------------------------------------------------
            # ONLY SAVE AFTER SUCCESS
            # ------------------------------------------------

            if not manual:

                database.save_post(
                    post,
                    sent=True
                )

            if status_message:

                try:
                    await status_message.edit_text(
                        "✅ Video downloaded and sent "
                        "successfully."
                    )
                except Exception:
                    pass

            logging.info(
                "Queue job completed successfully: %s",
                url
            )

        except Exception as e:

            logging.exception(
                "Queue job failed: %s",
                url
            )

            # IMPORTANT:
            #
            # We DO NOT save failed Reddit posts here.
            #
            # That means the monitor can retry them
            # during a future polling cycle.

            if status_message:

                error = escape(
                    str(e)[:3000]
                )

                try:
                    await status_message.edit_text(
                        "❌ Video failed.\n\n"
                        f"<code>{error}</code>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        finally:

            queued_posts.discard(
                key
            )

            current_job = None

            video_queue.task_done()

            # Small gap between jobs
            await asyncio.sleep(1)


# ============================================================
# START
# ============================================================

@dp.message(Command("start"))
async def start(message: Message):

    if not is_admin(message):
        return

    await message.answer(
        "🤖 Reddit Video Scrapper\n\n"
        "/add r/subreddit 100\n"
        "/remove r/subreddit\n"
        "/list\n"
        "/setchat\n"
        "/send <reddit link>\n"
        "/queue\n"
        "/status\n"
        "/pause\n"
        "/resume"
    )


# ============================================================
# SET CHAT
# ============================================================

@dp.message(Command("setchat"))
async def setchat_command(message: Message):

    if not is_admin(message):
        return

    chat_id = message.chat.id

    database.set_setting(
        "destination_chat_id",
        chat_id
    )

    await message.answer(
        "✅ Destination chat saved permanently.\n\n"
        f"Chat ID: <code>{chat_id}</code>",
        parse_mode="HTML"
    )


# ============================================================
# MANUAL SEND
# ============================================================

@dp.message(Command("send"))
async def send_command(message: Message):

    if not is_admin(message):
        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await message.answer(
            "Usage:\n"
            "/send https://www.reddit.com/..."
        )

        return

    url = parts[1].strip()

    if (
        "reddit.com" not in url
        and "redd.it" not in url
    ):

        await message.answer(
            "❌ That doesn't look like a Reddit URL."
        )

        return

    destination = get_destination()

    if not destination:

        await message.answer(
            "❌ Destination chat is not set.\n\n"
            "Use /setchat first."
        )

        return

    status = await message.answer(
        "⏳ Adding video to queue..."
    )

    post = {
        "id": f"manual_{id(message)}",
        "subreddit": "Reddit",
        "permalink": url,
        "created_utc": 0,
        "is_video": True
    }

    added = await add_to_queue(
        post,
        status_message=status,
        manual=True
    )

    if not added:

        await status.edit_text(
            "⚠️ This video is already in the queue."
        )

        return

    position = video_queue.qsize()

    await status.edit_text(
        "📥 Video added to queue.\n\n"
        f"Queue waiting: {position}\n"
        "It will be downloaded and sent automatically."
    )


# ============================================================
# QUEUE STATUS
# ============================================================

@dp.message(Command("queue"))
async def queue_command(message: Message):

    if not is_admin(message):
        return

    waiting = video_queue.qsize()

    if current_job:

        current_url = (
            current_job["post"].get("permalink")
            or current_job["post"].get("url")
            or "Unknown"
        )

        await message.answer(
            "📦 Video queue\n\n"
            f"🔄 Currently processing:\n"
            f"{current_url}\n\n"
            f"⏳ Waiting in queue: {waiting}"
        )

    else:

        await message.answer(
            "📦 Video queue\n\n"
            "🟢 Worker idle\n"
            f"⏳ Waiting in queue: {waiting}"
        )


# ============================================================
# ADD SUBREDDIT
# ============================================================

@dp.message(Command("add"))
async def add_command(message: Message):

    if not is_admin(message):
        return

    parts = message.text.split()

    if len(parts) != 3:

        await message.answer(
            "Usage:\n"
            "/add r/memes 100"
        )

        return

    subreddit = clean_subreddit(
        parts[1]
    )

    if not subreddit:

        await message.answer(
            "❌ Invalid subreddit."
        )

        return

    try:

        limit = int(parts[2])

    except ValueError:

        await message.answer(
            "❌ Number must be an integer."
        )

        return

    if limit < 1 or limit > 1000:

        await message.answer(
            "❌ Number must be between 1 and 1000."
        )

        return

    database.add_community(
        subreddit,
        limit
    )

    await message.answer(
        f"🔎 Added r/{subreddit}\n"
        "Checking existing posts..."
    )

    try:

        posts = await fetch_posts(
            subreddit,
            limit=min(limit, 100)
        )

    except Exception as e:

        await message.answer(
            "❌ Reddit lookup failed:\n"
            f"{escape(str(e)[:2000])}",
            parse_mode="HTML"
        )

        return

    videos = [
        post
        for post in posts
        if post.get("is_video")
    ]

    # Oldest → newest
    videos.reverse()

    queued = 0
    skipped = 0

    for post in videos:

        if database.post_exists(
            post["id"]
        ):

            skipped += 1
            continue

        added = await add_to_queue(
            post,
            manual=False
        )

        if added:
            queued += 1
        else:
            skipped += 1

    await message.answer(
        f"✅ r/{subreddit} configured.\n\n"
        f"Posts checked: {len(posts)}\n"
        f"Videos found: {len(videos)}\n"
        f"Added to queue: {queued}\n"
        f"Skipped: {skipped}\n\n"
        "Automatic monitoring is ON."
    )


# ============================================================
# REMOVE
# ============================================================

@dp.message(Command("remove"))
async def remove_command(message: Message):

    if not is_admin(message):
        return

    parts = message.text.split()

    if len(parts) != 2:

        await message.answer(
            "Usage:\n"
            "/remove r/memes"
        )

        return

    subreddit = clean_subreddit(
        parts[1]
    )

    if not subreddit:

        await message.answer(
            "❌ Invalid subreddit."
        )

        return

    if database.remove_community(
        subreddit
    ):

        await message.answer(
            f"🗑 Removed r/{subreddit}"
        )

    else:

        await message.answer(
            f"❌ r/{subreddit} isn't being monitored."
        )


# ============================================================
# LIST
# ============================================================

@dp.message(Command("list"))
async def list_command(message: Message):

    if not is_admin(message):
        return

    communities = database.get_communities()

    if not communities:

        await message.answer(
            "📭 No communities monitored."
        )

        return

    text = "📡 Monitored communities:\n\n"

    for community in communities:

        text += (
            f"• r/{community['subreddit']} "
            f"({community['post_limit']})\n"
        )

    await message.answer(
        text
    )


# ============================================================
# STATUS
# ============================================================

@dp.message(Command("status"))
async def status_command(message: Message):

    if not is_admin(message):
        return

    communities = database.get_communities()

    destination = get_destination()

    if current_job:

        processing = "YES"

    else:

        processing = "NO"

    await message.answer(
        "🤖 Bot status\n\n"
        f"Communities: {len(communities)}\n"
        f"Polling: {config.POLL_SECONDS}s\n"
        f"Destination: "
        f"{destination or 'NOT SET'}\n\n"
        f"Currently processing: {processing}\n"
        f"Queue waiting: {video_queue.qsize()}"
    )


# ============================================================
# PAUSE
# ============================================================

@dp.message(Command("pause"))
async def pause_command(message: Message):

    if not is_admin(message):
        return

    conn = database.connect()

    conn.execute(
        "UPDATE communities SET enabled = 0"
    )

    conn.commit()
    conn.close()

    await message.answer(
        "⏸ Monitoring paused.\n\n"
        "Videos already in the queue will still be processed."
    )


# ============================================================
# RESUME
# ============================================================

@dp.message(Command("resume"))
async def resume_command(message: Message):

    if not is_admin(message):
        return

    conn = database.connect()

    conn.execute(
        "UPDATE communities SET enabled = 1"
    )

    conn.commit()
    conn.close()

    await message.answer(
        "▶️ Monitoring resumed."
    )


# ============================================================
# REDDIT MONITOR
# ============================================================

async def monitor():

    logging.info(
        "Reddit video monitor started."
    )

    while True:

        try:

            communities = (
                database.get_communities()
            )

            for community in communities:

                # Skip disabled communities
                if not community.get(
                    "enabled",
                    1
                ):
                    continue

                subreddit = community[
                    "subreddit"
                ]

                logging.info(
                    "Checking r/%s",
                    subreddit
                )

                try:

                    posts = await fetch_posts(
                        subreddit,
                        limit=100
                    )

                except Exception as e:

                    logging.error(
                        "Failed checking r/%s: %s",
                        subreddit,
                        e
                    )

                    continue

                for post in posts:

                    if not post.get(
                        "is_video"
                    ):
                        continue

                    post_id = post.get(
                        "id"
                    )

                    if not post_id:
                        continue

                    # Already successfully handled
                    if database.post_exists(
                        post_id
                    ):
                        continue

                    # Already waiting in memory
                    if queue_key(post) in queued_posts:
                        continue

                    logging.info(
                        "New video found: %s",
                        post.get("permalink")
                    )

                    added = await add_to_queue(
                        post,
                        manual=False
                    )

                    if added:

                        logging.info(
                            "Video added to queue: %s",
                            post_id
                        )

                    await asyncio.sleep(0.5)

                await asyncio.sleep(1)

        except Exception:

            logging.exception(
                "Monitor loop error"
            )

        await asyncio.sleep(
            config.POLL_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    database.init_db()

    logging.info(
        "Bot starting..."
    )

    logging.info(
        "Saved destination: %s",
        get_destination()
    )

    # Start exactly ONE queue worker
    asyncio.create_task(
        queue_worker()
    )

    # Start Reddit monitor
    asyncio.create_task(
        monitor()
    )

    logging.info(
        "Starting Telegram polling..."
    )

    await dp.start_polling(
        bot
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(
        main()
    )