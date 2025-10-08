from __future__ import annotations

import os
import json
from typing import Iterable, Mapping, Optional

import httpx
import logging
from math import inf
from ...utils.logging import TRACE_LEVEL


logger = logging.getLogger(__name__)


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
)
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_APP_URL = os.getenv("OPENROUTER_APP_URL", "https://gremlin.example")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "GremlinBot")


class OpenRouterError(RuntimeError):
    """Базовая ошибка общения с OpenRouter."""


class OpenRouterRateLimitError(OpenRouterError):
    """OpenRouter вернул 429 Too Many Requests."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def generate(
    messages: Iterable[Mapping[str, str]],
    *,
    temperature: float = 0.8,
    top_p: float = 0.9,
    max_tokens: Optional[int] = None,
) -> str:
    if not OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")

    payload: dict[str, object] = {
        "model": OPENROUTER_MODEL,
        "messages": list(messages),
        "temperature": temperature,
        "top_p": top_p,
    }
    if max_tokens and max_tokens > 0:
        payload["max_tokens"] = max_tokens

    if logger.isEnabledFor(TRACE_LEVEL):
        try:
            logger.log(TRACE_LEVEL, "LLM request payload: %s", json.dumps(payload, ensure_ascii=False))
        except Exception:
            logger.log(TRACE_LEVEL, "LLM request payload (repr): %r", payload)

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_APP_URL,
        "X-Title": OPENROUTER_APP_NAME,
    }

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            text = exc.response.text
            if status == 429:
                retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
                logger.warning(
                    "OpenRouter rate limit hit (retry_after=%s, body=%s)",
                    retry_after,
                    text,
                )
                raise OpenRouterRateLimitError(text or "rate limit", retry_after=retry_after) from exc
            logger.error(
                "OpenRouter request failed status=%s body=%s", status, text
            )
            raise OpenRouterError(
                f"OpenRouter request failed: {status} {text}"
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception("OpenRouter network error: %s", exc)
            raise OpenRouterError(f"OpenRouter network error: {exc}") from exc

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        raise OpenRouterError(f"Unexpected OpenRouter response: {data}") from exc
