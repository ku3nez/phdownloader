"""MTProto publishing for completed videos.

The Telegram account session is deliberately kept outside the repository.  Only
the node consuming the dedicated Telegram RQ queue needs these settings.
"""

import asyncio
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


def build_video_thumbnail(file_path: str) -> str | None:
    """Create a compact JPEG thumbnail required for Telegram's video tile."""
    fd, thumbnail_path = tempfile.mkstemp(prefix="telegram-thumb-", suffix=".jpg", dir=os.path.dirname(file_path))
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "00:00:01",
                "-i",
                file_path,
                "-frames:v",
                "1",
                "-vf",
                "scale=240:-2",
                "-q:v",
                "10",
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

    api_id, api_hash, target_chat_id, session_path = _settings()
    if not os.path.isfile(file_path):
        raise RuntimeError(f"Telegram publication file does not exist: {file_path}")
    session_parent = os.path.dirname(session_path)
    if session_parent:
        os.makedirs(session_parent, exist_ok=True)
    thumbnail_path = build_video_thumbnail(file_path)
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
