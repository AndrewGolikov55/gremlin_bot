from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm.client import (
    FALLBACK_MAP,
    LLMError,
    LLMRateLimitError,
    generate_with_fallback,
)


MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "hi"},
]


def test_fallback_map_is_symmetric() -> None:
    assert FALLBACK_MAP == {"openrouter": "openai", "openai": "openrouter"}


@pytest.mark.asyncio
async def test_primary_success_returns_without_fallback() -> None:
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(return_value="ok"),
    ) as mock_generate, patch(
        "app.services.llm.client.OPENAI_API_KEY", "stub"
    ):
        result = await generate_with_fallback(MESSAGES, primary="openrouter")

    assert result == "ok"
    assert mock_generate.await_count == 1
    assert mock_generate.await_args.kwargs["provider"] == "openrouter"


@pytest.mark.asyncio
async def test_rate_limit_triggers_fallback() -> None:
    side_effects = [LLMRateLimitError("429", retry_after=1.0), "fallback-ok"]
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(side_effect=side_effects),
    ) as mock_generate, patch(
        "app.services.llm.client.OPENAI_API_KEY", "stub"
    ):
        result = await generate_with_fallback(MESSAGES, primary="openrouter")

    assert result == "fallback-ok"
    assert mock_generate.await_count == 2
    assert mock_generate.await_args_list[0].kwargs["provider"] == "openrouter"
    assert mock_generate.await_args_list[1].kwargs["provider"] == "openai"


@pytest.mark.asyncio
async def test_5xx_triggers_fallback() -> None:
    side_effects = [LLMError("upstream 503", status_code=503), "fallback-ok"]
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(side_effect=side_effects),
    ) as mock_generate, patch(
        "app.services.llm.client.OPENAI_API_KEY", "stub"
    ):
        result = await generate_with_fallback(MESSAGES, primary="openrouter")

    assert result == "fallback-ok"
    assert mock_generate.await_count == 2


@pytest.mark.asyncio
async def test_400_does_not_trigger_fallback() -> None:
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(side_effect=LLMError("bad request", status_code=400)),
    ) as mock_generate, patch(
        "app.services.llm.client.OPENAI_API_KEY", "stub"
    ):
        with pytest.raises(LLMError) as excinfo:
            await generate_with_fallback(MESSAGES, primary="openrouter")

    assert excinfo.value.status_code == 400
    assert mock_generate.await_count == 1


@pytest.mark.asyncio
async def test_network_error_without_status_does_not_trigger_fallback() -> None:
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(side_effect=LLMError("network")),
    ) as mock_generate, patch(
        "app.services.llm.client.OPENAI_API_KEY", "stub"
    ):
        with pytest.raises(LLMError):
            await generate_with_fallback(MESSAGES, primary="openrouter")

    assert mock_generate.await_count == 1


@pytest.mark.asyncio
async def test_missing_fallback_key_propagates_original() -> None:
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(side_effect=LLMRateLimitError("429")),
    ) as mock_generate, patch(
        "app.services.llm.client.OPENAI_API_KEY", ""
    ):
        with pytest.raises(LLMRateLimitError):
            await generate_with_fallback(MESSAGES, primary="openrouter")

    assert mock_generate.await_count == 1


@pytest.mark.asyncio
async def test_symmetric_openai_to_openrouter_on_rate_limit() -> None:
    side_effects = [LLMRateLimitError("429"), "ok"]
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(side_effect=side_effects),
    ) as mock_generate, patch(
        "app.services.llm.client.OPENROUTER_API_KEY", "stub"
    ):
        result = await generate_with_fallback(MESSAGES, primary="openai")

    assert result == "ok"
    assert mock_generate.await_args_list[0].kwargs["provider"] == "openai"
    assert mock_generate.await_args_list[1].kwargs["provider"] == "openrouter"


@pytest.mark.asyncio
async def test_fallback_failure_raises_fallback_error() -> None:
    primary_exc = LLMRateLimitError("primary 429")
    fallback_exc = LLMError("fallback upstream 502", status_code=502)
    with patch(
        "app.services.llm.client.generate",
        new=AsyncMock(side_effect=[primary_exc, fallback_exc]),
    ), patch(
        "app.services.llm.client.OPENAI_API_KEY", "stub"
    ):
        with pytest.raises(LLMError) as excinfo:
            await generate_with_fallback(MESSAGES, primary="openrouter")

    assert excinfo.value is fallback_exc
