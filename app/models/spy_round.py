from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SpyRound(Base):
    __tablename__ = "spy_rounds"
    __table_args__ = (
        Index("ix_spy_rounds_chat_status", "chat_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    initiator_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    location: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    spy_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    vote_poll_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vote_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpyPlayer(Base):
    __tablename__ = "spy_players"

    round_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("spy_rounds.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_spy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
