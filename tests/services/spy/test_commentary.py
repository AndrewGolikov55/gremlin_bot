from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, cast

import pytest

from app.models.spy import SpyPost, SpySource
from app.services.spy.commentary import SpyCommentaryService


@dataclass(slots=True)
class FakeLLM:
    response: str = "Гремлинский комментарий."
    calls: list[dict[str, object]] = field(default_factory=list)

    async def generate(
        self,
        messages: list[Mapping[str, object]],
        *,
        max_tokens: int,
        primary: str | None = None,
    ) -> str:
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "primary": primary})
        return self.response


def _source() -> SpySource:
    return SpySource(
        username="gospodindirectorpivs",
        title="Господин директор Пивс",
        public_url="https://t.me/gospodindirectorpivs",
        reader_mode="mtproto",
        status="active",
    )


def _post(**kwargs: object) -> SpyPost:
    defaults: dict[str, object] = {
        "source_id": 1,
        "external_post_id": "101",
        "text": "Новый пост про инфраструктуру бара.",
        "source_url": "https://t.me/gospodindirectorpivs/101",
        "published_at": datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
        "media": [],
        "raw_payload": {"id": 101},
    }
    defaults.update(kwargs)
    return SpyPost(**defaults)


@pytest.mark.asyncio
async def test_generate_comment_uses_text_llm_prompt_for_plain_post() -> None:
    llm = FakeLLM(response="Сварил короткий ехидный вывод.")
    service = SpyCommentaryService(llm)

    comment = await service.generate_comment(post=_post(), source=_source())

    assert comment == "Сварил короткий ехидный вывод."
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["primary"] is None
    assert call["max_tokens"] == 180
    messages = call["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert "Господин директор Пивс" in str(messages[-1]["content"])
    assert "Новый пост про инфраструктуру бара." in str(messages[-1]["content"])
    assert "https://t.me/gospodindirectorpivs/101" in str(messages[-1]["content"])


@pytest.mark.asyncio
async def test_generate_comment_routes_image_posts_through_vision_prompt() -> None:
    llm = FakeLLM(response="Вижу картинку, делаю вывод.")
    service = SpyCommentaryService(llm)
    post = _post(media=[{"kind": "photo", "data_url": "data:image/jpeg;base64,abc"}])

    comment = await service.generate_comment(post=post, source=_source())

    assert comment == "Вижу картинку, делаю вывод."
    call = llm.calls[0]
    assert call["primary"] == "openai"
    messages = cast(list[dict[str, Any]], call["messages"])
    content = cast(list[dict[str, Any]], messages[-1]["content"])
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,abc", "detail": "low"},
    }


@pytest.mark.asyncio
async def test_generate_comment_returns_fallback_when_llm_returns_blank() -> None:
    llm = FakeLLM(response="   ")
    service = SpyCommentaryService(llm)

    comment = await service.generate_comment(post=_post(text=None), source=_source())

    assert comment == "Гремлин изучил пост и недовольно хмыкнул. Подробности — по ссылке выше."
