from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.quotebook import QuotebookService, _week_start_for


def test_week_start_for_sunday_evening_returns_previous_monday():
    # Воскресенье 17 мая 2026, 20:00 МСК. Прошлая неделя пн..вс = 11..17 мая.
    now = datetime(2026, 5, 17, 20, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert _week_start_for(now) == date(2026, 5, 11)


def test_week_start_for_monday_morning_returns_previous_monday():
    # Понедельник 18 мая 2026, 09:00 МСК — это уже новая неделя, прошлая = 11..17 мая.
    now = datetime(2026, 5, 18, 9, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert _week_start_for(now) == date(2026, 5, 11)


def test_week_start_for_sunday_morning_returns_two_mondays_ago():
    # Воскресенье 17 мая 2026, 09:00 МСК — текущая неделя ещё идёт (пн..вс = 11..17).
    # Cron в 20:00; здесь покрываем краевой случай: ручной вызов утром тех же суток.
    # Прошлая неделя = пн..вс предыдущая (4..10 мая) — week_start=4 мая.
    now = datetime(2026, 5, 17, 9, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert _week_start_for(now) == date(2026, 5, 4)


def test_week_start_for_requires_tz_aware():
    naive = datetime(2026, 5, 17, 20, 0, 0)
    with pytest.raises(ValueError):
        _week_start_for(naive)


@pytest.mark.asyncio
async def test_service_lock_is_per_chat(sessionmaker):
    from unittest.mock import AsyncMock, create_autospec

    from app.services.app_config import AppConfigService
    from app.services.settings import SettingsService

    svc = QuotebookService(
        sessionmaker=sessionmaker,
        bot=AsyncMock(),
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
    )
    lock1 = svc._get_lock(1)
    lock2 = svc._get_lock(2)
    lock1_again = svc._get_lock(1)
    assert lock1 is lock1_again
    assert lock1 is not lock2


from datetime import timedelta
from unittest.mock import AsyncMock, create_autospec

from app.models import Chat, Message
from app.services.app_config import AppConfigService
from app.services.settings import SettingsService


def _make_svc(sessionmaker, *, bot_username: str = "gremlin_bot"):
    bot = AsyncMock()
    me = type("Me", (), {})()
    me.username = bot_username
    bot.get_me = AsyncMock(return_value=me)
    return QuotebookService(
        sessionmaker=sessionmaker,
        bot=bot,
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
    )


@pytest.mark.asyncio
async def test_collect_candidates_filters_bot_and_short_and_long(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)  # naive UTC-like

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        # Эталон — пройдёт
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=100,
            text="а" * 50, is_bot=False,
            date=now - timedelta(days=1),
        ))
        # is_bot=True — отфильтровать
        session.add(Message(
            chat_id=chat_id, message_id=2, user_id=999,
            text="а" * 50, is_bot=True,
            date=now - timedelta(days=1),
        ))
        # Короткое (< 20) — отфильтровать
        session.add(Message(
            chat_id=chat_id, message_id=3, user_id=100,
            text="коротко", is_bot=False,
            date=now - timedelta(days=1),
        ))
        # Длинное (> 300) — отфильтровать
        session.add(Message(
            chat_id=chat_id, message_id=4, user_id=100,
            text="а" * 400, is_bot=False,
            date=now - timedelta(days=1),
        ))
        # Команда — отфильтровать
        session.add(Message(
            chat_id=chat_id, message_id=5, user_id=100,
            text="/start " + "а" * 30, is_bot=False,
            date=now - timedelta(days=1),
        ))
        # Обращение к боту — отфильтровать
        session.add(Message(
            chat_id=chat_id, message_id=6, user_id=100,
            text="@gremlin_bot " + "а" * 30, is_bot=False,
            date=now - timedelta(days=1),
        ))
        # Вне окна (8 дней назад) — отфильтровать
        session.add(Message(
            chat_id=chat_id, message_id=7, user_id=100,
            text="а" * 50, is_bot=False,
            date=now - timedelta(days=8),
        ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    candidates = await svc.collect_candidates(chat_id=chat_id, now=now)

    assert [c.message_id for c in candidates] == [1]


@pytest.mark.asyncio
async def test_collect_candidates_counts_replies_within_window(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        # Целевая цитата
        session.add(Message(
            chat_id=chat_id, message_id=100, user_id=10,
            text="мощная цитата длиннее двадцати символов",
            is_bot=False, date=now - timedelta(days=2),
        ))
        # 2 ответа в окне
        for mid in (101, 102):
            session.add(Message(
                chat_id=chat_id, message_id=mid, user_id=20,
                text="ответ длиннее двадцати символов сюда",
                is_bot=False, reply_to_id=100,
                date=now - timedelta(days=1),
            ))
        # 1 ответ ВНЕ окна — не должен считаться в reply_count
        session.add(Message(
            chat_id=chat_id, message_id=103, user_id=20,
            text="ответ длиннее двадцати символов сюда",
            is_bot=False, reply_to_id=100,
            date=now - timedelta(days=10),
        ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    candidates = await svc.collect_candidates(chat_id=chat_id, now=now)
    # Только сообщение id=100 проходит фильтр и считается; ответы тоже подходят
    # под фильтр (длина OK, не бот), они тоже окажутся в списке. Проверим reply_count
    # для исходного сообщения.
    by_id = {c.message_id: c for c in candidates}
    assert 100 in by_id
    assert by_id[100].reply_count == 2
    # Ответы (101, 102) тоже кандидаты, но их reply_count = 0
    assert by_id[101].reply_count == 0
    assert by_id[102].reply_count == 0


def test_score_grows_with_replies():
    from app.services.quotebook import Candidate, score_candidate

    now = datetime(2026, 5, 17, 20, 0, 0)
    a = Candidate(message_id=1, user_id=10, text="а" * 100, reply_count=0,
                  date=now - timedelta(days=1))
    b = Candidate(message_id=2, user_id=10, text="а" * 100, reply_count=10,
                  date=now - timedelta(days=1))
    assert score_candidate(b, max_reply=10, now=now) > score_candidate(a, max_reply=10, now=now)


def test_score_grows_with_length_until_cap():
    from app.services.quotebook import Candidate, score_candidate

    now = datetime(2026, 5, 17, 20, 0, 0)
    short = Candidate(message_id=1, user_id=10, text="а" * 30, reply_count=0,
                      date=now - timedelta(days=1))
    long_ = Candidate(message_id=2, user_id=10, text="а" * 180, reply_count=0,
                      date=now - timedelta(days=1))
    capped = Candidate(message_id=3, user_id=10, text="а" * 300, reply_count=0,
                       date=now - timedelta(days=1))
    s_short = score_candidate(short, max_reply=1, now=now)
    s_long = score_candidate(long_, max_reply=1, now=now)
    s_capped = score_candidate(capped, max_reply=1, now=now)
    assert s_long > s_short
    # длина свыше 200 кэпится, дальше не растёт сама по себе
    assert abs(s_capped - s_long) < 0.05


def test_score_decays_with_age():
    from app.services.quotebook import Candidate, score_candidate

    now = datetime(2026, 5, 17, 20, 0, 0)
    fresh = Candidate(message_id=1, user_id=10, text="а" * 100, reply_count=0,
                      date=now - timedelta(hours=1))
    old = Candidate(message_id=2, user_id=10, text="а" * 100, reply_count=0,
                    date=now - timedelta(days=6, hours=23))
    assert score_candidate(fresh, max_reply=1, now=now) > score_candidate(old, max_reply=1, now=now)


def test_score_handles_zero_max_reply():
    from app.services.quotebook import Candidate, score_candidate
    now = datetime(2026, 5, 17, 20, 0, 0)
    c = Candidate(message_id=1, user_id=10, text="а" * 100, reply_count=0,
                  date=now - timedelta(days=1))
    # max_reply=0 — деление на ноль не должно случиться, reply-составляющая = 0
    s = score_candidate(c, max_reply=0, now=now)
    assert 0.0 <= s <= 1.0


def _cand(i: int, *, replies: int = 0, length: int = 50, age_days: float = 1.0, now: datetime | None = None):
    from app.services.quotebook import Candidate
    base = now or datetime(2026, 5, 17, 20, 0, 0)
    return Candidate(
        message_id=i, user_id=100 + i, text="а" * length,
        reply_count=replies, date=base - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_select_options_skips_when_under_three(sessionmaker):
    svc = _make_svc(sessionmaker)
    now = datetime(2026, 5, 17, 20, 0, 0)
    candidates = [_cand(1, now=now), _cand(2, now=now)]
    result = await svc.select_options(candidates, now=now)
    assert result == []


@pytest.mark.asyncio
async def test_select_options_returns_all_when_three_to_six_without_llm(sessionmaker, monkeypatch):
    svc = _make_svc(sessionmaker)
    now = datetime(2026, 5, 17, 20, 0, 0)
    candidates = [_cand(i, now=now, replies=i) for i in range(1, 6)]  # 5 кандидатов

    called = False
    async def fake_llm(*a, **kw):
        nonlocal called
        called = True
        raise AssertionError("LLM must not be called for 3..6 candidates")

    monkeypatch.setattr("app.services.quotebook.llm_generate", fake_llm)
    result = await svc.select_options(candidates, now=now)

    assert len(result) == 5
    assert called is False
    # Сортировка по score desc → id с большим reply_count раньше
    assert result[0].source_message_id == 5
    assert result[-1].source_message_id == 1


import json as _json


@pytest.mark.asyncio
async def test_select_options_more_than_six_calls_llm_with_top_fifty(sessionmaker, monkeypatch):
    svc = _make_svc(sessionmaker)
    svc.app_config.get_all = AsyncMock(return_value={})
    now = datetime(2026, 5, 17, 20, 0, 0)
    # 60 кандидатов
    candidates = [_cand(i, now=now, replies=i) for i in range(1, 61)]

    captured: dict = {}

    async def fake_llm(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        # LLM выбирает индексы 1, 3, 5 (1-based) из присланного списка
        return "[1, 3, 5]"

    monkeypatch.setattr("app.services.quotebook.llm_generate", fake_llm)
    result = await svc.select_options(candidates, now=now)

    # LLM получает топ-50 по score (1..60 по reply_count → id 60..11)
    user_msg = captured["messages"][-1]["content"]
    assert "[1]" in user_msg  # формат списка
    # max_tokens из LLM_MAX_TOKENS
    assert captured["kwargs"].get("max_tokens") == 100
    # LLM выбрал 3 индекса — получаем 3 опции
    assert len(result) == 3


@pytest.mark.asyncio
async def test_select_options_falls_back_on_bad_json(sessionmaker, monkeypatch):
    svc = _make_svc(sessionmaker)
    svc.app_config.get_all = AsyncMock(return_value={})
    now = datetime(2026, 5, 17, 20, 0, 0)
    candidates = [_cand(i, now=now, replies=i) for i in range(1, 11)]  # 10 кандидатов

    async def bad_llm(messages, **kwargs):
        return "это не JSON, тут только болтовня"

    monkeypatch.setattr("app.services.quotebook.llm_generate", bad_llm)
    result = await svc.select_options(candidates, now=now)

    # Fallback на эвристический top-6
    assert len(result) == 6
    assert [opt.source_message_id for opt in result] == [10, 9, 8, 7, 6, 5]


@pytest.mark.asyncio
async def test_select_options_falls_back_on_llm_error(sessionmaker, monkeypatch):
    from app.services.llm.client import LLMError

    svc = _make_svc(sessionmaker)
    svc.app_config.get_all = AsyncMock(return_value={})
    now = datetime(2026, 5, 17, 20, 0, 0)
    candidates = [_cand(i, now=now, replies=i) for i in range(1, 11)]

    async def boom(messages, **kwargs):
        raise LLMError("provider down")

    monkeypatch.setattr("app.services.quotebook.llm_generate", boom)
    result = await svc.select_options(candidates, now=now)
    assert len(result) == 6


@pytest.mark.asyncio
async def test_select_options_drops_out_of_range_indices(sessionmaker, monkeypatch):
    svc = _make_svc(sessionmaker)
    svc.app_config.get_all = AsyncMock(return_value={})
    now = datetime(2026, 5, 17, 20, 0, 0)
    candidates = [_cand(i, now=now, replies=i) for i in range(1, 11)]

    async def weird_llm(messages, **kwargs):
        # 99 — вне диапазона, 0 — вне (1-based), 1 — ок, 2 — ок
        return "[99, 0, 1, 2]"

    monkeypatch.setattr("app.services.quotebook.llm_generate", weird_llm)
    result = await svc.select_options(candidates, now=now)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_select_options_caps_at_six_even_if_llm_returns_more(sessionmaker, monkeypatch):
    svc = _make_svc(sessionmaker)
    svc.app_config.get_all = AsyncMock(return_value={})
    now = datetime(2026, 5, 17, 20, 0, 0)
    candidates = [_cand(i, now=now, replies=i) for i in range(1, 11)]

    async def big_llm(messages, **kwargs):
        return _json.dumps(list(range(1, 11)))  # 10 индексов

    monkeypatch.setattr("app.services.quotebook.llm_generate", big_llm)
    result = await svc.select_options(candidates, now=now)
    assert len(result) == 6
