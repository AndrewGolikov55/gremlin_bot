from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DiceRound(Base):
    __tablename__ = "dice_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    picks: Mapped[list[int]] = mapped_column(JSON)
    dice_value: Mapped[int] = mapped_column(Integer)
    won: Mapped[bool] = mapped_column(Boolean)
    delta: Mapped[int] = mapped_column(Integer)
    rolled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    dice_message_id: Mapped[int] = mapped_column(BigInteger)
