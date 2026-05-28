#!/usr/bin/env python
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.spy.config import SpyConfig  # noqa: E402
from app.services.spy.readers.telethon import TelethonChannelReader  # noqa: E402

DEFAULT_CHANNEL = "https://t.me/gospodindirectorpivs"


def _safe_config_summary(config: SpyConfig) -> dict[str, object]:
    return {
        "enabled": config.enabled,
        "poll_seconds": config.poll_seconds,
        "batch_limit": config.batch_limit,
        "max_image_bytes": config.max_image_bytes,
        "telegram_api_id_configured": config.telegram_api_id is not None,
        "telegram_api_hash_configured": bool(config.telegram_api_hash),
        "telegram_session_configured": bool(config.telegram_session),
        "telegram_session_file_configured": bool(config.telegram_session_file),
    }


def _has_credentials(config: SpyConfig) -> bool:
    return bool(config.telegram_api_id and config.telegram_api_hash)


async def _probe(channel: str, limit: int, *, check_config_only: bool) -> int:
    config = SpyConfig.from_env()
    print(json.dumps({"config": _safe_config_summary(config)}, ensure_ascii=False))
    if check_config_only:
        return 0
    if not _has_credentials(config):
        print(json.dumps({"ok": False, "error": "Telegram API credentials are not configured"}, ensure_ascii=False))
        return 2

    try:
        from telethon import TelegramClient  # type: ignore[import-not-found]
        from telethon.sessions import StringSession  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        print(json.dumps({"ok": False, "error": "Telethon is not installed"}, ensure_ascii=False))
        return 2

    session = StringSession(config.telegram_session) if config.telegram_session else config.telegram_session_file
    client = TelegramClient(session, config.telegram_api_id, config.telegram_api_hash)
    async with client:
        reader = TelethonChannelReader(client)
        info = await reader.resolve_channel(channel)
        posts = await reader.fetch_latest_posts(info.username, limit=limit)
    payload = {
        "ok": True,
        "channel": {
            "username": info.username,
            "title": info.title,
            "telegram_channel_id": info.telegram_channel_id,
            "access_mode": info.access_mode,
            "public_url": info.public_url,
        },
        "posts": [
            {
                "external_post_id": post.external_post_id,
                "text_present": bool(post.text),
                "text_preview": (post.text or "")[:160],
                "source_url": post.source_url,
                "published_at": post.published_at.isoformat() if post.published_at else None,
                "media_kinds": [media.kind for media in post.media],
                "image_media": [media.kind for media in post.media if media.is_image],
            }
            for post in posts
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe Gremlin Spy public-channel probe")
    parser.add_argument("channel", nargs="?", default=DEFAULT_CHANNEL)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--check-config-only",
        action="store_true",
        help="Print redacted config summary and skip Telegram network calls",
    )
    args = parser.parse_args()
    return asyncio.run(_probe(args.channel, max(1, min(args.limit, 20)), check_config_only=args.check_config_only))


if __name__ == "__main__":
    raise SystemExit(main())
