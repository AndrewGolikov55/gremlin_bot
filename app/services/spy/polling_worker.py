from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.spy import SpyDelivery, SpyPost, SpySource, SpySubscription
from app.services.spy.types import SpyMedia, SpyPostPayload


class PollingChannelReader(Protocol):
    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]: ...


@dataclass(frozen=True, slots=True)
class SpyPollingResult:
    sources_checked: int = 0
    posts_created: int = 0
    deliveries_created: int = 0
    errors: int = 0


class SpyPollingWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        reader: PollingChannelReader,
        *,
        fetch_limit: int = 20,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._reader = reader
        self._fetch_limit = fetch_limit

    async def tick(self) -> SpyPollingResult:
        result = SpyPollingResult()
        sources = await self._load_pollable_sources()
        for source in sources:
            result = await self._poll_source(source, result)
        return result

    async def _load_pollable_sources(self) -> list[SpySource]:
        async with self._sessionmaker() as session:
            rows = await session.execute(
                select(SpySource)
                .join(SpySubscription, SpySubscription.source_id == SpySource.id)
                .where(
                    SpySource.status == "active",
                    SpySubscription.enabled.is_(True),
                    SpySource.username.is_not(None),
                )
                .distinct()
                .order_by(SpySource.id)
            )
            return list(rows.scalars().all())

    async def _poll_source(self, source_snapshot: SpySource, result: SpyPollingResult) -> SpyPollingResult:
        username = source_snapshot.username
        if not username:
            return result

        try:
            payloads = await self._reader.fetch_latest_posts(username, limit=self._fetch_limit)
        except Exception as exc:
            await self._mark_source_error(source_snapshot.id, exc)
            return SpyPollingResult(
                sources_checked=result.sources_checked + 1,
                posts_created=result.posts_created,
                deliveries_created=result.deliveries_created,
                errors=result.errors + 1,
            )

        posts_created, deliveries_created, errors = await self._persist_source_posts(source_snapshot.id, payloads)
        return SpyPollingResult(
            sources_checked=result.sources_checked + 1,
            posts_created=result.posts_created + posts_created,
            deliveries_created=result.deliveries_created + deliveries_created,
            errors=result.errors + errors,
        )

    async def _persist_source_posts(
        self,
        source_id: int,
        payloads: list[SpyPostPayload],
    ) -> tuple[int, int, int]:
        async with self._sessionmaker() as session:
            try:
                return await self._persist_source_posts_in_session(session, source_id, payloads)
            except IntegrityError:
                await session.rollback()
                # Another worker won a race. Let the next tick converge without crashing.
                return 0, 0, 0

    async def _persist_source_posts_in_session(
        self,
        session: AsyncSession,
        source_id: int,
        payloads: list[SpyPostPayload],
    ) -> tuple[int, int, int]:
        source = await session.get(SpySource, source_id)
        if source is None:
            return 0, 0, 0

        new_payloads = [
            payload
            for payload in self._deduplicate_payloads(payloads)
            if self._is_after(payload.external_post_id, source.last_seen_external_id)
        ]
        if not new_payloads:
            source.status = "active"
            source.last_error = None
            await session.commit()
            return 0, 0, 0

        if self._fetch_window_did_not_reach_last_seen(payloads, source.last_seen_external_id):
            source.status = "error"
            source.last_error = "polling window exhausted before reaching last_seen_external_id"
            await session.commit()
            return 0, 0, 1

        subscriptions = (
            await session.execute(
                select(SpySubscription).where(
                    SpySubscription.source_id == source_id,
                    SpySubscription.enabled.is_(True),
                )
            )
        ).scalars().all()
        existing_posts = {
            post.external_post_id: post
            for post in (
                await session.execute(
                    select(SpyPost).where(
                        SpyPost.source_id == source_id,
                        SpyPost.external_post_id.in_([payload.external_post_id for payload in new_payloads]),
                    )
                )
            ).scalars().all()
        }

        posts_created = 0
        deliveries_created = 0
        for payload in sorted(new_payloads, key=lambda item: self._sort_key(item.external_post_id)):
            post = existing_posts.get(payload.external_post_id)
            if post is None:
                post = SpyPost(
                    source_id=source_id,
                    external_post_id=payload.external_post_id,
                    text=payload.text,
                    source_url=payload.source_url,
                    published_at=payload.published_at,
                    media=[self._media_to_json(media) for media in payload.media],
                    raw_payload=payload.raw,
                )
                session.add(post)
                await session.flush()
                existing_posts[payload.external_post_id] = post
                posts_created += 1
            for subscription in subscriptions:
                if await self._delivery_exists(session, post.id, subscription.chat_id):
                    continue
                session.add(
                    SpyDelivery(
                        post_id=post.id,
                        chat_id=subscription.chat_id,
                        status="pending",
                    )
                )
                deliveries_created += 1

        source.last_seen_external_id = max(
            [source.last_seen_external_id or "", *[payload.external_post_id for payload in new_payloads]],
            key=self._sort_key,
        )
        source.status = "active"
        source.last_error = None
        await session.commit()
        return posts_created, deliveries_created, 0

    async def _delivery_exists(self, session: AsyncSession, post_id: int, chat_id: int) -> bool:
        return (
            await session.execute(
                select(SpyDelivery.id).where(
                    SpyDelivery.post_id == post_id,
                    SpyDelivery.chat_id == chat_id,
                )
            )
        ).scalar_one_or_none() is not None

    async def _mark_source_error(self, source_id: int, exc: Exception) -> None:
        async with self._sessionmaker() as session:
            source = await session.get(SpySource, source_id)
            if source is None:
                return
            source.status = "error"
            source.last_error = str(exc)
            await session.commit()

    def _fetch_window_did_not_reach_last_seen(
        self,
        payloads: list[SpyPostPayload],
        last_seen: str | None,
    ) -> bool:
        if last_seen is None or len(payloads) < self._fetch_limit:
            return False
        return all(self._is_after(payload.external_post_id, last_seen) for payload in payloads)

    def _deduplicate_payloads(self, payloads: list[SpyPostPayload]) -> list[SpyPostPayload]:
        by_id: dict[str, SpyPostPayload] = {}
        for payload in payloads:
            by_id.setdefault(payload.external_post_id, payload)
        return list(by_id.values())

    def _is_after(self, post_id: str, last_seen: str | None) -> bool:
        if last_seen is None:
            return True
        return self._sort_key(post_id) > self._sort_key(last_seen)

    def _sort_key(self, post_id: str) -> tuple[int, int | str]:
        if post_id.isdigit():
            return (0, int(post_id))
        return (1, post_id)

    def _media_to_json(self, media: SpyMedia) -> dict[str, object]:
        return {key: value for key, value in asdict(media).items() if value not in (None, {}, [])}
