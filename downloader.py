import os
import re
import glob
import asyncio
import aiohttp
import yt_dlp

from aiogram.types import FSInputFile


ARCTIC_BASE = "https://arctic-shift.photon-reddit.com"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
}


# ============================================================
# ARCTIC SHIFT
# ============================================================

async def get_json(endpoint, params=None):
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=HEADERS
    ) as session:

        async with session.get(
            f"{ARCTIC_BASE}{endpoint}",
            params=params or {}
        ) as response:

            text = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"Arctic Shift HTTP {response.status}\n"
                    f"{text[:1500]}"
                )

            try:
                return await response.json(content_type=None)
            except Exception:
                raise RuntimeError(
                    "Arctic Shift returned invalid JSON:\n"
                    + text[:1500]
                )


# ============================================================
# REDDIT URL HANDLING
# ============================================================

def extract_post_id(url):
    if not url:
        return None

    match = re.search(
        r"/comments/([A-Za-z0-9]+)",
        url,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    match = re.search(
        r"(?:reddit\.com|redd\.it)/([A-Za-z0-9]+)$",
        url.rstrip("/"),
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return None


def is_short_link(url):
    return bool(
        re.search(
            r"https?://(?:www\.)?reddit\.com/"
            r"(?:r/[^/]+/)?s/[^/?#]+",
            url,
            re.IGNORECASE
        )
    )


async def resolve_short_link(url):
    if not is_short_link(url):
        return url

    print(f"Resolving Reddit short link: {url}")

    match = re.search(
        r"https?://(?:www\.)?reddit\.com"
        r"(?P<path>/(?:r/[^/]+/)?s/[^/?#]+)",
        url,
        re.IGNORECASE
    )

    if not match:
        return url

    short_path = match.group("path")

    # --------------------------------------------------------
    # Try Arctic Shift first
    # --------------------------------------------------------

    try:
        data = await get_json(
            "/api/short_links",
            {"paths": short_path}
        )

        full_path = None

        if isinstance(data, dict):

            results = data.get("data") or []

            if isinstance(results, list) and results:

                item = results[0]

                if isinstance(item, str):
                    full_path = item

                elif isinstance(item, dict):
                    full_path = (
                        item.get("full_path")
                        or item.get("url")
                        or item.get("full_url")
                    )

        elif isinstance(data, list) and data:

            item = data[0]

            if isinstance(item, str):
                full_path = item

            elif isinstance(item, dict):
                full_path = (
                    item.get("full_path")
                    or item.get("url")
                    or item.get("full_url")
                )

        if full_path:

            if full_path.startswith("http"):
                canonical = full_path
            else:
                canonical = (
                    "https://www.reddit.com" + full_path
                )

            print(f"Resolved through Arctic Shift: {canonical}")

            return canonical

        print(
            "Arctic Shift has no mapping. "
            "Trying Reddit redirect..."
        )

    except Exception as e:
        print(f"Arctic Shift resolver failed: {e}")

    # --------------------------------------------------------
    # Follow Reddit redirect
    # --------------------------------------------------------

    timeout = aiohttp.ClientTimeout(total=30)

    redirect_headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
    }

    try:

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=redirect_headers
        ) as session:

            async with session.get(
                url,
                allow_redirects=True
            ) as response:

                final_url = str(response.url)

                print(
                    f"Reddit redirect result: {final_url}"
                )

                if "/comments/" in final_url:

                    canonical = final_url.split("?")[0]

                    print(
                        f"Canonical Reddit URL: {canonical}"
                    )

                    return canonical

                text = await response.text()

                patterns = [
                    r'https?://(?:www\.)?reddit\.com'
                    r'/r/[^"\']+/comments/[A-Za-z0-9]+'
                    r'[^"\']*',

                    r'https?://(?:www\.)?reddit\.com'
                    r'/comments/[A-Za-z0-9]+'
                    r'[^"\']*',
                ]

                for pattern in patterns:

                    found = re.search(
                        pattern,
                        text,
                        re.IGNORECASE
                    )

                    if found:

                        canonical = (
                            found.group(0)
                            .replace("&amp;", "&")
                            .split("?")[0]
                        )

                        print(
                            f"Found canonical URL: {canonical}"
                        )

                        return canonical

    except Exception as e:
        print(f"Reddit redirect failed: {e}")

    raise RuntimeError(
        "Could not resolve Reddit /s/ link."
    )


# ============================================================
# GET REDDIT POST
# ============================================================

async def get_post_from_arctic(post_id):

    print(
        f"Looking up post in Arctic Shift: {post_id}"
    )

    data = await get_json(
        "/api/posts/ids",
        {"ids": post_id}
    )

    if isinstance(data, dict):
        posts = data.get("data") or []

    elif isinstance(data, list):
        posts = data

    else:
        posts = []

    if not posts:
        raise RuntimeError(
            f"Post {post_id} was not found in Arctic Shift."
        )

    post = posts[0]

    print(
        f"Post found: {post.get('title', '')}"
    )

    return post


