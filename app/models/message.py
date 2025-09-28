from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_messages_chat_message"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    reply_to_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    is_bot: Mapped[bool]
