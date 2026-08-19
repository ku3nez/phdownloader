"""MTProto publishing for completed videos.

The Telegram account session is deliberately kept outside the repository.  Only
the node consuming the dedicated Telegram RQ queue needs these settings.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _settings() -> tuple[int, str, int, str]:
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    target_chat_id = os.getenv("TELEGRAM_TARGET_CHAT_ID", "").strip()
    session_path = os.getenv("TELEGRAM_SESSION_PATH", "/opt/phdownloader/telegram.session").strip()
    if not api_id_raw or not api_hash or not target_chat_id:
        raise RuntimeError("Telegram is not configured: TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_TARGET_CHAT_ID are required")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID must be numeric") from exc
    try:
        numeric_target_chat_id = int(target_chat_id)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_TARGET_CHAT_ID must be a numeric Telegram chat ID") from exc
    return api_id, api_hash, numeric_target_chat_id, session_path


def caption_for_file(file_path: str) -> str:
    """Use the downloaded title, without the file extension, as the caption."""
    title = re.sub(r"^\[SERVER\]\s*", "", Path(file_path).stem, flags=re.IGNORECASE)
    return re.sub(r"_(?:360|480|720|1080)p?$|_best$", "", title, flags=re.IGNORECASE)


def get_video_metadata(file_path: str) -> tuple[float, int, int]:
    """Return duration, width and height without optional Python codecs."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        return float(data.get("format", {}).get("duration") or 0), int(stream.get("width") or 1), int(stream.get("height") or 1)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, json.JSONDecodeError):
        return 1.0, 1, 1


def build_video_thumbnail(file_path: str, seek_seconds: float) -> str | None:
    """Create a compact JPEG thumbnail required for Telegram's video tile."""
    fd, thumbnail_path = tempfile.mkstemp(prefix="telegram-thumb-", suffix=".jpg", dir=os.path.dirname(file_path))
    os.close(fd)
    try:
        # Telegram accepts JPEG thumbnails up to 320 px and 200 KiB. Try the
        # least compressed image first, then only reduce quality if necessary.
        for quality in (2, 3, 4, 6, 8, 10):
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{seek_seconds:.3f}",
                    "-i",
                    file_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=320:-2",
                    "-q:v",
                    str(quality),
                    thumbnail_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if result.returncode == 0 and os.path.getsize(thumbnail_path) <= 200 * 1024:
                return thumbnail_path
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.remove(thumbnail_path)
    except OSError:
        pass
    return None


async def _publish_video_async(file_path: str) -> int:
    from telethon import TelegramClient
    from telethon.tl.types import DocumentAttributeVideo

    api_id, api_hash, target_chat_id, session_path = _settings()
    if not os.path.isfile(file_path):
        raise RuntimeError(f"Telegram publication file does not exist: {file_path}")
    session_parent = os.path.dirname(session_path)
    if session_parent:
        os.makedirs(session_parent, exist_ok=True)
    duration, width, height = get_video_metadata(file_path)
    # The first seconds often contain a black intro. A frame around 10% into
    # the video gives a useful preview while keeping the seek bounded.
    thumbnail_path = build_video_thumbnail(file_path, min(max(duration * 0.1, 3.0), 30.0))
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram account is not authorized; complete telegram_auth.py first")
        message = await client.send_file(
            target_chat_id,
            file_path,
            caption=caption_for_file(file_path),
            mime_type="video/mp4",
            thumb=thumbnail_path,
            attributes=[
                DocumentAttributeVideo(
                    duration=max(1, int(round(duration))),
                    w=max(1, width),
                    h=max(1, height),
                    supports_streaming=True,
                )
            ],
            supports_streaming=True,
            force_document=False,
        )
        return int(message.id)
    finally:
        await client.disconnect()
        if thumbnail_path:
            try:
                os.remove(thumbnail_path)
            except OSError:
                pass


def publish_video(file_path: str) -> int:
    return asyncio.run(_publish_video_async(file_path))