# ============================================================
# MEDIA DETECTION
# ============================================================

def is_redgifs_url(url):
    if not url:
        return False

    return bool(
        re.search(
            r"redgifs\.com",
            url,
            re.IGNORECASE
        )
    )


def is_reddit_video_url(url):
    if not url:
        return False

    lower = url.lower()

    return (
        "v.redd.it" in lower
        or lower.endswith(".mp4")
        or lower.endswith(".webm")
        or lower.endswith(".mov")
    )


def get_media_url(post):

    post_url = post.get("url")

    if post_url:

        if is_reddit_video_url(post_url):

            print(
                f"Detected Reddit video: {post_url}"
            )

            return post_url

        if is_redgifs_url(post_url):

            print(
                f"Detected RedGifs video: {post_url}"
            )

            return post_url

    # --------------------------------------------------------
    # Reddit media object
    # --------------------------------------------------------

    media = post.get("media")

    if isinstance(media, dict):

        reddit_video = media.get("reddit_video")

        if isinstance(reddit_video, dict):

            video_url = (
                reddit_video.get("fallback_url")
                or reddit_video.get("dash_url")
            )

            if video_url:
                print(
                    f"Detected Reddit media URL: {video_url}"
                )

                return video_url

    # --------------------------------------------------------
    # Secure media
    # --------------------------------------------------------

    secure_media = post.get("secure_media")

    if isinstance(secure_media, dict):

        reddit_video = secure_media.get(
            "reddit_video"
        )

        if isinstance(reddit_video, dict):

            video_url = (
                reddit_video.get("fallback_url")
                or reddit_video.get("dash_url")
            )

            if video_url:
                print(
                    f"Detected secure Reddit video: "
                    f"{video_url}"
                )

                return video_url

    return None


# ============================================================
# FILE FINDING
# ============================================================

def find_downloaded_file(
    before_files,
    expected_file=None
):

    if expected_file and os.path.exists(expected_file):
        return expected_file

    after_files = set(
        glob.glob(
            os.path.join(DOWNLOAD_DIR, "*")
        )
    )

    new_files = [
        path
        for path in (after_files - before_files)
        if os.path.isfile(path)
    ]

    new_files = [
        path
        for path in new_files
        if not path.endswith(
            (".part", ".ytdl")
        )
    ]

    if not new_files:
        return None

    return max(
        new_files,
        key=os.path.getmtime
    )


# ============================================================
# DOWNLOAD WITH YT-DLP
# ============================================================

def download_with_ytdlp(
    media_url,
    post_id
):

    print("")
    print("=" * 60)
    print("Downloading actual media:")
    print(media_url)
    print("=" * 60)

    output_template = os.path.join(
        DOWNLOAD_DIR,
        f"{post_id}_%(autonumber)03d.%(ext)s"
    )

    before_files = set(
        glob.glob(
            os.path.join(DOWNLOAD_DIR, "*")
        )
    )

    # --------------------------------------------------------
    # Use the correct headers for the actual media host.
    # --------------------------------------------------------

    if is_redgifs_url(media_url):

        media_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.redgifs.com/",
            "Origin": "https://www.redgifs.com",
            "Accept": "*/*",
        }

        print("Using RedGifs media headers.")

    else:

        media_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.reddit.com/",
            "Accept": "*/*",
        }

        print("Using Reddit media headers.")

    options = {
        "format": "bv*+ba/best",

        "outtmpl": output_template,

        "noplaylist": True,

        "merge_output_format": "mp4",

        "retries": 5,

        "fragment_retries": 5,

        "file_access_retries": 5,

        "continuedl": True,

        "quiet": False,

        "no_warnings": False,

        "http_headers": media_headers,

        "writethumbnail": False,

        "writesubtitles": False,

        "keepvideo": True,
    }

    def run_ytdlp():

        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                media_url,
                download=True
            )

            filename = ydl.prepare_filename(
                info
            )

            return info, filename

    try:

        info, filename = run_ytdlp()

    except Exception as first_error:

        print(
            f"yt-dlp first attempt failed: {first_error}"
        )

        # ----------------------------------------------------
        # RedGifs second attempt
        # ----------------------------------------------------

        if is_redgifs_url(media_url):

            print(
                "Retrying RedGifs with single best format..."
            )

            retry_options = dict(options)

            retry_options["format"] = "best"

            def retry_ytdlp():

                with yt_dlp.YoutubeDL(
                    retry_options
                ) as ydl:

                    info = ydl.extract_info(
                        media_url,
                        download=True
                    )

                    filename = ydl.prepare_filename(
                        info
                    )

                    return info, filename

            try:

                info, filename = retry_ytdlp()

            except Exception as second_error:

                raise RuntimeError(
                    "RedGifs download failed.\n\n"
                    f"First attempt:\n"
                    f"{first_error}\n\n"
                    f"Retry:\n"
                    f"{second_error}"
                )

        else:

            raise RuntimeError(
                f"Video download failed:\n"
                f"{first_error}"
            )

    # --------------------------------------------------------
    # Find final downloaded file
    # --------------------------------------------------------

    expected = filename

    if expected:

        base, _ = os.path.splitext(expected)

        mp4_file = base + ".mp4"

        if os.path.exists(mp4_file):
            expected = mp4_file

    filepath = find_downloaded_file(
        before_files,
        expected
    )

    if not filepath:

        raise RuntimeError(
            "yt-dlp completed but no video file was found."
        )

    if not os.path.exists(filepath):

        raise RuntimeError(
            f"Downloaded file does not exist:\n"
            f"{filepath}"
        )

    size = os.path.getsize(filepath)

    if size <= 0:

        raise RuntimeError(
            "Downloaded video is empty."
        )

    print(
        f"Downloaded file: {filepath}"
    )

    print(
        f"Size: {size / (1024 * 1024):.2f} MB"
    )

    return filepath, info

