from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.dice_round import DiceRound
from ..models.roulette import RouletteScoreAdjustment

logger = logging.getLogger("bot.dice_game")

MoscowTZ = ZoneInfo("Europe/Moscow")


def _moscow_midnight(now: datetime) -> datetime:
    """Today's Moscow midnight as a naive UTC datetime.

    Naive `now` is interpreted as UTC (matching `datetime.utcnow()`).
    """
    aware = now if now.tzinfo else now.replace(tzinfo=ZoneInfo("UTC"))
    msk = aware.astimezone(MoscowTZ)
    midnight_msk = msk.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_msk.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def compute_delta(picks: list[int], dice_value: int) -> int:
    """Return roulette score delta for a dice roll outcome.

    Win:  -2 if single pick (1/6), -1 if double pick (2/6).
    Loss: +2 if single pick,        +1 if double pick.
    """
    win_amount = -2 if len(picks) == 1 else -1
    loss_amount = 2 if len(picks) == 1 else 1
    return win_amount if dice_value in picks else loss_amount


class AlreadyPlayedTodayError(Exception):
    """Raised when a user tries to roll twice in the same Moscow day."""


class DiceGameService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def can_play_today(self, *, chat_id: int, user_id: int, now: datetime) -> bool:
        midnight = _moscow_midnight(now)
        async with self.sessionmaker() as session:
            existing = (await session.execute(
                select(DiceRound.id).where(
                    DiceRound.chat_id == chat_id,
                    DiceRound.user_id == user_id,
                    DiceRound.rolled_at >= midnight,
                ).limit(1)
            )).scalar_one_or_none()
        return existing is None

    async def record_roll(
        self,
        *,
        chat_id: int,
        user_id: int,
        picks: list[int],
        dice_value: int,
        dice_message_id: int,
        now: datetime,
    ) -> tuple[DiceRound, int]:
        """Atomically: recheck daily limit, insert DiceRound, optionally insert RouletteScoreAdjustment.

        Raises AlreadyPlayedTodayError if a roll for this (chat_id, user_id) already exists today.
        """
        delta = compute_delta(picks, dice_value)
        won = delta != 0
        midnight = _moscow_midnight(now)

        async with self.sessionmaker() as session:
            async with session.begin():
                existing = (await session.execute(
                    select(DiceRound.id).where(
                        DiceRound.chat_id == chat_id,
                        DiceRound.user_id == user_id,
                        DiceRound.rolled_at >= midnight,
                    ).limit(1)
                )).scalar_one_or_none()
                if existing is not None:
                    raise AlreadyPlayedTodayError(f"chat={chat_id} user={user_id} already played")

                round_ = DiceRound(
                    chat_id=chat_id,
                    user_id=user_id,
                    picks=list(picks),
                    dice_value=dice_value,
                    won=won,
                    delta=delta,
                    rolled_at=now,
                    dice_message_id=dice_message_id,
                )
                session.add(round_)
                await session.flush()  # populate round_.id before adjustment refers to it

                if won:
                    session.add(RouletteScoreAdjustment(
                        chat_id=chat_id,
                        user_id=user_id,
                        delta=delta,
                        reason="dice_win",
                        source_id=round_.id,
                    ))
            await session.refresh(round_)
            return round_, delta
