from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.dice_round import DiceRound
from ..models.roulette import RouletteScoreAdjustment

logger = logging.getLogger("bot.dice_game")


def compute_delta(picks: list[int], dice_value: int) -> int:
    """Return roulette score delta for a dice roll outcome.

    Win:  -2 if single pick (1/6), -1 if double pick (2/6).
    Loss: +2 if single pick,        +1 if double pick.
    """
    win_amount = -2 if len(picks) == 1 else -1
    loss_amount = 2 if len(picks) == 1 else 1
    return win_amount if dice_value in picks else loss_amount


class DiceGameService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

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
        """Atomically insert DiceRound + optional RouletteScoreAdjustment."""
        delta = compute_delta(picks, dice_value)
        won = delta < 0  # negative delta = score reduction = win

        async with self.sessionmaker() as session:
            async with session.begin():
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

                if delta != 0:
                    session.add(RouletteScoreAdjustment(
                        chat_id=chat_id,
                        user_id=user_id,
                        delta=delta,
                        reason="dice_win" if won else "dice_loss",
                        source_id=round_.id,
                    ))
            await session.refresh(round_)
            return round_, delta
