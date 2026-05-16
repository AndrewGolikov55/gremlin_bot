from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DiceRound, RouletteScoreAdjustment
from app.services.dice_game import AlreadyPlayedTodayError, DiceGameService, compute_delta


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, 0)


class TestComputeDelta:
    @pytest.mark.parametrize("picks,dice_value,expected", [
        # 1 number — win: -2, lose: 0
        ([3], 3, -2),
        ([3], 4, 0),
        ([6], 6, -2),
        ([1], 2, 0),
        # 2 numbers — win: -1, lose: 0
        ([1, 4], 1, -1),
        ([1, 4], 4, -1),
        ([1, 4], 5, 0),
        ([2, 5], 3, 0),
    ])
    def test_table(self, picks: list[int], dice_value: int, expected: int) -> None:
        assert compute_delta(picks, dice_value) == expected


@pytest.mark.asyncio
async def test_can_play_today_true_when_no_round(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    assert await svc.can_play_today(chat_id=-1, user_id=10, now=_utc(2026, 5, 16)) is True


@pytest.mark.asyncio
async def test_can_play_today_false_after_record(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    now = _utc(2026, 5, 16, hour=10)
    await svc.record_roll(
        chat_id=-1, user_id=10, picks=[3], dice_value=4,
        dice_message_id=100, now=now,
    )
    assert await svc.can_play_today(chat_id=-1, user_id=10, now=now + timedelta(hours=2)) is False


@pytest.mark.asyncio
async def test_can_play_today_true_next_day(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    now = _utc(2026, 5, 16, hour=10)
    await svc.record_roll(
        chat_id=-1, user_id=10, picks=[3], dice_value=4,
        dice_message_id=100, now=now,
    )
    # next Moscow day: roll at 22:00 UTC today is 01:00 MSK NEXT day → next day
    next_day = _utc(2026, 5, 16, hour=22)
    assert await svc.can_play_today(chat_id=-1, user_id=10, now=next_day) is True


@pytest.mark.asyncio
async def test_can_play_today_other_user_still_can_play(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    now = _utc(2026, 5, 16)
    await svc.record_roll(chat_id=-1, user_id=10, picks=[3], dice_value=4, dice_message_id=100, now=now)
    assert await svc.can_play_today(chat_id=-1, user_id=11, now=now) is True


@pytest.mark.asyncio
async def test_can_play_today_other_chat_still_can_play(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    now = _utc(2026, 5, 16)
    await svc.record_roll(chat_id=-1, user_id=10, picks=[3], dice_value=4, dice_message_id=100, now=now)
    assert await svc.can_play_today(chat_id=-2, user_id=10, now=now) is True


@pytest.mark.asyncio
async def test_record_roll_win_single_pick_writes_adjustment_minus_two(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    svc = DiceGameService(sessionmaker)
    round_, delta = await svc.record_roll(
        chat_id=-1, user_id=10, picks=[5], dice_value=5,
        dice_message_id=100, now=_utc(2026, 5, 16),
    )
    assert delta == -2
    assert round_.won is True

    async with sessionmaker() as session:
        adj = (await session.execute(
            select(RouletteScoreAdjustment).where(RouletteScoreAdjustment.user_id == 10)
        )).scalar_one()
        assert adj.delta == -2
        assert adj.reason == "dice_win"
        assert adj.source_id == round_.id


@pytest.mark.asyncio
async def test_record_roll_win_double_pick_writes_adjustment_minus_one(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    svc = DiceGameService(sessionmaker)
    round_, delta = await svc.record_roll(
        chat_id=-1, user_id=10, picks=[3, 5], dice_value=3,
        dice_message_id=100, now=_utc(2026, 5, 16),
    )
    assert delta == -1
    async with sessionmaker() as session:
        adj = (await session.execute(select(RouletteScoreAdjustment))).scalar_one()
        assert adj.delta == -1
        assert adj.reason == "dice_win"


@pytest.mark.asyncio
async def test_record_roll_loss_does_not_write_adjustment(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    svc = DiceGameService(sessionmaker)
    round_, delta = await svc.record_roll(
        chat_id=-1, user_id=10, picks=[3], dice_value=4,
        dice_message_id=100, now=_utc(2026, 5, 16),
    )
    assert delta == 0
    assert round_.won is False
    async with sessionmaker() as session:
        adjs = (await session.execute(select(RouletteScoreAdjustment))).scalars().all()
        assert adjs == []
        rounds = (await session.execute(select(DiceRound))).scalars().all()
        assert len(rounds) == 1
        assert rounds[0].delta == 0


@pytest.mark.asyncio
async def test_record_roll_twice_same_day_raises(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = DiceGameService(sessionmaker)
    now = _utc(2026, 5, 16, hour=10)
    await svc.record_roll(chat_id=-1, user_id=10, picks=[3], dice_value=4, dice_message_id=100, now=now)
    with pytest.raises(AlreadyPlayedTodayError):
        await svc.record_roll(
            chat_id=-1, user_id=10, picks=[5], dice_value=2,
            dice_message_id=101, now=now + timedelta(hours=1),
        )
