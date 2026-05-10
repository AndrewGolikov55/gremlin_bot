from __future__ import annotations

import unittest.mock as um
from datetime import date, datetime
from unittest.mock import AsyncMock, create_autospec
from zoneinfo import ZoneInfo

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from app.models import Chat, ChatMemory, MonthlyChampion, RouletteWinner
from app.services.app_config import AppConfigService
from app.services.llm.client import LLMError
from app.services.monthly_champion import MonthlyChampionService, _previous_period  # noqa: F401
from app.services.roulette import RouletteService
from app.services.settings import SettingsService


def test_previous_period_basic():
    # 1 мая 12:00 MSK → period [2026-04-01, 2026-05-01)
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2026, 4, 1)
    assert end_excl == date(2026, 5, 1)


def test_previous_period_january():
    # 1 января 12:00 MSK → period [2025-12-01, 2026-01-01)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2025, 12, 1)
    assert end_excl == date(2026, 1, 1)


def test_previous_period_mid_month():
    # 15 мая 03:00 MSK → period [2026-04-01, 2026-05-01)
    now = datetime(2026, 5, 15, 3, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2026, 4, 1)
    assert end_excl == date(2026, 5, 1)


def test_previous_period_requires_aware_datetime():
    naive = datetime(2026, 5, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        _previous_period(naive)


def _make_member(name: str | None, *, username: str | None = None, status: ChatMemberStatus = ChatMemberStatus.MEMBER) -> object:
    m = type("M", (), {})()
    m.status = status
    m.user = type("U", (), {})()
    m.user.first_name = name
    m.user.username = username
    return m


def _make_service(sessionmaker, bot):
    return MonthlyChampionService(
        sessionmaker=sessionmaker,
        bot=bot,
        roulette=create_autospec(RouletteService, instance=True),
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
    )


@pytest.mark.asyncio
async def test_resolve_display_name_active_member(sessionmaker):
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member("Андрей", username="andrew"))

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=42, user_id=100)
    assert name == "Андрей"


@pytest.mark.asyncio
async def test_resolve_display_name_uses_username_when_no_first_name(sessionmaker):
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member(None, username="andrew"))

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=42, user_id=100)
    assert name == "andrew"


@pytest.mark.asyncio
async def test_resolve_display_name_left_falls_back_to_winner_snapshot(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=100, username="andrew_old",
            won_at=date(2026, 4, 5), title="t", title_code="test",
            created_at=datetime(2026, 4, 5, 10, 0, 0),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member(None, status=ChatMemberStatus.LEFT))

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=chat_id, user_id=100)
    assert name == "andrew_old"


@pytest.mark.asyncio
async def test_resolve_display_name_get_chat_member_raises(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=100, username="snapshot_name",
            won_at=date(2026, 4, 5), title="t", title_code="test",
            created_at=datetime(2026, 4, 5, 10, 0, 0),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="user not found")  # type: ignore[arg-type]
    )

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=chat_id, user_id=100)
    assert name == "snapshot_name"


@pytest.mark.asyncio
async def test_resolve_display_name_no_snapshot_returns_id_string(sessionmaker):
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="not found")  # type: ignore[arg-type]
    )

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=42, user_id=100)
    assert name == "id100"


@pytest.mark.asyncio
async def test_render_winner_calls_llm_with_top(sessionmaker):
    from app.services.roulette import StatsEntry

    captured: dict = {}

    async def fake_generate(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "🏆 Король Мудаков месяца — Андрей! Ёбана."

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={"llm_provider": "openrouter"})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_winner(
            top=[
                StatsEntry(user_id=1, username="Андрей", wins=7),
                StatsEntry(user_id=2, username="Семён", wins=4),
            ],
            champion_name="Андрей",
            daily_title="Мудак дня",
            month_label="апрель 2026",
        )

    assert "Король" in text or "Андрей" in text
    assert any("Мудак дня" in m["content"] for m in captured["messages"])
    assert any("Андрей" in m["content"] for m in captured["messages"])


@pytest.mark.asyncio
async def test_render_winner_falls_back_when_llm_fails(sessionmaker):
    from app.services.roulette import StatsEntry

    async def fake_generate(messages, **kwargs):
        raise LLMError("provider down")

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_winner(
            top=[StatsEntry(user_id=1, username="Андрей", wins=7)],
            champion_name="Андрей",
            daily_title="Мудак дня",
            month_label="апрель 2026",
        )

    assert "Андрей" in text
    assert "Мудак" in text


