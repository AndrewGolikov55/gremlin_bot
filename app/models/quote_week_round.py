from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class QuoteWeekRound(Base):
    __tablename__ = "quote_week_rounds"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "week_start", name="uq_quote_week_rounds_chat_week"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    poll_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    poll_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    options: Mapped[list[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list, nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    winner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    winner_option_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_counts: Mapped[list[int] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
