"""Find the video of pages yt-dlp has no extractor for.

Some sites build their player with a few lines of JavaScript: the stream
address sits in the page as base64 (sometimes reversed) and a script puts it
into <source src>. yt-dlp's generic extractor does not run scripts, so it
reports "Unsupported URL". resolve() looks for such addresses itself and
returns the direct stream (HLS or MP4) together with the page title.
"""

import base64
import html
import re
import urllib.parse
import urllib.request

import url_guard

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MEDIA_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mp4|webm)(?:\?[^\s\"'<>\\]*)?", re.I)
B64_RE = re.compile(r"['\"]([A-Za-z0-9+/=]{24,})['\"]")
PLAYER_PAGES = ("fp.html", "embed", "player")


def _fetch(url, proxy=None, limit=2_000_000):
    url_guard.check_public_url(url)
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en,ru;q=0.8"})
    with opener.open(request, timeout=30) as response:
        data = response.read(limit)
        charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace"), response.geturl()


def _decoded_candidates(text):
    """Base64 strings in scripts, as written and reversed, decoded."""
    for match in B64_RE.finditer(text):
        raw = match.group(1)
        for value in (raw, raw[::-1]):
            padded = value + "=" * (-len(value) % 4)
            try:
                decoded = base64.b64decode(padded, validate=True).decode("utf-8", errors="strict")
            except Exception:
                continue
            yield decoded


def _media_in(text, base_url):
    found = []
    for match in re.finditer(r"<source[^>]+src=[\"']([^\"']+)[\"']", text, re.I):
        found.append(urllib.parse.urljoin(base_url, html.unescape(match.group(1))))
    found += MEDIA_RE.findall(text)
    for decoded in _decoded_candidates(text):
        found += MEDIA_RE.findall(decoded)
    # HLS first: it usually carries every quality.
    unique = list(dict.fromkeys(u for u in found if u.startswith(("http://", "https://"))))
    return sorted(unique, key=lambda u: 0 if ".m3u8" in u.lower() else 1)


def _title(text):
    for pattern in (r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)",
                    r"<title[^>]*>([^<]+)"):
        match = re.search(pattern, text, re.I)
        if match:
            title = html.unescape(match.group(1)).strip()
            title = re.split(r"\s+[|–—-]\s+[^|–—-]+$", title)[0].strip() or title
            return title
    return None


def resolve(url, proxy=None):
    """{"url": stream, "title": ..., "referer": page} or None."""
    try:
        page, final_url = _fetch(url, proxy)
    except Exception:
        return None
    pages = [(page, final_url)]
    # The player often lives in an iframe (…/fp.html, /embed/…); and a player
    # page has no title, its parent page does.
    for match in re.finditer(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", page, re.I):
        frame = urllib.parse.urljoin(final_url, html.unescape(match.group(1)))
        if any(key in frame for key in PLAYER_PAGES):
            try:
                pages.append(_fetch(frame, proxy))
            except Exception:
                pass
            break
    title = _title(page)
    if not title and final_url.rstrip("/").endswith(PLAYER_PAGES):
        parent = final_url.rsplit("/", 1)[0] + "/"
        try:
            title = _title(_fetch(parent, proxy)[0])
        except Exception:
            title = None
    for text, base in pages:
        media = _media_in(text, base)
        for candidate in media:
            try:
                url_guard.check_public_url(candidate)
            except url_guard.BlockedURL:
                continue
            slug = urllib.parse.urlparse(url).path.rstrip("/").split("/")
            fallback = next((part for part in reversed(slug) if part and part not in PLAYER_PAGES), "video")
            return {"url": candidate, "title": title or fallback.replace("-", " "), "referer": base}
    return None
