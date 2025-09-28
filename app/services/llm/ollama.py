from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional

import httpx


OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
)
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_APP_URL = os.getenv("OPENROUTER_APP_URL", "https://gremlin.example")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "GremlinBot")


class OpenRouterError(RuntimeError):
    pass


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

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_APP_URL,
        "X-Title": OPENROUTER_APP_NAME,
    }

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, headers=headers, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenRouterError(
                f"OpenRouter request failed: {exc.response.status_code} {exc.response.text}"
            ) from exc

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        raise OpenRouterError(f"Unexpected OpenRouter response: {data}") from exc
