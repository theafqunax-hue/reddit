import aiohttp
import re
from urllib.parse import urlencode


BASE_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"

HEADERS = {
    "User-Agent": "RedditTelegramLinkBot/1.0"
}


def clean_subreddit(subreddit: str) -> str:
    subreddit = subreddit.strip()

    if subreddit.startswith("https://www.reddit.com/r/"):
        subreddit = subreddit.split("/r/", 1)[1].split("/", 1)[0]

    elif subreddit.startswith("https://reddit.com/r/"):
        subreddit = subreddit.split("/r/", 1)[1].split("/", 1)[0]

    elif subreddit.lower().startswith("r/"):
        subreddit = subreddit[2:]

    subreddit = subreddit.strip()

    if not re.fullmatch(r"[A-Za-z0-9_]+", subreddit):
        raise ValueError("Invalid subreddit name")

    return subreddit


def is_video_post(post: dict) -> bool:
    """
    Determine whether the Reddit post appears to contain video/media.
    """

    if post.get("is_video"):
        return True

    if post.get("post_hint") == "hosted:video":
        return True

    url = (post.get("url") or "").lower()

    video_extensions = (
        ".mp4",
        ".webm",
        ".mov",
        ".mkv",
        ".gif"
    )

    if url.endswith(video_extensions):
        return True

    # Common Reddit video URLs
    if "v.redd.it" in url:
        return True

    # Some archived records expose media metadata
    media = post.get("media")

    if isinstance(media, dict):
        if media.get("reddit_video"):
            return True

    secure_media = post.get("secure_media")

    if isinstance(secure_media, dict):
        if secure_media.get("reddit_video"):
            return True

    return False


def get_post_url(post: dict) -> str:
    """
    Always prefer the actual Reddit permalink.
    """

    permalink = post.get("permalink")

    if permalink:
        if permalink.startswith("http"):
            return permalink

        return "https://www.reddit.com" + permalink

    url = post.get("url")

    if url and "reddit.com" in url:
        return url

    return ""


async def fetch_posts(
    subreddit: str,
    limit: int = 100,
    sort: str = "desc"
):
    """
    Fetch posts from Arctic Shift.

    sort=desc -> newest first
    sort=asc  -> oldest first
    """

    subreddit = clean_subreddit(subreddit)

    limit = max(1, min(int(limit), 100))

    params = {
        "subreddit": subreddit,
        "limit": limit,
        "sort": sort,
    }

    url = f"{BASE_URL}?{urlencode(params)}"

    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout
    ) as session:

        async with session.get(url) as response:

            text = await response.text()

            if response.status != 200:
                raise RuntimeError(
                    f"Arctic Shift returned HTTP "
                    f"{response.status}: {text[:500]}"
                )

            try:
                data = await response.json()

            except Exception:
                raise RuntimeError(
                    f"Arctic Shift returned invalid JSON: "
                    f"{text[:500]}"
                )

    posts = data.get("data", [])

    results = []

    for post in posts:

        reddit_id = post.get("id")

        if not reddit_id:
            continue

        results.append({
            "id": reddit_id,
            "name": post.get("name"),
            "title": post.get("title") or "",
            "url": post.get("url") or "",
            "permalink": get_post_url(post),
            "subreddit": post.get(
                "subreddit",
                subreddit
            ),
            "created_utc": post.get(
                "created_utc",
                0
            ),
            "author": post.get(
                "author",
                "[deleted]"
            ),
            "is_video": is_video_post(post),
            "post_hint": post.get("post_hint"),
        })

    return results


async def fetch_video_posts(
    subreddit: str,
    limit: int = 100
):
    posts = await fetch_posts(
        subreddit,
        limit=limit,
        sort="desc"
    )

    return [
        post
        for post in posts
        if is_video_post(post)
    ]