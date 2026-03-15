from __future__ import annotations

import json
import logging
import os
from typing import Iterable, Mapping, Sequence

import httpx

from ...utils.logging import TRACE_LEVEL
from ...utils.proxy import get_proxy_display, httpx_client_kwargs


logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "openrouter"
VALID_PROVIDERS = {"openrouter", "openai"}

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
)
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_APP_URL = os.getenv("OPENROUTER_APP_URL", "https://gremlin.example")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "GremlinBot")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
OPENAI_ORG = os.getenv("OPENAI_ORG")
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT")

CENSORSHIP_MARKERS = [
    "i'm sorry, but i can't help with that.",
    "i'm sorry, but i can't help with that",
    "i'm sorry, but i can't comply with that.",
    "i'm sorry, but i can't comply with that",
    "извини, но я не могу помочь с этим.",
    "извини, но я не могу помочь с этим",
]


class LLMError(RuntimeError):
    """Base error raised while talking to an LLM provider."""


class LLMRateLimitError(LLMError):
    """Raised when a provider returns 429 Too Many Requests."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def _normalize_provider(provider: str | None) -> str:
    if not provider:
        return DEFAULT_PROVIDER
    value = provider.strip().lower()
    if value not in VALID_PROVIDERS:
        return DEFAULT_PROVIDER
    return value


def _log_payload(label: str, payload: dict[str, object]) -> None:
    if not logger.isEnabledFor(TRACE_LEVEL):
        return
    try:
        serialized = json.dumps(payload, ensure_ascii=False)
        logger.log(TRACE_LEVEL, "%s payload: %s", label, serialized)
    except Exception:
        logger.log(TRACE_LEVEL, "%s payload (repr): %r", label, payload)


def _log_response(label: str, payload: object) -> None:
    if not logger.isEnabledFor(TRACE_LEVEL):
        return
    try:
        serialized = json.dumps(payload, ensure_ascii=False)
        logger.log(TRACE_LEVEL, "%s response: %s", label, serialized)
    except Exception:
        logger.log(TRACE_LEVEL, "%s response (repr): %r", label, payload)


def _log_content(label: str, content: str) -> None:
    if logger.isEnabledFor(TRACE_LEVEL):
        logger.log(TRACE_LEVEL, "%s content: %s", label, content)


def _looks_censored(text: str) -> bool:
    if not text:
        return False
    normalized = text.strip().lower().replace("’", "'")
    normalized = " ".join(normalized.split())
    return any(marker in normalized for marker in CENSORSHIP_MARKERS)


def _build_openrouter_payload(
    message_list: list[Mapping[str, object]],
    *,
    temperature: float,
    top_p: float,
    max_tokens: int | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": OPENROUTER_MODEL,
        "messages": message_list,
        "temperature": temperature,
        "top_p": top_p,
    }
    if max_tokens and max_tokens > 0:
        payload["max_tokens"] = max_tokens
    return payload


def _build_openai_payload(
    message_list: list[Mapping[str, object]],
    *,
    temperature: float,
    max_tokens: int | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": OPENAI_MODEL,
        "messages": message_list,
        "temperature": temperature,
    }
    if max_tokens and max_tokens > 0:
        payload["max_completion_tokens"] = max_tokens
    return payload


async def _post_json(
    *,
    label: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
) -> dict[str, object]:
    _log_payload(label, payload)

    client_kwargs = httpx_client_kwargs(timeout=60)
    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            text = exc.response.text
            if status == 429:
                retry_after = _parse_retry_after(
                    exc.response.headers.get("Retry-After")
                )
                logger.warning(
                    "%s rate limit hit (retry_after=%s, body=%s)",
                    label,
                    retry_after,
                    text,
                )
                raise LLMRateLimitError(
                    text or "rate limit",
                    retry_after=retry_after,
                ) from exc
            logger.error("%s request failed status=%s body=%s", label, status, text)
            raise LLMError(f"{label} request failed: {status} {text}") from exc
        except httpx.HTTPError as exc:
            proxy_hint = get_proxy_display()
            if proxy_hint:
                logger.exception("%s network error via %s: %s", label, proxy_hint, exc)
            else:
                logger.exception("%s network error: %s", label, exc)
            raise LLMError(f"{label} network error: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        logger.exception("Failed to decode %s response: %s", label, exc)
        raise LLMError(f"{label} returned invalid JSON") from exc

    _log_response(label, data)
    return data


def _extract_openrouter_content(data: Mapping[str, object]) -> str:
    try:
        content = _flatten_message_content(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected OpenRouter response: {data}") from exc

    _log_content("OpenRouter", content)
    return content


def _extract_openai_content_meta(data: Mapping[str, object]) -> tuple[str, str]:
    try:
        choice = data["choices"][0]
        message = choice.get("message", {})
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError(f"Unexpected OpenAI response: {data}") from exc

    content = _flatten_message_content(message.get("content"))
    finish_reason = str(choice.get("finish_reason") or "").strip().lower()
    _log_content("OpenAI", content)

    if not content:
        logger.warning(
            "OpenAI returned empty content (finish_reason=%s, usage=%s, filters=%s)",
            choice.get("finish_reason"),
            data.get("usage"),
            choice.get("content_filter_results"),
        )
    return content, finish_reason


def _extract_openai_content(data: Mapping[str, object]) -> str:
    content, _finish_reason = _extract_openai_content_meta(data)
    return content


def _flatten_message_content(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray, str)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


async def _generate_openrouter(
    message_list: list[Mapping[str, object]],
    *,
    temperature: float,
    top_p: float,
    max_tokens: int | None,
) -> str:
    if not OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY is not set")

    payload = _build_openrouter_payload(
        message_list,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
    )
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_APP_URL,
        "X-Title": OPENROUTER_APP_NAME,
    }
    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"

    data = await _post_json(
        label="OpenRouter",
        url=url,
        headers=headers,
        payload=payload,
    )
    return _extract_openrouter_content(data)


async def _generate_openai(
    message_list: list[Mapping[str, object]],
    *,
    temperature: float,
    max_tokens: int | None,
) -> str:
    if not OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY is not set")

    payload = _build_openai_payload(
        message_list,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENAI_ORG:
        headers["OpenAI-Organization"] = OPENAI_ORG
    if OPENAI_PROJECT:
        headers["OpenAI-Project"] = OPENAI_PROJECT

    url = f"{OPENAI_API_BASE.rstrip('/')}/chat/completions"
    data = await _post_json(
        label="OpenAI",
        url=url,
        headers=headers,
        payload=payload,
    )
    content, finish_reason = _extract_openai_content_meta(data)
    if not content and finish_reason == "length" and max_tokens and 0 < max_tokens < 128:
        retry_max_tokens = min(512, max(128, max_tokens * 4))
        logger.info(
            "OpenAI returned empty content with finish_reason=length and max_completion_tokens=%s; retrying with %s",
            max_tokens,
            retry_max_tokens,
        )
        retry_payload = _build_openai_payload(
            message_list,
            temperature=temperature,
            max_tokens=retry_max_tokens,
        )
        retry_data = await _post_json(
            label="OpenAI",
            url=url,
            headers=headers,
            payload=retry_payload,
        )
        return _extract_openai_content(retry_data)
    return content


async def generate(
    messages: Iterable[Mapping[str, object]],
    *,
    temperature: float = 1.0,
    top_p: float = 0.9,
    max_tokens: int | None = None,
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

    if (
        fallback_active
        and provider_name == "openai"
        and _looks_censored(primary_response)
    ):
        logger.info(
            "Detected possible OpenAI censorship, falling back to OpenRouter model %s",
            OPENROUTER_MODEL,
        )
        return await _call("openrouter")

    return primary_response


def resolve_llm_options(conf: Mapping[str, object] | None) -> tuple[str, bool]:
    provider_raw: str | None = None
    fallback_enabled = False

    if conf:
        raw_value = conf.get("llm_provider")
        if isinstance(raw_value, str):
            provider_raw = raw_value
        fallback_enabled = bool(conf.get("llm_openai_censorship_fallback", False))

    provider = _normalize_provider(provider_raw)
    return provider, fallback_enabled
