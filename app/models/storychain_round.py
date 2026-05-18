from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class StorychainRound(Base):
    __tablename__ = "storychain_rounds"
    __table_args__ = (
        Index("ix_storychain_rounds_chat_status", "chat_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    seed: Mapped[str] = mapped_column(Text, nullable=False)
    target_contributions: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    seed_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finalised_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finale: Mapped[str | None] = mapped_column(Text, nullable=True)


class StorychainContribution(Base):
    __tablename__ = "storychain_contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("storychain_rounds.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
