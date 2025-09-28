from __future__ import annotations

from typing import Iterable, List, Mapping, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.message import Message
from ..models.user import User


class ContextService:
    """Мининструмент для выборки последних сообщений из чата."""

    async def get_recent_turns(
        self,
        session: AsyncSession,
        chat_id: int,
        limit: int,
    ) -> List[Tuple[str, str]]:
        stmt = (
            select(Message, User)
            .outerjoin(User, User.tg_id == Message.user_id)
            .where(Message.chat_id == chat_id)
            .order_by(Message.date.desc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        rows: Sequence[Tuple[Message, User | None]] = res.all()
        turns: List[Tuple[str, str]] = []
        for msg, user in reversed(rows):
            speaker = _resolve_name(user, msg.user_id)
            turns.append((speaker, msg.text or ""))
        return turns


def build_messages(system_prompt: str, turns: Iterable[Tuple[str, str]], max_turns: int = 20):
    msgs = [{"role": "system", "content": system_prompt}]
    tail = list(turns)[-max_turns:]
    for speaker, text in tail:
        msgs.append({"role": "user", "content": f"{speaker}: {text}"})
    msgs.append({"role": "user", "content": "Ответь уместно одним сообщением."})
    return msgs


def _resolve_name(user: User | None, user_id: int | None) -> str:
    if user and user.username:
        return user.username
    if user_id:
        return str(user_id)
    return "unknown"


def build_system_prompt(conf: Mapping[str, object], focus_text: str | None = None) -> str:
    style = conf.get("style", "neutral")
    tone = conf.get("tone", 3)
    profanity = conf.get("profanity", "soft")
    base = (
        "Ты — участник Telegram-чата. "
        f"Текущий стиль: {style}, "
        f"агрессивность: {tone}/10. "
        f"Обсценная лексика: {profanity}. "
        "Пиши кратко и по делу. Не выдавай преамбул и дисклеймеров."
    )
    if focus_text:
        sanitized = focus_text.strip().replace("\n", " ")
        sanitized = sanitized.replace('"', "'")
        if len(sanitized) > 400:
            sanitized = sanitized[:400] + "…"
        base += " Ты отвечаешь на конкретный вопрос пользователя: \"" + sanitized + "\". Дай прямой, содержательный ответ."
    return base
