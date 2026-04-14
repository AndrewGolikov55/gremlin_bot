from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.llm.whisper import TranscriptionResult, transcribe_file_id


@pytest.fixture(autouse=True)
def _stub_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.llm.whisper.OPENAI_API_KEY", "test-key")


class _FakeBot:
    def __init__(self, file_path: str | None, payload: bytes) -> None:
        self._file_path = file_path
        self._payload = payload
        self.get_file = AsyncMock(return_value=SimpleNamespace(file_path=file_path))

    async def download_file(self, file_path: str, destination: io.BytesIO) -> None:
        assert file_path == self._file_path
        destination.write(self._payload)


def _make_response(status_code: int, json_body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body if json_body is not None else {},
        request=httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
    )


@pytest.mark.asyncio
async def test_successful_transcription_returns_text() -> None:
    bot = _FakeBot(file_path="voice/abc.oga", payload=b"\x00\x01\x02fake-opus")
    response = _make_response(200, {"text": "привет, как дела"})
    with patch("app.services.llm.whisper._post_audio", new=AsyncMock(return_value=response)):
        result = await transcribe_file_id(bot, "file-id", duration_hint=15.0)  # type: ignore[arg-type]
    assert isinstance(result, TranscriptionResult)
    assert result.text == "привет, как дела"
    assert result.duration_seconds == 15.0


@pytest.mark.asyncio
async def test_returns_none_when_duration_exceeds_max() -> None:
    bot = _FakeBot(file_path="voice/abc.oga", payload=b"")
    with patch("app.services.llm.whisper._post_audio", new=AsyncMock()) as mock_post:
        result = await transcribe_file_id(
            bot, "file-id",  # type: ignore[arg-type]
            max_seconds=60,
            duration_hint=120.0,
        )
    assert result is None
    bot.get_file.assert_not_awaited()
    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_none_when_file_too_large() -> None:
    huge = b"\x00" * (26 * 1024 * 1024)  # 26 MiB
    bot = _FakeBot(file_path="voice/abc.oga", payload=huge)
    with patch("app.services.llm.whisper._post_audio", new=AsyncMock()) as mock_post:
        result = await transcribe_file_id(bot, "file-id", duration_hint=10.0)  # type: ignore[arg-type]
    assert result is None
    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_none_on_5xx() -> None:
    bot = _FakeBot(file_path="voice/abc.oga", payload=b"data")
    response = _make_response(503, {"error": "service unavailable"})
    with patch("app.services.llm.whisper._post_audio", new=AsyncMock(return_value=response)):
        result = await transcribe_file_id(bot, "file-id", duration_hint=5.0)  # type: ignore[arg-type]
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_empty_text() -> None:
    bot = _FakeBot(file_path="voice/abc.oga", payload=b"data")
    response = _make_response(200, {"text": "   "})
    with patch("app.services.llm.whisper._post_audio", new=AsyncMock(return_value=response)):
        result = await transcribe_file_id(bot, "file-id", duration_hint=5.0)  # type: ignore[arg-type]
    assert result is None


@pytest.mark.asyncio
async def test_language_hint_passed_in_form_data() -> None:
    bot = _FakeBot(file_path="voice/abc.oga", payload=b"data")
    response = _make_response(200, {"text": "hello"})
    captured: dict[str, Any] = {}

    async def capture_post(payload_bytes: bytes, *, model: str, language: str | None) -> httpx.Response:
        captured["model"] = model
        captured["language"] = language
        captured["size"] = len(payload_bytes)
        return response

    with patch("app.services.llm.whisper._post_audio", new=capture_post):
        result = await transcribe_file_id(
            bot, "file-id",  # type: ignore[arg-type]
            language="ru",
            duration_hint=5.0,
        )

    assert result is not None
    assert captured["language"] == "ru"
    assert captured["model"] == "whisper-1"
    assert captured["size"] == 4
