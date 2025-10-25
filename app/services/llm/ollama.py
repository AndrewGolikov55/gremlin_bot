from __future__ import annotations

import json
import logging
import os
from typing import Iterable, Mapping, Optional

import httpx

from ...utils.logging import TRACE_LEVEL
from ...utils.proxy import get_proxy_display, httpx_client_kwargs


logger = logging.getLogger(__name__)


# OpenRouter configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
)
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_APP_URL = os.getenv("OPENROUTER_APP_URL", "https://gremlin.example")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "GremlinBot")

# OpenAI configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
OPENAI_ORG = os.getenv("OPENAI_ORG")
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT")

VALID_PROVIDERS = {"openrouter", "openai"}
CENSORSHIP_MARKERS = [
    "i'm sorry, but i can't help with that.",
    "i'm sorry, but i can't help with that",
    "i'm sorry, but i can't comply with that.",
    "i'm sorry, but i can't comply with that",
    "извини, но я не могу помочь с этим.",
    "извини, но я не могу помочь с этим",
]


class LLMError(RuntimeError):
    """Базовая ошибка общения с LLM."""


class LLMRateLimitError(LLMError):
    """Провайдер вернул 429 Too Many Requests."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Backwards compatibility with existing imports.
OpenRouterError = LLMError
OpenRouterRateLimitError = LLMRateLimitError


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


def _normalize_provider(provider: str | None) -> str:
    if not provider:
        return "openrouter"
    value = provider.strip().lower()
    if value not in VALID_PROVIDERS:
        return "openrouter"
    return value


def _log_payload(label: str, payload: dict[str, object]) -> None:
    if not logger.isEnabledFor(TRACE_LEVEL):
        return
    try:
        logger.log(TRACE_LEVEL, "%s payload: %s", label, json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.log(TRACE_LEVEL, "%s payload (repr): %r", label, payload)


def _looks_censored(text: str) -> bool:
    if not text:
        return False
    normalized = text.strip().lower().replace("’", "'")
    normalized = " ".join(normalized.split())
    return any(marker in normalized for marker in CENSORSHIP_MARKERS)


async def _generate_openrouter(
    message_list: list[Mapping[str, object]],
    *,
    temperature: float,
    top_p: float,
    max_tokens: Optional[int],
) -> str:
    if not OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY is not set")

    payload: dict[str, object] = {
        "model": OPENROUTER_MODEL,
        "messages": message_list,
        "temperature": temperature,
        "top_p": top_p,
    }
    if max_tokens and max_tokens > 0:
        payload["max_tokens"] = max_tokens

    _log_payload("OpenRouter", payload)

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_APP_URL,
        "X-Title": OPENROUTER_APP_NAME,
    }

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    client_kwargs = httpx_client_kwargs(timeout=60)
    async with httpx.AsyncClient(**client_kwargs) as client:
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
                raise LLMRateLimitError(text or "rate limit", retry_after=retry_after) from exc
            logger.error("OpenRouter request failed status=%s body=%s", status, text)
            raise LLMError(f"OpenRouter request failed: {status} {text}") from exc
        except httpx.HTTPError as exc:
            proxy_hint = get_proxy_display()
            if proxy_hint:
                logger.exception("OpenRouter network error via %s: %s", proxy_hint, exc)
            else:
                logger.exception("OpenRouter network error: %s", exc)
            raise LLMError(f"OpenRouter network error: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        logger.exception("Failed to decode OpenRouter response: %s", exc)
        raise LLMError("OpenRouter returned invalid JSON") from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        raise LLMError(f"Unexpected OpenRouter response: {data}") from exc


async def _generate_openai(
    message_list: list[Mapping[str, object]],
    *,
    temperature: float,
    max_tokens: Optional[int],
) -> str:
    if not OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY is not set")

    payload: dict[str, object] = {
        "model": OPENAI_MODEL,
        "messages": message_list,
        "temperature": temperature,
    }
    if max_tokens and max_tokens > 0:
        payload["max_completion_tokens"] = max_tokens

    _log_payload("OpenAI", payload)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENAI_ORG:
        headers["OpenAI-Organization"] = OPENAI_ORG
    if OPENAI_PROJECT:
        headers["OpenAI-Project"] = OPENAI_PROJECT

    url = f"{OPENAI_API_BASE.rstrip('/')}/chat/completions"
    client_kwargs = httpx_client_kwargs(timeout=60)

    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            text = exc.response.text
            if status == 429:
                retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
                logger.warning(
                    "OpenAI rate limit hit (retry_after=%s, body=%s)",
                    retry_after,
                    text,
                )
                raise LLMRateLimitError(text or "rate limit", retry_after=retry_after) from exc
            logger.error("OpenAI request failed status=%s body=%s", status, text)
            raise LLMError(f"OpenAI request failed: {status} {text}") from exc
        except httpx.HTTPError as exc:
            proxy_hint = get_proxy_display()
            if proxy_hint:
                logger.exception("OpenAI network error via %s: %s", proxy_hint, exc)
            else:
                logger.exception("OpenAI network error: %s", exc)
            raise LLMError(f"OpenAI network error: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        logger.exception("Failed to decode OpenAI response: %s", exc)
        raise LLMError("OpenAI returned invalid JSON") from exc

    try:
        choice = data["choices"][0]
        message = choice.get("message", {})
        content = (message.get("content") or "").strip()
        finish_reason = choice.get("finish_reason")
        if not content:
            logger.warning(
                "OpenAI returned empty content (finish_reason=%s, usage=%s, filters=%s)",
                finish_reason,
                data.get("usage"),
                choice.get("content_filter_results"),
            )
        return content
    except (KeyError, IndexError, AttributeError) as exc:
        raise LLMError(f"Unexpected OpenAI response: {data}") from exc


async def generate(
    messages: Iterable[Mapping[str, object]],
    *,
    temperature: float = 1.0,
    top_p: float = 0.9,
    max_tokens: Optional[int] = None,
    provider: str | None = None,
    fallback_enabled: bool | None = None,
) -> str:
    message_list = list(messages)
    provider_name = _normalize_provider(provider)
    fallback_active = bool(fallback_enabled)
    fallback_provider = "openai" if provider_name == "openrouter" else "openrouter"

    async def _call(target: str) -> str:
        if target == "openai":
            return await _generate_openai(
                message_list,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return await _generate_openrouter(
            message_list,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )

    try:
        primary_response = await _call(provider_name)
    except LLMRateLimitError as exc:
        if fallback_active:
            logger.info(
                "Primary provider %s hit rate limit (%s); attempting fallback to %s",
                provider_name,
                exc,
                fallback_provider,
            )
            return await _call(fallback_provider)
        raise
    except LLMError as exc:
        if fallback_active:
            logger.warning(
                "Primary provider %s failed (%s); attempting fallback to %s",
                provider_name,
                exc,
                fallback_provider,
            )
            return await _call(fallback_provider)
        raise

    if fallback_active and not (primary_response or "").strip():
        logger.info(
            "Primary provider %s returned empty response; attempting fallback to %s",
            provider_name,
            fallback_provider,
        )
        return await _call(fallback_provider)

    if fallback_active and provider_name == "openai" and _looks_censored(primary_response):
        logger.info(
            "Detected possible OpenAI censorship, falling back to OpenRouter model %s",
            OPENROUTER_MODEL,
        )
        return await _call("openrouter")

    return primary_response


def resolve_llm_options(conf: Mapping[str, object] | None) -> tuple[str, bool]:
    provider_raw: str | None = None
    fallback = False

    if conf:
        raw_value = conf.get("llm_provider")
        if isinstance(raw_value, str):
            provider_raw = raw_value
        fallback = bool(conf.get("llm_openai_censorship_fallback", False))

    provider = _normalize_provider(provider_raw)
    return provider, fallback
