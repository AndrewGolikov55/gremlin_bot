from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpyConfig:
    enabled: bool
    poll_seconds: int
    batch_limit: int
    max_image_bytes: int
    telegram_api_id: int | None
    telegram_api_hash: str | None
    telegram_session: str | None
    telegram_session_file: str

    @classmethod
    def from_env(cls) -> "SpyConfig":
        return cls(
            enabled=_env_bool("SPY_ENABLED", True),
            poll_seconds=_env_int("SPY_POLL_SECONDS", 60),
            batch_limit=_env_int("SPY_BATCH_LIMIT", 20),
            max_image_bytes=_env_int("SPY_MAX_IMAGE_BYTES", 8 * 1024 * 1024),
            telegram_api_id=_env_optional_int("TELEGRAM_API_ID"),
            telegram_api_hash=_env_optional_str("TELEGRAM_API_HASH"),
            telegram_session=_env_optional_str("TELEGRAM_SESSION"),
            telegram_session_file=_env_optional_str("TELEGRAM_SESSION_FILE") or "/app/data/telethon.session",
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return int(raw)


def _env_optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw
