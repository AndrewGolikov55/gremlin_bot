from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpySource
from app.services.spy.refs import normalize_channel_ref
from app.services.spy.types import SpyChannelInfo, SpyPostPayload


class SourceChannelReader(Protocol):
    async def resolve_channel(self, ref: str) -> SpyChannelInfo: ...

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]: ...


class SpySourceService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        reader: SourceChannelReader,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._reader = reader

    async def add_or_resolve_source(self, ref: str) -> SpySource:
        username = normalize_channel_ref(ref)
        info = await self._reader.resolve_channel(username)
        resolved_username = normalize_channel_ref(info.username) if info.username else username

        async with self._sessionmaker() as session:
            source = await self._get_by_username(session, resolved_username)
            if source is None:
                latest_posts = await self._reader.fetch_latest_posts(resolved_username, limit=1)
                source = SpySource(
                    source_type="telegram_channel",
                    username=resolved_username,
                    title=info.title,
                    public_url=info.public_url,
                    reader_mode=info.access_mode,
                    status="active",
                    last_seen_external_id=(
                        latest_posts[0].external_post_id if latest_posts else None
                    ),
                    metadata_json=self._metadata(info),
                )
                session.add(source)
            else:
                self._apply_resolved_info(source, info)

            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                source = await self._get_by_username(session, resolved_username)
                if source is None:
                    raise
                self._apply_resolved_info(source, info)
                await session.commit()

            await session.refresh(source)
            return source

    async def get_source_by_ref(self, ref: str) -> SpySource | None:
        username = normalize_channel_ref(ref)
        async with self._sessionmaker() as session:
            return await self._get_by_username(session, username)

    async def _get_by_username(self, session: AsyncSession, username: str) -> SpySource | None:
        return (
            await session.execute(select(SpySource).where(SpySource.username == username))
        ).scalar_one_or_none()

    def _apply_resolved_info(self, source: SpySource, info: SpyChannelInfo) -> None:
        source.title = info.title
        source.public_url = info.public_url
        source.reader_mode = info.access_mode
        source.status = "active"
        source.last_error = None
        source.metadata_json = self._metadata(info)

    def _metadata(self, info: SpyChannelInfo) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if info.telegram_channel_id is not None:
            metadata["telegram_channel_id"] = info.telegram_channel_id
        return metadata
