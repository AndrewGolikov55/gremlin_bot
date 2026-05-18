from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RapbattleRound(Base):
    __tablename__ = "rapbattle_rounds"
    __table_args__ = (
        Index("ix_rapbattle_rounds_chat_status", "chat_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    challenger_a_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    challenger_b_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verses: Mapped[list[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list,
    )
    poll_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    poll_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    winner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
