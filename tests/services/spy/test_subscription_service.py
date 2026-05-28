from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpySource, SpySubscription
from app.services.spy.source_service import SpySourceService
from app.services.spy.subscription_service import SpyAdminRequired, SpySubscriptionService
from app.services.spy.types import SpyChannelInfo, SpyPostPayload


@dataclass(slots=True)
class FakeReader:
    info: SpyChannelInfo = field(
        default_factory=lambda: SpyChannelInfo(
            username="gospodindirectorpivs",
            title="Господин директор Пивс",
            telegram_channel_id=777,
            access_mode="mtproto",
        )
    )
    posts: list[SpyPostPayload] = field(
        default_factory=lambda: [
            SpyPostPayload(
                external_post_id="12345",
                text="latest",
                published_at=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
                source_url="https://t.me/gospodindirectorpivs/12345",
            )
        ]
    )

    async def resolve_channel(self, ref: str) -> SpyChannelInfo:
        return self.info

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
        return self.posts[:limit]


@dataclass(slots=True)
class FakeAdminChecker:
    allowed: bool = True
    calls: list[tuple[int, int]] = field(default_factory=list)

    async def is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        self.calls.append((chat_id, user_id))
        return self.allowed


def _make_service(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    admin_allowed: bool = True,
) -> tuple[SpySubscriptionService, FakeAdminChecker]:
    checker = FakeAdminChecker(allowed=admin_allowed)
    source_service = SpySourceService(sessionmaker, FakeReader())
    return SpySubscriptionService(sessionmaker, source_service, checker), checker


@pytest.mark.asyncio
async def test_add_subscription_requires_chat_admin(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service, checker = _make_service(sessionmaker, admin_allowed=False)

    with pytest.raises(SpyAdminRequired):
        await service.add_subscription(
            chat_id=-1001,
            source_ref="@gospodindirectorpivs",
            actor_user_id=42,
        )

    assert checker.calls == [(-1001, 42)]
    async with sessionmaker() as session:
        subscriptions = (await session.execute(select(SpySubscription))).scalars().all()
        assert subscriptions == []


@pytest.mark.asyncio
async def test_add_subscription_creates_source_and_enabled_subscription(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service, checker = _make_service(sessionmaker)

    subscription = await service.add_subscription(
        chat_id=-1001,
        source_ref="https://t.me/gospodindirectorpivs",
        actor_user_id=42,
    )

    assert checker.calls == [(-1001, 42)]
    assert subscription.chat_id == -1001
    assert subscription.enabled is True
    assert subscription.created_by_user_id == 42

    async with sessionmaker() as session:
        source = (await session.execute(select(SpySource))).scalar_one()
        assert source.username == "gospodindirectorpivs"
        assert source.last_seen_external_id == "12345"
        assert subscription.source_id == source.id


@pytest.mark.asyncio
async def test_remove_subscription_requires_chat_admin(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service, _checker = _make_service(sessionmaker)
    await service.add_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=42,
    )
    blocked_service, blocked_checker = _make_service(sessionmaker, admin_allowed=False)

    with pytest.raises(SpyAdminRequired):
        await blocked_service.remove_subscription(
            chat_id=-1001,
            source_ref="@gospodindirectorpivs",
            actor_user_id=43,
        )

    assert blocked_checker.calls == [(-1001, 43)]
    async with sessionmaker() as session:
        subscription = (await session.execute(select(SpySubscription))).scalar_one()
        assert subscription.enabled is True


@pytest.mark.asyncio
async def test_add_subscription_is_idempotent_and_reenables_existing_subscription(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service, _checker = _make_service(sessionmaker)
    first = await service.add_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=42,
    )
    await service.remove_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=42,
    )

    second = await service.add_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=99,
    )

    assert second.id == first.id
    assert second.enabled is True
    assert second.created_by_user_id == 42
    async with sessionmaker() as session:
        subscriptions = (await session.execute(select(SpySubscription))).scalars().all()
        assert len(subscriptions) == 1


@pytest.mark.asyncio
async def test_remove_subscription_disables_subscription(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service, checker = _make_service(sessionmaker)
    await service.add_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=42,
    )

    removed = await service.remove_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=43,
    )

    assert checker.calls[-1] == (-1001, 43)
    assert removed is True
    async with sessionmaker() as session:
        subscription = (await session.execute(select(SpySubscription))).scalar_one()
        assert subscription.enabled is False


@pytest.mark.asyncio
async def test_list_subscriptions_returns_enabled_sources_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    service, checker = _make_service(sessionmaker)
    await service.add_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=42,
    )
    await service.add_subscription(
        chat_id=-1002,
        source_ref="@gospodindirectorpivs",
        actor_user_id=42,
        require_admin=False,
    )
    await service.remove_subscription(
        chat_id=-1001,
        source_ref="@gospodindirectorpivs",
        actor_user_id=42,
    )

    items = await service.list_subscriptions(chat_id=-1001)
    other_items = await service.list_subscriptions(chat_id=-1002)

    assert checker.calls == [(-1001, 42), (-1001, 42)]
    assert items == []
    assert len(other_items) == 1
    assert other_items[0].username == "gospodindirectorpivs"
    assert other_items[0].title == "Господин директор Пивс"
    assert other_items[0].public_url == "https://t.me/gospodindirectorpivs"
