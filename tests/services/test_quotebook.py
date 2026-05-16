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

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.models import Chat, Message, QuoteWeekRound, RouletteScoreAdjustment
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


def _truncate_for_assert(s: str) -> str:
    return s if len(s) <= 100 else s[:99] + "…"


@pytest.mark.asyncio
async def test_open_new_round_publishes_poll_and_persists(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        for i in range(1, 5):  # 4 кандидата — лестница: 3..6 → all
            session.add(Message(
                chat_id=chat_id, message_id=i, user_id=100 + i,
                text=f"цитата номер {i} длиной побольше двадцати",
                is_bot=False, date=now - timedelta(days=1),
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    poll_msg = type("PM", (), {})()
    poll_msg.message_id = 999
    poll_msg.poll = type("P", (), {})()
    poll_msg.poll.id = "poll-abc"
    svc.bot.send_poll = AsyncMock(return_value=poll_msg)

    opened = await svc.open_new_round(chat_id=chat_id, now=now)
    assert opened is True

    svc.bot.send_poll.assert_awaited_once()
    kwargs = svc.bot.send_poll.call_args.kwargs
    assert kwargs["chat_id"] == chat_id
    assert kwargs["type"] == "regular"
    assert kwargs["is_anonymous"] is False
    assert kwargs["allows_multiple_answers"] is False
    assert len(kwargs["options"]) == 4

    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        row = (await session.execute(_s(QuoteWeekRound))).scalar_one()
        assert row.chat_id == chat_id
        assert row.poll_id == "poll-abc"
        assert row.poll_message_id == 999
        assert len(row.options) == 4
        assert row.closed_at is None


@pytest.mark.asyncio
async def test_open_new_round_skips_when_under_three(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        # Только 2 годных
        for i in range(1, 3):
            session.add(Message(
                chat_id=chat_id, message_id=i, user_id=100 + i,
                text=f"короткая цитата {i} норм длины уже",
                is_bot=False, date=now - timedelta(days=1),
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.bot.send_poll = AsyncMock()

    opened = await svc.open_new_round(chat_id=chat_id, now=now)
    assert opened is False
    svc.bot.send_poll.assert_not_awaited()

    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        rows = (await session.execute(_s(QuoteWeekRound))).all()
        assert rows == []


@pytest.mark.asyncio
async def test_open_new_round_truncates_long_options(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        long_text = "а" * 280  # > 100
        for i in range(1, 4):
            session.add(Message(
                chat_id=chat_id, message_id=i, user_id=100 + i,
                text=long_text, is_bot=False, date=now - timedelta(days=1),
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    poll_msg = type("PM", (), {})()
    poll_msg.message_id = 1
    poll_msg.poll = type("P", (), {})()
    poll_msg.poll.id = "p"
    svc.bot.send_poll = AsyncMock(return_value=poll_msg)

    opened = await svc.open_new_round(chat_id=chat_id, now=now)
    assert opened is True

    kwargs = svc.bot.send_poll.call_args.kwargs
    for opt in kwargs["options"]:
        assert len(opt) <= 100
        assert opt.endswith("…")

    # В персистенс пишем ПОЛНЫЙ текст
    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        row = (await session.execute(_s(QuoteWeekRound))).scalar_one()
        for opt in row.options:
            assert len(opt["text"]) == 280


@pytest.mark.asyncio
async def test_open_new_round_idempotent_on_integrity_race(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)
    week_start = date(2026, 5, 11)  # понедельник прошлой недели для now=17

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        # Preexisting row на ту же неделю — будет конфликт по UNIQUE
        session.add(QuoteWeekRound(
            chat_id=chat_id, week_start=week_start,
            poll_id="existing", poll_message_id=1, options=[],
            opened_at=datetime(2026, 5, 17, 19, 0, 0),
        ))
        for i in range(1, 5):
            session.add(Message(
                chat_id=chat_id, message_id=i, user_id=100 + i,
                text=f"цитата {i} длиннее двадцати символов норм",
                is_bot=False, date=now - timedelta(days=1),
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    poll_msg = type("PM", (), {})()
    poll_msg.message_id = 2
    poll_msg.poll = type("P", (), {})()
    poll_msg.poll.id = "new-poll"
    svc.bot.send_poll = AsyncMock(return_value=poll_msg)
    svc.bot.stop_poll = AsyncMock()

    opened = await svc.open_new_round(chat_id=chat_id, now=now)
    assert opened is False  # racing insert lost
    svc.bot.stop_poll.assert_awaited_once_with(chat_id=chat_id, message_id=2)


@pytest.mark.asyncio
async def test_open_new_round_handles_forbidden(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        for i in range(1, 5):
            session.add(Message(
                chat_id=chat_id, message_id=i, user_id=100 + i,
                text=f"цитата {i} длиннее двадцати символов норм",
                is_bot=False, date=now - timedelta(days=1),
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.bot.send_poll = AsyncMock(
        side_effect=TelegramForbiddenError(method=None, message="bot was kicked")  # type: ignore[arg-type]
    )

    # Не должно бросить
    opened = await svc.open_new_round(chat_id=chat_id, now=now)
    assert opened is False
    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        rows = (await session.execute(_s(QuoteWeekRound))).all()
        assert rows == []


def _stub_stop_poll_result(voter_counts: list[int]):
    """Return a fake Poll object compatible with bot.stop_poll return value."""
    poll = type("P", (), {})()
    poll.total_voter_count = sum(voter_counts)
    opts = []
    for c in voter_counts:
        o = type("O", (), {})()
        o.voter_count = c
        opts.append(o)
    poll.options = opts
    return poll


@pytest.mark.asyncio
async def test_close_previous_no_open_round_is_noop(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.bot.stop_poll = AsyncMock()
    svc.bot.send_message = AsyncMock()

    await svc.close_previous_round_if_any(chat_id=chat_id, now=now)

    svc.bot.stop_poll.assert_not_awaited()
    svc.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_previous_zero_votes_no_adjustment(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        session.add(QuoteWeekRound(
            chat_id=chat_id, week_start=date(2026, 5, 4),
            poll_id="p1", poll_message_id=10,
            options=[
                {"text": "a", "author_user_id": 100, "source_message_id": 1},
                {"text": "b", "author_user_id": 101, "source_message_id": 2},
            ],
            opened_at=datetime(2026, 5, 10, 17, 0, 0),
        ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.bot.stop_poll = AsyncMock(return_value=_stub_stop_poll_result([0, 0]))
    svc.bot.send_message = AsyncMock()

    await svc.close_previous_round_if_any(chat_id=chat_id, now=now)

    svc.bot.stop_poll.assert_awaited_once_with(chat_id=chat_id, message_id=10)
    svc.bot.send_message.assert_awaited()

    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        row = (await session.execute(_s(QuoteWeekRound))).scalar_one()
        assert row.closed_at is not None
        assert row.winner_user_id is None
        assert row.winner_option_idx is None
        assert row.final_counts == [0, 0]
        adjustments = (await session.execute(_s(RouletteScoreAdjustment))).all()
        assert adjustments == []


@pytest.mark.asyncio
async def test_close_previous_single_winner_adds_plus_one(sessionmaker, monkeypatch):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        session.add(QuoteWeekRound(
            chat_id=chat_id, week_start=date(2026, 5, 4),
            poll_id="p1", poll_message_id=10,
            options=[
                {"text": "первый вариант", "author_user_id": 100, "source_message_id": 1},
                {"text": "второй вариант", "author_user_id": 101, "source_message_id": 2},
            ],
            opened_at=datetime(2026, 5, 10, 17, 0, 0),
        ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.app_config.get_all = AsyncMock(return_value={})
    svc.bot.stop_poll = AsyncMock(return_value=_stub_stop_poll_result([1, 3]))
    svc.bot.send_message = AsyncMock()
    svc.bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="not found")  # type: ignore[arg-type]
    )

    async def fake_gen(messages, **kw):
        return "📜 Афоризм недели — «второй вариант» от id101!"
    monkeypatch.setattr("app.services.quotebook.llm_generate", fake_gen)

    await svc.close_previous_round_if_any(chat_id=chat_id, now=now)

    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        row = (await session.execute(_s(QuoteWeekRound))).scalar_one()
        assert row.winner_user_id == 101
        assert row.winner_option_idx == 1
        assert row.final_counts == [1, 3]
        assert row.closed_at is not None

        adj = (await session.execute(_s(RouletteScoreAdjustment))).scalars().all()
        assert len(adj) == 1
        assert adj[0].user_id == 101
        assert adj[0].delta == 1
        assert adj[0].reason == "quote_week_winner"
        assert adj[0].source_id == row.id

    # Одно объявление (LLM-рендер)
    svc.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_previous_runoff_on_tie(sessionmaker, monkeypatch):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        session.add(QuoteWeekRound(
            chat_id=chat_id, week_start=date(2026, 5, 4),
            poll_id="p1", poll_message_id=10,
            options=[
                {"text": "первый", "author_user_id": 100, "source_message_id": 1},
                {"text": "второй", "author_user_id": 101, "source_message_id": 2},
                {"text": "третий", "author_user_id": 102, "source_message_id": 3},
            ],
            opened_at=datetime(2026, 5, 10, 17, 0, 0),
        ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.app_config.get_all = AsyncMock(return_value={})
    # Ничья на местах 0 и 2: counts = [3, 1, 3]
    svc.bot.stop_poll = AsyncMock(return_value=_stub_stop_poll_result([3, 1, 3]))
    svc.bot.send_message = AsyncMock()
    svc.bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="x")  # type: ignore[arg-type]
    )

    async def fake_gen(messages, **kw):
        return "Финальное объявление в персоне."
    monkeypatch.setattr("app.services.quotebook.llm_generate", fake_gen)
    monkeypatch.setattr("app.services.quotebook.DRAMA_PAUSE_SEC", 0)
    monkeypatch.setattr(
        "app.services.quotebook.random.choice",
        lambda seq: seq[-1],  # выбираем индекс 2
    )

    await svc.close_previous_round_if_any(chat_id=chat_id, now=now)

    # 3 сообщения runoff + 1 финальное оглашение = 4
    assert svc.bot.send_message.await_count == 4

    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        row = (await session.execute(_s(QuoteWeekRound))).scalar_one()
        assert row.winner_option_idx == 2
        assert row.winner_user_id == 102

        adj = (await session.execute(_s(RouletteScoreAdjustment))).scalars().all()
        assert len(adj) == 1
        assert adj[0].user_id == 102
        assert adj[0].delta == 1


@pytest.mark.asyncio
async def test_process_chat_closes_then_opens(sessionmaker, monkeypatch):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        # Открытый раунд предыдущей недели
        session.add(QuoteWeekRound(
            chat_id=chat_id, week_start=date(2026, 5, 4),
            poll_id="prev", poll_message_id=10,
            options=[
                {"text": "цитата прошлой недели", "author_user_id": 100, "source_message_id": 1},
                {"text": "ещё одна цитата прошлой недели", "author_user_id": 101, "source_message_id": 2},
            ],
            opened_at=datetime(2026, 5, 10, 17, 0, 0),
        ))
        # Сообщения для нового раунда
        for i in range(1, 5):
            session.add(Message(
                chat_id=chat_id, message_id=200 + i, user_id=100 + i,
                text=f"новая цитата {i} длиннее двадцати символов",
                is_bot=False, date=now - timedelta(days=1),
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.settings.get_all = AsyncMock(return_value={"is_active": True})
    svc.app_config.get_all = AsyncMock(return_value={})
    svc.bot.stop_poll = AsyncMock(return_value=_stub_stop_poll_result([0, 1]))
    svc.bot.send_message = AsyncMock()
    svc.bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="x")  # type: ignore[arg-type]
    )

    new_poll = type("PM", (), {})()
    new_poll.message_id = 555
    new_poll.poll = type("P", (), {})()
    new_poll.poll.id = "new-poll"
    svc.bot.send_poll = AsyncMock(return_value=new_poll)

    async def fake_gen(messages, **kw):
        return "🏆 Объявление"
    monkeypatch.setattr("app.services.quotebook.llm_generate", fake_gen)

    await svc.process_chat(chat_id=chat_id, now=now)

    # Закрытие: stop_poll по старому
    svc.bot.stop_poll.assert_awaited_once_with(chat_id=chat_id, message_id=10)
    # Публикация нового poll
    svc.bot.send_poll.assert_awaited_once()

    async with sessionmaker() as session:
        from sqlalchemy import select as _s
        rows = (await session.execute(_s(QuoteWeekRound).order_by(QuoteWeekRound.opened_at))).scalars().all()
        assert len(rows) == 2
        # Старый закрыт
        assert rows[0].poll_id == "prev"
        assert rows[0].closed_at is not None
        assert rows[0].winner_user_id == 101
        # Новый открыт
        assert rows[1].poll_id == "new-poll"
        assert rows[1].closed_at is None


@pytest.mark.asyncio
async def test_process_chat_skips_inactive_chat(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=False))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.settings.get_all = AsyncMock(return_value={"is_active": True})
    svc.bot.stop_poll = AsyncMock()
    svc.bot.send_poll = AsyncMock()
    svc.bot.send_message = AsyncMock()

    await svc.process_chat(chat_id=chat_id, now=datetime(2026, 5, 17, 20, 0, 0))

    svc.bot.stop_poll.assert_not_awaited()
    svc.bot.send_poll.assert_not_awaited()
    svc.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_chat_skips_when_settings_disabled(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        await session.commit()

    svc = _make_svc(sessionmaker)
    svc.settings.get_all = AsyncMock(return_value={"is_active": False})
    svc.bot.send_poll = AsyncMock()
    svc.bot.send_message = AsyncMock()

    await svc.process_chat(chat_id=chat_id, now=datetime(2026, 5, 17, 20, 0, 0))

    svc.bot.send_poll.assert_not_awaited()
    svc.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_all_chats_iterates_active_with_isolation(sessionmaker, monkeypatch):
    async with sessionmaker() as session:
        session.add(Chat(id=1, title="A", is_active=True))
        session.add(Chat(id=2, title="B", is_active=True))
        session.add(Chat(id=3, title="C", is_active=False))  # пропустится
        await session.commit()

    svc = _make_svc(sessionmaker)
    processed: list[int] = []

    async def fake_process(*, chat_id, now):
        processed.append(chat_id)
        if chat_id == 1:
            raise RuntimeError("simulated chat 1 failure")

    monkeypatch.setattr(svc, "process_chat", fake_process)
    monkeypatch.setattr("app.services.quotebook.PER_CHAT_SLEEP_SEC", 0)

    fixed_now = datetime(2026, 5, 17, 20, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    import datetime as _real
    class _DT:
        now = staticmethod(lambda tz=None: fixed_now if tz else fixed_now.replace(tzinfo=None))
        combine = staticmethod(_real.datetime.combine)
        utcnow = staticmethod(_real.datetime.utcnow)
    monkeypatch.setattr("app.services.quotebook.datetime", _DT)

    # Не должно бросить
    await svc.tick_all_chats()

    # Оба активных чата попытаны (даже после исключения первого)
    assert sorted(processed) == [1, 2]
