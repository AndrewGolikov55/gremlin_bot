from __future__ import annotations

import math
from typing import Iterable, List, Mapping, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.message import Message
from ..models.user import User
from .persona import DEFAULT_STYLE_PROMPTS


DEFAULT_CHAT_PROMPT = (
    "Ты — участник чата. Отвечай строго в рамках роли ниже."
    " Не раскрывай внутренние рассуждения и не делай преамбул."
    " Пиши 1–2 коротких предложения обычным текстом."
)

DEFAULT_INTERJECT_SUFFIX = "Отвечай без приглашения, оставайся в своей роли."

DEFAULT_FOCUS_SUFFIX = 'Вопрос: "{question}". Ответь одним сообщением.'


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


def build_messages(
    system_prompt: str,
    turns: Iterable[Tuple[str, str]],
    max_turns: int = 20,
    max_tokens: int | None = None,
    closing_text: str | None = "Ответь одним сообщением.",
):
    def _estimate_tokens(text: str) -> int:
        # Простая оценка, чтобы не превышать окно модели (≈4 символа на токен)
        return max(1, math.ceil(len(text) / 4))

    system_prompt = system_prompt.strip()
    msgs = [{"role": "system", "content": system_prompt}]
    tokens_budget = _estimate_tokens(system_prompt)

    tail = list(turns)[-max_turns:]
    collected: List[dict[str, str]] = []
    for speaker, text in reversed(tail):
        sanitized = (text or "").strip()
        if not sanitized:
            continue
        sanitized = sanitized.replace("\n", " ")
        sanitized = " ".join(sanitized.split())
        prefix = f"{speaker}: " if speaker else ""
        content = f"{prefix}{sanitized}".strip()
        if not content:
            continue
        est = _estimate_tokens(content)
        if max_tokens and tokens_budget + est > max_tokens:
            break
        tokens_budget += est
        collected.append({"role": "user", "content": content})

    for message in reversed(collected):
        msgs.append(message)

    if closing_text:
        closing = closing_text.strip()
        if closing:
            closing_tokens = _estimate_tokens(closing)
            if not max_tokens or tokens_budget + closing_tokens <= max_tokens:
                msgs.append({"role": "user", "content": closing})
    return msgs


def _resolve_name(user: User | None, user_id: int | None) -> str:
    if user and user.username:
        return user.username
    if user_id:
        return str(user_id)
    return "unknown"


def build_system_prompt(
    conf: Mapping[str, object],
    focus_text: str | None = None,
    *,
    interject: bool = False,
    style_prompts: Mapping[str, str] | None = None,
    base_prompt: str | None = None,
    interject_suffix: str | None = None,
    focus_suffix: str | None = None,
) -> str:
    style = str(conf.get("style", "standup"))
    prompts = style_prompts or DEFAULT_STYLE_PROMPTS
    style_block = prompts.get(style, prompts.get("standup", DEFAULT_STYLE_PROMPTS["standup"]))

    base_parts = [(base_prompt or DEFAULT_CHAT_PROMPT).strip()]
    style_clean = style_block.strip()
    if style_clean:
        base_parts.append(style_clean)
    base = "\n\n".join(base_parts) + "\n"

    if interject:
        suffix = (interject_suffix or DEFAULT_INTERJECT_SUFFIX).strip()
        if suffix:
            base += "\n" + suffix

    if focus_text:
        sanitized = focus_text.strip().replace("\n", " ").replace('"', "'")
        if len(sanitized) > 400:
            sanitized = sanitized[:400] + "…"
        suffix_tpl = (focus_suffix or DEFAULT_FOCUS_SUFFIX).strip()
        if suffix_tpl:
            try:
                addition = suffix_tpl.format(question=sanitized)
            except KeyError:
                addition = suffix_tpl
            base += "\n" + addition

    return base