@pytest.mark.asyncio
async def test_render_runoff_winner(sessionmaker):
    async def fake_generate(messages, **kwargs):
        return "Победил Андрей! Король Мудаков месяца, ёбана."

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_runoff_winner(
            tied_names=["Андрей", "Семён"],
            winner_name="Андрей",
            daily_title="Мудак дня",
        )
    assert "Андрей" in text


@pytest.mark.asyncio
async def test_render_runoff_falls_back_to_text_when_llm_fails(sessionmaker):
    async def fake_generate(messages, **kwargs):
        raise LLMError("down")

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_runoff_winner(
            tied_names=["Андрей", "Семён"],
            winner_name="Андрей",
            daily_title="Мудак дня",
        )
    assert "Андрей" in text
    assert "Мудак" in text


@pytest.mark.asyncio
async def test_render_empty_month(sessionmaker):
    async def fake_generate(messages, **kwargs):
        return "В этом месяце короля не нашлось."

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_empty(daily_title="Мудак дня", month_label="апрель 2026")
    assert text


@pytest.mark.asyncio
async def test_render_empty_falls_back(sessionmaker):
    async def fake_generate(messages, **kwargs):
        raise LLMError("down")

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_empty(daily_title="Мудак дня", month_label="апрель 2026")
    assert "Мудак" in text


@pytest.mark.asyncio
async def test_process_chat_single_winner(sessionmaker):
    from app.services.roulette import StatsEntry

    chat_id = 42
    period_start = date(2026, 4, 1)
    period_end = date(2026, 5, 1)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member("Андрей", username="andrew"))
    bot.send_message = AsyncMock()

    svc = _make_service(sessionmaker, bot)
    svc.roulette._aggregate = AsyncMock(return_value=[
        StatsEntry(user_id=100, username="andrew", wins=7),
        StatsEntry(user_id=101, username="semen", wins=4),
    ])
    svc.settings.get_all = AsyncMock(return_value={"is_active": True, "roulette_custom_title": "Мудак дня"})
    svc.app_config.get_all = AsyncMock(return_value={})

    async def fake_gen(messages, **kw):
        return "🏆 Король Мудаков месяца — Андрей!"

    with um.patch("app.services.monthly_champion.llm_generate", fake_gen):
        await svc.process_chat(chat_id=chat_id, period_start=period_start, period_end_excl=period_end)

    assert bot.send_message.await_count == 1

    async with sessionmaker() as session:
        row = (await session.execute(select(MonthlyChampion))).scalar_one()
        assert row.user_id == 100
        assert row.display_name == "Андрей"
        assert row.score == 7
        assert row.tied_with == []
        assert row.daily_title_snapshot == "Мудак дня"

        mem = await session.get(ChatMemory, chat_id)
        assert mem is not None
        assert mem.monthly_champion["user_id"] == 100
        assert mem.monthly_champion["title"] == "Мудак дня"
        assert mem.monthly_champion["display_name"] == "Андрей"
        assert mem.monthly_champion["period_start"] == "2026-04-01"


