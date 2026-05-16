from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec

import pytest

from app.services.app_config import AppConfigService
from app.services.persona import StylePromptService
from app.services.settings import SettingsService
from app.services.ship import ShipService


def _make_service(sessionmaker):
    return ShipService(
        sessionmaker=sessionmaker,
        bot=AsyncMock(),
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
        personas=create_autospec(StylePromptService, instance=True),
    )


def test_canonicalize_pair_orders_user_ids(sessionmaker):
    assert ShipService.canonicalize_pair(200, 100) == (100, 200)
    assert ShipService.canonicalize_pair(100, 200) == (100, 200)
    assert ShipService.canonicalize_pair(5, 5) == (5, 5)


@pytest.mark.asyncio
async def test_service_creates_lock_per_chat(sessionmaker):
    svc = _make_service(sessionmaker)
    l1 = svc._get_lock(42)
    l2 = svc._get_lock(42)
    l3 = svc._get_lock(43)
    assert l1 is l2
    assert l1 is not l3


from datetime import datetime, timedelta

from app.models import Message


async def _seed_msg(session, *, chat_id, user_id, msg_id, text="x", reply_to=None, days_ago=1, is_bot=False):
    session.add(Message(
        chat_id=chat_id,
        message_id=msg_id,
        user_id=user_id,
        text=text,
        reply_to_id=reply_to,
        date=datetime.utcnow() - timedelta(days=days_ago),
        is_bot=is_bot,
        tg_file_id=None,
        media_group_id=None,
    ))


@pytest.mark.asyncio
async def test_compute_reply_rate_counts_mutual_replies(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        # A wrote msg 1, B replied with msg 2 (B→A)
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=1)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=2, reply_to=1)
        # A wrote msg 3, B replied with msg 4 (B→A)
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=3)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=4, reply_to=3)
        # B wrote msg 5, A replied with msg 6 (A→B)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=5)
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=6, reply_to=5)
        # Noise: C wrote msg 7, A replied to C (msg 8) — must NOT count
        await _seed_msg(session, chat_id=chat_id, user_id=999, msg_id=7)
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=8, reply_to=7)
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        count, denom = await svc._reply_stats(session, chat_id=chat_id, a=a, b=b)
    assert count == 3  # 2 B→A + 1 A→B
    # A_total = 4 msgs (1, 3, 6, 8); B_total = 3 msgs (2, 4, 5); min = 3
    assert denom == 3


@pytest.mark.asyncio
async def test_compute_reply_rate_zero_when_no_messages(sessionmaker):
    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        count, denom = await svc._reply_stats(session, chat_id=42, a=100, b=200)
    assert count == 0
    assert denom == 0


@pytest.mark.asyncio
async def test_compute_reply_rate_excludes_old_messages(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=1, days_ago=60)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=2, reply_to=1, days_ago=60)
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        count, denom = await svc._reply_stats(session, chat_id=chat_id, a=a, b=b)
    assert count == 0
    assert denom == 0


from app.models import RouletteParticipant


@pytest.mark.asyncio
async def test_compute_mention_rate_counts_at_username_occurrences(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        session.add(RouletteParticipant(chat_id=chat_id, user_id=a, username="alice"))
        session.add(RouletteParticipant(chat_id=chat_id, user_id=b, username="bob"))
        # A's messages mention @bob twice
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=1, text="привет @bob как ты")
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=2, text="@bob ещё раз")
        # B's message mentions @alice once
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=3, text="@alice yo")
        # Noise: someone mentions @bob — must NOT count (not from A)
        await _seed_msg(session, chat_id=chat_id, user_id=999, msg_id=4, text="@bob noise")
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        count, denom = await svc._mention_stats(session, chat_id=chat_id, a=a, b=b)
    assert count == 3  # 2 (A→@bob) + 1 (B→@alice)
    # denom = A_total + B_total = 2 + 1 = 3
    assert denom == 3


@pytest.mark.asyncio
async def test_compute_mention_rate_zero_when_no_usernames_known(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=1, text="@bob hi")
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        count, denom = await svc._mention_stats(session, chat_id=chat_id, a=a, b=b)
    assert count == 0
    assert denom == 1  # A_total=1, B_total=0


@pytest.mark.asyncio
async def test_co_activity_counts_overlapping_days(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        # day 1: both wrote
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=1, days_ago=1)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=2, days_ago=1)
        # day 3: only A
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=3, days_ago=3)
        # day 5: both
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=4, days_ago=5)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=5, days_ago=5)
        # day 7: only B
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=6, days_ago=7)
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        days = await svc._co_active_days(session, chat_id=chat_id, a=a, b=b)
    assert days == 2


@pytest.mark.asyncio
async def test_co_activity_zero_when_no_overlap(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=1, days_ago=1)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=2, days_ago=2)
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        days = await svc._co_active_days(session, chat_id=chat_id, a=a, b=b)
    assert days == 0
