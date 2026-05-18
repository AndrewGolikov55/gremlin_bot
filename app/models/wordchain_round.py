from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WordchainRound(Base):
    __tablename__ = "wordchain_rounds"
    __table_args__ = (
        Index("ix_wordchain_rounds_chat_status", "chat_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_word: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_word_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    loser_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class WordchainWord(Base):
    __tablename__ = "wordchain_words"
    __table_args__ = (
        UniqueConstraint("round_id", "word", name="uq_wordchain_words_round_word"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wordchain_rounds.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    word: Mapped[str] = mapped_column(String(64), nullable=False)
    played_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
