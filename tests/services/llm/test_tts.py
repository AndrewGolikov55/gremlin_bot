from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _stub_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.llm.tts.OPENAI_API_KEY", "test-key")


def _make_response(status_code: int, content: bytes = b"") -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=content,
        request=httpx.Request("POST", "https://api.openai.com/v1/audio/speech"),
    )


@pytest.mark.asyncio
async def test_returns_audio_bytes_on_success() -> None:
    from app.services.llm.tts import synthesize_speech

    fake_opus = b"\x4f\x67\x67\x53fake-opus-bytes"
    response = _make_response(200, content=fake_opus)
    with patch("app.services.llm.tts._post_speech", new=AsyncMock(return_value=response)):
        result = await synthesize_speech("Привет", voice="onyx")
    assert result == fake_opus


@pytest.mark.asyncio
async def test_voice_parameter_passed_to_api() -> None:
    from app.services.llm.tts import synthesize_speech

    captured: dict[str, Any] = {}

    async def capture(body: dict[str, Any]) -> httpx.Response:
        captured.update(body)
        return _make_response(200, content=b"ok")

    with patch("app.services.llm.tts._post_speech", new=capture):
        await synthesize_speech("text", voice="nova")

    assert captured["voice"] == "nova"
    assert captured["input"] == "text"
    assert captured["response_format"] == "opus"


@pytest.mark.asyncio
async def test_instructions_omitted_when_none() -> None:
    from app.services.llm.tts import synthesize_speech

    captured: dict[str, Any] = {}

    async def capture(body: dict[str, Any]) -> httpx.Response:
        captured.update(body)
        return _make_response(200, content=b"ok")

    with patch("app.services.llm.tts._post_speech", new=capture):
        await synthesize_speech("text", voice="alloy", instructions=None)

    assert "instructions" not in captured


@pytest.mark.asyncio
async def test_instructions_included_when_given() -> None:
    from app.services.llm.tts import synthesize_speech

    captured: dict[str, Any] = {}

    async def capture(body: dict[str, Any]) -> httpx.Response:
        captured.update(body)
        return _make_response(200, content=b"ok")

    with patch("app.services.llm.tts._post_speech", new=capture):
        await synthesize_speech("text", voice="onyx", instructions="говори грубо")

    assert captured["instructions"] == "говори грубо"


@pytest.mark.asyncio
async def test_returns_none_on_5xx() -> None:
    from app.services.llm.tts import synthesize_speech

    response = _make_response(503)
    with patch("app.services.llm.tts._post_speech", new=AsyncMock(return_value=response)):
        result = await synthesize_speech("text", voice="onyx")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_4xx() -> None:
    from app.services.llm.tts import synthesize_speech

    response = _make_response(400)
    with patch("app.services.llm.tts._post_speech", new=AsyncMock(return_value=response)):
        result = await synthesize_speech("text", voice="onyx")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_empty_body() -> None:
    from app.services.llm.tts import synthesize_speech

    response = _make_response(200, content=b"")
    with patch("app.services.llm.tts._post_speech", new=AsyncMock(return_value=response)):
        result = await synthesize_speech("text", voice="onyx")
    assert result is None


def test_persona_tts_instructions_covers_all_personas() -> None:
    from app.services.llm.tts import PERSONA_TTS_INSTRUCTIONS

    assert set(PERSONA_TTS_INSTRUCTIONS.keys()) == {
        "gopnik", "chatmate", "standup", "boss", "zoomer", "jarvis",
    }


def test_openai_tts_voices_has_six_classic() -> None:
    from app.services.llm.tts import OPENAI_TTS_VOICES

    assert set(OPENAI_TTS_VOICES) == {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
