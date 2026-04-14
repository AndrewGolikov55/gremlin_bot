"""OpenAI Whisper integration for voice transcription.

Standalone module: takes a Telegram file_id, downloads via aiogram,
posts to /v1/audio/transcriptions, returns text or None on any failure.
Reuses the same httpx client kwargs (timeout, proxy) as the chat
completion client. No fallback to other providers — Whisper is
OpenAI-only.
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from typing import Any, cast

import httpx
from aiogram import Bot

from ...utils.proxy import httpx_client_kwargs

logger = logging.getLogger(__name__)

WHISPER_API_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE")  # optional hint
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

WHISPER_FILE_HARD_LIMIT_BYTES = 25 * 1024 * 1024  # OpenAI hard limit


@dataclass
class TranscriptionResult:
    text: str
    duration_seconds: float


async def transcribe_file_id(
    bot: Bot,
    file_id: str,
    *,
    language: str | None = None,
    max_seconds: int = 0,
    duration_hint: float | None = None,
) -> TranscriptionResult | None:
    """Download a Telegram file and transcribe via OpenAI Whisper.

    Returns None on:
    - duration_hint > max_seconds (when max_seconds > 0)
    - downloaded file > 25 MiB (Whisper hard limit)
    - network/HTTP error from Whisper
    - empty transcript text
    """
    if max_seconds > 0 and duration_hint is not None and duration_hint > max_seconds:
        logger.info(
            "Skip whisper: duration %.0fs > limit %ds (file_id=%s)",
            duration_hint, max_seconds, file_id,
        )
        return None

    if not OPENAI_API_KEY:
        logger.warning("Skip whisper: OPENAI_API_KEY not set")
        return None

    try:
        tg_file = await bot.get_file(file_id)
    except Exception:
        logger.exception("Whisper: bot.get_file failed (file_id=%s)", file_id)
        return None

    file_path = getattr(tg_file, "file_path", None)
    if not file_path:
        logger.warning("Whisper: empty file_path from get_file (file_id=%s)", file_id)
        return None

    buffer = io.BytesIO()
    try:
        await bot.download_file(file_path, destination=buffer)
    except Exception:
        logger.exception("Whisper: bot.download_file failed (file_id=%s)", file_id)
        return None

    payload = buffer.getvalue()
    if len(payload) > WHISPER_FILE_HARD_LIMIT_BYTES:
        logger.info(
            "Skip whisper: file %d bytes > %d limit (file_id=%s)",
            len(payload), WHISPER_FILE_HARD_LIMIT_BYTES, file_id,
        )
        return None

    chosen_language = language if language is not None else WHISPER_LANGUAGE

    try:
        response = await _post_audio(payload, model=WHISPER_MODEL, language=chosen_language)
    except Exception:
        logger.exception("Whisper: HTTP call failed (file_id=%s)", file_id)
        return None

    if response.status_code >= 500:
        logger.warning("Whisper: 5xx %d (file_id=%s)", response.status_code, file_id)
        return None
    if response.status_code >= 400:
        logger.warning(
            "Whisper: %d %s (file_id=%s)",
            response.status_code, response.text[:200], file_id,
        )
        return None

    try:
        body = response.json()
    except Exception:
        logger.exception("Whisper: invalid JSON response (file_id=%s)", file_id)
        return None

    text = str(body.get("text") or "").strip()
    if not text:
        logger.info("Whisper: empty transcript (file_id=%s)", file_id)
        return None

    return TranscriptionResult(text=text, duration_seconds=duration_hint or 0.0)


async def _post_audio(
    payload_bytes: bytes,
    *,
    model: str,
    language: str | None,
) -> httpx.Response:
    """POST audio bytes to Whisper API as multipart/form-data."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {"file": ("audio.bin", payload_bytes, "application/octet-stream")}
    data: dict[str, str] = {"model": model}
    if language:
        data["language"] = language

    client_kwargs = cast(dict[str, Any], httpx_client_kwargs(timeout=120))
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await client.post(WHISPER_API_URL, headers=headers, files=files, data=data)
