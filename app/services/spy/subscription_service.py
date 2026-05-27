from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpySource, SpySubscription
from app.services.spy.source_service import SpySourceService


class SpyAdminRequired(PermissionError):
    pass


class SpyAdminChecker(Protocol):
    async def is_chat_admin(self, chat_id: int, user_id: int) -> bool: ...


class AiogramChatAdminChecker:
    def __init__(self, bot: object) -> None:
        self._bot = bot

    async def is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        member = await self._bot.get_chat_member(chat_id, user_id)  # type: ignore[attr-defined]
        status = getattr(member, "status", None)
        value = getattr(status, "value", status)
        return value in {"creator", "administrator"}


@dataclass(frozen=True, slots=True)
class SpySubscriptionView:
    subscription_id: int
    source_id: int
    username: str | None
    title: str | None
    public_url: str | None
    reader_mode: str
    last_seen_external_id: str | None


class SpySubscriptionService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        source_service: SpySourceService,
        admin_checker: SpyAdminChecker,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._source_service = source_service
        self._admin_checker = admin_checker

    async def add_subscription(
        self,
        *,
        chat_id: int,
        source_ref: str,
        actor_user_id: int,
        require_admin: bool = True,
    ) -> SpySubscription:
        await self._ensure_admin(chat_id, actor_user_id, require_admin=require_admin)
        source = await self._source_service.add_or_resolve_source(source_ref)

        async with self._sessionmaker() as session:
            subscription = await self._get_subscription(session, chat_id, source.id)
            if subscription is None:
                subscription = SpySubscription(
                    chat_id=chat_id,
                    source_id=source.id,
                    enabled=True,
                    created_by_user_id=actor_user_id,
                )
                session.add(subscription)
            else:
                subscription.enabled = True
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                subscription = await self._get_subscription(session, chat_id, source.id)
                if subscription is None:
                    raise
                subscription.enabled = True
                await session.commit()
            await session.refresh(subscription)
            return subscription

    async def remove_subscription(
        self,
        *,
        chat_id: int,
        source_ref: str,
        actor_user_id: int,
        require_admin: bool = True,
    ) -> bool:
        await self._ensure_admin(chat_id, actor_user_id, require_admin=require_admin)
        source = await self._source_service.get_source_by_ref(source_ref)
        if source is None:
            return False

        async with self._sessionmaker() as session:
            subscription = await self._get_subscription(session, chat_id, source.id)
            if subscription is None or not subscription.enabled:
                return False
            subscription.enabled = False
            await session.commit()
            return True

    async def list_subscriptions(self, *, chat_id: int) -> list[SpySubscriptionView]:
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(SpySubscription, SpySource)
                    .join(SpySource, SpySource.id == SpySubscription.source_id)
                    .where(
                        SpySubscription.chat_id == chat_id,
                        SpySubscription.enabled.is_(True),
                    )
                    .order_by(SpySource.username)
                )
            ).all()
            return [
                SpySubscriptionView(
                    subscription_id=subscription.id,
                    source_id=source.id,
                    username=source.username,
                    title=source.title,
                    public_url=source.public_url,
                    reader_mode=source.reader_mode,
                    last_seen_external_id=source.last_seen_external_id,
                )
                for subscription, source in rows
            ]

    async def _ensure_admin(
        self,
        chat_id: int,
        user_id: int,
        *,
        require_admin: bool,
    ) -> None:
        if not require_admin:
            return
        if not await self._admin_checker.is_chat_admin(chat_id, user_id):
            raise SpyAdminRequired("spy subscriptions can be changed only by chat admins")

    async def _get_subscription(
        self,
        session: AsyncSession,
        chat_id: int,
        source_id: int,
    ) -> SpySubscription | None:
        return (
            await session.execute(
                select(SpySubscription).where(
                    SpySubscription.chat_id == chat_id,
                    SpySubscription.source_id == source_id,
                )
            )
        ).scalar_one_or_none()
