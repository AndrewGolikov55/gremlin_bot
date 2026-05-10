from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MonthlyChampion(Base):
    __tablename__ = "monthly_champions"
    __table_args__ = (
        UniqueConstraint("chat_id", "period_start", name="uq_monthly_champions_chat_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tied_with: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list, nullable=False
    )
    daily_title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    announced_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
