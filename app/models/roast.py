from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RoastRun(Base):
    __tablename__ = "roast_runs"
    __table_args__ = (
        Index("ix_roast_runs_chat_run_at", "chat_id", "run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    target_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    initiator_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