@pytest.mark.asyncio
async def test_process_chat_idempotent(sessionmaker):
    chat_id = 42
    period_start = date(2026, 4, 1)
    period_end = date(2026, 5, 1)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        session.add(MonthlyChampion(
            chat_id=chat_id, period_start=period_start,
            user_id=100, display_name="Андрей", score=7,
            tied_with=[], daily_title_snapshot="Мудак дня",
            announced_at=datetime(2026, 5, 1, 12, 0, 0),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.roulette._aggregate = AsyncMock()
    svc.settings.get_all = AsyncMock(return_value={"is_active": True})
    svc.app_config.get_all = AsyncMock(return_value={})

    await svc.process_chat(chat_id=chat_id, period_start=period_start, period_end_excl=period_end)

    bot.send_message.assert_not_awaited()
    svc.roulette._aggregate.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_chat_skips_inactive_chat(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=False))
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.settings.get_all = AsyncMock(return_value={"is_active": True})
    svc.app_config.get_all = AsyncMock(return_value={})

    await svc.process_chat(
        chat_id=chat_id, period_start=date(2026, 4, 1), period_end_excl=date(2026, 5, 1),
    )

    bot.send_message.assert_not_awaited()
    async with sessionmaker() as session:
        rows = (await session.execute(select(MonthlyChampion))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_process_chat_skips_when_settings_disabled(sessionmaker):
    """ChatSetting['is_active']=False → skip even though Chat.is_active=True"""
    chat_id = 42
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.settings.get_all = AsyncMock(return_value={"is_active": False})
    svc.app_config.get_all = AsyncMock(return_value={})

    await svc.process_chat(
        chat_id=chat_id, period_start=date(2026, 4, 1), period_end_excl=date(2026, 5, 1),
    )
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_chat_runoff(sessionmaker, monkeypatch):
    from app.services.roulette import StatsEntry

    chat_id = 42
    period_start = date(2026, 4, 1)
    period_end = date(2026, 5, 1)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        await session.commit()

    members_by_id = {
        100: _make_member("Андрей", username="andrew"),
        101: _make_member("Семён", username="semen"),
    }

    async def get_member(chat_id, user_id):
        return members_by_id[user_id]

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(side_effect=get_member)
    bot.send_message = AsyncMock()

    svc = _make_service(sessionmaker, bot)
    svc.roulette._aggregate = AsyncMock(return_value=[
        StatsEntry(user_id=100, username="andrew", wins=5),
        StatsEntry(user_id=101, username="semen", wins=5),
    ])
    svc.settings.get_all = AsyncMock(return_value={"is_active": True, "roulette_custom_title": "Мудак дня"})
    svc.app_config.get_all = AsyncMock(return_value={})

    monkeypatch.setattr(
        "app.services.monthly_champion.random.choice",
        lambda seq: next(x for x in seq if x.user_id == 100),
    )
    monkeypatch.setattr("app.services.monthly_champion.DRAMA_PAUSE_SEC", 0)

    async def fake_gen(messages, **kw):
        return "Король Мудаков — Андрей!"

    with um.patch("app.services.monthly_champion.llm_generate", fake_gen):
        await svc.process_chat(chat_id=chat_id, period_start=period_start, period_end_excl=period_end)

    # 3 сообщения: ничья, кости, оглашение
    assert bot.send_message.await_count == 3

    async with sessionmaker() as session:
        row = (await session.execute(select(MonthlyChampion))).scalar_one()
        assert row.user_id == 100
        assert sorted(row.tied_with) == [100, 101]


@pytest.mark.asyncio
async def test_process_chat_empty_month(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.get_chat_member = AsyncMock()

    svc = _make_service(sessionmaker, bot)
    svc.roulette._aggregate = AsyncMock(return_value=[])
    svc.settings.get_all = AsyncMock(return_value={"is_active": True})
    svc.app_config.get_all = AsyncMock(return_value={})

    async def fake_gen(messages, **kw):
        return "Никто не отличился."

    with um.patch("app.services.monthly_champion.llm_generate", fake_gen):
        await svc.process_chat(
            chat_id=chat_id, period_start=date(2026, 4, 1), period_end_excl=date(2026, 5, 1),
        )

    assert bot.send_message.await_count == 1
    async with sessionmaker() as session:
        row = (await session.execute(select(MonthlyChampion))).scalar_one()
        assert row.user_id is None
        assert row.score == 0
        mem = await session.get(ChatMemory, chat_id)
        assert mem is None or mem.monthly_champion is None


@pytest.mark.asyncio
async def test_process_chat_telegram_forbidden_does_not_raise(sessionmaker):
    from aiogram.exceptions import TelegramForbiddenError  # noqa: PLC0415

    from app.services.roulette import StatsEntry  # noqa: PLC0415

    chat_id = 42
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member("Андрей", username="andrew"))
    bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(method=None, message="bot was kicked"))  # type: ignore[arg-type]

    svc = _make_service(sessionmaker, bot)
    svc.roulette._aggregate = AsyncMock(return_value=[StatsEntry(user_id=100, username="andrew", wins=3)])
    svc.settings.get_all = AsyncMock(return_value={"is_active": True})
    svc.app_config.get_all = AsyncMock(return_value={})

    async def fake_gen(messages, **kw):
        return "x"

    # Не должно бросить
    with um.patch("app.services.monthly_champion.llm_generate", fake_gen):
        await svc.process_chat(
            chat_id=chat_id, period_start=date(2026, 4, 1), period_end_excl=date(2026, 5, 1),
        )

    # Сообщение пытались отправить (и упало)
    bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_run_monthly_summary_iterates_active_chats(sessionmaker, monkeypatch):
    async with sessionmaker() as session:
        session.add(Chat(id=1, title="A", is_active=True))
        session.add(Chat(id=2, title="B", is_active=True))
        session.add(Chat(id=3, title="C", is_active=False))  # inactive
        await session.commit()

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)

    processed: list[int] = []

    async def fake_process(*, chat_id, period_start, period_end_excl):
        processed.append(chat_id)

    monkeypatch.setattr(svc, "process_chat", fake_process)
    monkeypatch.setattr("app.services.monthly_champion.PER_CHAT_SLEEP_SEC", 0)

    fixed_now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    import datetime as _real_datetime

    class _DT:
        now = staticmethod(lambda tz=None: fixed_now if tz else fixed_now.replace(tzinfo=None))
        combine = staticmethod(_real_datetime.datetime.combine)
        utcnow = staticmethod(_real_datetime.datetime.utcnow)

    monkeypatch.setattr("app.services.monthly_champion.datetime", _DT)

    await svc.run_monthly_summary()

    # Только активные чаты (Chat.is_active=True)
    assert sorted(processed) == [1, 2]


