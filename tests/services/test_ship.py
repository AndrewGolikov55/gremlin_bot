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


import unittest.mock as um  # noqa: E402

from app.services.llm.client import LLMError  # noqa: E402


@pytest.mark.asyncio
async def test_render_calls_llm_with_persona_system_and_numbers(sessionmaker):
    captured: dict = {}

    async def fake_generate(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "💞 73/100\nалиса и боб — энергия общая, цифры тёплые."

    svc = _make_service(sessionmaker)
    svc.settings.get_all = AsyncMock(return_value={"style": "standup"})
    svc.app_config.get_all = AsyncMock(return_value={})
    svc.personas.get = AsyncMock(return_value="ТЫ — STANDUP-комик.")

    metrics = ShipMetrics(
        reply_count=5, mention_count=2, co_active_days=8,
        pref_overlap_keywords=["docker", "vim"],
        reply_rate=0.5, mention_rate=0.1, co_activity=0.27, pref_overlap=0.3,
    )

    with um.patch("app.services.ship.llm_generate", fake_generate):
        text = await svc._render(
            chat_id=42, name_a="Алиса", name_b="Боб", score=73, metrics=metrics,
        )

    assert "73" in text or "Алиса" in text
    # system message contains persona
    assert captured["messages"][0]["role"] == "system"
    assert "STANDUP" in captured["messages"][0]["content"]
    # user message contains numbers and names
    user_content = captured["messages"][1]["content"]
    assert "Алиса" in user_content
    assert "Боб" in user_content
    assert "73" in user_content
    assert "5" in user_content  # reply_count
    assert "docker" in user_content


@pytest.mark.asyncio
async def test_render_falls_back_to_plain_text_on_llm_error(sessionmaker):
    svc = _make_service(sessionmaker)
    svc.settings.get_all = AsyncMock(return_value={"style": "standup"})
    svc.app_config.get_all = AsyncMock(return_value={})
    svc.personas.get = AsyncMock(return_value="anything")

    async def boom(messages, **kwargs):
        raise LLMError("provider down")

    metrics = ShipMetrics(
        reply_count=5, mention_count=2, co_active_days=8,
        pref_overlap_keywords=["docker", "vim"],
        reply_rate=0.5, mention_rate=0.1, co_activity=0.27, pref_overlap=0.3,
    )

    with um.patch("app.services.ship.llm_generate", boom):
        text = await svc._render(
            chat_id=42, name_a="Алиса", name_b="Боб", score=73, metrics=metrics,
        )

    assert "💞" in text
    assert "Алиса" in text and "Боб" in text
    assert "73" in text
    assert "docker" in text
    assert "5" in text  # reply_count


@pytest.mark.asyncio
async def test_render_fallback_when_keywords_empty(sessionmaker):
    svc = _make_service(sessionmaker)
    svc.settings.get_all = AsyncMock(return_value={"style": "standup"})
    svc.app_config.get_all = AsyncMock(return_value={})
    svc.personas.get = AsyncMock(return_value="anything")

    async def boom(messages, **kwargs):
        raise LLMError("down")

    metrics = ShipMetrics(
        reply_count=0, mention_count=0, co_active_days=0,
        pref_overlap_keywords=[],
        reply_rate=0.0, mention_rate=0.0, co_activity=0.0, pref_overlap=0.0,
    )

    with um.patch("app.services.ship.llm_generate", boom):
        text = await svc._render(
            chat_id=42, name_a="Алиса", name_b="Боб", score=0, metrics=metrics,
        )

    assert "не нашлось" in text or "не нашло" in text or "почти нет" in text.lower()


@pytest.mark.asyncio
async def test_resolve_candidate_by_username_from_roulette_participant(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteParticipant(chat_id=chat_id, user_id=100, username="alice"))
        await session.commit()

    svc = _make_service(sessionmaker)
    res = await svc.resolve_candidate(chat_id=chat_id, candidate=("username", "alice"))
    assert res is not None
    user_id, display = res
    assert user_id == 100
    assert display == "alice"


@pytest.mark.asyncio
async def test_resolve_candidate_by_username_case_insensitive_and_with_at(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteParticipant(chat_id=chat_id, user_id=100, username="Alice"))
        await session.commit()

    svc = _make_service(sessionmaker)
    res = await svc.resolve_candidate(chat_id=chat_id, candidate=("username", "@ALICE"))
    assert res is not None
    assert res[0] == 100


@pytest.mark.asyncio
async def test_resolve_candidate_by_username_falls_back_to_users_table(sessionmaker):
    from app.models import User
    async with sessionmaker() as session:
        session.add(User(tg_id=100, username="alice"))
        await session.commit()

    svc = _make_service(sessionmaker)
    res = await svc.resolve_candidate(chat_id=42, candidate=("username", "alice"))
    assert res is not None
    assert res[0] == 100


@pytest.mark.asyncio
async def test_resolve_candidate_returns_none_for_unknown_username(sessionmaker):
    svc = _make_service(sessionmaker)
    res = await svc.resolve_candidate(chat_id=42, candidate=("username", "ghost"))
    assert res is None


@pytest.mark.asyncio
async def test_resolve_candidate_by_id_returns_username_when_known(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteParticipant(chat_id=chat_id, user_id=100, username="alice"))
        await session.commit()

    svc = _make_service(sessionmaker)
    res = await svc.resolve_candidate(chat_id=chat_id, candidate=("id", 100))
    assert res is not None
    assert res == (100, "alice")


@pytest.mark.asyncio
async def test_resolve_candidate_by_id_returns_id_string_when_unknown(sessionmaker):
    svc = _make_service(sessionmaker)
    res = await svc.resolve_candidate(chat_id=42, candidate=("id", 100))
    assert res is not None
    assert res == (100, "id100")


@pytest.mark.asyncio
async def test_compute_or_cached_self_ship_returns_meta_no_llm(sessionmaker):
    svc = _make_service(sessionmaker)
    svc.bot.id = 7
    # ensure get_me unused; we pass bot_id explicitly anyway

    async def must_not_call(*a, **kw):
        raise AssertionError("LLM must not be called for self-ship")

    with um.patch("app.services.ship.llm_generate", must_not_call):
        outcome = await svc.compute_or_cached(
            chat_id=42, a=(100, "Алиса"), b=(100, "Алиса"), bot_id=7,
        )

    assert outcome.score == -1
    assert outcome.cached is False
    assert "100%" in outcome.rendered_text or "синдром" in outcome.rendered_text.lower()

    # No row written
    async with sessionmaker() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(ShipResult))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_compute_or_cached_refuses_bot_in_pair(sessionmaker):
    svc = _make_service(sessionmaker)

    async def must_not_call(*a, **kw):
        raise AssertionError("LLM must not be called when bot is in pair")

    with um.patch("app.services.ship.llm_generate", must_not_call):
        outcome = await svc.compute_or_cached(
            chat_id=42, a=(7, "GremlinBot"), b=(100, "Алиса"), bot_id=7,
        )

    assert outcome.score == -1
    assert outcome.cached is False
    assert "бот" in outcome.rendered_text.lower()

    async with sessionmaker() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(ShipResult))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_compute_or_cached_pre_check_no_messages_does_not_persist(sessionmaker):
    svc = _make_service(sessionmaker)

    outcome = await svc.compute_or_cached(
        chat_id=42, a=(100, "Алиса"), b=(200, "Боб"), bot_id=7,
    )

    assert outcome.score == -1
    assert "@Алиса" in outcome.rendered_text or "@Боб" in outcome.rendered_text
    async with sessionmaker() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(ShipResult))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_compute_or_cached_cache_hit_skips_llm(sessionmaker):
    chat_id = 42
    a_id, b_id = 100, 200
    async with sessionmaker() as session:
        session.add(ShipResult(
            chat_id=chat_id, user_id_a=a_id, user_id_b=b_id,
            score=42, payload={}, rendered_text="💞 кэш",
            computed_at=datetime.utcnow() - timedelta(hours=2),
        ))
        # And messages so pre-check passes
        await _seed_msg(session, chat_id=chat_id, user_id=a_id, msg_id=1)
        await _seed_msg(session, chat_id=chat_id, user_id=b_id, msg_id=2)
        await session.commit()

    svc = _make_service(sessionmaker)

    async def must_not_call(*a, **kw):
        raise AssertionError("LLM must not be called on cache hit")

    with um.patch("app.services.ship.llm_generate", must_not_call):
        outcome = await svc.compute_or_cached(
            chat_id=chat_id, a=(a_id, "Алиса"), b=(b_id, "Боб"), bot_id=7,
        )

    assert outcome.cached is True
    assert outcome.score == 42
    assert outcome.rendered_text == "💞 кэш"


