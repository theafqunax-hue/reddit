import asyncio
import os
import shutil

from telegram import Bot

from config import (
    DOWNLOAD_DIR,
    MAX_CONCURRENT_DOWNLOADS,
)

from database import (
    save_post,
    update_post_status,
)

from downloader import download_video


TELEGRAM_MAX_SIZE = 50 * 1024 * 1024


class DownloadWorker:

    def __init__(self, bot: Bot):

        self.bot = bot

        self.queue = asyncio.Queue()

        self.semaphore = asyncio.Semaphore(
            MAX_CONCURRENT_DOWNLOADS
        )

        self.running = True

    async def add(self, post):

        await self.queue.put(post)

    async def run(self):

        while self.running:

            post = await self.queue.get()

            try:

                await self.process(post)

            except Exception as e:

                print(
                    f"[WORKER ERROR] {post['id']}: {e}"
                )

            finally:

                self.queue.task_done()

    async def process(self, post):

        reddit_id = post["id"]

        save_post(
            reddit_id=reddit_id,
            subreddit=post["subreddit"],
            title=post["title"],
            reddit_url=post["permalink"],
            media_url=post["url"],
            status="downloading",
            created_at=str(post["created_utc"])
        )

        async with self.semaphore:

            try:

                print(
                    f"[DOWNLOAD] {reddit_id}"
                )

                file_path = await asyncio.to_thread(
                    download_video,
                    post["url"],
                    reddit_id
                )

                size = os.path.getsize(
                    file_path
                )

                if size > TELEGRAM_MAX_SIZE:

                    update_post_status(
                        reddit_id,
                        "failed",
                        "File exceeds Telegram Bot API 50 MB limit"
                    )

                    print(
                        f"[SKIP] {reddit_id}: "
                        f"{size / 1024 / 1024:.1f} MB"
                    )

                    return

                destination = await self.get_destination()

                if not destination:

                    update_post_status(
                        reddit_id,
                        "failed",
                        "Destination chat not configured"
                    )

                    return

                print(
                    f"[UPLOAD] {reddit_id}"
                )

                with open(file_path, "rb") as video:

                    await self.bot.send_video(
                        chat_id=destination,
                        video=video,
                        caption=(
                            f"🎬 {post['title']}\n\n"
                            f"r/{post['subreddit']}\n"
                            f"{post['permalink']}"
                        ),
                        supports_streaming=True
                    )

                update_post_status(
                    reddit_id,
                    "sent"
                )

                print(
                    f"[SENT] {reddit_id}"
                )

            except Exception as e:

                print(
                    f"[FAILED] {reddit_id}: {e}"
                )

                update_post_status(
                    reddit_id,
                    "failed",
                    str(e)[:1000]
                )

            finally:

                # Delete temporary download
                try:

                    post_dir = os.path.join(
                        DOWNLOAD_DIR,
                        reddit_id
                    )

                    shutil.rmtree(
                        post_dir,
                        ignore_errors=True
                    )

                except Exception:
                    pass

    async def get_destination(self):

        from database import get_setting

        return get_setting(
            "destination_chat"
        )