from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpyDelivery, SpyPost, SpySource


class SpyTelegramDeliveryService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Any,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bot = bot

    async def send_pending_delivery(self, delivery_id: int) -> bool:
        loaded = await self._load_pending_delivery(delivery_id)
        if loaded is None:
            return False
        delivery, post, source = loaded
        text = format_spy_delivery_message(source=source, post=post, delivery=delivery)

        try:
            sent = await self._bot.send_message(
                chat_id=delivery.chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
        except Exception as exc:
            await self._mark_error(delivery_id, exc)
            return False

        message_id = getattr(sent, "message_id", None)
        await self._mark_sent(delivery_id, message_id if isinstance(message_id, int) else None)
        return True

    async def send_pending_deliveries(self, *, limit: int = 50) -> int:
        delivery_ids = await self._load_pending_delivery_ids(limit=limit)
        sent_count = 0
        for delivery_id in delivery_ids:
            if await self.send_pending_delivery(delivery_id):
                sent_count += 1
        return sent_count

    async def _load_pending_delivery_ids(self, *, limit: int) -> list[int]:
        async with self._sessionmaker() as session:
            rows = await session.execute(
                select(SpyDelivery.id)
                .where(SpyDelivery.status == "pending")
                .order_by(SpyDelivery.id)
                .limit(max(1, limit))
            )
            return list(rows.scalars().all())

    async def _load_pending_delivery(self, delivery_id: int) -> tuple[SpyDelivery, SpyPost, SpySource] | None:
        async with self._sessionmaker() as session:
            delivery = await session.get(SpyDelivery, delivery_id)
            if delivery is None or delivery.status != "pending":
                return None
            post = await session.get(SpyPost, delivery.post_id)
            if post is None:
                return None
            source = await session.get(SpySource, post.source_id)
            if source is None:
                return None
            return delivery, post, source

    async def _mark_sent(self, delivery_id: int, message_id: int | None) -> None:
        async with self._sessionmaker() as session:
            delivery = await session.get(SpyDelivery, delivery_id)
            if delivery is None:
                return
            delivery.status = "sent"
            delivery.delivered_message_id = message_id
            delivery.delivered_at = datetime.utcnow()
            delivery.error = None
            await session.commit()

    async def _mark_error(self, delivery_id: int, exc: Exception) -> None:
        async with self._sessionmaker() as session:
            delivery = await session.get(SpyDelivery, delivery_id)
            if delivery is None:
                return
            delivery.status = "error"
            delivery.error = str(exc)
            await session.commit()


def format_spy_delivery_message(*, source: SpySource, post: SpyPost, delivery: SpyDelivery) -> str:
    source_name = source.title or source.username or "Telegram-канал"
    parts = [f"<b>🍻 {escape(source_name)}</b>"]
    if post.text:
        parts.append(escape(post.text))
    else:
        parts.append("<i>Медиа-пост без текста.</i>")
    if delivery.comment_text:
        parts.append(f"<blockquote>{escape(delivery.comment_text)}</blockquote>")
    if post.source_url:
        url = escape(post.source_url, quote=True)
        parts.append(f'<a href="{url}">Открыть пост</a>')
    return "\n\n".join(parts)
