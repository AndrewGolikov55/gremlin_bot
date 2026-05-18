from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AkinatorRound(Base):
    __tablename__ = "akinator_rounds"
    __table_args__ = (
        Index("ix_akinator_rounds_chat_status", "chat_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    initiator_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    questions_asked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    winner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AkinatorQuestion(Base):
    __tablename__ = "akinator_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("akinator_rounds.id", ondelete="CASCADE"), index=True
    )
    asker_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(String(8), nullable=False)
    asked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
