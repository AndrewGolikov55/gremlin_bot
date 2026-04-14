"""OpenAI TTS (/v1/audio/speech) integration.

Returns OGG/Opus bytes ready for Telegram bot.send_voice.
No fallback to other providers (OpenRouter doesn't support TTS).
On any failure returns None — caller decides behavior (fallback to text).
"""
from __future__ import annotations

import logging
import os
from typing import Any, cast

import httpx

from ...utils.proxy import httpx_client_kwargs

logger = logging.getLogger(__name__)

TTS_API_URL = "https://api.openai.com/v1/audio/speech"
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

OPENAI_TTS_VOICES: tuple[str, ...] = (
    "alloy", "echo", "fable", "onyx", "nova", "shimmer",
)

PERSONA_TTS_INSTRUCTIONS: dict[str, str] = {
    "gopnik": "Говори грубо, с характерной интонацией дворового парня. Коротко, в лоб.",
    "chatmate": "Говори дружелюбно, как близкий приятель в чате.",
    "standup": "Говори с лёгкой подколкой, как стенд-ап комик на сцене.",
    "boss": "Говори уверенно, авторитетно, командным тоном.",
    "zoomer": "Говори быстро, непринуждённо, с зумер-энергией.",
    "jarvis": "Говори спокойно, вежливо, как британский дворецкий.",
}


async def synthesize_speech(
    text: str,
    *,
    voice: str,
    instructions: str | None = None,
) -> bytes | None:
    """Call OpenAI TTS, return OGG/Opus bytes or None on failure."""
    if not OPENAI_API_KEY:
        logger.warning("Skip TTS: OPENAI_API_KEY not set")
        return None
    if not text.strip():
        logger.info("Skip TTS: empty text")
        return None

    body: dict[str, Any] = {
        "model": TTS_MODEL,
        "input": text,
        "voice": voice,
        "response_format": "opus",
    }
    if instructions:
        body["instructions"] = instructions

    try:
        response = await _post_speech(body)
    except Exception:
        logger.exception("TTS: HTTP call failed (voice=%s)", voice)
        return None

    if response.status_code >= 500:
        logger.warning("TTS: 5xx %d (voice=%s)", response.status_code, voice)
        return None
    if response.status_code >= 400:
        logger.warning(
            "TTS: %d %s (voice=%s)",
            response.status_code, response.text[:200], voice,
        )
        return None

    content = response.content
    if not content:
        logger.info("TTS: empty audio body (voice=%s)", voice)
        return None

    return content


async def _post_speech(body: dict[str, Any]) -> httpx.Response:
    """POST JSON body to OpenAI TTS endpoint."""
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    client_kwargs = cast(dict[str, Any], httpx_client_kwargs(timeout=60))
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await client.post(TTS_API_URL, headers=headers, json=body)
