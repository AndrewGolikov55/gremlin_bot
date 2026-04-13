from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm.vision import download_file_id_as_data_url


class _FakeBot:
    def __init__(self, file_path: str | None, payload: bytes) -> None:
        self._file_path = file_path
        self._payload = payload
        self.get_file = AsyncMock(return_value=SimpleNamespace(file_path=file_path))

    async def download_file(self, file_path: str, destination: io.BytesIO) -> None:
        assert file_path == self._file_path
        destination.write(self._payload)


@pytest.mark.asyncio
async def test_download_returns_data_url_for_jpeg() -> None:
    bot = _FakeBot(file_path="photos/abc.jpg", payload=b"\xff\xd8\xff\xe0body")
    url = await download_file_id_as_data_url(bot, "file-id-stub")  # type: ignore[arg-type]
    assert url is not None
    assert url.startswith("data:image/jpeg;base64,")
    bot.get_file.assert_awaited_once_with("file-id-stub")


@pytest.mark.asyncio
async def test_download_returns_none_when_file_path_missing() -> None:
    bot = _FakeBot(file_path=None, payload=b"")
    url = await download_file_id_as_data_url(bot, "file-id")  # type: ignore[arg-type]
    assert url is None


@pytest.mark.asyncio
async def test_download_returns_none_on_empty_payload() -> None:
    bot = _FakeBot(file_path="photos/x.jpg", payload=b"")
    url = await download_file_id_as_data_url(bot, "file-id")  # type: ignore[arg-type]
    assert url is None


@pytest.mark.asyncio
async def test_download_defaults_to_jpeg_when_mime_unknown() -> None:
    bot = _FakeBot(file_path="photos/noext", payload=b"data")
    url = await download_file_id_as_data_url(bot, "file-id")  # type: ignore[arg-type]
    assert url is not None
    assert url.startswith("data:image/jpeg;base64,")
