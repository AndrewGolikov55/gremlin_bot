from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpyDelivery, SpyPost, SpySource, SpySubscription
from app.services.spy.polling_worker import SpyPollingWorker
from app.services.spy.types import SpyMedia, SpyPostPayload


@dataclass(slots=True)
class FakeReader:
    posts_by_username: dict[str, list[SpyPostPayload]]
    calls: list[tuple[str, int]] = field(default_factory=list)

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
        self.calls.append((username, limit))
        return self.posts_by_username.get(username, [])[:limit]


def _payload(post_id: int, *, text: str | None = None) -> SpyPostPayload:
    return SpyPostPayload(
        external_post_id=str(post_id),
        text=text or f"post {post_id}",
        published_at=datetime(2026, 5, 27, 12, post_id % 60, tzinfo=timezone.utc),
        source_url=f"https://t.me/gospodindirectorpivs/{post_id}",
        media=[SpyMedia(kind="photo", file_id=f"photo-{post_id}")],
        raw={"id": post_id},
    )


async def _seed_source(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    username: str = "gospodindirectorpivs",
    last_seen_external_id: str | None = "100",
    status: str = "active",
) -> int:
    async with sessionmaker() as session:
        source = SpySource(
            username=username,
            title="Господин директор Пивс",
            public_url=f"https://t.me/{username}",
            reader_mode="mtproto",
            status=status,
            last_seen_external_id=last_seen_external_id,
        )
        session.add(source)
        await session.flush()
        source_id = source.id
        await session.commit()
        return source_id


async def _seed_subscription(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    source_id: int,
    chat_id: int,
    enabled: bool = True,
) -> None:
    async with sessionmaker() as session:
        session.add(SpySubscription(chat_id=chat_id, source_id=source_id, enabled=enabled))
        await session.commit()


@pytest.mark.asyncio
async def test_tick_inserts_new_posts_fans_out_to_enabled_subscriptions_and_advances_state(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(sessionmaker, last_seen_external_id="100")
    await _seed_subscription(sessionmaker, source_id=source_id, chat_id=-1001)
    await _seed_subscription(sessionmaker, source_id=source_id, chat_id=-1002)
    await _seed_subscription(sessionmaker, source_id=source_id, chat_id=-1003, enabled=False)
    reader = FakeReader({"gospodindirectorpivs": [_payload(102), _payload(101), _payload(100)]})
    worker = SpyPollingWorker(sessionmaker, reader, fetch_limit=10)

    result = await worker.tick()

    assert result.sources_checked == 1
    assert result.posts_created == 2
    assert result.deliveries_created == 4
    assert reader.calls == [("gospodindirectorpivs", 10)]

    async with sessionmaker() as session:
        source = await session.get(SpySource, source_id)
        assert source is not None
        assert source.last_seen_external_id == "102"
        posts = (
            await session.execute(select(SpyPost).order_by(SpyPost.external_post_id))
        ).scalars().all()
        assert [post.external_post_id for post in posts] == ["101", "102"]
        assert posts[0].media == [{"kind": "photo", "file_id": "photo-101"}]
        deliveries = (
            await session.execute(select(SpyDelivery).order_by(SpyDelivery.chat_id, SpyDelivery.post_id))
        ).scalars().all()
        assert [(delivery.chat_id, delivery.status) for delivery in deliveries] == [
            (-1002, "pending"),
            (-1002, "pending"),
            (-1001, "pending"),
            (-1001, "pending"),
        ]


@pytest.mark.asyncio
async def test_tick_deduplicates_existing_posts_and_deliveries(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(sessionmaker, last_seen_external_id="100")
    await _seed_subscription(sessionmaker, source_id=source_id, chat_id=-1001)
    async with sessionmaker() as session:
        post = SpyPost(source_id=source_id, external_post_id="101", text="old")
        session.add(post)
        await session.flush()
        session.add(SpyDelivery(post_id=post.id, chat_id=-1001, status="pending"))
        await session.commit()
    reader = FakeReader({"gospodindirectorpivs": [_payload(102), _payload(101)]})
    worker = SpyPollingWorker(sessionmaker, reader, fetch_limit=5)

    result = await worker.tick()

    assert result.posts_created == 1
    assert result.deliveries_created == 1
    async with sessionmaker() as session:
        posts = (await session.execute(select(SpyPost))).scalars().all()
        deliveries = (await session.execute(select(SpyDelivery))).scalars().all()
        source = await session.get(SpySource, source_id)
        assert len(posts) == 2
        assert len(deliveries) == 2
        assert source is not None
        assert source.last_seen_external_id == "102"


@pytest.mark.asyncio
async def test_tick_skips_sources_without_enabled_subscriptions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(sessionmaker, last_seen_external_id="100")
    await _seed_subscription(sessionmaker, source_id=source_id, chat_id=-1001, enabled=False)
    reader = FakeReader({"gospodindirectorpivs": [_payload(101)]})
    worker = SpyPollingWorker(sessionmaker, reader)

    result = await worker.tick()

    assert result.sources_checked == 0
    assert reader.calls == []


@pytest.mark.asyncio
async def test_tick_does_not_advance_or_deliver_when_fetch_window_does_not_reach_last_seen(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(sessionmaker, last_seen_external_id="100")
    await _seed_subscription(sessionmaker, source_id=source_id, chat_id=-1001)
    reader = FakeReader({"gospodindirectorpivs": [_payload(103), _payload(102)]})
    worker = SpyPollingWorker(sessionmaker, reader, fetch_limit=2)

    result = await worker.tick()

    assert result.sources_checked == 1
    assert result.posts_created == 0
    assert result.deliveries_created == 0
    assert result.errors == 1
    async with sessionmaker() as session:
        source = await session.get(SpySource, source_id)
        posts = (await session.execute(select(SpyPost))).scalars().all()
        assert source is not None
        assert source.last_seen_external_id == "100"
        assert source.status == "error"
        assert source.last_error == "polling window exhausted before reaching last_seen_external_id"
        assert posts == []


@pytest.mark.asyncio
async def test_tick_marks_source_error_when_reader_fails(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _seed_source(sessionmaker, last_seen_external_id="100")
    await _seed_subscription(sessionmaker, source_id=source_id, chat_id=-1001)

    class BrokenReader:
        async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
            raise RuntimeError("boom")

    worker = SpyPollingWorker(sessionmaker, BrokenReader())

    result = await worker.tick()

    assert result.sources_checked == 1
    assert result.errors == 1
    async with sessionmaker() as session:
        source = await session.get(SpySource, source_id)
        assert source is not None
        assert source.status == "error"
        assert source.last_error == "boom"
        posts = (await session.execute(select(SpyPost))).scalars().all()
        assert posts == []
