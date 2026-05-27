from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpyDelivery, SpyPost, SpySource
from app.services.spy.telegram_delivery import (
    SpyTelegramDeliveryService,
    format_spy_delivery_message,
)


@dataclass(slots=True)
class FakeBot:
    fail: bool = False
    sent: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=555)


async def _seed_delivery(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    comment_text: str | None = "Гремлин одобряет подозрительно полезный пост.",
    status: str = "pending",
) -> int:
    async with sessionmaker() as session:
        source = SpySource(
            username="gospodindirectorpivs",
            title="Господин <директор> Пивс",
            public_url="https://t.me/gospodindirectorpivs",
            reader_mode="mtproto",
            status="active",
        )
        session.add(source)
        await session.flush()
        post = SpyPost(
            source_id=source.id,
            external_post_id="101",
            text="Пост с <важными> новостями & нюансами.",
            source_url="https://t.me/gospodindirectorpivs/101",
            published_at=datetime(2026, 5, 27, 12, 0),
        )
        session.add(post)
        await session.flush()
        delivery = SpyDelivery(
            post_id=post.id,
            chat_id=-1001,
            status=status,
            comment_text=comment_text,
        )
        session.add(delivery)
        await session.flush()
        delivery_id = delivery.id
        await session.commit()
        return delivery_id


@pytest.mark.asyncio
async def test_format_spy_delivery_message_escapes_html_and_includes_comment_and_link(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    delivery_id = await _seed_delivery(sessionmaker)
    async with sessionmaker() as session:
        delivery = await session.get(SpyDelivery, delivery_id)
        assert delivery is not None
        post = await session.get(SpyPost, delivery.post_id)
        assert post is not None
        source = await session.get(SpySource, post.source_id)
        assert source is not None

        text = format_spy_delivery_message(source=source, post=post, delivery=delivery)

    assert "<b>🍻 Господин &lt;директор&gt; Пивс</b>" in text
    assert "Пост с &lt;важными&gt; новостями &amp; нюансами." in text
    assert "<blockquote>Гремлин одобряет подозрительно полезный пост.</blockquote>" in text
    assert '<a href="https://t.me/gospodindirectorpivs/101">Открыть пост</a>' in text


@pytest.mark.asyncio
async def test_send_pending_delivery_marks_sent_and_stores_message_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    delivery_id = await _seed_delivery(sessionmaker)
    bot = FakeBot()
    service = SpyTelegramDeliveryService(sessionmaker, bot)

    sent = await service.send_pending_delivery(delivery_id)

    assert sent is True
    assert bot.sent[0]["chat_id"] == -1001
    assert bot.sent[0]["parse_mode"] == "HTML"
    assert bot.sent[0]["disable_web_page_preview"] is False
    async with sessionmaker() as session:
        delivery = await session.get(SpyDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == "sent"
        assert delivery.delivered_message_id == 555
        assert delivery.delivered_at is not None
        assert delivery.error is None


@pytest.mark.asyncio
async def test_send_pending_delivery_marks_error_when_telegram_send_fails(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    delivery_id = await _seed_delivery(sessionmaker)
    service = SpyTelegramDeliveryService(sessionmaker, FakeBot(fail=True))

    sent = await service.send_pending_delivery(delivery_id)

    assert sent is False
    async with sessionmaker() as session:
        delivery = await session.get(SpyDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == "error"
        assert delivery.error == "telegram unavailable"
        assert delivery.delivered_message_id is None


@pytest.mark.asyncio
async def test_send_pending_delivery_ignores_non_pending_delivery(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    delivery_id = await _seed_delivery(sessionmaker, status="sent")
    bot = FakeBot()
    service = SpyTelegramDeliveryService(sessionmaker, bot)

    sent = await service.send_pending_delivery(delivery_id)

    assert sent is False
    assert bot.sent == []
