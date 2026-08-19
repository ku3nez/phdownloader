"""One-time Telegram MTProto account authorization and target discovery.

Run only on the node that consumes the Telegram queue.  It never prints API
credentials or session data.
"""

import argparse
import asyncio
import json
import os
import stat
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def settings() -> tuple[int, str, str, str, str]:
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    phone = os.getenv("TELEGRAM_PHONE", "").strip()
    session_path = os.getenv("TELEGRAM_SESSION_PATH", "/opt/phdownloader/telegram.session").strip()
    state_path = os.getenv("TELEGRAM_AUTH_STATE_PATH", "/opt/phdownloader/telegram-auth.json").strip()
    if not api_id_raw or not api_hash or not phone:
        raise RuntimeError("TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE are required")
    return int(api_id_raw), api_hash, phone, session_path, state_path


def write_state(state_path: str, state: dict) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


async def request_code() -> None:
    from telethon import TelegramClient

    api_id, api_hash, phone, session_path, state_path = settings()
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Telegram account is already authorized.")
            return
        sent = await client.send_code_request(phone)
        write_state(state_path, {"phone": phone, "phone_code_hash": sent.phone_code_hash})
        print("Telegram code requested. Enter it with: python telegram_auth.py complete-code --code <code>")
    finally:
        await client.disconnect()


async def complete_code(code: str) -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    api_id, api_hash, phone, session_path, state_path = settings()
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        try:
            await client.sign_in(state.get("phone", phone), code, phone_code_hash=state["phone_code_hash"])
        except SessionPasswordNeededError as exc:
            raise RuntimeError("Telegram two-step verification is enabled; run complete-code again with --password") from exc
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram did not authorize the account")
        Path(state_path).unlink(missing_ok=True)
        print("Telegram account authorization completed.")
    finally:
        await client.disconnect()


async def complete_password(password: str) -> None:
    from telethon import TelegramClient

    api_id, api_hash, _, session_path, state_path = settings()
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        await client.sign_in(password=password)
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram did not authorize the account")
        Path(state_path).unlink(missing_ok=True)
        print("Telegram account authorization completed.")
    finally:
        await client.disconnect()


async def list_dialogs() -> None:
    from telethon import TelegramClient

    api_id, api_hash, _, session_path, _ = settings()
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram account is not authorized")
        async for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                print(f"{dialog.name}\t{dialog.id}")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("request-code")
    complete = sub.add_parser("complete-code")
    complete.add_argument("--code")
    complete.add_argument("--password")
    sub.add_parser("list-dialogs")
    args = parser.parse_args()
    if args.command == "request-code":
        asyncio.run(request_code())
    elif args.command == "complete-code":
        if not args.password and not args.code:
            parser.error("complete-code requires --code or --password")
        asyncio.run(complete_password(args.password) if args.password else complete_code(args.code))
    else:
        asyncio.run(list_dialogs())


if __name__ == "__main__":
    main()
