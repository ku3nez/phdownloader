"""Look at a link before downloading: is it a playlist/series, which episodes
does it have, and which audio tracks (dubs) can be chosen."""

import os
import re

import yt_dlp

from media_urls import is_youtube_url

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except Exception:  # pragma: no cover
    ImpersonateTarget = None

MAX_ENTRIES = int(os.getenv("PLAYLIST_MAX_ENTRIES", "500"))


class _QuietLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        print(f"inspect: {msg}", flush=True)


def _options(url, flat):
    cookie_file = os.getenv("YT_DLP_COOKIE_FILE") or "cookies.txt"
    js_runtimes = [item.strip() for item in os.getenv("YT_DLP_JS_RUNTIME", "deno,node").split(",") if item.strip()]
    proxy = os.getenv("YT_DLP_PROXY")
    youtube_proxy = os.getenv("YT_DLP_YOUTUBE_PROXY")
    youtube = is_youtube_url(url)
    options = {
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietLogger(),
        "skip_download": True,
        "playlistend": MAX_ENTRIES,
        "socket_timeout": 30,
        "cookiefile": cookie_file if os.path.exists(cookie_file) else None,
        "js_runtimes": {runtime: {} for runtime in js_runtimes} or None,
        "remote_components": ["ejs:github"],
        "proxy": (youtube_proxy if youtube and youtube_proxy else proxy) or None,
        "impersonate": ImpersonateTarget.from_str("chrome") if ImpersonateTarget else None,
    }
    if flat:
        options["extract_flat"] = "in_playlist"
    else:
        options["noplaylist"] = True
    return options


def _extract(url, flat):
    options = _options(url, flat)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        if options.get("impersonate") and ("Impersonate" in str(exc) or "curl_cffi" in str(exc)):
            options["impersonate"] = None
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=False)
        raise


def audio_tracks(info):
    """Distinct audio languages of a video: [{"id", "label", "original"}].

    Only returned when there is a real choice (two or more languages).
    """
    tracks = {}
    for fmt in info.get("formats") or []:
        if fmt.get("acodec") in (None, "none"):
            continue
        language = fmt.get("language")
        if not language:
            continue
        note = str(fmt.get("format_note") or "")
        if "drc" in str(fmt.get("format_id") or ""):
            continue
        label = re.sub(r"\((?:default)\)|\boriginal\b|\bdubbed(?:-auto)?\b", "", note.split(",")[0], flags=re.I)
        label = " ".join(label.split()).strip(" ,(")
        entry = tracks.setdefault(language, {"id": language, "label": label or language, "original": False})
        if "original" in note.lower():
            entry["original"] = True
            entry["label"] = label or entry["label"]
    result = sorted(tracks.values(), key=lambda t: (not t["original"], t["label"].lower()))
    return result if len(result) > 1 else []


def _entry_url(entry, base_url):
    url = entry.get("url") or entry.get("webpage_url")
    if url and not re.match(r"^https?://", str(url)):
        if entry.get("ie_key") == "Youtube" or is_youtube_url(base_url):
            url = f"https://www.youtube.com/watch?v={entry.get('id') or url}"
        else:
            url = None
    return url


def inspect_url(url):
    info = _extract(url, flat=True)
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        entries = []
        for position, entry in enumerate(info.get("entries") or [], start=1):
            if not entry:
                continue
            entry_url = _entry_url(entry, url)
            if not entry_url:
                continue
            entries.append({
                "index": position,
                "title": str(entry.get("title") or f"#{position}"),
                "url": entry_url,
                "duration": entry.get("duration"),
            })
        current = None
        match = re.search(r"[?&]v=([\w-]{6,})", url)
        if match:
            current = next((e["index"] for e in entries if f"v={match.group(1)}" in e["url"]), None)
        tracks = []
        if entries:
            try:
                tracks = audio_tracks(_extract(entries[0]["url"], flat=False))
            except Exception:
                tracks = []
        return {
            "kind": "playlist",
            "title": str(info.get("title") or ""),
            "entries": entries,
            "current": current,
            "audio_tracks": tracks,
            "truncated": len(entries) >= MAX_ENTRIES,
        }
    if info.get("_type") == "url" and info.get("url") and info.get("url") != url:
        return inspect_url(info["url"])
    if not info.get("formats"):
        info = _extract(url, flat=False)
    return {
        "kind": "video",
        "title": str(info.get("title") or ""),
        "duration": info.get("duration"),
        "audio_tracks": audio_tracks(info),
    }
