from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.message import Message
from ..models.user import User
from .persona import DEFAULT_STYLE_PROMPTS, DEFAULT_STYLE_KEY


DEFAULT_CHAT_PROMPT = (
    "Ты — участник чата. Отвечай строго в рамках роли ниже."
    " Не раскрывай внутренние рассуждения и не делай преамбул."
    " Пиши 1–2 коротких предложения обычным текстом, не используй Markdown."
)

DEFAULT_INTERJECT_SUFFIX = "Отвечай без приглашения, оставайся в своей роли."

DEFAULT_FOCUS_SUFFIX = 'Вопрос: "{question}". Ответь одним сообщением.'


@dataclass(frozen=True, slots=True)
class ChatTurn:
    speaker: str | None
    text: str
    is_bot: bool


class ContextService:
    """Мининструмент для выборки последних сообщений из чата."""

    async def get_recent_turns(
        self,
        session: AsyncSession,
        chat_id: int,
        limit: int,
    ) -> List[ChatTurn]:
        stmt = (
            select(Message, User)
            .outerjoin(User, User.tg_id == Message.user_id)
            .where(Message.chat_id == chat_id)
            .order_by(Message.date.desc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        rows: Sequence[tuple[Message, User | None]] = res.all()
        turns: List[ChatTurn] = []
        for msg, user in reversed(rows):
            speaker = _resolve_name(user, msg.user_id)
            turns.append(ChatTurn(speaker, msg.text or "", bool(msg.is_bot)))
        return turns


def build_messages(
    system_prompt: str,
    turns: Iterable[ChatTurn],
    max_turns: int = 20,
    max_tokens: int | None = None,
    closing_text: str | None = None,
) -> list[dict[str, str]]:
    def _estimate_tokens(text: str) -> int:
        # Простая оценка, чтобы не превышать окно модели (≈4 символа на токен)
        return max(1, math.ceil(len(text) / 4))

    def _is_service_text(text: str) -> bool:
        lowered = text.lower()
        if any(kw in lowered for kw in (" joined the group", " joined the chat", " left the group", " left the chat")):
            return len(text) <= 160
        if " joined via " in lowered and ("invite" in lowered or "ссылк" in lowered):
            return len(text) <= 160
        if " pinned a message" in lowered or lowered.endswith(" was pinned"):
            return len(text) <= 160
        if any(kw in lowered for kw in (" changed the chat photo", " changed the chat title", " changed the group name", " changed the group photo", " set the chat photo")):
            return len(text) <= 160
        if (" invited " in lowered or " added " in lowered) and (
            " to the chat" in lowered or " to the group" in lowered or " в чат" in lowered
        ):
            return len(text) <= 160
        if (" removed " in lowered or " kicked " in lowered) and (" from the chat" in lowered or " from the group" in lowered):
            return len(text) <= 160
        ru_markers = (
            "пригласил в чат",
            "пригласила в чат",
            "добавил в чат",
            "добавила в чат",
            "вступил в чат",
            "вступила в чат",
            "вышел из чата",
            "вышла из чата",
            "закрепил сообщение",
            "закрепила сообщение",
            "сообщение закреплено",
            "изменил название чата",
            "изменил(а) название чата",
            "обновил фото чата",
            "обновила фото чата",
            "удалил из чата",
            "удалила из чата",
        )
        if any(marker in lowered for marker in ru_markers):
            return len(text) <= 160
        return False

    system_content = system_prompt.strip()
    msgs: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    tokens_budget = _estimate_tokens(system_content)

    tail = list(turns)[-max_turns:]
    entries: list[dict[str, object]] = []
    for turn in tail:
        raw = (turn.text or "").strip()
        if not raw:
            continue
        sanitized = " ".join(raw.replace("\n", " ").split())
        if not sanitized:
            continue
        if sanitized.lstrip().startswith("/"):
            continue
        if _is_service_text(sanitized):
            continue
        entries.append(
            {
                "speaker": turn.speaker or "unknown",
                "text": sanitized,
                "is_bot": bool(turn.is_bot),
            }
        )

    # Выделяем последние подряд идущие сообщения одного пользователя как текущий запрос
    current_text: str | None = None
    if entries and not entries[-1]["is_bot"]:
        current_text = str(entries.pop()["text"]).strip()

    final_content = ""
    if current_text:
        final_content = current_text
    if not final_content and closing_text:
        final_content = closing_text.strip()
    elif final_content and closing_text:
        extra = closing_text.strip()
        if extra:
            final_content = f"{final_content}\n\n{extra}"
    if not final_content:
        final_content = "Ответь одним сообщением."
    final_tokens = _estimate_tokens(final_content)

    # Объединяем последовательные сообщения одного автора
    combined: list[dict[str, object]] = []
    for entry in entries:
        if (
            combined
            and combined[-1]["speaker"] == entry["speaker"]
            and combined[-1]["is_bot"] == entry["is_bot"]
        ):
            combined[-1]["texts"].append(entry["text"])
        else:
            combined.append(
                {
                    "speaker": entry["speaker"],
                    "is_bot": entry["is_bot"],
                    "texts": [entry["text"]],
                }
            )

    history_lines_raw: list[str] = []
    for entry in combined:
        speaker = str(entry["speaker"] or "unknown")
        text = " ".join(entry["texts"])
        history_lines_raw.append(f"{speaker}: {text}")

    history_header = "История:"
    placeholder = "(пусто)"
    available_for_history = None
    if max_tokens:
        available_for_history = max_tokens - tokens_budget - final_tokens
        if available_for_history <= 0:
            history_lines = []
        else:
            total = _estimate_tokens(history_header)
            selected: list[str] = []
            for line in reversed(history_lines_raw):
                tokens_line = _estimate_tokens(line)
                if total + tokens_line > available_for_history:
                    break
                selected.append(line)
                total += tokens_line
            history_lines = list(reversed(selected))
    else:
        history_lines = history_lines_raw

    if history_lines:
        history_content = history_header + "\n" + "\n".join(history_lines)
    else:
        if (
            available_for_history is not None
            and available_for_history < _estimate_tokens(history_header) + _estimate_tokens(placeholder)
        ):
            history_content = history_header
        else:
            history_content = f"{history_header}\n{placeholder}"

    msgs.append({"role": "user", "content": history_content})
    msgs.append({"role": "user", "content": final_content})
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
    style = str(conf.get("style", DEFAULT_STYLE_KEY))
    prompts = style_prompts or DEFAULT_STYLE_PROMPTS
    default_prompt = prompts.get(
        DEFAULT_STYLE_KEY,
        DEFAULT_STYLE_PROMPTS.get(DEFAULT_STYLE_KEY, DEFAULT_STYLE_PROMPTS.get("standup", "")),
    )
    style_block = prompts.get(style, default_prompt)

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