@pytest.mark.asyncio
async def test_compute_or_cached_full_run_persists_and_returns_text(sessionmaker):
    chat_id = 42
    a_id, b_id = 100, 200
    async with sessionmaker() as session:
        # seed messages so both have presence + 1 reply pair
        await _seed_msg(session, chat_id=chat_id, user_id=a_id, msg_id=1)
        await _seed_msg(session, chat_id=chat_id, user_id=b_id, msg_id=2, reply_to=1)
        await session.commit()

    svc = _make_service(sessionmaker)
    svc.settings.get_all = AsyncMock(return_value={"style": "standup"})
    svc.app_config.get_all = AsyncMock(return_value={})
    svc.personas.get = AsyncMock(return_value="persona")

    async def fake_generate(messages, **kwargs):
        return "💞 рассчитано"

    with um.patch("app.services.ship.llm_generate", fake_generate):
        outcome = await svc.compute_or_cached(
            chat_id=chat_id, a=(a_id, "Алиса"), b=(b_id, "Боб"), bot_id=7,
        )

    assert outcome.cached is False
    assert outcome.score >= 0
    assert outcome.rendered_text == "💞 рассчитано"

    async with sessionmaker() as session:
        from sqlalchemy import select
        rows = (await session.execute(select(ShipResult))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id_a == min(a_id, b_id)
    assert rows[0].user_id_b == max(a_id, b_id)
    assert rows[0].rendered_text == "💞 рассчитано"


@pytest.mark.asyncio
async def test_pick_random_pair_returns_none_when_under_two_active(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        await _seed_msg(session, chat_id=chat_id, user_id=100, msg_id=1)
        await session.commit()

    svc = _make_service(sessionmaker)
    res = await svc.pick_random_pair(chat_id=chat_id, bot_id=7)
    assert res is None


@pytest.mark.asyncio
async def test_pick_random_pair_excludes_bot_and_is_bot_messages(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        await _seed_msg(session, chat_id=chat_id, user_id=100, msg_id=1)
        await _seed_msg(session, chat_id=chat_id, user_id=7, msg_id=2)  # bot's user id
        await _seed_msg(session, chat_id=chat_id, user_id=999, msg_id=3, is_bot=True)
        await session.commit()

    svc = _make_service(sessionmaker)
    res = await svc.pick_random_pair(chat_id=chat_id, bot_id=7)
    # only user 100 remains active → not enough
    assert res is None


@pytest.mark.asyncio
async def test_pick_random_pair_returns_two_active(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteParticipant(chat_id=chat_id, user_id=100, username="alice"))
        session.add(RouletteParticipant(chat_id=chat_id, user_id=200, username="bob"))
        await _seed_msg(session, chat_id=chat_id, user_id=100, msg_id=1)
        await _seed_msg(session, chat_id=chat_id, user_id=200, msg_id=2)
        await session.commit()

    svc = _make_service(sessionmaker)
    res = await svc.pick_random_pair(chat_id=chat_id, bot_id=7)
    assert res is not None
    a, b = res
    ids = {a[0], b[0]}
    assert ids == {100, 200}


@pytest.mark.asyncio
async def test_pick_random_pair_prefers_uncached_pairs(sessionmaker):
    chat_id = 42
    # 3 active users; pair (100,200) is cached, pair (100,300) and (200,300) not.
    async with sessionmaker() as session:
        for uid, name in [(100, "alice"), (200, "bob"), (300, "carl")]:
            session.add(RouletteParticipant(chat_id=chat_id, user_id=uid, username=name))
            await _seed_msg(session, chat_id=chat_id, user_id=uid, msg_id=uid)
        session.add(ShipResult(
            chat_id=chat_id, user_id_a=100, user_id_b=200,
            score=42, payload={}, rendered_text="cached",
            computed_at=datetime.utcnow() - timedelta(hours=1),
        ))
        await session.commit()

    svc = _make_service(sessionmaker)
    # Run repeatedly: must never pick (100,200) when uncached options exist
    seen_pairs: set[frozenset[int]] = set()
    for _ in range(20):
        res = await svc.pick_random_pair(chat_id=chat_id, bot_id=7)
        assert res is not None
        seen_pairs.add(frozenset({res[0][0], res[1][0]}))
    assert frozenset({100, 200}) not in seen_pairs
