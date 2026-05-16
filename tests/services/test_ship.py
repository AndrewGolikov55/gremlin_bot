from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec

import pytest

from app.services.app_config import AppConfigService
from app.services.persona import StylePromptService
from app.services.settings import SettingsService
from app.services.ship import ShipMetrics, ShipService


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


from app.models import UserMemoryProfile  # noqa: E402


@pytest.mark.asyncio
async def test_pref_overlap_intersection_keywords(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        session.add(UserMemoryProfile(
            chat_id=chat_id, user_id=a,
            preferences=["docker", "kafka", "vim"],
            projects=["gremlin", "labs"],
            identity=["devops"],
        ))
        session.add(UserMemoryProfile(
            chat_id=chat_id, user_id=b,
            preferences=["docker", "emacs"],
            projects=["gremlin", "infra"],
            identity=["devops", "sre"],
        ))
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        keywords, ratio = await svc._pref_overlap(session, chat_id=chat_id, a=a, b=b)

    assert set(keywords) == {"docker", "gremlin", "devops"}
    # union = {docker, kafka, vim, gremlin, labs, devops, emacs, infra, sre} = 9
    # intersection = 3 → 3/9
    assert ratio == pytest.approx(3 / 9)


@pytest.mark.asyncio
async def test_pref_overlap_returns_zero_when_no_profiles(sessionmaker):
    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        keywords, ratio = await svc._pref_overlap(session, chat_id=42, a=100, b=200)
    assert keywords == []
    assert ratio == 0.0


@pytest.mark.asyncio
async def test_pref_overlap_case_insensitive_and_dedup(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        session.add(UserMemoryProfile(
            chat_id=chat_id, user_id=a,
            preferences=["Docker", "DOCKER", "vim"],
            projects=[], identity=[],
        ))
        session.add(UserMemoryProfile(
            chat_id=chat_id, user_id=b,
            preferences=["docker"],
            projects=[], identity=[],
        ))
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        keywords, ratio = await svc._pref_overlap(session, chat_id=chat_id, a=a, b=b)
    assert keywords == ["docker"]
    # union = {docker, vim} = 2; intersection = 1
    assert ratio == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_compute_metrics_returns_shipmetrics_dataclass(sessionmaker):
    chat_id = 42
    a, b = 100, 200
    async with sessionmaker() as session:
        # 1 reply pair: A→B
        await _seed_msg(session, chat_id=chat_id, user_id=a, msg_id=1)
        await _seed_msg(session, chat_id=chat_id, user_id=b, msg_id=2, reply_to=1)
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        metrics = await svc._compute_metrics(session, chat_id=chat_id, a=a, b=b)

    assert metrics.reply_count == 1
    assert 0.0 <= metrics.reply_rate <= 1.0
    assert 0.0 <= metrics.mention_rate <= 1.0
    assert 0.0 <= metrics.co_activity <= 1.0
    assert 0.0 <= metrics.pref_overlap <= 1.0


def test_aggregate_score_clamps_to_0_100():
    metrics = ShipMetrics(
        reply_count=10, mention_count=2, co_active_days=15,
        pref_overlap_keywords=["x"],
        reply_rate=1.0, mention_rate=1.0, co_activity=1.0, pref_overlap=1.0,
    )
    assert ShipService.aggregate_score(metrics) == 100


def test_aggregate_score_zero_when_no_signal():
    metrics = ShipMetrics(
        reply_count=0, mention_count=0, co_active_days=0,
        pref_overlap_keywords=[],
        reply_rate=0.0, mention_rate=0.0, co_activity=0.0, pref_overlap=0.0,
    )
    assert ShipService.aggregate_score(metrics) == 0


def test_aggregate_score_weighted_mid():
    # reply=0.5*0.35 + mention=0.2*0.15 + co=0.4*0.25 + pref=0.6*0.25 = 0.175+0.03+0.10+0.15 ≈ 0.455
    # Float-precision: 100*weighted == 45.49999999999999 → round() = 45
    metrics = ShipMetrics(
        reply_count=0, mention_count=0, co_active_days=0,
        pref_overlap_keywords=[],
        reply_rate=0.5, mention_rate=0.2, co_activity=0.4, pref_overlap=0.6,
    )
    assert ShipService.aggregate_score(metrics) == 45


from app.models import ShipResult  # noqa: E402


@pytest.mark.asyncio
async def test_load_cached_returns_recent_row(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(ShipResult(
            chat_id=chat_id, user_id_a=100, user_id_b=200,
            score=73, payload={"reply_count": 5},
            rendered_text="💞 73/100. Любовь да и только.",
            computed_at=datetime.utcnow() - timedelta(hours=1),
        ))
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        cached = await svc._load_cached(session, chat_id=chat_id, a=100, b=200)
    assert cached is not None
    assert cached.score == 73


@pytest.mark.asyncio
async def test_load_cached_returns_none_when_stale(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(ShipResult(
            chat_id=chat_id, user_id_a=100, user_id_b=200,
            score=73, payload={}, rendered_text="old",
            computed_at=datetime.utcnow() - timedelta(hours=30),
        ))
        await session.commit()

    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        cached = await svc._load_cached(session, chat_id=chat_id, a=100, b=200)
    assert cached is None


@pytest.mark.asyncio
async def test_load_cached_returns_none_when_missing(sessionmaker):
    svc = _make_service(sessionmaker)
    async with sessionmaker() as session:
        cached = await svc._load_cached(session, chat_id=42, a=100, b=200)
    assert cached is None


@pytest.mark.asyncio
async def test_persist_inserts_then_updates_same_pair(sessionmaker):
    svc = _make_service(sessionmaker)
    await svc._persist(
        chat_id=42, a=100, b=200,
        score=50, payload={"v": 1}, rendered_text="first",
    )
    await svc._persist(
        chat_id=42, a=100, b=200,
        score=80, payload={"v": 2}, rendered_text="second",
    )

    async with sessionmaker() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(ShipResult))).scalars().all()
    assert len(rows) == 1
    assert rows[0].score == 80
    assert rows[0].rendered_text == "second"
    assert rows[0].payload["v"] == 2