# ============================================================
# MAIN DOWNLOAD FUNCTION
# ============================================================

async def download(url):

    if not url:
        raise RuntimeError(
            "No Reddit URL supplied."
        )

    print("")
    print("=" * 60)
    print("Original Reddit URL:")
    print(url)
    print("=" * 60)

    # Resolve /s/
    canonical = await resolve_short_link(url)

    print(
        f"Canonical URL: {canonical}"
    )

    # Get post ID
    post_id = extract_post_id(canonical)

    if not post_id:

        raise RuntimeError(
            "Could not extract Reddit post ID from:\n"
            + canonical
        )

    print(
        f"Post ID: {post_id}"
    )

    # Get metadata
    post = await get_post_from_arctic(
        post_id
    )

    # Get actual media
    media_url = get_media_url(post)

    if not media_url:

        raise RuntimeError(
            "This Reddit post was found, but no usable "
            "video URL was provided.\n\n"
            f"Post URL:\n{post.get('url')}"
        )

    print(
        f"Actual media URL: {media_url}"
    )

    # Download
    filepath, info = await asyncio.to_thread(
        download_with_ytdlp,
        media_url,
        post_id
    )

    # Store metadata for bot.py
    info["_reddit_post_id"] = post_id
    info["_reddit_url"] = canonical
    info["_source_media_url"] = media_url
    info["_reddit_title"] = post.get(
        "title",
        ""
    )
    info["_reddit_subreddit"] = post.get(
        "subreddit",
        ""
    )

    return filepath, info


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def upload_to_telegram(
    bot,
    chat_id,
    filepath,
    info=None,
    caption=None
):

    if not filepath:
        raise RuntimeError(
            "No file supplied for Telegram upload."
        )

    if not os.path.exists(filepath):
        raise FileNotFoundError(
            f"Video file not found:\n{filepath}"
        )

    if caption is None and info:

        title = info.get(
            "_reddit_title",
            ""
        )

        subreddit = info.get(
            "_reddit_subreddit",
            ""
        )

        reddit_url = info.get(
            "_reddit_url",
            ""
        )

        parts = []

        if title:
            parts.append(title)

        if subreddit:
            parts.append(
                f"r/{subreddit}"
            )

        if reddit_url:
            parts.append(
                reddit_url
            )

        caption = "\n".join(parts)

    if caption:
        caption = caption[:1024]

    print(
        f"Uploading video to Telegram: {filepath}"
    )

    telegram_file = FSInputFile(
        filepath
    )

    try:

        message = await bot.send_video(
            chat_id=chat_id,
            video=telegram_file,
            caption=caption,
            supports_streaming=True
        )

        print(
            "Telegram upload successful."
        )

        return message

    except Exception as video_error:

        print(
            f"send_video failed: {video_error}"
        )

        print(
            "Trying send_document..."
        )

        telegram_file = FSInputFile(
            filepath
        )

        try:

            message = await bot.send_document(
                chat_id=chat_id,
                document=telegram_file,
                caption=caption
            )

            print(
                "Telegram document upload successful."
            )

            return message

        except Exception as document_error:

            raise RuntimeError(
                "Telegram upload failed.\n\n"
                f"send_video:\n{video_error}\n\n"
                f"send_document:\n{document_error}"
            )


# ============================================================
# DOWNLOAD + TELEGRAM
# ============================================================

async def download_and_upload(
    bot,
    chat_id,
    url,
    caption=None
):

    filepath = None

    try:

        filepath, info = await download(
            url
        )

        message = await upload_to_telegram(
            bot=bot,
            chat_id=chat_id,
            filepath=filepath,
            info=info,
            caption=caption
        )

        return message

    finally:

        if filepath and os.path.exists(filepath):

            try:

                os.remove(filepath)

                print(
                    f"Deleted local file: {filepath}"
                )

            except Exception as e:

                print(
                    f"Could not delete local file: {e}"
                )