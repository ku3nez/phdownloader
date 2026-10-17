"""Video extraction for Threads posts, which yt-dlp does not support.

A public Threads post page embeds the post as JSON (the same media objects as
Instagram) including signed CDN links to the MP4 files, so no login is needed.
"""

import json
import re
from urllib.parse import urlparse

POST_CODE_RE = re.compile(r"/(?:@[^/]+/post|post|t)/([A-Za-z0-9_-]+)")
JSON_SCRIPT_RE = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)
FILENAME_FORBIDDEN_RE = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


class ThreadsError(RuntimeError):
    pass


def _find_post(node, code):
    """Depth-first search for the media object of the post with this code."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("code") == code and ("video_versions" in item or "carousel_media" in item or "image_versions2" in item):
                return item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _best_video_url(media):
    versions = [v for v in (media.get("video_versions") or []) if isinstance(v, dict) and v.get("url")]
    if not versions:
        return None
    # Variants usually carry no dimensions and are the same file; prefer the
    # largest declared size and otherwise keep Threads' own order.
    best = max(versions, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
    return best["url"]


def _title(post, code):
    username = ((post.get("user") or {}).get("username") or "threads").strip()
    caption = post.get("caption")
    text = caption.get("text", "") if isinstance(caption, dict) else ""
    text = " ".join(text.split())
    if len(text) > 80:
        text = text[:80].rsplit(" ", 1)[0] + "…"
    title = f"{username} - {text}" if text else f"{username} - {code}"
    title = FILENAME_FORBIDDEN_RE.sub("_", title).strip(" ._") or code
    return title


def resolve_threads_post(url, proxy=None, timeout=30):
    """Return {"video_urls": [...], "title": str, "code": str} for a public Threads post."""
    from curl_cffi import requests

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(url, impersonate="chrome", timeout=timeout, proxies=proxies, allow_redirects=True)
    except Exception as exc:
        raise ThreadsError(f"Could not open the Threads post: {exc}") from exc
    if response.status_code != 200:
        raise ThreadsError(f"Threads returned HTTP {response.status_code} for this post")

    final_url = str(response.url)
    match = POST_CODE_RE.search(urlparse(final_url).path) or POST_CODE_RE.search(urlparse(url).path)
    if not match:
        raise ThreadsError("This Threads link does not point to a post")
    code = match.group(1)

    html = response.text
    post = None
    for blob in JSON_SCRIPT_RE.findall(html):
        if f'"code":"{code}"' not in blob:
            continue
        try:
            post = _find_post(json.loads(blob), code)
        except ValueError:
            continue
        if post:
            break
    if not post:
        raise ThreadsError("Threads did not return this post; it may be private, deleted or require login")

    items = post.get("carousel_media") or [post]
    video_urls = [u for u in (_best_video_url(item) for item in items if isinstance(item, dict)) if u]
    if not video_urls:
        raise ThreadsError("This Threads post has no video")
    return {"video_urls": video_urls, "title": _title(post, code), "code": code}
