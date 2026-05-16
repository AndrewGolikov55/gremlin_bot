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