@pytest.mark.asyncio
async def test_run_monthly_summary_continues_on_chat_failure(sessionmaker, monkeypatch):
    async with sessionmaker() as session:
        session.add(Chat(id=1, title="A", is_active=True))
        session.add(Chat(id=2, title="B", is_active=True))
        await session.commit()

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)

    processed: list[int] = []

    async def fake_process(*, chat_id, period_start, period_end_excl):
        processed.append(chat_id)
        if chat_id == 1:
            raise RuntimeError("simulated chat 1 failure")

    monkeypatch.setattr(svc, "process_chat", fake_process)
    monkeypatch.setattr("app.services.monthly_champion.PER_CHAT_SLEEP_SEC", 0)

    fixed_now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    import datetime as _real_datetime

    class _DT:
        now = staticmethod(lambda tz=None: fixed_now if tz else fixed_now.replace(tzinfo=None))
        combine = staticmethod(_real_datetime.datetime.combine)
        utcnow = staticmethod(_real_datetime.datetime.utcnow)

    monkeypatch.setattr("app.services.monthly_champion.datetime", _DT)

    # Не должно выбросить
    await svc.run_monthly_summary()

    # Оба чата были попытаны
    assert sorted(processed) == [1, 2]


@pytest.mark.asyncio
async def test_catch_up_skips_after_day_seven(sessionmaker, monkeypatch):
    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)

    called = False

    async def fake_run():
        nonlocal called
        called = True

    monkeypatch.setattr(svc, "run_monthly_summary", fake_run)

    fixed_now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    import datetime as _real_datetime

    class _DT:
        now = staticmethod(lambda tz=None: fixed_now if tz else fixed_now.replace(tzinfo=None))
        combine = staticmethod(_real_datetime.datetime.combine)
        utcnow = staticmethod(_real_datetime.datetime.utcnow)

    monkeypatch.setattr("app.services.monthly_champion.datetime", _DT)

    await svc.catch_up_if_needed()
    assert called is False


@pytest.mark.asyncio
async def test_catch_up_runs_within_day_seven(sessionmaker, monkeypatch):
    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)

    called = False

    async def fake_run():
        nonlocal called
        called = True

    monkeypatch.setattr(svc, "run_monthly_summary", fake_run)

    fixed_now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    import datetime as _real_datetime

    class _DT:
        now = staticmethod(lambda tz=None: fixed_now if tz else fixed_now.replace(tzinfo=None))
        combine = staticmethod(_real_datetime.datetime.combine)
        utcnow = staticmethod(_real_datetime.datetime.utcnow)

    monkeypatch.setattr("app.services.monthly_champion.datetime", _DT)

    await svc.catch_up_if_needed()
    assert called is True
