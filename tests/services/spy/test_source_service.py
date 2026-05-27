from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpyPost, SpySource
from app.services.spy.source_service import SpySourceService
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
    resolved_refs: list[str] = field(default_factory=list)
    fetch_calls: list[tuple[str, int]] = field(default_factory=list)

    async def resolve_channel(self, ref: str) -> SpyChannelInfo:
        self.resolved_refs.append(ref)
        return self.info

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
        self.fetch_calls.append((username, limit))
        return self.posts[:limit]


@pytest.mark.asyncio
async def test_add_source_resolves_and_stores_latest_id_without_backfill(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    reader = FakeReader()
    service = SpySourceService(sessionmaker, reader)

    source = await service.add_or_resolve_source("https://t.me/GospodinDirectorPivs")

    assert source.username == "gospodindirectorpivs"
    assert source.title == "Господин директор Пивс"
    assert source.public_url == "https://t.me/gospodindirectorpivs"
    assert source.reader_mode == "mtproto"
    assert source.status == "active"
    assert source.last_seen_external_id == "12345"
    assert source.metadata_json == {"telegram_channel_id": 777}
    assert reader.resolved_refs == ["gospodindirectorpivs"]
    assert reader.fetch_calls == [("gospodindirectorpivs", 1)]

    async with sessionmaker() as session:
        posts = (await session.execute(select(SpyPost))).scalars().all()
        assert posts == []


@pytest.mark.asyncio
async def test_add_source_reuses_existing_source_without_resetting_last_seen(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(
            SpySource(
                username="gospodindirectorpivs",
                title="Old title",
                public_url="https://t.me/gospodindirectorpivs",
                reader_mode="mtproto",
                status="active",
                last_seen_external_id="99999",
                metadata_json={"telegram_channel_id": 1},
            )
        )
        await session.commit()

    reader = FakeReader()
    service = SpySourceService(sessionmaker, reader)

    source = await service.add_or_resolve_source("@GospodinDirectorPivs")

    assert source.title == "Господин директор Пивс"
    assert source.last_seen_external_id == "99999"
    assert source.metadata_json == {"telegram_channel_id": 777}
    assert reader.resolved_refs == ["gospodindirectorpivs"]
    assert reader.fetch_calls == []


@pytest.mark.asyncio
async def test_add_source_with_empty_channel_initializes_without_last_seen(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    reader = FakeReader(posts=[])
    service = SpySourceService(sessionmaker, reader)

    source = await service.add_or_resolve_source("gospodindirectorpivs")

    assert source.last_seen_external_id is None
    assert reader.fetch_calls == [("gospodindirectorpivs", 1)]


@pytest.mark.asyncio
async def test_reader_returned_username_is_normalized_before_persisting(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    reader = FakeReader(
        info=SpyChannelInfo(
            username="GospodinDirectorPivs",
            title="Господин директор Пивс",
            telegram_channel_id=777,
            access_mode="mtproto",
        )
    )
    service = SpySourceService(sessionmaker, reader)

    source = await service.add_or_resolve_source("gospodindirectorpivs")
    loaded = await service.get_source_by_ref("@GospodinDirectorPivs")

    assert source.username == "gospodindirectorpivs"
    assert loaded is not None
    assert loaded.id == source.id
    assert reader.fetch_calls == [("gospodindirectorpivs", 1)]


@pytest.mark.asyncio
async def test_successful_reresolve_clears_previous_error(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(
            SpySource(
                username="gospodindirectorpivs",
                title="Old title",
                public_url="https://t.me/gospodindirectorpivs",
                reader_mode="mtproto",
                status="error",
                last_seen_external_id="99999",
                last_error="previous failure",
                metadata_json={"telegram_channel_id": 1},
            )
        )
        await session.commit()

    reader = FakeReader()
    service = SpySourceService(sessionmaker, reader)

    source = await service.add_or_resolve_source("@GospodinDirectorPivs")

    assert source.status == "active"
    assert source.last_error is None


@pytest.mark.asyncio
async def test_get_source_by_ref_uses_normalized_username(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    reader = FakeReader()
    service = SpySourceService(sessionmaker, reader)
    created = await service.add_or_resolve_source("https://t.me/gospodindirectorpivs/123")

    loaded = await service.get_source_by_ref("@GospodinDirectorPivs")

    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.username == "gospodindirectorpivs"
