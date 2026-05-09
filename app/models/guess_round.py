from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class GuessRound(Base):
    __tablename__ = "guess_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    poll_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    chat_message_id: Mapped[int] = mapped_column(BigInteger)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    author_user_id: Mapped[int] = mapped_column(BigInteger)
    correct_option_id: Mapped[int] = mapped_column(Integer)
    option_user_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    first_winner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_winner_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    selection_mode: Mapped[str] = mapped_column(String(16), default="llm")
