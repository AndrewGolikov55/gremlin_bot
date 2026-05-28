from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.spy import SpyDelivery, SpyPost, SpySource, SpySubscription
from tests.db import create_test_sessionmaker


@pytest.mark.asyncio
async def test_spy_source_subscription_post_delivery_persist():
    sessionmaker = await create_test_sessionmaker()

    async with sessionmaker() as session:
        source = SpySource(
            source_type="telegram_channel",
            username="gospodindirectorpivs",
            title="Господин Директор Пивс",
            public_url="https://t.me/gospodindirectorpivs",
            reader_mode="mtproto",
            status="active",
            last_seen_external_id="100",
        )
        session.add(source)
        await session.flush()

        sub = SpySubscription(chat_id=-1001, source_id=source.id, enabled=True, created_by_user_id=42)
        post = SpyPost(
            source_id=source.id,
            external_post_id="101",
            text="Новый пост",
            source_url="https://t.me/gospodindirectorpivs/101",
        )
        session.add_all([sub, post])
        await session.flush()

        delivery = SpyDelivery(
            post_id=post.id,
            chat_id=-1001,
            status="sent",
            comment_text="Гремлин видел.",
        )
        session.add(delivery)
        await session.commit()

    async with sessionmaker() as session:
        sources = (await session.execute(select(SpySource))).scalars().all()
        assert len(sources) == 1
        assert sources[0].username == "gospodindirectorpivs"


@pytest.mark.asyncio
async def test_spy_subscription_unique_per_chat_source():
    sessionmaker = await create_test_sessionmaker()
    async with sessionmaker() as session:
        source = SpySource(username="x", reader_mode="mtproto", status="active")
        session.add(source)
        await session.flush()
        session.add_all([
            SpySubscription(chat_id=-1001, source_id=source.id, created_by_user_id=1),
            SpySubscription(chat_id=-1001, source_id=source.id, created_by_user_id=1),
        ])
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_spy_source_username_is_unique():
    sessionmaker = await create_test_sessionmaker()
    async with sessionmaker() as session:
        session.add_all([
            SpySource(username="same", reader_mode="mtproto", status="active"),
            SpySource(username="same", reader_mode="bot_api", status="active"),
        ])
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_spy_post_unique_per_source_external_id():
    sessionmaker = await create_test_sessionmaker()
    async with sessionmaker() as session:
        source = SpySource(username="source", reader_mode="mtproto", status="active")
        session.add(source)
        await session.flush()
        session.add_all([
            SpyPost(source_id=source.id, external_post_id="101"),
            SpyPost(source_id=source.id, external_post_id="101"),
        ])
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_spy_delivery_unique_per_post_chat():
    sessionmaker = await create_test_sessionmaker()
    async with sessionmaker() as session:
        source = SpySource(username="delivery", reader_mode="mtproto", status="active")
        session.add(source)
        await session.flush()
        post = SpyPost(source_id=source.id, external_post_id="101")
        session.add(post)
        await session.flush()
        session.add_all([
            SpyDelivery(post_id=post.id, chat_id=-1001, status="sent"),
            SpyDelivery(post_id=post.id, chat_id=-1001, status="sent"),
        ])
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_spy_json_defaults_are_independent_and_mutable():
    sessionmaker = await create_test_sessionmaker()

    async with sessionmaker() as session:
        source = SpySource(username="json", reader_mode="mtproto", status="active")
        session.add(source)
        await session.flush()
        post = SpyPost(source_id=source.id, external_post_id="101")
        session.add(post)
        await session.commit()
        source_id = source.id
        post_id = post.id

    async with sessionmaker() as session:
        loaded_source = await session.get(SpySource, source_id)
        loaded_post = await session.get(SpyPost, post_id)
        assert loaded_source is not None
        assert loaded_post is not None
        assert loaded_source.metadata_json == {}
        assert loaded_post.media == []
        assert loaded_post.raw_payload == {}

        loaded_source.metadata_json["seen"] = True
        loaded_post.media.append({"kind": "photo"})
        loaded_post.raw_payload["id"] = 101
        await session.commit()

    async with sessionmaker() as session:
        saved_source = await session.get(SpySource, source_id)
        saved_post = await session.get(SpyPost, post_id)
        assert saved_source is not None
        assert saved_post is not None
        assert saved_source.metadata_json == {"seen": True}
        assert saved_post.media == [{"kind": "photo"}]
        assert saved_post.raw_payload == {"id": 101}
